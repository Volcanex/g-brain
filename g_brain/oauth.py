"""OAuth 2.1 authorization server for brain MCP.

Strategy
--------
Both Claude.ai (web) and ChatGPT (Developer Mode) connectors require OAuth
2.1; neither accepts static bearer tokens or arbitrary HTTP headers in the
"Add custom connector" UI. ChatGPT additionally requires Dynamic Client
Registration (RFC 7591): its redirect_uri is now per-connection.

We use FastMCP's ``InMemoryOAuthProvider`` for the AS plumbing (PKCE,
/authorize, /token, /.well-known) and:

- enable DCR so ChatGPT can self-register
- restrict ``redirect_uri`` to an allowlisted set of hosts (claude.ai,
  chatgpt.com, chat.openai.com) so randos can't register their own
  client and get a token aimed at their own domain
- persist registered clients to ``BRAIN_ROOT/.oauth-clients.json`` so a
  restart doesn't force re-registration / re-pairing
- persist tokens to ``BRAIN_ROOT/.oauth-tokens.json`` so a restart doesn't
  invalidate existing sessions: refresh tokens have no expiry, so auth
  survives indefinitely without manual re-auth
- auto-approve the consent step (single-user brain, no consent UI)
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from mcp.server.auth.provider import AccessToken, RefreshToken, RegistrationError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from fastmcp.server.auth.providers.in_memory import (
    ClientRegistrationOptions,
    InMemoryOAuthProvider,
)

# Hosts allowed to register an MCP client. Anything else gets rejected
# at /register time.
ALLOWED_REDIRECT_HOSTS: set[str] = {
    "claude.ai",
    "chatgpt.com",
    "chat.openai.com",
}


def _redirect_uri_allowed(uri: str) -> bool:
    parsed = urllib.parse.urlparse(uri)
    host = (parsed.hostname or "").lower()
    # RFC 8252 §8.3: loopback redirect URIs are blessed for native/CLI clients.
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    if parsed.scheme != "https":
        return False
    return host in ALLOWED_REDIRECT_HOSTS


class BrainOAuthProvider(InMemoryOAuthProvider):
    """InMemoryOAuthProvider with restricted DCR, on-disk client + token registry."""

    def __init__(
        self,
        *,
        base_url: str,
        store_path: Path,
        tokens_store_path: Path,
        resource_base_url: str | None = None,
        service_access=None,
    ):
        super().__init__(
            base_url=base_url,
            resource_base_url=resource_base_url,
            client_registration_options=ClientRegistrationOptions(enabled=True),
        )
        self._store_path = store_path
        self.service_access = service_access
        self._tokens_store_path = tokens_store_path
        self._load_clients()
        self._load_tokens()

    async def load_access_token(self, token: str):
        access = await super().load_access_token(token)
        if access is not None or self.service_access is None:
            return access
        return await self.service_access.load_access_token(token)

    # ---- client persistence ------------------------------------------

    def _load_clients(self) -> None:
        if not self._store_path.exists():
            return
        try:
            raw: dict[str, Any] = json.loads(self._store_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        for cid, info in raw.items():
            try:
                self.clients[cid] = OAuthClientInformationFull(**info)
            except Exception:
                continue

    def _save_clients(self) -> None:
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        out = {
            cid: ci.model_dump(mode="json", exclude_none=True)
            for cid, ci in self.clients.items()
        }
        _atomic_write(self._store_path, out)

    # ---- token persistence -------------------------------------------

    def _load_tokens(self) -> None:
        if not self._tokens_store_path.exists():
            return
        try:
            raw: dict[str, Any] = json.loads(self._tokens_store_path.read_text())
        except (OSError, json.JSONDecodeError):
            return

        now = time.time()

        for token_str, info in raw.get("access_tokens", {}).items():
            try:
                tok = AccessToken(**info)
                if tok.expires_at is not None and tok.expires_at < now:
                    continue  # expired: skip, will be naturally absent
                self.access_tokens[token_str] = tok
            except Exception:
                continue

        for token_str, info in raw.get("refresh_tokens", {}).items():
            try:
                tok = RefreshToken(**info)
                if tok.expires_at is not None and tok.expires_at < now:
                    continue
                self.refresh_tokens[token_str] = tok
            except Exception:
                continue

        # Rebuild the maps from whatever survived the expiry filter above.
        # Persisted maps may reference tokens we just skipped, so we
        # reconstruct only from what's actually in memory.
        for at, rt in raw.get("access_to_refresh", {}).items():
            if at in self.access_tokens and rt in self.refresh_tokens:
                self._access_to_refresh_map[at] = rt
        for rt, at in raw.get("refresh_to_access", {}).items():
            if rt in self.refresh_tokens and at in self.access_tokens:
                self._refresh_to_access_map[rt] = at

        loaded = len(self.access_tokens), len(self.refresh_tokens)
        print(f"[brain] loaded {loaded[0]} access token(s), {loaded[1]} refresh token(s) from disk")

    def _save_tokens(self) -> None:
        self._tokens_store_path.parent.mkdir(parents=True, exist_ok=True)
        out: dict[str, Any] = {
            "access_tokens": {
                t: tok.model_dump(mode="json", exclude_none=True)
                for t, tok in self.access_tokens.items()
            },
            "refresh_tokens": {
                t: tok.model_dump(mode="json", exclude_none=True)
                for t, tok in self.refresh_tokens.items()
            },
            "access_to_refresh": dict(self._access_to_refresh_map),
            "refresh_to_access": dict(self._refresh_to_access_map),
        }
        _atomic_write(self._tokens_store_path, out)

    # ---- override token-issuing methods to persist -------------------

    @contextmanager
    def _token_transaction(self) -> Iterator[None]:
        # The parent replaces entries without mutating the token objects.
        state = {name: getattr(self, name).copy() for name in (
            "auth_codes", "access_tokens", "refresh_tokens",
            "_access_to_refresh_map", "_refresh_to_access_map",
        )}
        try:
            yield
            self._save_tokens()
        except Exception:
            for name, snapshot in state.items():
                current = getattr(self, name)
                current.clear()
                current.update(snapshot)
            raise

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: Any,
    ) -> OAuthToken:
        with self._token_transaction():
            token = await super().exchange_authorization_code(client, authorization_code)
        return token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        with self._token_transaction():
            token = await super().exchange_refresh_token(client, refresh_token, scopes)
        return token

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self._token_transaction():
            await super().revoke_token(token)

    # ---- DCR override ------------------------------------------------

    async def register_client(
        self, client_info: OAuthClientInformationFull
    ) -> None:
        bad: list[str] = []
        for uri in client_info.redirect_uris or []:
            if not _redirect_uri_allowed(str(uri)):
                bad.append(str(uri))
        if bad:
            raise RegistrationError(
                "invalid_redirect_uri",
                "redirect_uri host(s) not in allowlist "
                f"({', '.join(sorted(ALLOWED_REDIRECT_HOSTS))}): {bad}",
            )
        await super().register_client(client_info)
        self._save_clients()


# ---- helpers ---------------------------------------------------------

def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=path.name + ".", suffix=".tmp", delete=False,
    )
    try:
        with tmp:
            json.dump(data, tmp, indent=2, sort_keys=True)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp.name, path)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def build_provider(*, base_url: str, store_path: Path) -> BrainOAuthProvider:
    tokens_store_path = store_path.parent / ".oauth-tokens.json"
    issuer = os.environ.get("BRAIN_ACCESS_ISSUER", "").rstrip("/")
    audience = os.environ.get("BRAIN_ACCESS_AUDIENCE", "")
    clients = set(os.environ.get("BRAIN_ACCESS_CLIENT_IDS", "").split())
    service_access = None
    if issuer and audience and clients:
        from brain_access import ServiceAccessVerifier
        service_access = ServiceAccessVerifier(
            issuer=issuer, audience=audience, client_ids=clients,
            jwks_uri=f"{issuer}/cdn-cgi/access/certs",
        )
    return BrainOAuthProvider(
        base_url=base_url,
        store_path=store_path,
        tokens_store_path=tokens_store_path,
        service_access=service_access,
    )
