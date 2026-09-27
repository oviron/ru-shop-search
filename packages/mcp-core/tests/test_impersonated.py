"""curl_cffi как Chrome: статус и Location без редиректов, потолок тела; shop_request — пауза и стоп магазина."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from mcp_core.errors import TransportDownError
from mcp_core.pacing import Pacer
from mcp_core.stopgate import SourceStoppedError, StopGate, is_refusal
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


# ------------------------------------------------------------- shop_request ----


def _script(monkeypatch, *statuses: int) -> list[dict]:
    calls: list[dict] = []
    replies = iter(statuses)

    async def fake_get(url, **kwargs):
        calls.append({"method": "GET", "url": url})
        status = next(replies)
        return impersonated.Reply(status, None if status == 0 else "{}", "network: reset" if status == 0 else None)

    async def fake_post(url, body, **kwargs):
        calls.append({"method": "POST", "url": url, "body": body})
        return impersonated.Reply(next(replies), "{}", None)

    monkeypatch.setattr(impersonated, "impersonated_get", fake_get)
    monkeypatch.setattr(impersonated, "impersonated_post", fake_post)
    return calls


def _shop(pacer, **kwargs) -> impersonated.Reply:
    return asyncio.run(
        impersonated.shop_request(
            "https://shop.test/x", pacer=pacer, provider="shop", label="shop_search", backoff_s=0, **kwargs
        )
    )


@pytest.mark.parametrize("status", [401, 403, 418, 429])
def test_a_refusal_stops_the_shop_without_a_second_request(monkeypatch, status):
    calls = _script(monkeypatch, status, 200)
    pacer = Pacer(0.0, source="shop", gate=StopGate(None))

    with pytest.raises(TransportDownError) as excinfo:
        _shop(pacer)
    assert excinfo.value.to_dict()["status_code"] == status
    assert is_refusal(excinfo.value)

    with pytest.raises(SourceStoppedError):
        _shop(pacer)
    assert len(calls) == 1


@pytest.mark.parametrize("first", [0, 502, 503, 504])
def test_a_network_fault_or_gateway_error_is_retried_once(monkeypatch, first):
    calls = _script(monkeypatch, first, 200)

    reply = _shop(Pacer(0.0, source="shop", gate=StopGate(None)))

    assert reply.status == 200
    assert len(calls) == 2


def test_a_second_gateway_error_is_an_error_not_a_stop(monkeypatch):
    calls = _script(monkeypatch, 502, 502, 200)
    pacer = Pacer(0.0, source="shop", gate=StopGate(None))

    with pytest.raises(TransportDownError) as excinfo:
        _shop(pacer)
    assert not is_refusal(excinfo.value)
    assert _shop(pacer).status == 200
    assert len(calls) == 3


def test_other_statuses_come_back_to_the_caller(monkeypatch):
    _script(monkeypatch, 404)
    assert _shop(Pacer(0.0, source="shop", gate=StopGate(None))).status == 404


def test_a_body_makes_it_a_post(monkeypatch):
    calls = _script(monkeypatch, 200)

    _shop(Pacer(0.0, source="shop", gate=StopGate(None)), body=b'{"q":1}')

    assert calls == [{"method": "POST", "url": "https://shop.test/x", "body": b'{"q":1}'}]


def test_post_sends_the_body_as_data(monkeypatch):
    calls: list[dict] = []

    def post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _Resp(200, [b"{}"])

    monkeypatch.setattr(impersonated, "curl_requests", SimpleNamespace(post=post))

    reply = asyncio.run(
        impersonated.impersonated_post("https://shop.test/x", b"{}", timeout_s=5, wall_timeout_s=5, max_bytes=64)
    )

    assert reply.text == "{}"
    assert calls[0]["data"] == b"{}"
    assert calls[0]["allow_redirects"] is False
