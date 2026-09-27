"""Оценка лота Авито: свои веса, независимая медиана, минимальная выборка, дефекты с отрицаниями."""

from __future__ import annotations

import pytest
from avito_connector.scoring import Lot, Peer, evaluate, find_defects, independent_peers, median

YEAR = 2026
FIVE_AT_100 = [100.0] * 5


def labels(text: str) -> set[tuple[str, str]]:
    return {(d.severity, d.label) for d in find_defects(text)}


def full_lot(**extra) -> Lot:
    fields = {
        "title": "iPhone 13 128 ГБ",
        "price_rub": 80.0,
        "description": "Телефон в отличном состоянии, всё работает. " * 6,
        "params": ("Память: 128 ГБ", "Цвет: синий", "Состояние: б/у"),
        "photos": 5,
        "seller_rating": 5.0,
        "seller_reviews": 100,
        "seller_since_year": 2020,
        "delivery": True,
    }
    return Lot(**{**fields, **extra})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("iPhone 13 на запчасти, не включается", {("critical", "parts"), ("critical", "dead")}),
        ("Продаю нерабочий ноутбук", {("critical", "dead")}),
        ("Заблокирован iCloud", {("critical", "locked")}),
        ("iCloud заблокирован, пароля нет", {("critical", "locked")}),
        ("Люкс копия, не отличить", {("critical", "fake")}),
        ("После воды, сушили", {("critical", "water")}),
        ("Трещина на экране, Face ID не работает", {("serious", "broken_screen"), ("serious", "not_working")}),
        ("Был в ремонте, меняли экран", {("serious", "repaired")}),
        ("Без проверки, без возврата", {("serious", "no_check")}),
        ("Царапины на корпусе, аккумулятор менялся", {("minor", "wear"), ("minor", "battery")}),
        ("Без коробки", {("minor", "incomplete")}),
    ],
)
def test_defects_are_found_by_severity(text, expected):
    assert labels(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Состояние отличное, без царапин и сколов, не битый, не был в ремонте",
        "Не привязан к iCloud, iCloud не заблокирован",
        "Копия чека есть, гарантия",
        "Всё работает, нет ни царапин ни сколов",
        "Не утоплен, не вскрывался",
    ],
)
def test_negated_defects_are_not_defects(text):
    assert labels(text) == set()


def test_a_negation_does_not_leak_across_a_sentence():
    assert labels("Без проблем. Царапины есть") == {("minor", "wear")}


def test_the_same_defect_counts_once():
    assert [d.label for d in find_defects("Скол на крышке, ещё скол на рамке, царапины")] == ["wear"]


def test_median_of_an_even_sample_is_the_mean_of_the_middle_pair():
    assert median([150.0, 100.0, 140.0, 110.0, 130.0, 120.0]) == 125.0


def test_peers_exclude_the_lot_itself_priceless_defective_and_unrelated_listings():
    peers = [
        Peer("1", "iPhone 13 128 ГБ", 50_000.0),
        Peer("2", "iPhone 13 128GB синий", 52_000.0),
        Peer("3", "iPhone 13 на запчасти", 9_000.0),
        Peer("4", "iPhone 13 трещина экрана", 30_000.0),
        Peer("5", "iPhone 13, цена договорная", None),
        Peer("6", "Чехол для Samsung", 500.0),
        Peer("7", "iPhone 13 128 царапины на корпусе", 48_000.0),
    ]

    assert independent_peers(peers, query="iphone 13", lot_id="1") == [52_000.0, 48_000.0]


def test_accessories_are_not_peers():
    peers = [
        Peer("1", "Чехол для iPhone 13", 700.0),
        Peer("2", "Защитное стекло iPhone 13", 300.0),
        Peer("3", "Коробка от iPhone 13", 500.0),
        Peer("4", "Apple iPhone 13", 50_000.0),
        Peer("5", "Оригинальный чехол iPhone 13", 900.0),
        Peer("6", "iPhone 13, чехол и стекло в подарок", 51_000.0),
    ]

    assert independent_peers(peers, query="iphone 13", lot_id=None) == [50_000.0, 51_000.0]


def test_too_few_peers_gives_no_verdict_and_says_how_many_were_found():
    result = evaluate(full_lot(), peer_prices=[100.0] * 4, this_year=YEAR)

    assert (result.verdict, result.score, result.avito_sample, result.min_avito_sample) == (
        "insufficient_sample",
        None,
        4,
        5,
    )
    assert result.avito_median_rub is None
    assert any("4 of 5" in note for note in result.notes)


def test_a_lot_without_a_price_gets_no_verdict():
    result = evaluate(full_lot(price_rub=None), peer_prices=FIVE_AT_100, this_year=YEAR)

    assert (result.verdict, result.score) == ("insufficient_sample", None)
    assert "no_price" in result.flags


