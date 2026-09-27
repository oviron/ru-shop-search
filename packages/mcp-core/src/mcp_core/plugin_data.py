"""Каталог данных плагина: стопы магазинов, профиль Chrome, токены и снимки цен."""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["plugin_data_dir"]

_PLUGIN = "ru-shop-search"


def plugin_data_dir() -> Path:
    """``${CLAUDE_PLUGIN_DATA}`` под плагином; в CLI и разработке — последний каталог установки.

    Каталог зависит от способа установки: ``-inline`` для --plugin-dir, иначе имя marketplace.
    """
    env = os.environ.get("CLAUDE_PLUGIN_DATA")
    if env:
        return Path(env)
    root = Path.home() / ".claude" / "plugins" / "data"
    installs = sorted(root.glob(f"{_PLUGIN}-*"), key=lambda p: p.stat().st_mtime)
    return installs[-1] if installs else root / f"{_PLUGIN}-inline"
