"""Одна модель из разных магазинов: GTIN, бренд + MPN, код модели в названии, набор слов названия.

Названия взяты из живых фикстур DNS, Regard, Ситилинка и М.Видео (RTX 5070, сентябрь 2026).
"""

from __future__ import annotations

from compare_connector.identity import ProductIdentity
from compare_connector.matching import group_offers
from compare_connector.models_output import DealInfo, MarketOffer

DNS_PALIT = "Видеокарта Palit GeForce RTX 5070 Infinity 3 [NE75070019K9-GB2050S]"
REGARD_PALIT = "Видеокарта NVIDIA GeForce RTX 5070 Palit Infinity 3 12GB (NE75070019K9-GB2050S)"
CITILINK_PALIT = (
    "Видеокарта Palit NVIDIA  GeForce RTX 5070 PA-RTX5070 Infinity 3 12ГБ Infinity 3, GDDR7, Ret [ne75070019k9-gb2050s]"
)
MVIDEO_PALIT_OC = "Видеокарта Palit NVIDIA GeForce RTX 5070 INFINITY 3 OC 12GB [ne75070s19k9-gb2050s]"
EAN = "4006381333931"
OTHER_EAN = "4006381333948"


def offer(source: str, title: str, price: float | None, **extra) -> MarketOffer:
    return MarketOffer(source=source, product_id=f"{source}-{price}", title=title, price_rub=price, **extra)


def test_the_same_card_from_three_shops_is_one_likely_model_and_the_oc_version_is_not_in_it():
    offers = [
        offer("dns", DNS_PALIT, 62_999.0),
        offer("regard", REGARD_PALIT, 61_500.0),
        offer("citilink", CITILINK_PALIT, 63_990.0),
        offer("mvideo", MVIDEO_PALIT_OC, 64_999.0),
    ]

    [group] = group_offers(offers, "rtx 5070")

    assert (group.confidence, group.basis, group.key) == ("likely", "title_code", "ne75070019k9gb2050s")
    assert group.sources == ["citilink", "dns", "regard"]
    assert [o.price_rub for o in group.offers] == [61_500.0, 62_999.0, 63_990.0]
    assert group.cheapest.source == "regard"
    assert group.price_spread_rub == 2_490.0


def test_a_typed_part_number_matches_the_code_in_another_shops_title():
    typed = offer(
        "regard",
        "Видеокарта NVIDIA GeForce RTX 5070 Palit Infinity 3 12GB",
        61_500.0,
        identity=ProductIdentity(brand="Palit", mpn="NE75070019K9-GB2050S"),
    )

    [group] = group_offers([typed, offer("dns", DNS_PALIT, 62_999.0)], "rtx 5070")

    assert (group.confidence, group.basis) == ("likely", "title_code")


def test_the_same_gtin_is_an_exact_match():
    offers = [
        offer("detsky_mir", "Конструктор LEGO Technic", 9_000.0, identity=ProductIdentity(gtin=EAN)),
        offer("wildberries", "LEGO Техник набор", 8_500.0, identity=ProductIdentity(gtin=EAN)),
    ]

    [group] = group_offers(offers, "lego technic")

    assert (group.confidence, group.basis, group.key) == ("exact", "gtin", "0" + EAN)


def test_brand_and_part_number_are_an_exact_match():
    identity = ProductIdentity(brand="Palit", mpn="NE75070019K9GB2050S")
    offers = [
        offer("regard", "Видеокарта Palit", 61_500.0, identity=identity),
        offer("nix", "Видеокарта Palit RTX", 62_000.0, identity=identity),
    ]

    [group] = group_offers(offers, "rtx 5070")

    assert (group.confidence, group.basis) == ("exact", "brand_mpn")


def test_different_gtins_keep_a_shared_title_code_apart():
    offers = [
        offer("dns", DNS_PALIT, 62_999.0, identity=ProductIdentity(gtin=EAN)),
        offer("regard", REGARD_PALIT, 61_500.0, identity=ProductIdentity(gtin=OTHER_EAN)),
    ]

    assert group_offers(offers, "rtx 5070") == []


def test_different_brands_keep_a_shared_title_code_apart():
    offers = [
        offer("dns", "Видеокарта Palit [NE75070019K9-GB2050S]", 62_999.0, brand="Palit"),
        offer("regard", "Видеокарта Gainward [NE75070019K9-GB2050S]", 61_500.0, brand="Gainward"),
    ]

    assert group_offers(offers, "rtx 5070") == []


def test_different_storage_keeps_a_shared_model_code_apart():
    offers = [
        offer("dns", "Смартфон Samsung Galaxy S24 SM-S921B 128GB", 60_000.0),
        offer("citilink", "Смартфон Samsung Galaxy S24 SM-S921B 256 ГБ", 70_000.0),
    ]

    assert group_offers(offers, "samsung s24") == []


def test_the_query_written_as_one_word_is_not_a_model_code():
    offers = [
        offer("wildberries", "Видеокарта KFA2 RTX5070 EX Gamer", 58_000.0),
        offer("ozon", "Видеокарта Inno3D RTX5070 Twin X2", 59_000.0),
    ]

    assert group_offers(offers, "rtx 5070") == []


def test_units_and_specs_are_not_model_codes():
    offers = [
        offer("dns", "Видеокарта Palit RTX 5070 12GB GDDR7 PCIe5.0 2560MHz", 62_000.0),
        offer("citilink", "Видеокарта MSI RTX 5070 12GB GDDR7 PCIe5.0 2560MHz", 63_000.0),
    ]

    assert group_offers(offers, "rtx 5070") == []


