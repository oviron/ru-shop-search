"""Offline tests for the operator CLI.

Selfchecks are monkeypatched out — doctor's job is to aggregate and report, and
that logic is testable without touching a network.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from marketplace_connector import cli
from mcp_core import stopgate
from mcp_core.stopgate import StopGate


@pytest.fixture(autouse=True)
def offline_cdp_probe(monkeypatch):
    """Even successful doctor runs must not depend on a local Chrome session."""
    from mcp_core.transport import chrome_cdp

    async def fake_probe():
        return {"reachable": False, "reason": "offline test"}

    monkeypatch.setattr(chrome_cdp, "probe_session", fake_probe)


def test_doctor_reports_per_source_status(monkeypatch, capsys):
    async def fake_selfcheck(name, module_path, tool_name):
        table = {
            "wildberries": ("wildberries", "success", "ok"),
            "ozon": ("ozon", "inconclusive", "blocked"),
        }
        return table.get(name, (name, "success", "ok"))

    monkeypatch.setattr(cli, "_run_one_selfcheck", fake_selfcheck)

    rc = cli.cmd_doctor(["wildberries", "ozon"])

    out = capsys.readouterr().out
    assert "wildberries" in out
    assert "ozon" in out
    # One source inconclusive: not an alarm (that is 1), not a clean bill of
    # health either. A cron job that treats 0 as "all good" must not see 0 here.
    assert rc == 2


def test_doctor_exits_nonzero_on_drift(monkeypatch, capsys):
    async def fake_selfcheck(name, module_path, tool_name):
        return name, "drift_detected", "search:drift"

    monkeypatch.setattr(cli, "_run_one_selfcheck", fake_selfcheck)

    rc = cli.cmd_doctor(["wildberries"])

    assert rc == 1


def test_doctor_survives_a_selfcheck_that_raises(monkeypatch, capsys):
    async def fake_selfcheck(name, module_path, tool_name):
        return name, "error", "RuntimeError: boom"

    monkeypatch.setattr(cli, "_run_one_selfcheck", fake_selfcheck)

    rc = cli.cmd_doctor(["avito"])

    out = capsys.readouterr().out
    assert "avito" in out
    assert rc == 2  # unjudgeable, not healthy


def test_main_routes_subcommands_and_rejects_unknown(capsys):
    assert cli.main(["install", "claude"]) == 2
    assert cli.main(["no-such-command"]) == 2
    assert cli.main(["--help"]) == 0


def test_doctor_writes_a_status_file(monkeypatch, capsys, tmp_path):
    async def fake_selfcheck(name, module_path, tool_name):
        return name, "success", "ok"

    monkeypatch.setattr(cli, "_run_one_selfcheck", fake_selfcheck)
    target = tmp_path / "status.json"

    rc = cli.cmd_doctor(["wildberries", "--status-file", str(target)])

    assert rc == 0
    import json as _json

    report = _json.loads(target.read_text())
    assert report["healthy"] == 1
    assert report["sources"]["wildberries"]["status"] == "success"
    assert "checked_at" in report


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["ozno"], "unknown source"),
        (["wildberries", "ozno"], "unknown source"),
        ([" "], "unknown source"),
        (["--bogus"], "unknown doctor option"),
        (["wildberries", "--bogus"], "unknown doctor option"),
        (["--status-file"], "requires a path"),
        (["--status-file", ""], "requires a path"),
        (["--status-file", "--bogus"], "requires a path"),
        (["--status-file", "status.json", "--status-file", "other.json"], "only be supplied once"),
    ],
)
def test_invalid_doctor_arguments_fail_before_any_checks(argv, message, monkeypatch, capsys):
    from mcp_core.transport import chrome_cdp

    def unexpected_call(*args):
        pytest.fail("invalid arguments must not perform network checks")

    monkeypatch.setattr(cli, "_run_one_selfcheck", unexpected_call)
    monkeypatch.setattr(chrome_cdp, "probe_session", unexpected_call)

    assert cli.cmd_doctor(argv) == 2
    captured = capsys.readouterr()
    assert message in captured.err
    assert not captured.out


def test_doctor_accepts_install_source_names_and_includes_aliexpress(monkeypatch):
    checked = []

    async def fake_selfcheck(name, module_path, tool_name):
        checked.append((name, module_path, tool_name))
        return name, "success", "ok"

    monkeypatch.setattr(cli, "_run_one_selfcheck", fake_selfcheck)

    assert cli.cmd_doctor(["WB", "yandex-market", "ym", "detsky-mir", "detmir", "ali", "aliexpress"]) == 0
    assert [name for name, _, _ in checked] == ["wildberries", "yandex_market", "detsky_mir", "aliexpress"]
    assert checked[-1] == ("aliexpress", "aliexpress_connector.server", "aliexpress_selfcheck")


@pytest.mark.parametrize("as_dict", [True, False])
async def test_run_one_selfcheck_reads_dict_and_model_responses(as_dict, monkeypatch):
    payload = {
        "status": "inconclusive",
        "checks": {"search": {"state": "inconclusive", "reason": "blocked", "code": 403}},
    }

    async def selfcheck():
        return payload if as_dict else SimpleNamespace(**payload)

    monkeypatch.setitem(sys.modules, "cli_test_connector", SimpleNamespace(selfcheck=selfcheck))

    assert await cli._run_one_selfcheck("test", "cli_test_connector", "selfcheck") == (
        "test",
        "inconclusive",
        "search:inconclusive (blocked http 403)",
    )


def test_doctor_exit_code_prefers_drift_over_inconclusive(monkeypatch, capsys):
    """Drift is the alarm; a blocked source must not mask it."""

    async def fake_selfcheck(name, module_path, tool_name):
        if name == "wildberries":
            return name, "drift_detected", "search:drift"
        return name, "inconclusive", "blocked"

    monkeypatch.setattr(cli, "_run_one_selfcheck", fake_selfcheck)

    assert cli.cmd_doctor(["wildberries", "ozon"]) == 1


def test_doctor_returns_zero_only_when_everything_is_healthy(monkeypatch, capsys):
    async def fake_selfcheck(name, module_path, tool_name):
        return name, "success", "ok"

    monkeypatch.setattr(cli, "_run_one_selfcheck", fake_selfcheck)

    assert cli.cmd_doctor(["wildberries", "ozon"]) == 0


# ------------------------------------------------------- actionable selfcheck detail ----


def test_doctor_shows_why_a_check_was_inconclusive():
    """ "inconclusive" alone cannot be acted on.

    Rate-limited, IP-banned and no-CDP all render as the same word, and the
    operator's next move differs for each: wait, change network, start Chrome.
    Connectors already classify this — doctor used to drop it on the floor.
    """
    rate_limited = {"state": "inconclusive", "reason": "rate_limited", "code": 429}
    banned = {"state": "inconclusive", "reason": "blocked", "code": 403}

    assert cli._check_detail(rate_limited) == "inconclusive (rate_limited http 429)"
    assert cli._check_detail(banned) == "inconclusive (blocked http 403)"


def test_a_healthy_check_stays_terse():
    assert cli._check_detail({"state": "healthy", "reason": None}) == "healthy"
    assert cli._check_detail({"state": "healthy", "detail": "42 valid products", "notes": ["ok"]}) == "healthy"


def test_doctor_preserves_yandex_empty_shell_diagnosis():
    from yandex_connector.models_output import YandexSelfcheckEntry

    check = YandexSelfcheckEntry(
        state="inconclusive",
        detail='{"error": "transport_down", "message": "yandex_card: empty_product_shell',
        notes=["product frame arrived hollow: degraded serving or a delisted product"],
    )
    assert "empty_product_shell" in cli._check_detail(check)
    assert cli._check_detail(check).startswith("inconclusive (")


def test_doctor_uses_notes_if_no_reason_or_detail():
    assert cli._check_detail({"state": "drift", "notes": ["price field vanished", "inspect the parser"]}) == (
        "drift (price field vanished; inspect the parser)"
    )


def test_doctor_keeps_diagnostics_on_one_bounded_line():
    detail = "empty_product_shell\n" + "unavailable " * 100
    rendered = cli._check_detail({"state": "inconclusive", "detail": detail})
    assert "\n" not in rendered
    assert len(rendered) <= len("inconclusive ()") + 240
    assert rendered.endswith("...)")


def test_detail_survives_a_reason_without_a_code():
    assert cli._check_detail({"state": "drift", "reason": "parse_smoke_failed"}) == "drift (parse_smoke_failed)"


def test_detail_reads_model_objects_as_well_as_dicts():
    """Connectors return sub-checks as models in some packages, dicts in others."""

    class Entry:
        state = "inconclusive"
        reason = "transport_down"
        code = None

    assert cli._check_detail(Entry()) == "inconclusive (transport_down)"


# ------------------------------------------------------------ stop gate ----


@pytest.fixture
def plugin_data(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(stopgate, "_default", None)
    data = tmp_path / ".claude/plugins/data/ru-shop-search-inline"
    data.mkdir(parents=True)
    return data


def test_stops_lists_what_the_server_stopped(plugin_data, capsys):
    StopGate(plugin_data).trip("ozon", "captcha")

    assert cli.main(["stops"]) == 0

    out = capsys.readouterr().out
    assert "ozon" in out and "captcha" in out


def test_resume_lifts_a_stop_after_the_user_passed_the_captcha(plugin_data, capsys):
    StopGate(plugin_data).trip("ozon", "captcha")

    assert cli.main(["resume", "ozon"]) == 0

    assert StopGate(plugin_data).status() == {}


def test_resume_needs_a_shop(plugin_data, capsys):
    assert cli.main(["resume"]) == 2


def test_doctor_skips_a_stopped_source(plugin_data, monkeypatch, capsys):
    StopGate(plugin_data).trip("avito", "403")
    called = []

    async def fake_selfcheck(name, module_path, tool_name):
        called.append(name)
        return name, "success", "ok"

    monkeypatch.setattr(cli, "_run_one_selfcheck", fake_selfcheck)

    cli.cmd_doctor(["avito", "dns"])

    assert called == ["dns"]
    assert "stopped" in capsys.readouterr().out


@pytest.fixture
def fake_browser(monkeypatch):
    from mcp_core.transport import chrome_cdp

    calls = []

    async def start():
        calls.append("start")
        return True, "started chrome"

    async def close():
        calls.append("stop")
        return False

    monkeypatch.setattr(chrome_cdp, "start_browser", start)
    monkeypatch.setattr(chrome_cdp, "close_browser", close)
    return calls


def test_browser_starts_the_profile_by_default(fake_browser, capsys):
    assert cli.main(["browser"]) == 0

    assert fake_browser == ["start"]
    assert "127.0.0.1" in capsys.readouterr().out


def test_browser_stop_is_quiet_when_nothing_listens(fake_browser, capsys):
    assert cli.main(["browser", "stop"]) == 0

    assert fake_browser == ["stop"]
    assert "nothing listening" in capsys.readouterr().out


def test_browser_rejects_an_unknown_action(fake_browser, capsys):
    assert cli.main(["browser", "restart"]) == 2
    assert fake_browser == []
