"""Одна модель из разных магазинов и признаки строки, которая не сравнима с новым товаром."""

from __future__ import annotations

import re
from collections import defaultdict

from compare_connector.identity import match_product_identity, normalize_gtin, normalize_model, normalize_mpn
from compare_connector.models_output import MarketOffer, ModelGroup

CLASSIFIEDS = frozenset({"avito"})

# Живой случай июля 2026: по «iphone 15» чехол за 990 ₽ стал самым дешёвым предложением.
_ACCESSORY_MARKERS = (
    "чехол",
    "чехол-книжка",
    "бампер",
    "накладка",
    "защитное стекло",
    "стекло",
    "плёнка",
    "пленка",
    "кабель",
    "адаптер",
    "зарядка",
    "зарядное",
    "держатель",
    "ремешок",
    "подставка",
    "наклейка",
    "case",
    "cover",
    "screen protector",
    "cable",
    "charger",
    "strap",
)

# Там же дешёвые iPhone 15 на WB оказались «Восстановленный» и «Витринный образец».
_CONDITION_MARKERS = (
    "восстановленный",
    "восстановленная",
    "витринный образец",
    "витринный",
    "уценка",
    "уценённый",
    "уцененный",
    "б/у",
    "бывший в употреблении",
    "как новый",
    "refurbished",
    "renewed",
    "pre-owned",
    "used",
    "open box",
)

_BASES = (("gtin", "exact"), ("brand_mpn", "exact"), ("title_code", "likely"), ("title_words", "possible"))
_RANK = {"exact": 0, "likely": 1, "possible": 2}
_CAPACITY_RE = re.compile(r"(\d+)\s*(gb|гб|tb|тб)(?![a-zа-яё])")
_CODE_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_WORD_RE = re.compile(r"[a-z0-9]+")
_UNIT_RE = re.compile(r"\d+(?:gb|tb|mb|ghz|mhz|hz|mah|wh|w|mm|cm|bit|gbps|mbps|fps|k|p)")
_SPEC_RE = re.compile(r"(?:l?p?g?ddr|pcie|usb|wifi|hdmi|dp|bt|ipx?|type)\d")
_COLOURS = {
    **{w: w for w in ("black", "white", "blue", "red", "green", "yellow", "pink", "purple", "gray", "silver", "gold")},
    "grey": "gray",
    **{"черн": "black", "бел": "white", "син": "blue", "красн": "red", "зелен": "green", "желт": "yellow"},
    **{"розов": "pink", "фиолетов": "purple", "сер": "gray", "серебрист": "silver", "золот": "gold"},
}
_RU_COLOUR_RE = re.compile(
    r"(?<![а-я])(черн|бел|син|красн|зелен|желт|розов|фиолетов|сер|серебрист|золот)(?:ый|ий|ой|ая|яя|ое|ее|ые|ие)(?![а-я])"
)
_VARIANT_RE = re.compile(r"(?<![a-z])(oc|ti|super|xtx|xt|pro|max|plus|mini|ultra|lite)(?![a-z])")


def looks_like_another_condition(title: str, query: str) -> bool:
    """Б/у, восстановленный или витринный товар; маркер из самого запроса не считается."""
    low_title, low_query = title.lower(), query.lower()
    return any(m in low_title and m not in low_query for m in _CONDITION_MARKERS)


def looks_like_an_accessory(title: str, query: str) -> bool:
    """Аксессуар, о котором запрос не спрашивал; маркер из самого запроса не считается."""
    low_title, low_query = title.lower(), query.lower()
    return any(m in low_title and m not in low_query for m in _ACCESSORY_MARKERS)


def comparable(offer: MarketOffer, query: str) -> bool:
    """Новый товар с ценой в рублях: не аксессуар, не б/у и не частное объявление."""
    return (
        offer.price_rub is not None
        and offer.currency == "rub"
        and offer.source not in CLASSIFIEDS
        and not looks_like_an_accessory(offer.title, query)
        and not looks_like_another_condition(offer.title, query)
    )


def _capacity(match: re.Match[str]) -> str:
    return match.group(1) + ("tb" if match.group(2) in ("tb", "тб") else "gb")


def _normalized(title: str) -> str:
    return _CAPACITY_RE.sub(_capacity, title.casefold().replace("ё", "е"))


def _capacities(title: str) -> set[str]:
    return {_capacity(m) for m in _CAPACITY_RE.finditer(title.casefold())}


def _query_joins(query: str) -> set[str]:
    words = _WORD_RE.findall(query.casefold())
    written = {code.replace("-", "") for code in _CODE_RE.findall(query.casefold())}
    runs = {"".join(words[i:j]) for i in range(len(words)) for j in range(i + 2, len(words) + 1)}
    return runs - written


def _series_prefix(code: str, excluded: set[str]) -> bool:
    """PA-RTX5070 вокруг запроса «rtx 5070» — серия производителя, а не модель."""
    return any(join in code and len(code) - len(join) <= 3 for join in excluded)


