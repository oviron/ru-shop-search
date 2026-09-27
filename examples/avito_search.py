#!/usr/bin/env python3
"""Search Avito and show the ads' seller ratings.

    uv run python examples/avito_search.py "thinkpad x1"

Runs in the plugin's Chrome tab (`uv run marketplace-mcp browser start`), no login.
The first refusal stops Avito for 30 minutes. Priceless ads surface as None, not 0.
"""

from __future__ import annotations

import asyncio
import sys

from avito_connector.server import avito_search


async def main(query: str) -> int:
    result = await avito_search(query=query, page=1)
    print(f"Query: {result.query!r}  tier: {result.tier_used}  total: {result.total_count}\n")
    for item in result.items[:10]:
        price = f"{item.price_rub:,.0f} ₽" if item.price_rub else "цена не указана"
        rating = f"{item.seller_rating} ({item.seller_reviews} отз.)" if item.seller_rating is not None else "?"
        print(f"  {price:>16}  {item.title or ''}  — продавец {rating}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "ноутбук")))
