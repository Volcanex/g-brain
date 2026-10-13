"""Token persistence failures must leave the client's previous grant usable."""

import asyncio
import errno
import json
import time

import pytest
from mcp.server.auth.provider import AuthorizationCode
from mcp.shared.auth import OAuthClientInformationFull


@pytest.fixture
def oauth():
    from g_brain import oauth
    return oauth


@pytest.fixture
def provider(oauth, tmp_path):
    return oauth.BrainOAuthProvider(
        base_url="https://brain.test",
        store_path=tmp_path / "clients.json",
        tokens_store_path=tmp_path / "tokens.json",
    )


@pytest.fixture
def client():
    return OAuthClientInformationFull(
        client_id="test-client",
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
        scope="read",
    )


def _code(provider, client, value="test-code"):
    code = AuthorizationCode(
        code=value,
        client_id=client.client_id,
        redirect_uri=client.redirect_uris[0],
        redirect_uri_provided_explicitly=True,
        scopes=["read"],
        expires_at=time.time() + 300,
        code_challenge="test-challenge",
    )
    provider.auth_codes[value] = code
    return code


def _issue(provider, client):
    return asyncio.run(provider.exchange_authorization_code(client, _code(provider, client)))


def _state(provider):
    return {name: getattr(provider, name).copy() for name in (
        "auth_codes", "access_tokens", "refresh_tokens",
        "_access_to_refresh_map", "_refresh_to_access_map",
    )}


def _disk_full(*args, **kwargs):
    raise OSError(errno.ENOSPC, "No space left on device")


@pytest.mark.parametrize("failure", ["save", "write", "fsync", "replace"])
def test_failed_refresh_keeps_old_tokens_and_allows_retry(
    provider, client, oauth, tmp_path, monkeypatch, failure,
):
    original = _issue(provider, client)
    before = provider._tokens_store_path.read_bytes()
    state = _state(provider)
    refresh = asyncio.run(provider.load_refresh_token(client, original.refresh_token))

    with monkeypatch.context() as patch:
        if failure == "save":
            patch.setattr(provider, "_save_tokens", _disk_full)
        elif failure == "write":
            def partial_write(data, stream, **kwargs):
                stream.write('{"partial":')
                stream.flush()
                _disk_full()
            patch.setattr(oauth.json, "dump", partial_write)
        else:
            patch.setattr(oauth.os, failure, _disk_full)
        with pytest.raises(OSError) as error:
            asyncio.run(provider.exchange_refresh_token(client, refresh, ["read"]))

    assert error.value.errno == errno.ENOSPC
    assert _state(provider) == state
    assert provider._tokens_store_path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [provider._tokens_store_path]
    assert asyncio.run(provider.load_access_token(original.access_token)) is not None
    retry_refresh = asyncio.run(provider.load_refresh_token(client, original.refresh_token))
    assert retry_refresh is not None

    restarted = oauth.BrainOAuthProvider(
        base_url="https://brain.test", store_path=tmp_path / "clients.json",
        tokens_store_path=provider._tokens_store_path,
    )
    assert asyncio.run(restarted.load_refresh_token(client, original.refresh_token)) is not None

    rotated = asyncio.run(provider.exchange_refresh_token(client, retry_refresh, ["read"]))
    assert rotated.refresh_token != original.refresh_token
    assert asyncio.run(provider.load_refresh_token(client, original.refresh_token)) is None
    assert asyncio.run(provider.load_access_token(original.access_token)) is None
    assert asyncio.run(provider.load_refresh_token(client, rotated.refresh_token)) is not None
    saved = json.loads(provider._tokens_store_path.read_text())
    assert set(saved["refresh_tokens"]) == {rotated.refresh_token}
    assert saved["access_to_refresh"] == {rotated.access_token: rotated.refresh_token}
    assert saved["refresh_to_access"] == {rotated.refresh_token: rotated.access_token}


def test_failed_code_exchange_restores_code_for_retry(provider, client, monkeypatch):
    _issue(provider, client)
    code = _code(provider, client, "retry-code")
    before = provider._tokens_store_path.read_bytes()
    state = _state(provider)
    with monkeypatch.context() as patch:
        patch.setattr(provider, "_save_tokens", _disk_full)
        with pytest.raises(OSError) as error:
            asyncio.run(provider.exchange_authorization_code(client, code))

    assert error.value.errno == errno.ENOSPC
    assert _state(provider) == state
    assert provider._tokens_store_path.read_bytes() == before
    retry_code = asyncio.run(provider.load_authorization_code(client, code.code))
    assert retry_code is not None
    issued = asyncio.run(provider.exchange_authorization_code(client, retry_code))
    assert asyncio.run(provider.load_authorization_code(client, code.code)) is None
    assert asyncio.run(provider.load_refresh_token(client, issued.refresh_token)) is not None
    assert issued.refresh_token in json.loads(provider._tokens_store_path.read_text())["refresh_tokens"]


@pytest.mark.parametrize("kind", ["access", "refresh"])
def test_failed_revocation_restores_pair_for_retry(provider, client, monkeypatch, kind):
    original = _issue(provider, client)
    before = provider._tokens_store_path.read_bytes()
    state = _state(provider)
    token = (provider.access_tokens[original.access_token] if kind == "access"
             else provider.refresh_tokens[original.refresh_token])
    with monkeypatch.context() as patch:
        patch.setattr(provider, "_save_tokens", _disk_full)
        with pytest.raises(OSError) as error:
            asyncio.run(provider.revoke_token(token))

    assert error.value.errno == errno.ENOSPC
    assert _state(provider) == state
    assert provider._tokens_store_path.read_bytes() == before
    assert asyncio.run(provider.load_access_token(original.access_token)) is not None
    assert asyncio.run(provider.load_refresh_token(client, original.refresh_token)) is not None
    asyncio.run(provider.revoke_token(token))
    assert asyncio.run(provider.load_access_token(original.access_token)) is None
    assert asyncio.run(provider.load_refresh_token(client, original.refresh_token)) is None
    saved = json.loads(provider._tokens_store_path.read_text())
    assert all(not saved[name] for name in (
        "access_tokens", "refresh_tokens", "access_to_refresh", "refresh_to_access",
    ))
