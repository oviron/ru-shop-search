"""Subprocess regressions for the operational probes (no network needed)."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import mcp_startup
import mcp_wire
from stdio_probe import ProbeError, StdioProbe

SERVER = r"""
import json, sys, time
mode = sys.argv[1]
if mode == "silent":
    time.sleep(30)
if mode == "noisy":
    sys.stderr.write("x" * (2 * 1024 * 1024))
    sys.stderr.flush()
for line in sys.stdin:
    message = json.loads(line)
    request_id = message.get("id")
    if request_id is None:
        continue
    if mode == "interleaved":
        print("not json", flush=True)
        print("[]", flush=True)
        print(json.dumps({"jsonrpc": "2.0", "method": "notifications/progress"}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": 999, "result": {}}), flush=True)
    if mode == "error" + str(request_id):
        print(json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": "fixture error"}}), flush=True)
        continue
    version = "fixture-old" if mode == "stale_initialize" else "fixture-current"
    result = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "fixture", "version": version}}
    if mode == "missing_initialize":
        result["serverInfo"].pop("version")
    if request_id == 2:
        result = {"tools": [{"name": "tool_" + str(i), "inputSchema": {"type": "object"}} for i in range(40)]}
        if mode == "badtools":
            result = {"tools": None}
    elif request_id == 3:
        version = "fixture-old" if mode == "stale_sources" else "fixture-current"
        result = {"structuredContent": {"mounted_count": 14, "server_version": version}}
        if mode == "missing_sources":
            result["structuredContent"].pop("server_version")
        if mode == "toolerror":
            result["isError"] = True
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
"""


def command(mode: str) -> list[str]:
    return [sys.executable, "-u", "-c", SERVER, mode]


def test_silent_child_obeys_deadline_and_is_reaped() -> None:
    started = time.monotonic()
    with StdioProbe(command("silent")) as probe, pytest.raises(ProbeError, match="deadline exceeded"):
        probe.initialize("test", time.monotonic() + 0.15)
    assert time.monotonic() - started < 3
    assert probe.proc.poll() is not None
    assert all(not thread.is_alive() for thread in probe._threads)
    assert probe.proc.stdout.closed
    assert probe.proc.stderr.closed


@pytest.mark.parametrize("mode", ["noisy", "interleaved"])
def test_noisy_and_interleaved_child_completes(mode: str) -> None:
    with StdioProbe(command(mode)) as probe:
        deadline = time.monotonic() + 3
        probe.initialize("test", deadline)
        assert len(probe.list_tools(deadline)) == 40
        assert len(probe.stderr) <= 2500
    assert probe.proc.poll() is not None


@pytest.mark.parametrize("mode", ["error1", "error2", "badtools"])
def test_invalid_results_fail(mode: str) -> None:
    with StdioProbe(command(mode)) as probe:
        deadline = time.monotonic() + 3
        with pytest.raises(ProbeError, match=r"JSON-RPC error|named tool objects"):
            probe.initialize("test", deadline)
            probe.list_tools(deadline)
    assert probe.proc.poll() is not None


def test_early_exit_reports_stderr() -> None:
    with StdioProbe([sys.executable, "-c", "import sys; sys.stderr.write('fixture failed'); sys.exit(7)"]) as probe:
        probe.proc.wait(timeout=3)
        for thread in probe._threads:
            thread.join(timeout=1)
        with pytest.raises(ProbeError, match=r"stdout closed.*fixture failed"):
            probe.response(1, time.monotonic() + 3)
    assert probe.proc.returncode == 7


@pytest.fixture
def unrelated_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        yield proc
    finally:
        proc.terminate()
        proc.wait(timeout=3)


@pytest.mark.parametrize("wrapper_exits", [False, True])
def test_cleanup_stops_wrapper_and_its_child(
    tmp_path: Path, wrapper_exits: bool, unrelated_process: subprocess.Popen
) -> None:
    marker = tmp_path / "child-heartbeat.txt"
    child = (
        "import pathlib,time; "
        f"path=pathlib.Path({str(marker)!r}); deadline=time.monotonic()+5\n"
        "while time.monotonic()<deadline:\n"
        " with path.open('a') as stream: stream.write('x')\n"
        " time.sleep(0.02)\n"
    )
    wrapper = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-u','-c',{child!r}])"
    if not wrapper_exits:
        wrapper += "; time.sleep(30)"
    else:
        # Ensure the grandchild is running before its wrapper exits immediately.
        wrapper += f"\nwhile not __import__('pathlib').Path({str(marker)!r}).exists(): time.sleep(0.01)"
    with StdioProbe([sys.executable, "-u", "-c", wrapper]) as probe:
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        if wrapper_exits:
            assert probe.proc.wait(timeout=3) == 0
    size = marker.stat().st_size
    time.sleep(0.1)
    assert marker.stat().st_size == size
    assert probe.proc.poll() is not None
    assert all(not thread.is_alive() for thread in probe._threads)
    assert unrelated_process.poll() is None


def replace_command(monkeypatch: pytest.MonkeyPatch, module: object, mode: str) -> list[StdioProbe]:
    probes: list[StdioProbe] = []

    def start(_: list[str]) -> StdioProbe:
        child = StdioProbe(command(mode))
        probes.append(child)
        return child

    monkeypatch.setattr(module, "StdioProbe", start)
    return probes


@pytest.mark.parametrize("module", [mcp_wire, mcp_startup])
def test_all_probe_entrypoints_fail_on_silence(monkeypatch: pytest.MonkeyPatch, module: object) -> None:
    probes = replace_command(monkeypatch, module, "silent")
    started = time.monotonic()
    if module is mcp_wire:
        tools, elapsed = module.fetch_tools(Path.cwd(), "fixture", timeout=0.15)
        assert tools is None
        assert elapsed < 3
    else:
        assert module.measure(Path.cwd(), "fixture", timeout=0.15) == 1
    assert time.monotonic() - started < 3
    assert probes[0].proc.poll() is not None


@pytest.mark.parametrize("module", [mcp_wire, mcp_startup])
@pytest.mark.parametrize("mode", ["noisy", "interleaved", "error1", "error2"])
def test_all_probe_entrypoints_check_protocol(monkeypatch: pytest.MonkeyPatch, module: object, mode: str) -> None:
    probes = replace_command(monkeypatch, module, mode)
    success = not mode.startswith("error")
    if module is mcp_wire:
        tools, _ = module.fetch_tools(Path.cwd(), "fixture", timeout=3)
        assert (tools is not None) is success
    else:
        assert module.measure(Path.cwd(), "fixture", timeout=3) == (0 if success else 1)
    assert probes[0].proc.poll() is not None
