"""MCP transport, protocol-era and OAuth surface regressions; no network.

Exercises the same shapes production serves: the write-surface lock applied
through a proxied mount, Streamable HTTP under the 2025-06-18 and 2025-11-25
handshake and the stateless 2026-07-28 envelope, and the OAuth discovery
documents claude.ai reads before it registers.

Run: .venv/bin/python -m unittest tests.test_mcp_transport
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from fastmcp import Client, FastMCP
from mcp.server.auth.provider import AccessToken

import brain_surface as brain
from brain_oauth import BrainOAuthProvider

BASE = "https://mcp.example.test"
TOKEN = "test-access-token"
HANDSHAKE_VERSIONS = ("2025-06-18", "2025-11-25")
MODERN = "2026-07-28"


def upstream():
    """Stand-in for basic-memory: one read tool plus every blocked write tool."""
    server = FastMCP("basic-memory")

    @server.tool()
    def search(query: str) -> str:
        return f"found {query}"

    for name in brain.BLOCKED_TOOLS:
        server.tool(lambda: "written", name=name)
    return server


def parse(response):
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        events = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
        return json.loads(events[-1])
    return response.json()


class WriteSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_tools_are_hidden_and_uncallable_through_proxy(self):
        server = FastMCP("brain")
        server.mount(brain.proxy_basic_memory(upstream()))
        brain.lock_write_surface(server)
        async with Client(server) as client:
            names = {tool.name for tool in await client.list_tools()}
            self.assertIn("search", names)
            self.assertFalse(names & brain.BLOCKED_TOOLS)
            for name in brain.BLOCKED_TOOLS:
                with self.subTest(name=name), self.assertRaises(Exception):
                    await client.call_tool(name, {})


class TransportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.provider = BrainOAuthProvider(
            base_url=BASE, store_path=root / "clients.json",
            tokens_store_path=root / "tokens.json",
        )
        self.provider.access_tokens[TOKEN] = AccessToken(
            token=TOKEN, client_id="test-client", scopes=[],
            expires_at=int(time.time()) + 300,
        )
        server = FastMCP("brain", auth=self.provider)
        server.mount(brain.proxy_basic_memory(upstream()))
        brain.lock_write_surface(server)

        @server.tool()
        def brain_init() -> str:
            return "manual"

        self.client = TestClient(server.http_app(), base_url=BASE)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def post(self, body, headers=None, token=TOKEN):
        h = {"Accept": "application/json, text/event-stream",
             "Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        h.update(headers or {})
        return self.client.post("/mcp", content=json.dumps(body), headers=h)

    def handshake(self, version):
        init = self.post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": version, "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"}}})
        self.assertEqual(init.status_code, 200, init.text)
        self.assertEqual(parse(init)["result"]["protocolVersion"], version)
        headers = {"Mcp-Session-Id": init.headers["mcp-session-id"],
                   "MCP-Protocol-Version": version}
        ack = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"}, headers)
        self.assertEqual(ack.status_code, 202, ack.text)
        return headers

    def modern(self, rid, method, params=None, name=None):
        params = dict(params or {})
        params["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": MODERN,
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
        }
        headers = {"MCP-Protocol-Version": MODERN, "Mcp-Method": method}
        if name:
            headers["Mcp-Name"] = name
        response = self.post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, headers)
        self.assertEqual(response.status_code, 200, response.text)
        return parse(response)

    def assert_surface(self, tools, called):
        names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertTrue({"brain_init", "search"} <= names, names)
        self.assertFalse(names & brain.BLOCKED_TOOLS)
        self.assertEqual(called["result"]["content"][0]["text"], "found notes")

    def test_handshake_versions(self):
        for version in HANDSHAKE_VERSIONS:
            with self.subTest(version=version):
                headers = self.handshake(version)
                tools = parse(self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers))
                called = parse(self.post({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                    "name": "search", "arguments": {"query": "notes"}}}, headers))
                self.assert_surface(tools, called)

    def test_stateless_modern_version(self):
        discover = self.modern(1, "server/discover")
        self.assertIn(MODERN, discover["result"]["supportedVersions"])
        tools = self.modern(2, "tools/list")
        called = self.modern(3, "tools/call", {"name": "search", "arguments": {"query": "notes"}}, name="search")
        self.assert_surface(tools, called)

    def test_missing_and_unknown_tokens_are_challenged(self):
        for token in (None, "not-a-real-token"):
            with self.subTest(token=token):
                response = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=token)
                self.assertEqual(response.status_code, 401)
                self.assertIn("resource_metadata=", response.headers["www-authenticate"])

    def test_expired_token_rejected(self):
        self.provider.access_tokens[TOKEN] = AccessToken(
            token=TOKEN, client_id="test-client", scopes=[], expires_at=int(time.time()) - 1)
        self.assertEqual(self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code, 401)

    def test_oauth_discovery_documents(self):
        auth = self.client.get("/.well-known/oauth-authorization-server")
        self.assertEqual(auth.status_code, 200, auth.text)
        meta = auth.json()
        self.assertEqual(meta["issuer"].rstrip("/"), BASE)
        for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
            self.assertTrue(meta[key].startswith(BASE), key)
        self.assertIn("S256", meta["code_challenge_methods_supported"])
        resource = self.client.get("/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(resource.status_code, 200, resource.text)
        self.assertEqual(resource.json()["resource"], f"{BASE}/mcp")
        self.assertEqual([s.rstrip("/") for s in resource.json()["authorization_servers"]], [BASE])

    def test_registration_keeps_redirect_allowlist(self):
        body = {"redirect_uris": ["https://evil.example/cb"], "client_name": "x",
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}
        rejected = self.client.post("/register", json=body)
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json()["error"], "invalid_redirect_uri")
        body["redirect_uris"] = ["https://claude.ai/api/mcp/auth_callback"]
        self.assertEqual(self.client.post("/register", json=body).status_code, 201)


if __name__ == "__main__":
    unittest.main()
