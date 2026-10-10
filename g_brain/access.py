"""Verify explicitly enrolled headless clients without changing hosted OAuth."""

import math
import time

from fastmcp.server.auth.providers.jwt import JWTVerifier


class ServiceAccessVerifier(JWTVerifier):
    def __init__(self, *, client_ids, **kwargs):
        super().__init__(algorithm="RS256", **kwargs)
        self.client_ids = frozenset(client_ids)

    async def load_access_token(self, token):
        try:
            access = await super().load_access_token(token)
        except OverflowError:
            return None
        if access is None:
            return None
        claims = access.claims
        client = claims.get("common_name")
        if (claims.get("type") != "app" or claims.get("sub") != ""
                or not isinstance(client, str) or client not in self.client_ids):
            return None
        now = time.time()
        exp, nbf = claims.get("exp"), claims.get("nbf", 0)
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) for value in (exp, nbf)):
            return None
        if exp <= now or nbf > now:
            return None
        access.client_id = client
        return access


class AccessAssertionMiddleware:
    """Present Access's signed assertion to the normal bearer verifier.

    No header is trusted here: the provider verifies signature, issuer, audience,
    expiry and a dedicated service-client allowlist. The public OAuth Worker's
    upstream credential must never be on that allowlist.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and (
                scope["path"] == "/mcp" or scope["path"].startswith("/mcp/")):
            headers = scope.get("headers", [])
            auth = [v for k, v in headers if k.lower() == b"authorization"]
            assertions = [v for k, v in headers if k.lower() == b"cf-access-jwt-assertion"]
            if len(auth) > 1 or len(assertions) > 1:
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body",
                            "body": b'{"error":"ambiguous_authentication_headers"}'})
                return
            if not auth and len(assertions) == 1 and assertions[0]:
                scope = {**scope, "headers": [*headers,
                         (b"authorization", b"Bearer " + assertions[0])]}
        await self.app(scope, receive, send)