def _codes(title: str, excluded: set[str]) -> set[str]:
    out = set()
    for raw in _CODE_RE.findall(_normalized(title)):
        code = raw.replace("-", "")
        letters, digits = sum(c.isalpha() for c in code), sum(c.isdigit() for c in code)
        if len(code) < 6 or letters < 2 or digits < 2 or code in excluded or _series_prefix(code, excluded):
            continue
        if _UNIT_RE.fullmatch(code) or _SPEC_RE.match(code):
            continue
        out.add(code)
    return out


def _colours(title: str) -> set[str]:
    text = _normalized(title)
    return {_COLOURS[w] for w in _WORD_RE.findall(text) if w in _COLOURS} | {
        _COLOURS[stem] for stem in _RU_COLOUR_RE.findall(text)
    }


def _signature(title: str) -> str:
    words = set(_WORD_RE.findall(_normalized(title))) - _COLOURS.keys()
    if len(words) < 3 or not any(any(c.isdigit() for c in w) for w in words):
        return ""
    return " ".join(sorted(words))


def _keys(offer: MarketOffer, excluded: set[str]) -> dict[str, set[str]]:
    identity = offer.identity
    brand = normalize_model(identity.brand or offer.brand)
    gtin = normalize_gtin(identity.gtin)
    mpn = normalize_mpn(identity.mpn).lower()
    codes = _codes(offer.title, excluded) | ({mpn} if len(mpn) >= 4 else set())
    signature = _signature(offer.title)
    return {
        "gtin": {gtin.zfill(14)} if gtin else set(),
        "brand_mpn": {f"{brand}:{mpn}"} if brand and mpn else set(),
        "title_code": codes,
        "title_words": {signature} if signature else set(),
    }


def _variants(title: str) -> set[str]:
    return set(_VARIANT_RE.findall(_normalized(title)))


def _conflict(a: MarketOffer, b: MarketOffer, basis: str) -> bool:
    if basis in ("title_code", "title_words"):
        if _variants(a.title) != _variants(b.title):
            return True
        colours_a, colours_b = _colours(a.title), _colours(b.title)
        if colours_a and colours_b and not colours_a & colours_b:
            return True
    left = a.identity.model_copy(update={"brand": a.identity.brand or a.brand})
    right = b.identity.model_copy(update={"brand": b.identity.brand or b.brand})
    if match_product_identity(left, right).status == "mismatch":
        return True
    ca, cb = _capacities(a.title), _capacities(b.title)
    return bool(ca and cb and not ca & cb)


def _price(offer: MarketOffer) -> float:
    return offer.price_rub or 0.0


def _largest_compatible(members: list[int], offers: list[MarketOffer], basis: str) -> list[int]:
    """Каждая строка по очереди — основа; побеждает набор с большим числом магазинов, при равенстве — дешевле."""
    ordered = sorted(members, key=lambda m: _price(offers[m]))
    best: list[int] = []
    for anchor in ordered:
        chosen = [anchor]
        for i in ordered:
            if i != anchor and not any(_conflict(offers[i], offers[j], basis) for j in chosen):
                chosen.append(i)
        chosen.sort(key=lambda m: _price(offers[m]))
        if (len({offers[i].source for i in chosen}), len(chosen)) > (len({offers[i].source for i in best}), len(best)):
            best = chosen
    return best


def group_offers(offers: list[MarketOffer], query: str) -> list[ModelGroup]:
    """Сводит строки разных магазинов в модели; строка входит не больше чем в одну модель.

    Сначала более сильное основание: GTIN, бренд + MPN, код модели в названии, набор слов.
    """
    eligible = [offer for offer in offers if comparable(offer, query)]
    excluded = _query_joins(query)
    keys = [_keys(offer, excluded) for offer in eligible]
    taken: set[int] = set()
    groups: list[ModelGroup] = []
    for basis, confidence in _BASES:
        buckets: dict[str, list[int]] = defaultdict(list)
        for i, offer_keys in enumerate(keys):
            if i not in taken:
                for key in offer_keys[basis]:
                    buckets[key].append(i)
        for key, members in sorted(buckets.items(), key=lambda kv: (len(kv[1]), kv[0])):
            chosen = _largest_compatible([m for m in members if m not in taken], eligible, basis)
            if len({eligible[i].source for i in chosen}) < 2:
                continue
            taken.update(chosen)
            rows = [eligible[i] for i in chosen]
            groups.append(
                ModelGroup(
                    confidence=confidence,
                    basis=basis,
                    key=key,
                    sources=sorted({row.source for row in rows}),
                    cheapest=rows[0],
                    price_spread_rub=round(_price(rows[-1]) - _price(rows[0]), 2),
                    offers=rows,
                )
            )
    return sorted(groups, key=lambda g: (_RANK[g.confidence], -len(g.sources), _price(g.cheapest)))
