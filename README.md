# ru-shop-search

Плагин Claude Code для поиска и сравнения цен в российских магазинах. Спрашиваешь
«где дешевле RTX 5070» или «нормальная ли цена у этого лота на Авито» и получаешь
ответ по живым ценам нескольких магазинов, со ссылками и оценкой уверенности.

Личный инструмент: один пользователь, запуск с этого мака, с домашнего IP. Публикации
и поддержки чужих установок нет.

Форк [Vladimir-Human/ru-marketplace-mcp](https://github.com/Vladimir-Human/ru-marketplace-mcp)
(MIT): из него взяты ядро `mcp-core` и часть коннекторов.

## Установка

Нужны `uv` и Google Chrome. Разово, на одну сессию, плагин грузится прямо из чекаута:

```bash
claude --plugin-dir ~/Developer/oviron/ru-shop-search
```

Постоянно плагин ставится из коммитов ветки `plugin/bootstrap` через личный маркетплейс
`ru-shop-search-local`. Он лежит вне репозитория, в
`~/.claude/local-marketplaces/ru-shop-search-local/.claude-plugin/marketplace.json`:

```json
{
  "name": "ru-shop-search-local",
  "owner": { "name": "Dmitry Avdonkin" },
  "metadata": {
    "description": "Личный маркетплейс: ru-shop-search из коммитов ветки plugin/bootstrap основного чекаута"
  },
  "plugins": [
    {
      "name": "ru-shop-search",
      "source": {
        "source": "url",
        "url": "file:///Users/oviron/Developer/oviron/ru-shop-search",
        "ref": "plugin/bootstrap"
      },
      "description": "Поиск и сравнение цен в российских магазинах"
    }
  ]
}
```

```bash
claude plugin marketplace add ~/.claude/local-marketplaces/ru-shop-search-local
```

Плагин ставится в local scope проекта, где нужен поиск (сейчас `/cl`), и по умолчанию
выключен: сессия включает его строкой `"ru-shop-search@ru-shop-search-local": true` в
`enabledPlugins` своего `.claude/settings.local.json`. Команды — из корня проекта:

```bash
claude plugin install --scope local ru-shop-search@ru-shop-search-local
```

Установка клонирует локальный репозиторий (push не нужен) и кладёт в
`~/.claude/plugins/cache/ru-shop-search-local/ru-shop-search/2.4.2` только закоммиченные
файлы. Сервер запускается из этой копии и при первом старте создаёт в ней `.venv` без
dev-зависимостей. Маркетплейс из самого репозитория (`claude plugin marketplace add
~/Developer/oviron/ru-shop-search`) для постоянной установки не годится: он копирует в кэш
весь каталог, вместе с worktree, `.venv` и кэшами линтеров, и исключить что-то из этой
копии Claude Code не даёт.

Codex запускает сервер прямо из основного чекаута (`uv run --no-dev --directory
~/Developer/oviron/ru-shop-search marketplace-mcp`), поэтому чекаут держится на
`plugin/bootstrap`: иначе Claude и Codex работают на разном коде.

Версия в `plugin.json` закреплена, поэтому `claude plugin update` новых коммитов не видит.
После коммитов в `plugin/bootstrap` переустанови плагин, сохранив его данные:

```bash
claude plugin uninstall --scope local --keep-data ru-shop-search@ru-shop-search-local
```

```bash
claude plugin install --scope local ru-shop-search@ru-shop-search-local
```

Каталог версии при этом пересоздаётся на месте, уже без `.venv`. Запущенный сервер держит
старые файлы открытыми и отвечает, но магазины подгружает при первом обращении, и такой
импорт может не найти пакетов. Поэтому в открытых сессиях переподключи сервер через `/mcp`
или начни новую сессию.

Плагин поднимает один MCP-сервер (`uv run marketplace-mcp`). Магазинам, которые пускают
только браузер, нужен отдельный анонимный профиль Chrome с CDP на `127.0.0.1`. Сервер
запускает его сам; открыть заранее или закрыть можно так:

```bash
uv run marketplace-mcp browser
```

```bash
uv run marketplace-mcp browser stop
```

Подробности: [docs/CDP_SETUP.md](docs/CDP_SETUP.md).

## Что умеет

Инструменты MCP-сервера:

| Инструмент | Что делает |
|---|---|
| `search` | Ищет товар в выбранных магазинах параллельно (по умолчанию во всех, кроме Авито), отдаёт цены, ссылки и статус каждого магазина |
| `card` | Карточка одного товара по ссылке или id |
| `shops` | Какие магазины подключены и какие остановлены после отказа |
| `compare` | Сводит одну и ту же модель из разных магазинов и показывает уверенность сопоставления |
| `price_history` | История цены: своя (снимки в SQLite) плюс WB и DNS |
| `avito_evaluate` | Оценка лота на Авито против независимой выборки похожих лотов |

Историю от самого магазина `price_history` умеет брать только у WB и DNS; по остальным
магазинам он отдаёт цены, которые плагин видел в своих `search` и `compare`. Авито с
домашнего IP сейчас отвечает 429, поэтому `avito_evaluate` принимает лот и цены похожих
объявлений аргументами.

Скиллы в каталоге `skills/`:

| Скилл | Срабатывает на | Что делает |
|---|---|---|
| `find-cheaper` | «найди дешевле», «где дешевле купить», «сравни цены» | `compare` или `search` по выбранным магазинам, карточка победителя, сноски о ценах по подписке |
| `avito-lot` | ссылка на Авито, «оцени лот», «стоит ли брать» | `avito_evaluate` и разбор вердикта, дефектов и продавца |
| `honest-discount` | «честная ли скидка», «история цены» | `card` и `price_history`: была ли зачёркнутая цена и низкая ли текущая |

## Магазины

WB, Ozon, Яндекс Маркет, DNS, Ситилинк, М.Видео, Детский мир, Лэмода, ВсеИнструменты,
Regard, Никс, Холодильник, AliExpress, Pepper, Авито.

Что работает прямо сейчас и чем это проверено, записано в
[docs/LIVE_STATUS.md](docs/LIVE_STATUS.md).

Три уровня транспорта, выбирается самый дешёвый рабочий:

1. HTTP через `curl_cffi` с отпечатком Chrome.
2. HTTP с токеном, который выдал браузер (WB-поиск).
3. Вкладка браузера: `fetch` изнутри неё (Ozon, Pepper, AliExpress) или сама страница
   магазина (DNS, Ситилинк, карточка М.Видео).

## Правила, которые код соблюдает

- Не чаще запроса в 3 секунды на магазин. На первый отказ (401, 403, 429, капча,
  «доступ ограничен») магазин останавливается на 30 минут, повторов нет. Если капчу
  прошёл сам в окне браузера, стоп снимается вручную:

  ```bash
  uv run marketplace-mcp stops
  ```

  ```bash
  uv run marketplace-mcp resume ozon
  ```
- Капчи не решаются. Логинов, личных аккаунтов, корзин и сообщений продавцам нет.
- Токены и куки живут только в каталоге данных плагина, не в git и не в логах.

## Разработка

```bash
uv sync
npm ci --prefix tools/jsdom              # jsdom для DOM-тестов экстракторов
uv run pytest -m "not live and not cdp"
uv run ruff check
uv run mypy
```

Устройство кода: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Антиботы магазинов:
[docs/ANTI_BOT.md](docs/ANTI_BOT.md). Новый магазин: [docs/ADDING_A_SOURCE.md](docs/ADDING_A_SOURCE.md).

## Лицензия

MIT, см. [LICENSE](LICENSE). Авторы апстрима перечислены в [CONTRIBUTORS.md](CONTRIBUTORS.md).
