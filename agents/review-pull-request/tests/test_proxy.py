"""The model client goes through the proxy from the environment, like a default httpx client (PMAT-290).

The Claude Code sandbox only lets traffic out through its proxy, so a model client that
ignores HTTPS_PROXY cannot reach the model there.
"""

import socket
import threading

import pytest

from review_pull_request.config import Config
from review_pull_request.session import make_chat_model

PROXY_VARS = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"]


@pytest.fixture
def recording_proxy(monkeypatch):
    """A local proxy that records each request line it gets, then hangs up."""
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    server = socket.create_server(("127.0.0.1", 0))
    seen = []

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                seen.append(conn.recv(4096).split(b"\r\n")[0].decode())

    threading.Thread(target=serve, daemon=True).start()
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{server.getsockname()[1]}")
    yield seen
    server.close()


def _call_model():
    """One model call through make_chat_model to a host nothing listens on; it always fails."""
    model = make_chat_model(Config(model="m", api_url="https://127.0.0.1:9", api_token="t" * 12), "m")
    with pytest.raises(Exception):
        model.invoke("hello")


def test_model_call_goes_through_the_https_proxy(recording_proxy):
    _call_model()
    assert recording_proxy == ["CONNECT 127.0.0.1:9 HTTP/1.1"]


def test_model_call_skips_the_proxy_for_a_no_proxy_host(recording_proxy, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    _call_model()
    assert recording_proxy == []
