import os
from pathlib import Path

import pytest
from mcp_core import stopgate

# jsdom ставится в tools/jsdom (npm ci --prefix tools/jsdom), а не в корень плагина.
_JSDOM = Path(__file__).parent / "tools" / "jsdom" / "node_modules"
os.environ["NODE_PATH"] = os.pathsep.join(filter(None, [os.environ.get("NODE_PATH"), str(_JSDOM)]))


@pytest.fixture(autouse=True)
def _isolated_stop_gate(monkeypatch, tmp_path):
    """Стоп магазина из одного теста не должен останавливать его в следующем или у пользователя."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin-data"))
    monkeypatch.setattr(stopgate, "_default", stopgate.StopGate(None))
