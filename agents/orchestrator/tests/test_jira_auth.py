"""After a restart, a stale saved token is refreshed with the refresh token, not replaced by a new login
(PMAT-294). Runs the real auth provider against a fake Atlassian and records what it sends."""

import asyncio
import json
import time

import httpx2
import pytest

from orchestrator import jira

TOKEN_URL = "https://auth.example/oauth/token"
METADATA = {"issuer": "https://auth.example/", "authorization_endpoint": "https://auth.example/authorize",
            "token_endpoint": TOKEN_URL, "response_types_supported": ["code"]}
CLIENT = {"client_id": "c1", "redirect_uris": ["http://localhost:8765/callback"],
          "token_endpoint_auth_method": "none"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "atlassian-oauth.json"
    monkeypatch.setattr(jira, "STORE", path)
    return path


def saved(store, expires_at: float) -> None:
    store.write_text(json.dumps({
        "client": CLIENT, "metadata": METADATA, "expires_at": expires_at,
        "tokens": {"access_token": "old", "token_type": "Bearer", "expires_in": 28800, "refresh_token": "r1"}}))


def run(monkeypatch) -> list[httpx2.Request]:
    """Send one MCP request after a restart; return every request the provider made."""
    sent = []

    def fake_atlassian(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        if str(request.url) == TOKEN_URL:
            return httpx2.Response(200, json={"access_token": "new", "token_type": "Bearer",
                                               "expires_in": 28800, "refresh_token": "r2"})
        ok = request.headers.get("Authorization") in ("Bearer new", "Bearer fresh")
        return httpx2.Response(200 if ok else 401)

    async def no_login(url):
        raise AssertionError(f"login link shown: {url}")

    monkeypatch.setattr(jira, "_show_link", no_login)

    async def go():
        async with httpx2.AsyncClient(auth=jira.auth(), transport=httpx2.MockTransport(fake_atlassian)) as http:
            assert (await http.post(jira.URL, json={})).status_code == 200

    asyncio.run(go())
    return sent


def test_an_expired_token_is_refreshed_not_logged_in_again(store, monkeypatch):
    saved(store, expires_at=time.time() - 60)

    sent = run(monkeypatch)

    assert str(sent[0].url) == TOKEN_URL
    assert b"grant_type=refresh_token" in sent[0].content and b"refresh_token=r1" in sent[0].content
    assert [str(r.url) for r in sent[1:]] == [jira.URL]
    assert sent[1].headers["Authorization"] == "Bearer new"
    data = json.loads(store.read_text())
    assert data["tokens"]["access_token"] == "new"
    assert data["expires_at"] > time.time() + 28000


def test_a_fresh_token_is_used_as_is(store, monkeypatch):
    saved(store, expires_at=time.time() + 3600)
    data = json.loads(store.read_text())
    data["tokens"]["access_token"] = "fresh"
    store.write_text(json.dumps(data))

    sent = run(monkeypatch)

    assert [str(r.url) for r in sent] == [jira.URL]
    assert sent[0].headers["Authorization"] == "Bearer fresh"
