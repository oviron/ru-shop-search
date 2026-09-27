"""The Ozon card parse path against a live composer payload of 2026-09-27 (provenance beside it)."""

from __future__ import annotations

import json
from pathlib import Path

from ozon_connector import server

FIXTURE = Path(__file__).parent / "fixtures" / "card_composer_live.json"


def _card() -> dict:
    return server._parse_widgets(json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_live_card_keeps_the_everyday_and_the_ozon_card_price_apart() -> None:
    card = _card()

    assert (card["price"], card["card_price"], card["price_original"]) == (112777.0, 106011.0, None)
    assert card["is_available"] is True


def test_live_card_names_the_product_and_its_seller() -> None:
    card = _card()

    assert card["title"] == "Inno3D Видеокарта 12 ГБ (N50702-12D7-195064N)"
    assert card["seller"] == {"name": "М.Видео", "link": "https://www.ozon.ru/seller/mvideo-1865500/"}


def test_live_card_without_reviews_reports_no_rating() -> None:
    card = _card()

    assert (card["rating_score"], card["rating_count"]) == (None, None)


def test_live_card_keeps_the_short_characteristics() -> None:
    titles = [c["title"]["textRs"][0]["content"] for c in _card()["characteristics"]]

    assert titles == ["Тип", "Объем памяти", "Тип памяти", "Частота шины памяти, МГц", "Бренд графического процессора"]
