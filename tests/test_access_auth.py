"""Headless auth boundaries; local signed fixtures, no real credentials/network.

Run: .venv/bin/python -m unittest tests.test_access_auth
"""

import tempfile
import time
import unittest
from pathlib import Path

import httpx
from authlib.jose import JsonWebToken
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from mcp.server.auth.provider import AccessToken

from brain_access import AccessAssertionMiddleware, ServiceAccessVerifier
from brain_oauth import BrainOAuthProvider


ISSUER = "https://test-team.cloudflareaccess.com"
AUDIENCE = "test-brain-audience"
CLIENT = "test-codex-client.access"


class AccessTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = RSAKeyPair.generate()
        cls.other_keys = RSAKeyPair.generate()

    def setUp(self):
        self.verifier = ServiceAccessVerifier(
            client_ids={CLIENT},
            public_key=self.keys.public_key,
            issuer=ISSUER,
            audience=AUDIENCE,
        )

    def token(self, *, changes=None, omit=(), keys=None):
        claims = {
            "iss": ISSUER,
            "aud": [AUDIENCE],
            "exp": int(time.time()) + 300,
            "nbf": int(time.time()) - 10,
            "iat": int(time.time()) - 10,
            "type": "app",
            "sub": "",
            "common_name": CLIENT,
        }
        claims.update(changes or {})
        for key in omit:
            claims.pop(key, None)
        keypair = keys or self.keys
        return JsonWebToken(["RS256"]).encode(
            {"alg": "RS256"}, claims, keypair.private_key.get_secret_value()
        ).decode()

    async def test_scoped_headless_token_accepted(self):
        result = await self.verifier.verify_token(self.token())
        self.assertIsNotNone(result)
        self.assertEqual(result.claims["common_name"], CLIENT)
        self.assertGreater(result.expires_at, time.time())

    async def test_worker_and_other_service_tokens_rejected(self):
        for client in ["test-brain-worker.access", "test-chatgpt.access", "", None, [CLIENT]]:
            with self.subTest(client=client):
                self.assertIsNone(await self.verifier.verify_token(
                    self.token(changes={"common_name": client})
                ))

    async def test_missing_identity_and_configuration_rejected(self):
        for omitted in ["common_name", "type", "sub", "iss", "aud", "exp"]:
            with self.subTest(omitted=omitted):
                self.assertIsNone(await self.verifier.verify_token(self.token(omit=[omitted])))
        self.verifier.client_ids = set()
        self.assertIsNone(await self.verifier.verify_token(self.token()))

    async def test_wrong_signature_issuer_audience_type_and_subject_rejected(self):
        bad_claims = [
            {"iss": "https://other-team.cloudflareaccess.com"},
            {"aud": ["different-application"]},
            {"type": "org"},
            {"sub": "human-user-id"},
        ]
        for change in bad_claims:
            with self.subTest(change=change):
                self.assertIsNone(await self.verifier.verify_token(self.token(changes=change)))
        self.assertIsNone(await self.verifier.verify_token(self.token(keys=self.other_keys)))

    async def test_expiry_and_not_before_fail_closed(self):
        bad_claims = [
            {"exp": int(time.time()) - 1}, {"exp": None},
            {"exp": True}, {"exp": "9999999999"},
            {"exp": float("inf")}, {"exp": float("nan")},
            {"nbf": int(time.time()) + 300}, {"nbf": True},
            {"nbf": "0"}, {"nbf": float("inf")}, {"nbf": float("nan")},
        ]
        for change in bad_claims:
            with self.subTest(change=change):
                self.assertIsNone(await self.verifier.verify_token(self.token(changes=change)))
        self.assertIsNotNone(await self.verifier.verify_token(self.token(omit=["nbf"])))

    async def test_malformed_or_unsigned_tokens_rejected(self):
        for token in ["", "garbage", "a.b.c", "eyJhbGciOiJub25lIn0.e30."]:
            with self.subTest(token=token):
                self.assertIsNone(await self.verifier.verify_token(token))

    async def test_oauth_persistence_and_expiry_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            kwargs = dict(
                base_url="https://brain.test",
                store_path=Path(tmp) / "clients.json",
                tokens_store_path=Path(tmp) / "tokens.json",
            )
            original = BrainOAuthProvider(**kwargs)
            original.access_tokens["test-oauth-token"] = AccessToken(
                token="test-oauth-token", client_id="oauth-test-client", scopes=[],
                expires_at=int(time.time()) + 300,
            )
            original.access_tokens["test-expired-token"] = AccessToken(
                token="test-expired-token", client_id="oauth-test-client", scopes=[],
                expires_at=int(time.time()) - 300,
            )
            original._save_tokens()
            restored = BrainOAuthProvider(**kwargs)
            restored.service_access = self.verifier
            result = await restored.verify_token("test-oauth-token")
            self.assertIsNotNone(result)
            self.assertEqual(result.client_id, "oauth-test-client")
            self.assertIsNone(await restored.verify_token("test-expired-token"))
            self.assertIsNone(await restored.verify_token("unknown-oauth-token"))
            self.assertIsNotNone(await restored.verify_token(self.token()))

    async def test_mcp_http_authentication_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = BrainOAuthProvider(
                base_url="https://brain.test", store_path=Path(tmp) / "clients.json",
                tokens_store_path=Path(tmp) / "tokens.json", service_access=self.verifier,
            )
            provider.access_tokens["test-oauth"] = AccessToken(
                token="test-oauth", client_id="test-oauth-client", scopes=[],
                expires_at=int(time.time()) + 300,
            )
            server = FastMCP("auth-test", auth=provider)
            app = server.http_app(stateless_http=True)
            app.add_middleware(AccessAssertionMiddleware)
            payload = {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "auth-test", "version": "1"}},
            }
            good_assertion = self.token()
            cases = [
                ({}, 401),
                ({"cf-access-jwt-assertion": good_assertion}, 200),
                ({"authorization": "Bearer test-oauth"}, 200),
                ({"authorization": "Bearer invalid", "cf-access-jwt-assertion": good_assertion}, 401),
                ({"cf-access-jwt-assertion": self.token(changes={"common_name": "test-worker.access"})}, 401),
                ({"cf-access-jwt-assertion": "forged"}, 401),
            ]
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="https://brain.test",
                    headers={"accept": "application/json, text/event-stream"},
                ) as client:
                    for index, (headers, expected) in enumerate(cases):
                        with self.subTest(case=index):
                            response = await client.post("/mcp", json=payload, headers=headers)
                            self.assertEqual(response.status_code, expected)


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def forwarded(self, headers, path="/mcp", scope_type="http"):
        captured = []

        async def app(scope, receive, send):
            captured.append(scope)

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            pass

        scope = {"type": scope_type, "path": path, "headers": headers}
        await AccessAssertionMiddleware(app)(scope, receive, send)
        self.assertEqual(len(captured), 1)
        return captured[0]["headers"]

    async def test_assertion_promoted_on_mcp_paths(self):
        for path in ["/mcp", "/mcp/"]:
            with self.subTest(path=path):
                result = await self.forwarded([(b"cf-access-jwt-assertion", b"signed-jwt")], path)
                self.assertIn((b"authorization", b"Bearer signed-jwt"), result)

    async def test_oauth_header_preserved_even_when_invalid_or_empty(self):
        for bearer in [b"Bearer existing-oauth", b"Bearer invalid", b""]:
            with self.subTest(bearer=bearer):
                headers = [(b"authorization", bearer), (b"cf-access-jwt-assertion", b"signed-jwt")]
                self.assertEqual(await self.forwarded(headers), headers)

    async def test_other_routes_and_non_http_scopes_unchanged(self):
        headers = [(b"cf-access-jwt-assertion", b"signed-jwt")]
        for path in ["/api/pulse", "/authorize", "/token", "/mcpevil"]:
            with self.subTest(path=path):
                self.assertEqual(await self.forwarded(headers, path), headers)
        self.assertEqual(await self.forwarded(headers, scope_type="websocket"), headers)

    async def test_absent_and_empty_assertions_not_promoted(self):
        cases = [
            [], [(b"cf-access-jwt-assertion", b"")],
        ]
        for headers in cases:
            with self.subTest(headers=headers):
                result = await self.forwarded(headers)
                self.assertFalse(any(key == b"authorization" for key, value in result))

    async def test_duplicate_authentication_headers_rejected(self):
        for name in [b"authorization", b"cf-access-jwt-assertion"]:
            with self.subTest(name=name):
                messages = []

                async def app(scope, receive, send):
                    self.fail("Ambiguous credentials reached downstream application")

                async def receive():
                    return {"type": "http.request", "body": b""}

                async def send(message):
                    messages.append(message)

                scope = {"type": "http", "path": "/mcp", "headers": [(name, b"first"), (name, b"second")]}
                await AccessAssertionMiddleware(app)(scope, receive, send)
                self.assertEqual(messages[0]["status"], 401)


if __name__ == "__main__":
    unittest.main()
