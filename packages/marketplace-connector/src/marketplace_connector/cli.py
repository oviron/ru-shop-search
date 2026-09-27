"""Operator CLI for the marketplace connectors.

``marketplace-mcp doctor``
    Run every connector's selfcheck and report per-source status. This is the
    answer to "is it broken, or is it me": a source blocked from your network
    shows as inconclusive with the reason, a drifted parser shows as drift.
    A shop stopped after a refusal is skipped, not probed.

``marketplace-mcp stops``
    List shops stopped after a refusal and the seconds left on each stop.

``marketplace-mcp resume <shop>``
    Lift a stop by hand, e.g. after passing the captcha yourself in the browser window.

``marketplace-mcp browser [start|stop]``
    Start or close the plugin's anonymous Chrome profile (CDP on 127.0.0.1 only).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp_core import stopgate
from mcp_core.logging import log_event
from mcp_core.source_selection import canonical

_SELFCHECKS: list[tuple[str, str, str]] = [
    ("wildberries", "wb_connector.server", "wb_selfcheck"),
    ("ozon", "ozon_connector.server", "ozon_selfcheck"),
    ("yandex_market", "yandex_connector.server", "yandex_selfcheck"),
    ("detsky_mir", "detmir_connector.server", "detmir_selfcheck"),
    ("avito", "avito_connector.server", "avito_selfcheck"),
    ("lamoda", "lamoda_connector.server", "lamoda_selfcheck"),
    ("dns", "dns_connector.server", "dns_selfcheck"),
    ("citilink", "citilink_connector.server", "citilink_selfcheck"),
    ("mvideo", "mvideo_connector.server", "mvideo_selfcheck"),
    ("vseinstrumenti", "vseinstrumenti_connector.server", "vseinstrumenti_selfcheck"),
    ("aliexpress", "aliexpress_connector.server", "aliexpress_selfcheck"),
]


def _plugin_data_dirs() -> list[Path]:
    """Каталог данных зависит от способа установки: ``-inline`` для --plugin-dir, иначе имя marketplace."""
    env = os.environ.get("CLAUDE_PLUGIN_DATA")
    if env:
        return [Path(env)]
    return sorted(Path.home().glob(".claude/plugins/data/ru-shop-search-*"), key=lambda p: p.stat().st_mtime)


def cmd_stops(argv: list[str]) -> int:
    stopped = {shop: entry for data in _plugin_data_dirs() for shop, entry in stopgate.StopGate(data).status().items()}
    if not stopped:
        print("no shop is stopped")
    for shop, entry in sorted(stopped.items()):
        print(f"  {shop:<14} {entry['retry_after_s']:>5}s left  {entry['reason']}")
    return 0


def cmd_resume(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: marketplace-mcp resume <shop>", file=sys.stderr)
        return 2
    shop = canonical(argv[0])
    for data in _plugin_data_dirs():
        stopgate.StopGate(data).clear(shop)
    print(f"{shop}: stop lifted")
    return 0


def cmd_browser(argv: list[str]) -> int:
    from mcp_core.transport import chrome_cdp

    action = argv[0] if argv else "start"
    if action == "start" and len(argv) <= 1:
        ok, detail = asyncio.run(chrome_cdp.start_browser())
        print(f"Chrome CDP on {chrome_cdp.CDP_URL}: {detail}" if ok else f"Chrome did not start: {detail}")
        print(f"profile: {chrome_cdp.SCRAPING_PROFILE}")
        return 0 if ok else 1
    if action == "stop" and len(argv) == 1:
        closed = asyncio.run(chrome_cdp.close_browser())
        print("Chrome closed" if closed else f"nothing listening on {chrome_cdp.CDP_URL}")
        return 0
    print("usage: marketplace-mcp browser [start|stop]", file=sys.stderr)
    return 2


def _check_detail(check: object) -> str:
    """Render one sub-check as state plus the reason it reached that state.

    ``inconclusive`` on its own is unactionable — rate-limited, IP-banned and
    no-CDP all print the same word, and the operator is left guessing which.
    Connectors already classify this (avito sets rate_limited vs blocked vs
    transport_down, with the HTTP code), so the only thing missing was showing
    it. Wait out a 429; fix the network for a 403.
    """
    state = _attr(check, "state", "?")
    reason = _attr(check, "reason", None)
    code = _attr(check, "code", None)
    # Some canaries (notably Yandex) carry the diagnosis in `detail` or
    # `notes` instead of `reason`. Keep that explanation for non-healthy
    # states: a bare "inconclusive" loses the operator's next action.
    if not reason and state != "healthy":
        detail = _attr(check, "detail", None)
        if isinstance(detail, str) and detail.strip():
            reason = detail
        else:
            notes = _attr(check, "notes", None)
            if isinstance(notes, list):
                reason = "; ".join(note.strip() for note in notes if isinstance(note, str) and note.strip())
    if not reason:
        return str(state)
    reason = " ".join(str(reason).split())
    if len(reason) > 240:
        reason = reason[:237] + "..."
    suffix = f" http {code}" if code else ""
    return f"{state} ({reason}{suffix})"


def _attr(obj: object, key: str, default: object = None) -> object:
    """Sub-checks arrive as dicts from some connectors and models from others."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


