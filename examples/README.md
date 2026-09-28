# Примеры

Скрипты, которые зовут коннекторы напрямую, без MCP-клиента: проверить установку или
взять за основу своей автоматизации. Обычный путь — инструменты плагина, см. корневой
README.

Все скрипты ходят в живые магазины. Магазины со вкладкой (Ozon, DNS, Ситилинк и другие)
требуют запущенного профиля: `uv run marketplace-mcp browser start`, после работы
`uv run marketplace-mcp browser stop`. Стоп на первом отказе действует и здесь.

```bash
uv run python examples/price_check.py "стиральная машина узкая"
uv run python examples/seller_lookup.py 5535522
uv run python examples/rating_breakdown.py "iphone 15"
uv run python examples/health_check.py
```

`price_check.py` спрашивает все магазины с поиском, кроме Авито. `avito_search.py` идёт в
Авито напрямую; пока домашний IP под флагом Авито, его не запускай. Что происходит с
каждым магазином, записано в [docs/LIVE_STATUS.md](../docs/LIVE_STATUS.md).
