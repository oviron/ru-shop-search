"""Стоп на первом отказе: магазин, который отказал, не получает ни одного запроса до конца кулдауна."""

from __future__ import annotations

import json

import pytest
from fastmcp.exceptions import ToolError
from mcp_core import stopgate
from mcp_core.errors import (
    AuthMissingError,
    ChallengeRequiredError,
    ErrorCode,
    ParserDriftError,
    PermissionDeniedError,
    RateLimitedError,
    TransportDownError,
    UpstreamTimeoutError,
    raise_tool_error,
)
from mcp_core.pacing import Pacer
from mcp_core.stopgate import SourceStoppedError, StopGate


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def test_an_untouched_source_passes():
    StopGate(None).check("wildberries")


def test_a_tripped_source_is_stopped_with_the_time_left():
    clock = Clock()
    gate = StopGate(None, clock=clock)
    gate.trip("yandex_market", "captcha", cooldown_s=1800)
    clock.now += 600

    with pytest.raises(SourceStoppedError) as caught:
        gate.check("yandex_market")

    assert caught.value.code is ErrorCode.RATE_LIMITED
    assert caught.value.provider == "yandex_market"
    assert caught.value.retry_after_s == 1200
    assert "captcha" in str(caught.value)


def test_other_sources_are_not_affected():
    gate = StopGate(None)
    gate.trip("ozon", "403")

    gate.check("wildberries")


def test_the_stop_lifts_after_the_cooldown():
    clock = Clock()
    gate = StopGate(None, clock=clock)
    gate.trip("dns", "403", cooldown_s=60)
    clock.now += 60

    gate.check("dns")
    assert gate.status() == {}


def test_the_stop_survives_a_server_restart(tmp_path):
    clock = Clock()
    StopGate(tmp_path, clock=clock).trip("citilink", "429", cooldown_s=300)

    restarted = StopGate(tmp_path, clock=clock)

    with pytest.raises(SourceStoppedError):
        restarted.check("citilink")


def test_the_state_file_holds_no_request_detail(tmp_path):
    StopGate(tmp_path).trip("wildberries", "403 x_wbaas_token=secret")

    raw = (tmp_path / "stopped_sources.json").read_text(encoding="utf-8")

    assert "secret" not in raw
    assert set(json.loads(raw)["wildberries"]) == {"until", "reason"}


def test_a_corrupt_state_file_does_not_stop_everything(tmp_path):
    (tmp_path / "stopped_sources.json").write_text("{not json", encoding="utf-8")

    StopGate(tmp_path).check("ozon")


def test_clear_lifts_a_stop_by_hand(tmp_path):
    gate = StopGate(tmp_path)
    gate.trip("lamoda", "403")

    gate.clear("lamoda")

    gate.check("lamoda")


def test_status_lists_every_stopped_source_with_seconds_left():
    clock = Clock()
    gate = StopGate(None, clock=clock)
    gate.trip("ozon", "captcha", cooldown_s=100)
    gate.trip("dns", "403", cooldown_s=50)

    assert gate.status() == {
        "ozon": {"reason": "captcha", "retry_after_s": 100},
        "dns": {"reason": "403", "retry_after_s": 50},
    }


@pytest.mark.parametrize(
    "error",
    [
        RateLimitedError("wb"),
        ChallengeRequiredError("captcha", provider="yandex"),
        PermissionDeniedError(),
        TransportDownError("blocked", status_code=403),
        TransportDownError("blocked", status_code=401),
        AuthMissingError("auth failed (403)"),
    ],
)
def test_refusals_are_recognised(error):
    assert stopgate.is_refusal(error)


@pytest.mark.parametrize(
    "error",
    [
        TransportDownError("connection reset"),
        TransportDownError("bad gateway", status_code=502),
        UpstreamTimeoutError(),
        ParserDriftError(),
        SourceStoppedError("ozon", reason="403", retry_after_s=10),
        ValueError("boom"),
    ],
)
def test_our_own_failures_are_not_refusals(error):
    assert not stopgate.is_refusal(error)


def test_the_default_gate_lives_in_the_plugin_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    monkeypatch.setattr(stopgate, "_default", None)

    stopgate.default_gate().trip("ozon", "403")

    assert (tmp_path / "stopped_sources.json").exists()


async def test_a_named_pacer_stops_its_source_on_the_first_refusal():
    gate = StopGate(None)
    pacer = Pacer(0.0, source="ozon", gate=gate)
    await pacer.wait()

    pacer.record_refusal()

    with pytest.raises(SourceStoppedError):
        await pacer.wait()


async def test_a_pacer_refuses_to_start_a_request_for_a_stopped_source():
    gate = StopGate(None)
    gate.trip("dns", "captcha")

    with pytest.raises(SourceStoppedError):
        await Pacer(0.0, source="dns", gate=gate).wait()


def _as_tool_error(error):
    with pytest.raises(ToolError) as caught:
        raise_tool_error(error)
    return caught.value


def test_a_refusal_wrapped_in_a_tool_error_is_recognised():
    assert stopgate.is_refusal(_as_tool_error(ChallengeRequiredError("captcha", provider="yandex")))


def test_a_stop_wrapped_in_a_tool_error_is_not_a_new_refusal():
    assert not stopgate.is_refusal(_as_tool_error(SourceStoppedError("ozon", reason="403", retry_after_s=5)))


def test_a_tool_error_without_a_json_body_is_not_a_refusal():
    assert not stopgate.is_refusal(ToolError("403 somewhere in prose"))
