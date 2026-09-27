"""Оценка лота Авито: цена против независимой медианы, продавец, открытость объявления, дефекты.

Медиана считается по другим объявлениям без самого лота и без объявлений с серьёзными
дефектами; меньше MIN_AVITO_SAMPLE цен — вердикта по цене нет. Веса и словарь дефектов свои.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from avito_connector.models_output import AvitoEvaluation, DefectOut, EvaluationPart

MIN_AVITO_SAMPLE = 5
MIN_RETAIL_SAMPLE = 3
PENALTY = {"critical": 40, "serious": 20, "minor": 5}
PENALTY_CAP = 80
SUSPICIOUS_RATIO = 0.5
NEAR_NEW_RATIO = 0.9

# One of these before the model name means the ad sells something for the device.
_ACCESSORY_HEADS = frozenset(
    {
        "чехол",
        "стекло",
        "защитное",
        "пленка",
        "бампер",
        "накладка",
        "кабель",
        "зарядка",
        "зарядное",
        "коробка",
        "корпус",
        "дисплей",
        "экран",
        "аккумулятор",
        "шлейф",
        "держатель",
        "ремешок",
        "подставка",
    }
)
_NEGATORS = frozenset({"не", "без", "нет", "ни", "нету"})
_NEGATION_WINDOW = 3
_CLAUSE_RE = re.compile(r"[.!?;,\n]+")
_WORD_RE = re.compile(r"\w+")

_DEFECTS: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (severity, label, re.compile(pattern))
    for severity, label, pattern in (
        ("critical", "parts", r"на\s+(?:запчаст|детал|разбор)\w*|\bдонор\w*"),
        ("critical", "dead", r"нерабоч\w*|не\s+рабоч\w*|не\s+(?:включа|загружа|запуска)\w*|\bкирпич\w*"),
        (
            "critical",
            "locked",
            r"(?:заблокирован|залочен|привязан)\w*\s+(?:к\s+)?(?:\w+\s+)?(?:icloud|айклауд|аккаунт\w*|учетн\w*)"
            r"|(?:icloud|айклауд)\s+(?:(?!не\b)\w+\s+)?(?:заблок|залоч|привяз)\w*"
            r"|\bmdm\b|bypass|обход\w*\s+активац\w*",
        ),
        (
            "critical",
            "fake",
            r"\bреплик\w*|\bподделк\w*|(?:люкс|точная|качественн\w*)\s+копи\w*"
            r"|\bкопия\b(?!\s+(?:чек|документ|гарант|накладн|паспорт))",
        ),
        (
            "critical",
            "water",
            r"утоплен\w*|утопил\w*|после\s+(?:воды|влаги|залити\w*)|\bзалит\w*|попадани\w*\s+влаг\w*",
        ),
        (
            "serious",
            "broken_screen",
            r"разбит\w*|трещин\w*|\bбит(?:ый|ая|ое|ые)\b|бит\w*\s+пиксел\w*|полос\w*\s+на\s+экран\w*",
        ),
        ("serious", "not_working", r"не\s+работа\w*"),
        (
            "serious",
            "repaired",
            r"(?:был\w*|побывал\w*)\s+в\s+ремонт\w*|после\s+ремонт\w*"
            r"|(?:менял\w*|замен\w*)\s+(?:экран|дисплей|плат|модул)\w*|\bпаян\w*|перепа[яи]\w*",
        ),
        ("serious", "no_check", r"без\s+(?:проверк|возврат|претензи)\w*"),
        (
            "minor",
            "battery",
            r"(?:аккумулятор|батаре|акб)\w*\s+(?:\w+\s+)?(?:менял|замен)\w*|(?:менял|замен)\w*\s+(?:аккумулятор|батаре|акб)\w*"
            r"|(?:аккумулятор|батаре|акб|емкост)\w*\D{0,15}(?<!\d)[1-7]\d\s*%",
        ),
        ("minor", "wear", r"царап\w*|потерт\w*|\bскол(?:ы|ов|ом|ами|ах|а)?\b|вмятин\w*"),
        ("minor", "incomplete", r"без\s+(?:коробк|комплект|зарядк|документ)\w*|нет\s+(?:коробк|зарядк|документ)\w*"),
    )
)


@dataclass(frozen=True)
class Lot:
    title: str
    price_rub: float | None
    description: str = ""
    params: tuple[str, ...] = ()
    photos: int | None = None
    seller_rating: float | None = None
    seller_reviews: int | None = None
    seller_since_year: int | None = None
    delivery: bool | None = None
    url: str | None = None


@dataclass(frozen=True)
class Peer:
    item_id: str | None
    title: str
    price_rub: float | None


def _fold(text: str) -> str:
    return text.lower().replace("ё", "е")


def _negated(clause: str, start: int) -> bool:
    return any(word in _NEGATORS for word in _WORD_RE.findall(clause[:start])[-_NEGATION_WINDOW:])


def find_defects(text: str) -> list[DefectOut]:
    """Defects named in the text, one per family; a negator up to three words back in the clause cancels a match."""
    found: dict[str, DefectOut] = {}
    for clause in _CLAUSE_RE.split(_fold(text)):
        for severity, label, pattern in _DEFECTS:
            if label in found:
                continue
            for match in pattern.finditer(clause):
                if not _negated(clause, match.start()):
                    found[label] = DefectOut(
                        severity=severity, label=label, phrase=match.group(0), penalty=PENALTY[severity]
                    )
                    break
    return sorted(found.values(), key=lambda d: [label for _, label, _ in _DEFECTS].index(d.label))


def median(prices: Iterable[float]) -> float:
    return float(statistics.median(prices))


def _names_every_word(query: str, title: str) -> bool:
    folded = _fold(title)
    for word in _WORD_RE.findall(_fold(query)):
        if any(ch.isdigit() for ch in word):
            pattern = r"(?<!\d)" + re.escape(word) + r"(?!\d)"
        else:
            pattern = r"(?<![^\W\d_])" + re.escape(word[:5])
        if not re.search(pattern, folded):
            return False
    return True


def _is_accessory(query: str, title: str) -> bool:
    """An accessory noun before the first query word: «Оригинальный чехол iPhone 13», not «iPhone 13, чехол в подарок»."""
    words = _WORD_RE.findall(_fold(title))
    first = _WORD_RE.findall(_fold(query))[:1]
    stop = next((i for i, w in enumerate(words) if first and w.startswith(first[0][:5])), len(words))
    return any(w in _ACCESSORY_HEADS for w in words[:stop])


def independent_peers(peers: Iterable[Peer], *, query: str, lot_id: str | None) -> list[float]:
    """Prices of other listings of the same thing: not the lot, priced, not an accessory, every query word named, no serious defect."""
    prices: list[float] = []
    for peer in peers:
        if peer.price_rub is None or peer.price_rub <= 0 or (lot_id and peer.item_id == lot_id):
            continue
        if _is_accessory(query, peer.title) or not _names_every_word(query, peer.title):
            continue
        if any(d.severity != "minor" for d in find_defects(peer.title)):
            continue
        prices.append(peer.price_rub)
    return prices


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _seller(lot: Lot, this_year: int, flags: list[str]) -> EvaluationPart:
    reviews = lot.seller_reviews
    if reviews is None and lot.seller_rating is None:
        flags.append("seller_unknown")
    if reviews == 0:
        flags.append("new_seller")
    confidence = 0.0
    if reviews:
        confidence = 1.0 if reviews >= 20 else 0.5 if reviews >= 5 else 0.25
    rating_points = 20 * _clamp(((lot.seller_rating or 0.0) - 3.5) / 1.5) * confidence
    if lot.seller_rating is not None and lot.seller_rating < 4.0 and (reviews or 0) >= 5:
        flags.append("low_seller_rating")
    years = this_year - lot.seller_since_year if lot.seller_since_year else None
    since_points = 5 if years is not None and years >= 3 else 2 if years is not None and years >= 1 else 0
    return EvaluationPart(
        factor="seller",
        points=round(rating_points) + since_points,
        max_points=25,
        note=f"rating {lot.seller_rating}, {reviews} reviews, on Avito since {lot.seller_since_year}",
    )


def _transparency(lot: Lot) -> EvaluationPart:
    length = len(lot.description.strip())
    points = (10 if length >= 200 else 5 if length >= 50 else 0) + (5 if len(lot.params) >= 3 else 0)
    points += 5 if (lot.photos or 0) >= 3 else 0
    return EvaluationPart(
        factor="transparency",
        points=points,
        max_points=20,
        note=f"description {length} chars, {len(lot.params)} params, {lot.photos} photos",
    )


def _price(ratio: float, near_new: bool, flags: list[str]) -> EvaluationPart:
    if ratio < SUSPICIOUS_RATIO:
        flags.append("suspiciously_cheap")
        return EvaluationPart(factor="price", points=0, max_points=45, note="under half the median: a scam risk")
    if near_new:
        return EvaluationPart(factor="price", points=0, max_points=45, note="used at 90%+ of the new retail price")
    points = round(45 * _clamp((1.25 - ratio) / 0.45))
    return EvaluationPart(factor="price", points=points, max_points=45, note=f"{ratio:.2f} of the Avito median")


def _verdict(score: int, flags: list[str]) -> str:
    verdict = "good_deal" if score >= 75 else "fair" if score >= 55 else "caution" if score >= 35 else "avoid"
    order = ["good_deal", "fair", "caution", "avoid"]
    if "suspiciously_cheap" in flags:
        verdict = order[max(order.index(verdict), order.index("caution"))]
    if "near_new_price" in flags:
        verdict = order[max(order.index(verdict), order.index("fair"))]
    return verdict


def evaluate(
    lot: Lot,
    peer_prices: Sequence[float],
    retail_prices: Sequence[float] = (),
    *,
    this_year: int,
) -> AvitoEvaluation:
    flags: list[str] = []
    notes: list[str] = []
    defects = find_defects("\n".join([lot.title, lot.description, *lot.params]))
    penalty = min(PENALTY_CAP, sum(d.penalty for d in defects))

    avito_median = median(peer_prices) if len(peer_prices) >= MIN_AVITO_SAMPLE else None
    retail_median = median(retail_prices) if len(retail_prices) >= MIN_RETAIL_SAMPLE else None
    if avito_median is None:
        notes.append(f"avito_sample_too_small: {len(peer_prices)} of {MIN_AVITO_SAMPLE} comparable listings")
    if retail_median is None:
        notes.append(f"retail_sample_too_small: {len(retail_prices)} of {MIN_RETAIL_SAMPLE} new prices")
    price = lot.price_rub if lot.price_rub and lot.price_rub > 0 else None
    if price is None:
        flags.append("no_price")
    near_new = price is not None and retail_median is not None and price >= NEAR_NEW_RATIO * retail_median
    if near_new:
        flags.append("near_new_price")

    seller = _seller(lot, this_year, flags)
    transparency = _transparency(lot)
    delivery = EvaluationPart(
        factor="delivery", points=10 if lot.delivery else 0, max_points=10, note=f"Avito delivery: {lot.delivery}"
    )
    breakdown = [seller, transparency, delivery]
    score: int | None = None
    ratio = price / avito_median if price is not None and avito_median else None
    if ratio is not None:
        breakdown.insert(0, _price(ratio, near_new, flags))
        score = max(0, min(100, sum(part.points for part in breakdown) - penalty))

    if any(d.severity == "critical" for d in defects):
        verdict = "avoid"
    elif score is None:
        verdict = "insufficient_sample"
    else:
        verdict = _verdict(score, flags)

    return AvitoEvaluation(
        verdict=verdict,
        score=score,
        title=lot.title,
        price_rub=price,
        url=lot.url,
        breakdown=breakdown,
        defects=defects,
        defect_penalty=penalty,
        flags=flags,
        avito_median_rub=avito_median,
        avito_sample=len(peer_prices),
        min_avito_sample=MIN_AVITO_SAMPLE,
        price_to_avito_median=round(ratio, 3) if ratio is not None else None,
        retail_median_rub=retail_median,
        retail_sample=len(retail_prices),
        min_retail_sample=MIN_RETAIL_SAMPLE,
        price_to_retail_median=round(price / retail_median, 3) if price and retail_median else None,
        notes=notes,
    )
