"""Каталог данных плагина: env под плагином, иначе последняя установка, иначе каталог --plugin-dir."""

from __future__ import annotations

import os

from mcp_core.plugin_data import plugin_data_dir


def test_the_env_set_by_claude_code_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "given"))

    assert plugin_data_dir() == tmp_path / "given"


def test_without_env_the_most_recent_install_is_used(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / ".claude" / "plugins" / "data"
    old, new = root / "ru-shop-search-inline", root / "ru-shop-search-ru-shop-search"
    for i, path in enumerate((old, new)):
        path.mkdir(parents=True)
        os.utime(path, (1000 + i, 1000 + i))
    (root / "other-plugin-inline").mkdir()

    assert plugin_data_dir() == new


def test_with_no_install_it_falls_back_to_the_plugin_dir_location(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert plugin_data_dir() == tmp_path / ".claude" / "plugins" / "data" / "ru-shop-search-inline"
