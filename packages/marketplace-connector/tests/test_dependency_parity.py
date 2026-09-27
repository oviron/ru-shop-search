"""Каждый коннектор, который импортирует compare, объявлен зависимостью пакета сервера."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
COMPARE = ROOT / "packages/compare-connector/src/compare_connector/server.py"
PYPROJECT = ROOT / "packages/marketplace-connector/pyproject.toml"


def test_every_imported_connector_is_a_declared_workspace_dependency():
    imported = {
        name.replace("_", "-")
        for name in re.findall(r"^\s+from (\w+_connector) import server", COMPARE.read_text(), re.M)
    }
    project = tomllib.loads(PYPROJECT.read_text())
    declared = {re.match(r"[\w.-]+", dep)[0] for dep in project["project"]["dependencies"]}
    workspace = {name for name, src in project["tool"]["uv"]["sources"].items() if src.get("workspace")}

    assert len(imported) >= 9
    assert imported <= declared
    assert imported <= workspace