async def _run_one_selfcheck(name: str, module_path: str, tool_name: str) -> tuple[str, str, str]:
    """Run one connector's selfcheck, returning (name, status, detail)."""
    try:
        module = __import__(module_path, fromlist=[tool_name])
        tool = getattr(module, tool_name)
        result = await tool()
        status = str(_attr(result, "status", "unknown"))
        checks = _attr(result, "checks", {}) or {}
        if not isinstance(checks, dict):
            raise TypeError("selfcheck checks must be a mapping")
        detail = ", ".join(f"{k}:{_check_detail(v)}" for k, v in checks.items()) or status
        return name, status, detail
    except Exception as exc:
        return name, "error", f"{type(exc).__name__}: {str(exc)[:120]}"


def cmd_doctor(argv: list[str]) -> int:
    """Run every selfcheck and print a per-source health table.

    ``--status-file PATH`` also writes the machine-readable report: a JSON
    snapshot with per-source status and a timestamp, so a cron job or dashboard
    can watch drift without parsing the human table. The file is replaced
    atomically, never appended to — a monitoring reader always gets one
    complete snapshot.
    """
    status_file: str | None = None
    sources: list[str] = []
    it = iter(argv)
    for arg in it:
        if arg == "--status-file":
            if status_file is not None:
                print("--status-file may only be supplied once", file=sys.stderr)
                return 2
            status_file = next(it, None)
            if not status_file or status_file.startswith("-"):
                print("--status-file requires a path", file=sys.stderr)
                return 2
        elif arg.startswith("-"):
            print(f"unknown doctor option {arg!r}", file=sys.stderr)
            return 2
        else:
            sources.append(canonical(arg))
    only = set(sources) if sources else None
    known_sources = {name for name, _, _ in _SELFCHECKS}
    unknown = (only or set()) - known_sources
    if unknown:
        print(
            f"unknown source(s): {', '.join(sorted(unknown))}; expected one of {', '.join(sorted(known_sources))}",
            file=sys.stderr,
        )
        return 2
    results = []
    for name, module_path, tool_name in _SELFCHECKS:
        if only and name not in only:
            continue
        try:
            stopgate.default_gate().check(name)
        except stopgate.SourceStoppedError as exc:
            results.append((name, "stopped", str(exc)))
            continue
        results.append(asyncio.run(_run_one_selfcheck(name, module_path, tool_name)))

    print("Marketplace connector health (selfcheck):")
    healthy = blocked = drifted = 0
    for name, status, detail in results:
        marker = {"success": "ok ", "drift_detected": "DRF", "inconclusive": "blk", "stopped": "stp"}.get(status, "err")
        if status == "success":
            healthy += 1
        elif status == "drift_detected":
            drifted += 1
        else:
            blocked += 1
        print(f"  {marker} {name:<14} {status:<15} {detail}")
    print(f"\n  {healthy} healthy, {blocked} blocked/inconclusive, {drifted} drifted.")
    print("  'inconclusive' usually means the source is blocked from this network —")
    print("  run it from a machine with your Chrome (CDP) or a Russian residential IP.")

    # CDP-backed sources depend on the plugin's Chrome profile being up;
    # probe that session directly so a dead browser is reported as itself,
    # not mistaken for five separate marketplace outages.
    cdp_note = ""
    try:
        from mcp_core.transport.chrome_cdp import probe_session

        probe = asyncio.run(probe_session())
        if probe.get("reachable"):
            cdp_note = (
                f"Chrome CDP: reachable on {probe['host']}:{probe['port']} ({probe.get('contexts', '?')} context(s))."
            )
        else:
            cdp_note = f"Chrome CDP: NOT reachable — {probe.get('reason')}. Ozon/Avito/Lamoda-search/DNS/Citilink/AliExpress need it."
    except Exception as exc:
        cdp_note = f"Chrome CDP: probe failed ({type(exc).__name__})."
    print(f"\n  {cdp_note}")

    if status_file:
        import datetime
        import pathlib
        import tempfile

        report = {
            "checked_at": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
            "healthy": healthy,
            "blocked": blocked,
            "drifted": drifted,
            "sources": {name: {"status": status, "detail": detail} for name, status, detail in results},
        }
        target = pathlib.Path(status_file)
        try:
            with tempfile.NamedTemporaryFile(
                "w", dir=target.parent, delete=False, suffix=".tmp", encoding="utf-8"
            ) as fh:
                json.dump(report, fh, indent=2, ensure_ascii=False)
                tmp = fh.name
            pathlib.Path(tmp).replace(target)
            print(f"\n  status written to {target}")
        except OSError as exc:
            print(f"\n  could not write status file {target}: {exc}", file=sys.stderr)
            return 3

    # Exit codes are what a cron job or CI step actually reads, so they have to
    # separate the three outcomes an operator responds to differently:
    #   0 — everything that could be checked is healthy.
    #   1 — a parser drifted. Someone has to go look; this is the alarm.
    #   2 — nothing drifted but a source could not be judged (blocked, no CDP,
    #       wrong country). Not an alarm, not a clean bill of health either.
    # Collapsing 2 into 0 is how "all good" starts meaning "we checked nothing".
    if drifted:
        return 1
    if blocked:
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__ or "marketplace-mcp CLI")
        return 0
    command, rest = args[0], args[1:]
    commands = {"doctor": cmd_doctor, "stops": cmd_stops, "resume": cmd_resume, "browser": cmd_browser}
    if command in commands:
        return commands[command](rest)
    print(f"unknown command {command!r}; expected one of {', '.join(commands)}", file=sys.stderr)
    log_event("marketplace.cli.unknown", command=command)
    return 2


if __name__ == "__main__":
    sys.exit(main())
