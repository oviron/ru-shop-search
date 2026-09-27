"""GET через curl_cffi: статус и Location без редиректов, потолок тела, ошибки значением."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from mcp_core.transport import impersonated


class _Resp:
    def __init__(self, status: int, chunks: list[bytes], headers: dict[str, str] | None = None):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks
        self.pulled = 0

    def iter_content(self):
        for chunk in self._chunks:
            self.pulled += 1
            yield chunk


def _serve(monkeypatch, resp: _Resp) -> list[dict]:
    calls: list[dict] = []

    def get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return resp

    monkeypatch.setattr(impersonated, "curl_requests", SimpleNamespace(get=get))
    return calls


def _get(url: str = "https://example.test/x", **kwargs) -> impersonated.Reply:
    options = {"timeout_s": 5, "wall_timeout_s": 5, "max_bytes": 1024, **kwargs}
    return asyncio.run(impersonated.impersonated_get(url, **options))


def test_a_redirect_is_returned_with_its_location_not_followed(monkeypatch):
    calls = _serve(monkeypatch, _Resp(302, [b""], {"location": "https://example.test/showcaptcha?x=1"}))

    reply = _get()

    assert (reply.status, reply.location, reply.err) == (302, "https://example.test/showcaptcha?x=1", None)
    assert calls[0]["allow_redirects"] is False
    assert calls[0]["impersonate"] == "chrome"


def test_the_body_is_decoded_by_its_charset(monkeypatch):
    _serve(monkeypatch, _Resp(200, ["цена".encode("cp1251")], {"content-type": "text/html; charset=windows-1251"}))

    assert _get().text == "цена"


def test_an_oversized_body_stops_the_read_at_the_cap(monkeypatch):
    resp = _Resp(200, [b"x" * 512] * 64)
    _serve(monkeypatch, resp)

    reply = _get(max_bytes=1024)

    assert (reply.status, reply.text) == (200, None)
    assert "exceeded" in reply.err
    assert resp.pulled == 3


def test_a_hung_request_is_a_timeout_value(monkeypatch):
    def slow(url, **kwargs):
        time.sleep(2)
        raise AssertionError("the wall clock fires first")

    monkeypatch.setattr(impersonated, "curl_requests", SimpleNamespace(get=slow))

    reply = _get(wall_timeout_s=0.01)

    assert (reply.status, reply.text) == (0, None)
    assert reply.err.startswith("timeout:")


def test_a_curl_failure_is_a_network_value_without_secrets(monkeypatch):
    def boom(url, **kwargs):
        raise RuntimeError("proxy socks5://user:secret@10.0.0.1 failed")

    monkeypatch.setattr(impersonated, "curl_requests", SimpleNamespace(get=boom))

    reply = _get()

    assert reply.status == 0
    assert reply.err.startswith("network: RuntimeError")
    assert "secret" not in reply.err
