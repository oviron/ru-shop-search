"""Регион WB: город из снимка krein15 или dest как есть."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from wb_connector.settings import WBSettings


def test_without_a_city_the_site_default_dest_is_used(monkeypatch):
    monkeypatch.delenv("WB_REGION", raising=False)
    monkeypatch.delenv("WB_DEFAULT_DEST", raising=False)
    assert WBSettings(_env_file=None).dest() == "-1257786"


def test_a_city_selects_its_dest(monkeypatch):
    monkeypatch.setenv("WB_REGION", "Казань")
    assert WBSettings(_env_file=None).dest() == "-2133467"


def test_an_unknown_city_is_refused_with_the_known_ones(monkeypatch):
    monkeypatch.setenv("WB_REGION", "Атлантида")
    with pytest.raises(ValidationError, match="Санкт-Петербург"):
        WBSettings(_env_file=None)
