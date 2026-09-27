import pytest
from mcp_core import stopgate


@pytest.fixture(autouse=True)
def _isolated_stop_gate(monkeypatch, tmp_path):
    """Стоп магазина из одного теста не должен останавливать его в следующем или у пользователя."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin-data"))
    monkeypatch.setattr(stopgate, "_default", stopgate.StopGate(None))
