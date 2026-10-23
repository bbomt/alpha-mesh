"""Read-only Jira access through the Atlassian MCP server, with the orchestrator's own OAuth login.

Login is the same headless flow as Claude's: open the printed link in a fresh browser tab, accept, and
paste back the URL the browser ends on. Tokens are kept in ~/.config/alpha-mesh/atlassian-oauth.json
(mode 600) and refreshed, so this is needed once.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx2
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

URL = "https://mcp.atlassian.com/v2/mcp"
CLOUD_ID = "d91f7eef-5262-4e7b-8e6e-197d0b74b248"
STORE = Path.home() / ".config/alpha-mesh/atlassian-oauth.json"
WORKER_MARK = "MESSAGE-FROM-WORKER"


@dataclass(frozen=True)
class Ticket:
    labels: list[str]
    done: bool
    comment_id: str | None = None  # the owner's newest comment (workers' comments skipped)


class FileStorage(TokenStorage):
    def _load(self) -> dict:
        return json.loads(STORE.read_text()) if STORE.exists() else {}

    def _save(self, key: str, value: dict) -> None:
        data = self._load() | {key: value}
        STORE.parent.mkdir(parents=True, exist_ok=True)
        STORE.touch(mode=0o600)
        STORE.write_text(json.dumps(data))

    async def get_tokens(self):
        t = self._load().get("tokens")
        return OAuthToken.model_validate(t) if t else None

    async def set_tokens(self, tokens):
        self._save("tokens", tokens.model_dump(mode="json"))

    async def get_client_info(self):
        c = self._load().get("client")
        return OAuthClientInformationFull.model_validate(c) if c else None

    async def set_client_info(self, client_info):
        self._save("client", client_info.model_dump(mode="json"))


async def _show_link(url: str) -> None:
    print(f"\nAtlassian login: open this link in a fresh browser tab, accept, then copy the URL the browser"
          f" ends on (it will fail to load):\n\n{url}\n")


async def _read_redirect() -> AuthorizationCodeResult:
    q = parse_qs(urlparse(input("Paste the redirect URL: ").strip()).query)
    return AuthorizationCodeResult(code=q["code"][0], state=q.get("state", [None])[0], iss=q.get("iss", [None])[0])


def _data(result) -> dict:
    if result.is_error:
        raise RuntimeError(result.content[0].text)
    return json.loads(result.content[0].text)["data"]


def newest_owner_comment(newest_first: list[dict]) -> str | None:
    """The owner's newest comment, skipping workers' (marked on their first line). Only the newest overall
    would let a worker comment posted right after the owner's hide it (PMAT-221, 2026-10-23)."""
    return next((c["id"] for c in newest_first
                 if WORKER_MARK not in c["body"].strip().split("\n", 1)[0]), None)


async def snapshot(keys: list[str]) -> dict[str, Ticket]:
    """Labels, Done or not, and the owner's newest comment of each ticket."""
    auth = OAuthClientProvider(
        URL,
        OAuthClientMetadata(
            client_name="alpha-mesh orchestrator",
            redirect_uris=["http://localhost:8765/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        FileStorage(),
        redirect_handler=_show_link,
        callback_handler=_read_redirect,
    )
    tickets = {}
    async with (
        httpx2.AsyncClient(auth=auth, timeout=60) as http,
        streamable_http_client(URL, http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        for key in keys:
            # Labels come back only with the full view.
            issue = _data(await session.call_tool("getJiraIssue", {
                "cloudId": CLOUD_ID, "issueIdOrKey": key, "fields": ["labels", "status"], "view": "full"}))
            comments = _data(await session.call_tool("executeRead", {
                "name": "listJiraIssueComments", "cloudId": CLOUD_ID,
                "inputs": {"issueIdOrKey": key, "orderBy": "-created", "maxResults": 20}}))["comments"]
            tickets[key] = Ticket(
                labels=issue["fields"].get("labels", []),
                done=issue["fields"]["status"]["statusCategory"]["key"] == "done",
                comment_id=newest_owner_comment(comments),
            )
    return tickets
