"""The real extractors run in jsdom against pages captured live (see fixtures/*.provenance.json)."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp_core.domtest import JsdomUnavailable, run_extractor
from pepper_connector import server

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_URL = "https://www.pepper.ru/selective_search?keyword=rtx+5070&search_list=deals"


def _run(js_source: str, name: str, page_url: str = SEARCH_URL) -> dict:
    try:
        payload = run_extractor(js_source, (FIXTURES / name).resolve(), page_url=page_url)
    except JsdomUnavailable as exc:
        pytest.skip(str(exc))
    assert isinstance(payload, dict)
    return payload


def test_search_extractor_reads_deals_with_price_store_and_age():
    payload = _run(server._SEARCH_EXTRACT_JS, "search_rtx5070_live.html")

    assert (payload["captcha"], payload["nothing"], payload["total"]) == (False, False, "Показать все (4)")
    rows = {row["deal_id"]: row for row in payload["deals"]}
    assert list(rows) == ["694666", "694148", "692502", "689146"]
    assert rows["694148"] == {
        "deal_id": "694148",
        "store": "Wildberries",
        "href": "/deals/5070-694148",
        "title": "Видеокарта MSI RTX 5070 SHADOW 2X OC 12 ГБ (с ВБ кошельком)",
        "price": "75 000₽",
        "old": None,
        "stamp": "10 дней назад",
        "hot": "964°",
    }
    assert (rows["694666"]["price"], rows["694666"]["old"]) == ("142 883₽", "182 418₽")
    assert (rows["689146"]["stamp"], rows["689146"]["hot"]) == ("Истекло", "-64°")


def test_search_extractor_on_old_deals():
    payload = _run(server._SEARCH_EXTRACT_JS, "search_iphone15_live.html")

    assert len(payload["deals"]) == 5
    assert {row["stamp"] for row in payload["deals"]} == {"почти 2 года назад", "около 2 лет назад"}
    assert payload["deals"][0]["price"] == "59 892₽"


def test_search_extractor_sees_the_empty_state():
    payload = _run(server._SEARCH_EXTRACT_JS, "search_nothing_live.html")

    assert (payload["nothing"], payload["deals"], payload["captcha"]) == (True, [], False)


def test_card_extractor_reads_the_deal_json_ld():
    payload = _run(server._CARD_EXTRACT_JS, "card_active_live.html", "https://www.pepper.ru/deals/5070-694148")

    assert {k: payload[k] for k in ("products", "product_url", "merchant", "price", "currency")} == {
        "products": 1,
        "product_url": "https://www.pepper.ru/deals/5070-694148",
        "merchant": "Wildberries",
        "price": "75000.0",
        "currency": "RUB",
    }
    assert (payload["published"], payload["valid_until"]) == ("2026-09-17T14:43:13+03:00", "2026-09-30 00:59:00 +0300")
    assert (payload["availability"], payload["expired_notice"], payload["hot"]) == (
        "https://schema.org/InStock",
        False,
        "964°",
    )


def test_card_extractor_sees_an_expired_deal():
    url = "https://www.pepper.ru/deals/mechrevo-yaoshi16-utral-noutbuk-igrovoy-689146"
    payload = _run(server._CARD_EXTRACT_JS, "card_expired_live.html", url)

    assert (payload["availability"], payload["expired_notice"]) == ("https://schema.org/OutOfStock", True)
    assert (payload["price"], payload["merchant"], payload["hot"]) == ("219404.0", "OZON", "-64°")


@pytest.mark.parametrize("js", [server._SEARCH_EXTRACT_JS, server._CARD_EXTRACT_JS], ids=["search", "card"])
def test_the_js_check_is_not_a_captcha(js):
    payload = _run(js, "search_nothing_live.html", "https://www.pepper.ru/selective_search?keyword=rtx&_ycch=1")

    assert (payload["checking"], payload["captcha"]) == (True, False)


@pytest.mark.parametrize("js", [server._SEARCH_EXTRACT_JS, server._CARD_EXTRACT_JS], ids=["search", "card"])
def test_a_captcha_path_is_a_captcha(js):
    payload = _run(js, "search_nothing_live.html", "https://www.pepper.ru/showcaptcha?retpath=x")

    assert (payload["checking"], payload["captcha"]) == (False, True)


def test_a_query_about_captchas_is_not_a_captcha():
    url = "https://www.pepper.ru/selective_search?keyword=captcha&search_list=deals"
    payload = _run(server._SEARCH_EXTRACT_JS, "search_nothing_live.html", url)

    assert (payload["checking"], payload["captcha"]) == (False, False)