def test_a_well_documented_lot_at_80_percent_of_the_median_scores_full_marks():
    result = evaluate(full_lot(), peer_prices=FIVE_AT_100, this_year=YEAR)

    points = {part.factor: part.points for part in result.breakdown}
    assert points == {"price": 45, "seller": 25, "transparency": 20, "delivery": 10}
    assert (result.score, result.verdict, result.avito_median_rub, result.price_to_avito_median) == (
        100,
        "good_deal",
        100.0,
        0.8,
    )


def test_price_points_fall_linearly_to_zero_at_125_percent():
    at_110 = evaluate(full_lot(price_rub=110.0), peer_prices=FIVE_AT_100, this_year=YEAR)
    at_130 = evaluate(full_lot(price_rub=130.0), peer_prices=FIVE_AT_100, this_year=YEAR)

    assert {p.factor: p.points for p in at_110.breakdown}["price"] == 15
    assert {p.factor: p.points for p in at_130.breakdown}["price"] == 0


def test_half_the_median_is_a_risk_flag_not_a_bonus():
    result = evaluate(full_lot(price_rub=40.0), peer_prices=FIVE_AT_100, this_year=YEAR)

    assert "suspiciously_cheap" in result.flags
    assert {p.factor: p.points for p in result.breakdown}["price"] == 0
    assert result.verdict == "caution"


def test_a_used_lot_near_the_new_retail_price_earns_no_price_points():
    result = evaluate(
        full_lot(price_rub=110.0), peer_prices=[120.0] * 5, retail_prices=[110.0, 120.0, 130.0], this_year=YEAR
    )

    assert "near_new_price" in result.flags
    assert {p.factor: p.points for p in result.breakdown}["price"] == 0
    assert (result.retail_median_rub, result.retail_sample) == (120.0, 3)


def test_two_retail_prices_are_not_a_reference():
    result = evaluate(full_lot(price_rub=110.0), peer_prices=[120.0] * 5, retail_prices=[110.0, 120.0], this_year=YEAR)

    assert "near_new_price" not in result.flags
    assert (result.retail_median_rub, result.retail_sample) == (None, 2)


def test_a_critical_defect_means_avoid_whatever_the_score():
    result = evaluate(
        full_lot(description="Не включается, продаю как есть. " * 8), peer_prices=FIVE_AT_100, this_year=YEAR
    )

    assert result.verdict == "avoid"
    assert result.defect_penalty == 40
    assert result.score == 60


def test_a_critical_defect_means_avoid_even_without_a_sample():
    result = evaluate(full_lot(title="iPhone 13 на запчасти"), peer_prices=[], this_year=YEAR)

    assert (result.verdict, result.score) == ("avoid", None)


def test_defect_penalties_stop_at_the_cap():
    text = "На запчасти, не включается, заблокирован iCloud, после воды, трещина экрана"
    result = evaluate(full_lot(description=text), peer_prices=FIVE_AT_100, this_year=YEAR)

    assert result.defect_penalty == 80
    assert result.score == 15


@pytest.mark.parametrize(
    ("rating", "reviews", "since", "points"),
    [
        (5.0, 100, 2020, 25),
        (5.0, 10, None, 10),
        (4.25, 50, None, 10),
        (5.0, 3, 2025, 7),
    ],
)
def test_seller_points_weigh_rating_by_how_many_reviews_back_it(rating, reviews, since, points):
    result = evaluate(
        full_lot(seller_rating=rating, seller_reviews=reviews, seller_since_year=since),
        peer_prices=FIVE_AT_100,
        this_year=YEAR,
    )

    assert {p.factor: p.points for p in result.breakdown}["seller"] == points


def test_a_seller_without_reviews_is_flagged_new():
    result = evaluate(full_lot(seller_rating=None, seller_reviews=0), peer_prices=FIVE_AT_100, this_year=YEAR)

    assert "new_seller" in result.flags
    assert {p.factor: p.points for p in result.breakdown}["seller"] == 5


def test_a_low_rating_with_enough_reviews_is_flagged():
    result = evaluate(full_lot(seller_rating=3.9, seller_reviews=12), peer_prices=FIVE_AT_100, this_year=YEAR)

    assert "low_seller_rating" in result.flags


def test_a_bare_listing_gets_no_transparency_or_delivery_points():
    result = evaluate(Lot(title="iPhone 13", price_rub=80.0), peer_prices=FIVE_AT_100, this_year=YEAR)

    points = {p.factor: p.points for p in result.breakdown}
    assert (points["transparency"], points["delivery"], points["seller"]) == (0, 0, 0)
    assert "seller_unknown" in result.flags
    assert (result.score, result.verdict) == (45, "caution")