def test_the_same_latin_words_are_a_possible_match():
    offers = [
        offer("wildberries", "Видеокарта MSI GeForce RTX 5070 12G VENTUS 3X OC", 60_000.0),
        offer("dns", "Видеокарта MSI GeForce RTX 5070 VENTUS 3X OC [RTX 5070 12G VENTUS 3X OC]", 61_000.0),
        offer("citilink", "Видеокарта MSI GeForce RTX 5070 12G VENTUS 2X OC", 57_000.0),
    ]

    [group] = group_offers(offers, "rtx 5070")

    assert (group.confidence, group.basis) == ("possible", "title_words")
    assert group.sources == ["dns", "wildberries"]


def test_too_few_words_are_not_a_signature():
    offers = [offer("dns", "Смартфон iPhone", 50_000.0), offer("citilink", "iPhone смартфон", 51_000.0)]

    assert group_offers(offers, "iphone") == []


def test_one_shop_alone_is_not_a_cross_shop_model():
    offers = [
        offer("wildberries", DNS_PALIT, 62_000.0, seller="A"),
        MarketOffer(source="wildberries", product_id="2", title=DNS_PALIT, price_rub=63_000.0, seller="B"),
    ]

    assert group_offers(offers, "rtx 5070") == []


def test_used_accessories_deals_and_unpriced_rows_stay_out_of_models():
    offers = [
        offer("dns", DNS_PALIT, 62_999.0),
        offer("avito", DNS_PALIT, 40_000.0),
        offer("wildberries", f"Витринный образец {DNS_PALIT}", 50_000.0),
        offer("ozon", f"Кабель питания для {DNS_PALIT}", 900.0),
        offer("yandex_market", DNS_PALIT, None),
        MarketOffer(source="pepper", title=DNS_PALIT, deal=DealInfo(price_rub=55_000.0)),
        offer("aliexpress", DNS_PALIT, None, currency="cny", price_native=3_000.0),
    ]

    assert group_offers(offers, "rtx 5070") == []


def test_a_row_joins_at_most_one_model_and_stronger_evidence_wins():
    offers = [
        offer("dns", DNS_PALIT, 62_999.0, identity=ProductIdentity(gtin=EAN)),
        offer("regard", REGARD_PALIT, 61_500.0, identity=ProductIdentity(gtin=EAN)),
        offer("citilink", CITILINK_PALIT, 63_990.0),
    ]

    groups = group_offers(offers, "rtx 5070")

    assert [(g.basis, g.sources) for g in groups] == [("gtin", ["dns", "regard"])]


def test_a_series_prefix_around_the_query_is_not_a_model_code():
    offers = [
        offer("yandex_market", "Видеокарта игровая Palit NVIDIA GeForce RTX 5070 PA-RTX5070 Infinity 3", 101_182.0),
        offer("wildberries", "Видеокарта PCI-E 5.0 PA-RTX5070 GamingPro 12G", 99_000.0),
    ]

    assert group_offers(offers, "rtx 5070 palit infinity 3") == []


def test_an_oc_title_does_not_join_a_plain_one_on_a_shared_code():
    offers = [
        offer("dns", "Видеокарта Palit GeForce RTX 5070 Infinity 3 [NE75070019K9-GB2050S]", 62_999.0),
        offer("wildberries", "Видеокарта Palit RTX 5070 Infinity 3 OC NE75070019K9-GB2050S", 60_000.0),
        offer("nix", "Видеокарта Palit RTX5070Ti Infinity 3 NE75070019K9-GB2050S", 61_000.0),
    ]

    assert group_offers(offers, "rtx 5070") == []


def test_the_same_gtin_joins_even_when_one_title_says_oc():
    offers = [
        offer("dns", "Palit RTX 5070 Infinity 3", 62_999.0, identity=ProductIdentity(gtin=EAN)),
        offer("wildberries", "Palit RTX 5070 Infinity 3 OC", 60_000.0, identity=ProductIdentity(gtin=EAN)),
    ]

    [group] = group_offers(offers, "rtx 5070")

    assert group.basis == "gtin"


def test_different_colours_named_in_titles_keep_rows_apart():
    offers = [
        offer("wildberries", "Смартфон Apple iPhone 15 128GB черный", 69_990.0),
        offer("mvideo", "Смартфон Apple iPhone 15 128GB White", 70_990.0),
    ]

    assert group_offers(offers, "iphone 15 128") == []


def test_a_title_with_a_shared_colour_still_joins():
    offers = [
        offer("wildberries", "Смартфон Apple iPhone 15 128GB черный", 69_990.0),
        offer("mvideo", "Смартфон Apple iPhone 15 128GB Black", 70_990.0),
    ]

    [group] = group_offers(offers, "iphone 15 128")

    assert group.sources == ["mvideo", "wildberries"]


def test_a_cheaper_odd_row_does_not_keep_the_matching_rows_apart():
    offers = [
        offer("wildberries", "Видеокарта Palit RTX 5070 Infinity 3 OC NE75070019K9-GB2050S", 60_000.0),
        offer("dns", DNS_PALIT, 62_999.0),
        offer("citilink", CITILINK_PALIT, 63_990.0),
    ]

    [group] = group_offers(offers, "rtx 5070")

    assert group.sources == ["citilink", "dns"]
