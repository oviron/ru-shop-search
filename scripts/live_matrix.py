"""Живая матрица: один search на магазин через код плагина, без Авито.

    uv run python scripts/live_matrix.py

Магазины одного запроса идут параллельно, каждый получает один запрос; между запросами
пауза 5 с. Ссылки не печатаются: в них бывают метки трекеров. Chrome после прогона
закрывай сам: ``uv run marketplace-mcp browser stop``.
"""

from __future__ import annotations

import asyncio
import datetime
import sys

from marketplace_connector import server

MATRIX: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "rtx 5070",
        ("wildberries", "yandex_market", "ozon", "dns", "citilink", "mvideo", "regard", "nix", "pepper"),
    ),
    ("lego technic", ("detsky_mir",)),
    ("кроссовки adidas", ("lamoda",)),
    ("перфоратор makita", ("vseinstrumenti",)),
    ("холодильник atlant", ("holodilnik",)),
    ("realme watch", ("aliexpress",)),
)


def _row(outcome, offers) -> str:
    rows = [o for o in offers if o.source == outcome.source]
    priced = [o.price_rub for o in rows if o.price_rub is not None]
    other = [o for o in rows if o.price_condition or o.deal]
    linked = sum(1 for o in rows if o.url)
    span = f"{min(priced):,.0f}–{max(priced):,.0f} ₽" if priced else "—"
    note = f"; с ценой по условию (карта, купон, СУПЕРЦЕНА) или сделкой: {len(other)}" if other else ""
    detail = "" if outcome.status == "ok" else f"; {outcome.error_code or ''} {(outcome.detail or '')[:90]}"
    return (
        f"{outcome.source:<15} {outcome.status:<8} строк {len(rows):>2}, с ценой {len(priced):>2}, "
        f"со ссылкой {linked:>2}, {span}{note}{detail}"
    )


async def main() -> int:
    failed = 0
    for index, (query, shops) in enumerate(MATRIX):
        if index:
            await asyncio.sleep(5)
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        response = await server.search(query=query, shops=list(shops), per_shop=5)
        print(f"\n{stamp}  search({query!r})")
        for outcome in response.source_outcomes:
            print("  " + _row(outcome, response.offers))
            failed += outcome.status != "ok"
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
