# Anti-bot reality, source by source

> Это разведка апстрима `ru-marketplace-mcp` (июль–сентябрь 2026, в основном с
> датацентрового IP), оставленная как справка по механике антиботов. Разделы о
> вырезанных источниках (Taobao, Мегамаркет, Циан) удалены. Текущая картина с
> домашнего IP: [handoff/research-2026-09-27.md](handoff/research-2026-09-27.md),
> что работает сейчас: [LIVE_STATUS.md](LIVE_STATUS.md).

Every marketplace here was probed live from a datacenter IP in July 2026 —
except AliExpress, which was probed from a Russian residential IP in August 2026
and has its own section below. This
document records what actually happened — including the sources that only became
buildable once the CDP tier existed — because that determines what is buildable
and what is a trap.

The single most useful finding: **anti-bot posture, not API quality, decides
whether a marketplace is usable.** Wildberries has a messy API and works
perfectly. Megamarket has a clean one and was unreachable until the fetch moved
inside a real browser.

## Summary

| Marketplace | Datacenter IP | Anti-bot | In release |
|---|---|---|---|
| Wildberries | карточка ✅, поиск ❌ 403 | `wbaas`: поиск только с токеном из браузера | ✅ токен из твоего Chrome |
| Yandex Market | ✅ поиск и `/card/` | SmartCaptcha: `/product/<id>` сразу даёт капчу | ✅ `curl_cffi` |
| Detsky Mir | ⚠️ с домашнего ✅, с хостинга 418 | DDoS-Guard: 418 по адресу | ✅ `curl_cffi` |
| Ozon | ❌ 307 loop | Cloudflare | ✅ via your Chrome |
| Avito | ❌ 403 firewall | IP reputation + captcha | ✅ via your Chrome |
| Lamoda | ❌ ServicePipe; с домашнего ✅ | ServicePipe: отказ по адресу | ✅ `curl_cffi` |
| DNS | ❌ 401 | Qrator JS proof-of-work | ✅ via your Chrome |
| Citilink | ❌ 429 | Qrator: JS-проверка на 429, вкладка проходит сама | ✅ вкладка профиля |
| М.Видео | не проверено | карточка по HTTP — 403 на `/bff/pdp/info`, вкладка проходит | ✅ поиск `curl_cffi`, карточка вкладкой |
| ВсеИнструменты | ❌ 403 ServicePipe; с домашнего ✅ | ServicePipe: JS-проверка после серии запросов без кук | ✅ `curl_cffi` |
| AliExpress | ❌ x5sec challenge | x5sec; search works in a real Chrome | ✅ via your Chrome |

A second live run in July 2026 confirmed all four CDP sources on the maintainer's
own machine — a Russian residential IP, a logged-in Chrome over CDP — and it
changed some verdicts. Wildberries, Ozon, Yandex Market, Detsky Mir and Citilink
were stable and healthy. Citilink and DNS carried a real bug, now fixed: the
product-id regex demanded 24 hex characters (a MongoDB ObjectId shape) while the
live routes are `/product/noutbuk-lenovo-2169270/` on Citilink (a slug ending in
digits) and `/product/b7a1667f9b19ed20/` on DNS (16 hex), so search parsed zero
tiles and reported drift. Three sources stayed hard: Megamarket returns an empty
result to an anonymous session, Lamoda renders no product links, and Avito hands
back an IP block. The one lesson under all of it — there is no public endpoint
that turns these sources healthy. Every open-source implementation lands on the
same three requirements: a real browser or a residential/mobile IP, a logged-in
session, and requests paced apart. The per-source sections below say which each
one needs.

A burst of back-to-back requests is its own failure mode, separate from the
initial challenge. DNS went healthy after the regex fix, then degraded to
`transport_down` once it was hit with a run of requests with no gap between them;
Taobao did the same, dropping from healthy to a parse failure after a burst. The
connectors pace their own requests for exactly this reason. Do not remove the gap.

## Независимая проверка с резидентного IP, июль 2026

Прогон из отдельного облачного Chrome с резидентным прокси — не с машины
мейнтейнера. Он важен тем, что отделяет «площадка блокирует датацентр» от
«площадка блокирует всех».

**Wildberries** отвечает анонимно, как и раньше. Заодно выяснилось, почему по
запросу «iphone 15» самое дешёвое предложение оказывалось на треть ниже
остальных: дешёвые позиции — «Восстановленный» и «Витринный образец», то есть
телефон после ремонта и витринный экземпляр против новых у конкурентов. Ни по
названию, ни по величине скидки это не отличить автоматически без словаря
состояний, поэтому он и появился в `compare_prices`.

**Ситилинк и DNS-Shop** отдали настоящую выдачу без видимого челленджа Qrator.
Это не значит, что анонимный HTTP-клиент справится: запрос шёл из настоящего
Chrome, то есть ровно из того окружения, ради которого и существует CDP-уровень.
Вывод скромнее и полезнее — proof-of-work смотрит на репутацию адреса, и с
резидентного IP реальный браузер проходит его незаметно.

**Ситилинк в этой выдаче рисует плитки внутри iframe.** В верхнем документе
товарных ссылок нет вообще, все 27 лежат во вложенном фрейме того же
происхождения. Экстрактор, который читает только `document`, на такой раскладке
видит ноль плиток и сообщает дрейф, хотя парсер исправен. В сентябре 2026 плитки
пришли в самом документе, но поиск Ситилинка по-прежнему читает и такие фреймы.

**Lamoda** отвечает «Запрос отклонен» и на поиск, и на главную. Это блок по
адресу, а не смена вёрстки: страница вообще не отдаётся. Совпадает с выводом из
разбора публичных реализаций — искать несуществующий search-endpoint смысла нет.

Идентификатор товара Ситилинка подтверждён на живой карточке: числовой
(`2140628`), не 24-символьный hex.

Отдельно про Lamoda: её GraphQL-эндпоинт ответил **с датацентрового адреса**,
хотя HTML-страницы того же домена отдают «Запрос отклонен». То есть анонимный
tier карточек живёт по своим правилам. Снятые конверты (июль 2026), GraphQL плагин с
сентября 2026 не использует:

| Запрос | Ответ |
|---|---|
| корректные поля | `{"error": null, "result": [{…}]}` |
| неизвестный SKU | `{"error": null, "result": null}` |
| поле `old_price` | `{"error": "Internal server error", "code": -32603}` |

Стандартного GraphQL-конверта здесь нет: товары лежат в `result`, а не в
`data.products`, и ошибка приходит одной строкой `error`, а не массивом `errors`.
Коннектор читал стандартную форму, поэтому успешный ответ выглядел дрейфом.

Полная картина по всем десяти источникам с этого адреса, без единого логина:

| Источник | Что ответил | Что это значит |
|---|---|---|
| Wildberries | выдача с товарами | анонимный доступ работает |
| Яндекс Маркет | выдача с товарами | анонимный доступ работает |
| Детский мир | каталог отрисован | анонимный доступ работает |
| Ситилинк | выдача, плитки в iframe | челлендж не показан, но раскладка другая |
| DNS-Shop | 24 товарные ссылки | челлендж не показан, плитки в основном документе |
| Ozon | «Похоже, нет соединения» | соединение не состоялось: адрес отклонён на транспорте |
| Авито | «Доступ ограничен: проблема с IP» | ровно тот файрвол, что описан выше |
| Мегамаркет | слайдер-капча «разверните картинку» | ServicePipe показывает челлендж человеку |
| Lamoda | «Запрос отклонен» | отказ на всех путях, включая главную |
| Taobao | стена логина | без сессии выдачи нет, как и задокументировано |

Lamoda проверена с двух независимых резидентных адресов, на поиске, категории и
главной — везде одинаковый отказ. Это блокировка целого класса трафика, а не
сгоревший конкретный адрес, и перебор адресов её не решает.

Три источника разошлись с прогоном на машине мейнтейнера, и расхождение
полезно. Ситилинк и DNS отдали данные без логина и без челленджа — значит их
нестабильность у оператора была не про доступ, а про темп запросов подряд.
Ozon, наоборот, здоров с домашнего российского адреса и недоступен отсюда:
адрес решает больше, чем браузер.

## Shipped sources

### Wildberries — карточка открыта, поиск за токеном

Проверено 2026-09-27 с домашнего IP.

- Карточка `card.wb.ru/cards/v4/detail`, отзывы `feedbacks2.wb.ru` и basket-хосты
  отвечают обычному HTTP без токена.
- Поиск v9 `search.wb.ru` и v18 `www.wildberries.ru/__internal/u-search/...` без токена
  отвечают 403 от `wbaas`. Для стоп-правила это отказ, поэтому v9 из коннектора убран.
- v18 пускает с кукой `x_wbaas_token` и заголовком `deviceid: site_<32 hex>`. Куку
  ставит сам сайт: вкладка профиля открывает `www.wildberries.ru`, через 2–3 с кука
  появляется. Коннектор читает её и `navigator.userAgent`, дальше ходит через
  `curl_cffi` и хранит всё в `${CLAUDE_PLUGIN_DATA}/wb_session.json` (права 600) 30 ч.
  Токен живёт 36 ч; 498 значит «токен истёк», тогда он перевыпускается один раз.
- `dest` обязателен: без него цены и остатки неверные. По умолчанию `-1257786`, как
  шлёт сам сайт для Москвы; `WB_REGION=<город>` берёт код из снимка krein15
  (`wb_connector.regions`).
- `spp=30` сайт шлёт анонимному покупателю. 2026-09-27 цены с `spp=0` и `spp=30`
  совпали. Это не персональная цена с WB Кошельком, и выдавать её за такую нельзя.

Two more endpoints were verified live for v1.1.0, each with a silent-failure mode
worth knowing:

- **Buyer questions** live on `questions.wildberries.ru/api/v1/questions`, keyed by
  `imtId` like reviews. `take` and `skip` are **mandatory**: omit either and every
  product returns `{"questions": null, "count": 0}`, which reads as "nobody asked
  anything" but is really a rejected request. `take` is capped at 30 — `take=31` is
  a 400. There are no mirrors; `questions1`/`questions2` and `questions*.wb.ru` all
  answer 502. Worse, `feedbacks{1,2}.wb.ru` answer *any* questions-shaped path with
  an identical 277-byte empty stub, so pointing questions at the reviews hosts
  looks like a valid empty result.
- **Category feeds** live at `catalog.wb.ru/catalog/{shard}/v4/catalog`, with the
  shard in the path. Only v4 works; v2, v8, v9 and the shard-less form are 404.
  The catch is the shard `blackhole`, which several of the largest sections carry
  (smartphones `cat=9455`, laptops `cat=9491`, TV/audio `cat=9834`): those have no
  feed at all. The first probe answered 429, which looked like throttling, but four
  more spaced 8s apart all returned a clean empty 404. `wb_category_products`
  refuses those up front and names the alternative, because an empty product list
  would assert the category is empty.

### Яндекс Маркет — HTML со встроенным состоянием, капча на `/product/`

Маркет отдаёт страницы, отрисованные на сервере: состояние виджетов лежит JSON-ом
внутри HTML. Поиск весит около 1,8 МБ, карточка около 2,3 МБ. JSON API нет:
`/api/resolve` отвечает 403, `/api/products/{id}` говорит gRPC, публичный Content API
мёртв.

- Коннектор ходит через `curl_cffi` с отпечатком Chrome. 2026-09-27 с домашнего IP поиск
  и карточка по ссылке `/card/<slug>/<id>` ответили 200 (LIVE_STATUS).
- Та же карточка по `/product/<id>` через 3 секунды получила 302 на `/showcaptcha`.
  Такие 302 раньше выглядели как «пустой 302 примерно на каждый десятый запрос» и
  повторялись. Теперь это капча: стоп Маркета на 30 минут с первого ответа, а голый id
  до сети не доходит.
- Признак капчи — `showcaptcha` в `Location` или `SmartCaptcha`, `/showcaptcha`,
  `checkbox_captcha` в теле. Слово `captcha` само по себе не признак: на каждой
  здоровой странице есть пустой `captchaService`.
- Повторяются только сбои сети, 5xx и 302 без `Location`. 401, 403 и 429 — стоп.
- Разбор привязан к фронтенду, поэтому `yandex_selfcheck` здесь нужнее, чем у JSON API.

### Detsky Mir — страница поиска и JSON API, отказ по адресу

Поиск идёт через страницу сайта `/search/results/?qt=…`, карточка и каталог — через
`api.detmir.ru`. Всё по HTTP через `shop_request` (`curl_cffi`, `impersonate="chrome"`),
без кук.

- **Отказ — 418 от DDoS-Guard.** 2026-09-21 с хостингового адреса во Франкфурте
  `api.detmir.ru` отвечал 418 на любой запрос, с любыми заголовками и отпечатком. С
  домашнего адреса 2026-09-27 и страница поиска, и API отвечали 200. 418, как и
  401/403/429, останавливает Детмир на 30 минут; страница DDoS-Guard вместо выдачи — тоже.
- **Страница поиска несёт состояние целиком.** В `window.appData = JSON.parse("…")` лежат
  36 товаров первой страницы, регион (`RU-MOW` для запроса без кук) и подсказки поиска.
- **Нечёткий поиск.** Без совпадений сайт не отвечает «ничего не найдено», а отдаёт
  сотни популярных товаров. См. [ловушку поиска Детмира](#the-detsky-mir-search-trap).
- **HTTP 200 у API может нести `{"status": 404}`.** Одному коду статуса верить нельзя.
- **Листинги только `/v4/`.** Старого `/v2/products?filter=` нет.
- **Регион только через `filter=withregion:`.** Параметр `?withregion=RU-SPE` API
  принимает и молча игнорирует. Через фильтр число магазинов с товаром честное: 152 в
  Москве, 37 в Петербурге, 2 в Хабаровске для одного и того же товара.
- **Промокодная цена не цена.** `priceWithBestVoucher` требует промокод, в `price_rub` она
  не идёт.

### Lamoda — страницы сайта по HTTP

Проверено 2026-09-27 с домашнего адреса, `curl_cffi` как Chrome, без кук. С VPS в
датацентре те же страницы закрывает ServicePipe, в июле 2026 с других адресов было
«Запрос отклонен» — значит, это отказ по адресу, а не по запросу.

- **Поиск** `/catalogsearch/result/?q=` отвечает 200 за 0,8 с. Данные лежат JSON-ом за
  вторым ключом `payload:` в `window.__NUXT__`: `products` (60 на страницу),
  `pagination.found`, `products_meta`. На бессмысленный запрос выдача честно пустая
  (`found: 0`), а исправленный сайтом запрос помечен `is_predicted_term_corrected`.
- **Карточка** `/p/<sku>/<seo_tail>/` отвечает 200, в том же месте лежит `product`.
  Голый `/p/<sku>/` отвечает 301 на каноничный адрес; коннектор идёт по нему один раз и
  только на тот же SKU. Любой другой редирект — отказ.
- **Цена — цепочка.** Исходная цена, затем скидка сайта (`onsite`), затем акция сайта
  (`onsite_promo`) с `active_to`. У акции нет промокода, её получает любой покупатель
  до срока, и плитка сайта показывает именно её. `price_rub` — последняя ступень,
  `promo_until` — конец акции. Ступень с промокодом или неизвестного типа обрывает
  цепочку и даёт предупреждение.
- **GraphQL не годится для цены.** `POST /goapi/v2/catalog/graphql/products/` отдаёт
  `price_amount` без акции: 11 199 ₽ там, где сайт и страница товара показывают
  8 063 ₽. Поэтому карточка читает страницу товара, а не GraphQL.
- **Рейтинг** в поиске по шкале 0..100 (94,99), в карточке 0..5 (4,7). Коннектор
  делит поисковый на 20. Если поиск перейдёт на 0..5, рейтинги молча станут в 20 раз
  меньше: проверки на это нет.
- **Строки поиска.** Строка без `sku` выпадает с предупреждением `row_skipped`, строка
  без цены остаётся с `<sku>: no_price`. Без `is_sellable` наличие неизвестно (`None`),
  а не «нет».
- **Отказ.** 401/403/418/429, редирект, кроме каноничного, и страница без `__NUXT__`
  со словами антибота останавливают Лэмоду на 30 минут. В ошибке `challenge_required`
  стоит настоящий статус и `challenge_type`: `redirect` или `antibot_page`.

### М.Видео — поиск по API, карточка во вкладке

Проверено 2026-09-27 с домашнего адреса, без логина.

- **Поиск** `POST /bff/products` с телом как у сайта (`query`, `limit: 60`, сортировка по
  популярности) и куками города Москвы отвечает 200 по HTTP через `curl_cffi`. На запрос
  без совпадений — 204 без тела. Цена — `price.salePrice`, зачёркнутая —
  `price.basePrice`, если она больше. `bonusAmount` и значок «+1500 бонусов» — бонусные
  рубли, не цена. Название с «(МП)» — товар партнёра-маркетплейса.
- **Карточка** по HTTP закрыта: `/bff/pdp/info` и `/bff/location` отвечают 403 с HTML
  вместо JSON. Вкладка профиля открывает `/products/<id>` в 200, и сайт сам получает
  `/bff/pdp/info` с ответом 200. Коннектор читает schema.org `Product` (цена `offers.price`,
  наличие, рейтинг) и блок цены на странице: `.prices-base` — цена, `.prices-discount` —
  зачёркнутая. Если цена в блоке расходится с `offers.price`, берётся цена блока и
  появляется предупреждение `price_mismatch`.
- **Отказ.** 401/403/418/429, редирект, HTML вместо JSON и отказ вкладки
  (401/403/429 или страница без «М.Видео» в заголовке) останавливают М.Видео на 30
  минут. Сбой самой вкладки (тайм-аут, ошибка CDP) — `transport_down` без стопа. Поиск
  и карточка делят один темп и один стоп.

### ВсеИнструменты — страницы сайта по HTTP, ServicePipe следит за частотой

Проверено 2026-09-27 с домашнего адреса, `curl_cffi` как Chrome, без кук.

- **Поиск** `/search/?what=` отвечает 200 за 0,7 с. Страница собрана на Nuxt 3: данные
  лежат в `<script id="__NUXT_DATA__">` массивом devalue, где объекты ссылаются друг на
  друга по номеру. Товары — `pinia.ListingStore.products` (40 на страницу), число
  найденных — `productsCount`. Цена — `pricesV2.current`, зачёркнутая — `pricesV2.old`.
  `availableDiscountPrices` с типом `discountB2B` — цена для юрлиц, в `price_rub` не идёт.
- **Запрос-категория.** На «перфоратор» сайт отвечает 302 на
  `/category/perforatory-32/#searchQuery=…`. Коннектор идёт туда один раз и пишет
  предупреждение `category`. Любой другой редирект — отказ. Что страница категории
  хранит товары в том же `ListingStore`, вживую не проверено: запрос к ней получил 403.
- **Карточка** `/product/<slug>-<id>/` отвечает 200. Товар — объект с `pricesV2.default`
  под одним из ключей `data`. Старая цена есть только там: в schema.org `offers` её нет.
  Цену из данных коннектор сверяет с `offers.price` и при расхождении пишет
  `price_mismatch`. Голый `/product/<id>/` отвечает 404, поэтому карточка принимает
  только URL.
- **Город** сайт выбирает по IP: с домашнего адреса это Санкт-Петербург. Он приходит в
  поле `city`.
- **ServicePipe** закрывает HTTP без кук через несколько запросов. 2026-09-27 403 со
  страницей JS-проверки (`servicepipe.tech/checkjs`, `is_captcha: false`) пришёл
  дважды: на пятый запрос за семь минут и, после 30 минут тишины, на третий или
  четвёртый запрос за полторы минуты (журнал не сохранился). Капчи там нет, но это
  отказ: 401/403/418/429, чужой редирект и страница антибота на 200 останавливают
  ВсеИнструменты на 30 минут.
- **Персональные данные.** В странице карточки есть имена авторов вопросов и отзывов и
  идентификатор посетителя (`mkId`, `userId` в `$sactiveSigmaUserData`). Коннектор их не
  читает, фикстуры собраны без них.

### Ozon — needs your browser

`composer-api.bx` answers a datacenter IP with an endless self-referential 307
redirect loop (`?...&__rr=1`, `__rr=2`, …). curl follows it until it hits its
redirect ceiling. TLS impersonation alone does not clear it.

The working answer is not a better fingerprint but a different vantage point: run
the fetch inside a real browser over the DevTools Protocol. Setup and threat model:
[CDP_SETUP.md](CDP_SETUP.md).

> Проверено 2026-09-27 с домашнего IP, свежий анонимный профиль, без логина: главный
> документ `www.ozon.ru` отдаёт `403 Antibot Challenge Page`, через ~1 с вкладка сама
> перезагружается в 200, после этого `fetch` composer-api из вкладки отдаёт поиск.
> HTTP-уровень у коннектора убран: он всегда получал 307 и 403, то есть лишний отказ
> перед вкладкой. Отказом коннектор считает 401/403/429 из `fetch` и HTML-страницу с
> кодом 200 там, где должен быть JSON composer: после неё Ozon стоит 30 минут.

Ozon's seller pages were spiked for v1.1.0 and deliberately left out — see
[the seller-details refusal](#the-ozon-seller-details-refusal-v110).

### Avito — hard IP firewall, workable API behind it

Probed July 2026 from a datacenter IP with TLS impersonation (the same
`curl_cffi` setup that clears Ozon's tier 1):

- **Search page** (`/moskva/noutbuki?q=…`): HTTP 403, «Доступ ограничен:
  проблема с IP». The block page sets `srv_id` and `_avisc` cookies — Avito
  fingerprints the session before it will talk at all.
- **`/web/1/js/items`** (the internal JSON search endpoint, the one
  third-party parsers drive): 403 with
  `{"too-many-requests":{"link":"ru.avito://1/firewall/captcha/show"}}` — a
  firewall captcha challenge, not a rate limit that clears by waiting.
- **Mobile API** (`app.avito.ru/api/1/search`): nginx 404 — the route third
  parties use lives elsewhere, and it is gated the same way.

The API itself is well documented by open-source parsers (Duff89/parser_avito):
`GET /web/1/js/items` with `categoryId`, `locationId`, `p`, `q`, `context`,
`updateListOnly=true` plus price/seller/delivery filters returns the same JSON
the site renders. Those parsers survive on mobile proxies and bought cookies —
the firewall is the whole problem.

**Verdict:** tier 2. From a residential Russian IP with a warmed-up session the
`js/items` endpoint answers; from your logged-in Chrome it always does. The
connector tries tier 1 (impersonated HTTPS) first and falls back to CDP, same
as Ozon — but expect tier 1 to be dead from any datacenter.

The July 2026 run confirmed the endpoint is right and the block is the whole
problem. Even from the residential IP, search came back with an HTTP 439/429-class
refusal — an IP/rate block, not a challenge that a browser clears. The seller
endpoint answered while search did not, which is the rate limiter biting the
heavier query first. Three public parsers — `Duff89/parser_avito`,
`ihydrad/avito-parser`, `ergon73/avito-parser` — all drive the same `js/items`
endpoint this connector uses, and their code and issue trackers are dominated by
cookie handling and HTTP 429; Duff89 has an open issue reporting 429 even behind
a server proxy. Nobody has a cleaner path. IP reputation and request rate are the
blocker, so this one wants a residential or mobile IP and patience on top of the
browser.

## Sources that needed the CDP tier

The four below were rejected in v1.0/v1.1 because anonymous probing could not
confirm them end to end. The CDP tier changed that — in a real Chrome the
proof-of-work and IP-reputation challenges pass natively. All four shipped in
v1.2.0, and the July 2026 run on the maintainer's residential IP and logged-in
Chrome finally confirmed them end to end. DNS and Citilink went healthy once the
product-id regex was fixed. Megamarket did not: the challenge passes but the
result comes back empty. Lamoda did not either, and has since left the CDP tier:
from a home address its pages answer plain HTTP (see below).

### DNS — proof-of-work challenge

Every dynamic page returns **HTTP 401** with a Qrator JS challenge
(`/__qrator/qauth_utm_v2d_v9118.js`, ~349 KB). The valid `qrator_jsr` cookie is
only issued after executing that proof-of-work in a real JS engine; loading the
script does not set it.

- `restapi.dns-shop.ru` (from the client config) returns **403 even from a clean
  residential IP** — it is an internal SSR address.
- `/ajax-state/product-buy/` is the one route *not* behind Qrator (stable 200), but
  returns `null` without a valid CSRF token, which can only be obtained from a page
  that is itself behind Qrator.
- Anonymously reachable: `robots.txt`, `sitemap.xml` (~600k product URLs).

**Verdict:** shipped in v1.2.0 via the CDP tier. Qrator's proof-of-work executes
natively in a real Chrome, so the `dns-connector` renders the page and reads the
DOM rather than touching the CSRF-gated ajax routes.

> Проверено 2026-09-27 с домашнего IP, анонимный профиль плагина, без логина: поиск во
> вкладке получил 401 Qrator, через 1,9 с страница сама перезагрузилась в 200, плитки с
> ценами и наличием появились ещё через ~1 с. Следующие страницы и повторный запуск
> Chrome через полчаса отдали 200 сразу. Коннектор ждёт такой переход до 12 с
> (`challenge_grace_s`) и цены ещё до 8 с. 401/403/429, которые не ушли в 200, и
> страница «Доступ ограничен» останавливают DNS на 30 минут; 5xx — обычный сбой без
> стопа. Город цен сайт выбирает сам по IP, ответ несёт его в поле `city`.

Getting there took two separate fixes, and the first one only looked like a win.
The product-id regex demanded 24 hex while the real id is 16
(`/product/b7a1667f9b19ed20/`), so search had been parsing zero tiles; fixing it
made the tool report 24 links and `selfcheck` go green. The data was still empty —
every item came back with `title=None` and `price=None`, because the extractor
resolved a tile with `closest()`, which tests the element itself first and so
landed on the image link instead of the tile. That is the whole lesson of this
file in one bug: a green selfcheck proves the transport answered, not that the
parser understood the answer. The 2026-07-28 audit fixed the tile resolution and
pinned it with a test that runs the extractor against captured tile markup.

Note what the same audit found next door: DNS tiles advertise an instalment
("от 5 751 ₽/ мес.") beside the real price, and the old price heuristic took the
smallest number on the tile. Had the tile resolution been correct from the start,
DNS would have reported the monthly payment as the price — green, plausible, and
wrong. Prefer the null.

Then a burst of back-to-back requests gets the rendered browser rate-limited by
Qrator, not a parser fault; the plugin now treats that refusal as a stop. The DOM route is
the only route: no public JSON API exists, and every open-source DNS
implementation drives a real browser through Selenium, undetected-chromedriver or
Puppeteer — the same thing this connector does. Pace the calls and run
`dns_selfcheck` from your Chrome; from a datacenter address the site answers 401
and there is nothing to verify.

### Ситилинк — вкладка профиля, Qrator пропускает сам

Проверено 2026-09-27 с домашнего адреса, анонимный профиль плагина, без логина.

- **HTTP без браузера** получает 429 со страницей «Загрузка...»: это JS-проверка Qrator,
  которая ставит куку `_pcl`. `SZhukovWork/citilink-mcp` решает её на Python и повторяет
  запрос. Повтор после отказа нарушает наше стоп-правило, поэтому этот путь не взят.
- **Вкладка** получает тот же 429, и через 1,5 с страница сама перезагружается в 200 с
  выдачей. Мы в это время ничего не шлём. Поэтому коннектор передаёт в `open_page`
  `challenge_statuses={401, 403, 429}` и ждёт переход до 12 с; у остальных магазинов 429
  остаётся стопом сразу.
- **Поиск** `/search/?text=` читает плитки `ProductVerticalSnippet` и
  `ProductHorizontalSnippet` только внутри `ProductListLayout`, в самом документе и в
  iframe того же происхождения. На «ничего не найдено» сайт показывает рекомендации с
  ценами в другом блоке; без этой границы они читались бы как выдача. Цена из
  `data-meta-price`, старая цена — только если больше текущей, рейтинг — только при
  отзывах. Наличие: кнопка корзины — есть, кнопка «Сообщить о поступлении» — нет,
  иначе неизвестно.
- **Карточка** `/product/<slug>-<id>/` читает `productBase` из `__NEXT_DATA__`. Из
  вкладки уходят только поля товара и город; куки, адрес посетителя и токены из того же
  JSON наружу не попадают. Если число в конце slug не совпало с `id` товара, это другой
  товар, ответ `not_found`. Клубная цена (`price.club`) в `price_rub` не идёт.
- **Город** сайт выбирает по IP, ответ несёт его в поле `city`.
- **Отказ.** 401/403/429, которые не ушли в 200, и страница без выдачи и без слова
  «Ситилинк» в заголовке останавливают Ситилинк на 30 минут. 5xx — обычный сбой без
  стопа.
- **gRPC-web** (`rpc.citilink.ru/catalog-site/<Service>/<Method>`) не реверсится: схема
  protobuf закрыта, а вкладка даёт те же данные.

## Traps and refusals worth their own section

### The Wildberries search trap

`search-goods.wildberries.ru` отдаёт id снятых с продажи товаров: в июле 2026 на
«кроссовки мужские» из 19 карточек цена была у одной, а поиск v9 дал 100 товаров, все с
ценой. Ответ 200 без цен хуже ошибки: он читается как «предложений нет». Поэтому
`search-goods` из коннектора убран совсем, а страница, где ни у одного товара нет цены,
получает предупреждение `no_prices`.

### The Detsky Mir search trap

API Детмира принимает текстовые фильтры и игнорирует их: `q:`, `phrase:`, `search:` и
`text:` возвращали весь каталог, около 301 тысячи товаров, и первыми на «лего» шли
трусики. Апстрим построил на этом поиск, получил на «лего» подгузники, средство для посуды
и коллаген, каждый с верной ценой и рейтингом, и удалил его.

Поиск плагина читает страницу сайта `/search/results/`, и на настоящие запросы она
отвечает по делу: «lego technic» дал 424 набора LEGO. Но у неё своя ловушка. 2026-09-27
бессмысленный «zxqvwj пплк» вернул 291 товар: полки, комоды, подставки для книг. В
метаданных выдачи отличий нет, тот же `driver: anyQuerySolr`. Отличаются две вещи:
подсказки сайта (`search.suggestions`) пустые, и ни одно название или бренд на странице не
содержит слов запроса. Когда совпали обе, `detmir_search` отдаёт пустой список с
предупреждением `no_match`. Слова сверяются по первым пяти буквам, чтобы «комоды» нашли
«Комод».

### The Ozon seller-details refusal (v1.1.0)

Ozon product pages link to a seller page carrying the legal entity behind a
listing — the OGRN/INN equivalent of what `wb_seller` returns for Wildberries.
That parity would be genuinely useful, so it was spiked for v1.1.0. It is not in
the release.

The path is right. `composer-api.bx/page/json/v2?url=/seller/{slug}-{id}/` is the
route Ozon's own web client uses, and the seller id is already available: it comes
back on `ozon_card` as `seller.link`, so nothing has to be guessed.

What could not be done is see a successful response. From a datacenter IP every
attempt ends the same way:

| Composer `url=` | Result |
|---|---|
| `/product/{id}/` (known-good control) | 307 → 403 `fab_…` |
| `/seller/ozon-1749/` | 307 → 403 `fab_…` |
| `/seller/1749/` | 307 → 403 `fab_…` |
| `/` (homepage warmup) | 403 |

The first request returns 307 from nginx and sets a `__Secure-ETC` cookie with a
`&__rr=1` retry hint; following it with the cookie, under chrome124 TLS
impersonation, yields 403 with an anti-bot incident body. The control matters more
than the seller rows: the repo's **already-working** product path fails identically,
which proves this is the IP being gated, not a malformed seller URL.

So the endpoint almost certainly works. The **field paths do not exist yet** —
nobody has seen the widget that carries the seller's legal name and tax ids, and
Ozon's payload is a dictionary of JSON-encoded widget states whose keys carry
version suffixes. Writing a parser against an unseen shape means inventing field
names and shipping whatever they happen to match.

That is the Detsky Mir lesson in a different costume. A seller tool returning a
plausible-looking company name for the wrong legal entity is worse than no tool,
because the entire point of a seller lookup is telling an official brand store from
a lookalike reseller. Confidence without verification is the failure mode this
project refuses.

**What exists instead:** the verified URL template and the live-verification steps
are in the upstream `RELEASE_CHECKLIST.md`, for an operator on a Russian residential IP or with the
CDP tier available. If the payload confirms, the tool is a small addition on top of
the existing two-tier fetch. Until then there is no `ozon_seller`.

For the same reason, `wb_questions` **did** ship in v1.1.0: its endpoint was
verified across six products from this same datacenter IP before a line of it was
written. The difference is evidence, not ambition.

## Practical guidance

**Rate limits are real.** WB search rate-limits within a few requests; Yandex is
fine at a steady pace but bursts invite SmartCaptcha. Every connector enforces a
minimum gap by default. Do not disable it.

**A block is not an absence.** `transport_down` or `rate_limited` means "we were
refused", not "the product does not exist there". `compare_prices` keeps these
distinct via `source_outcomes` and `complete`.

**Residential IP changes the picture.** From a Russian residential address, Ozon
tier 1 often works, Lamoda's HTML likely opens, and Citilink's rate block may
clear — and Ozon's seller pages become verifiable, which is the one thing blocking
an `ozon_seller` tool. Set `*_PROXY` to route through one. As of v1.1.0 every
connector honours its own proxy variable (`WB_PROXY`, `OZON_PROXY`,
`YANDEX_PROXY`, `DETMIR_PROXY`), not just two of them.

**Run the selfchecks.** `success` / `drift_detected` / `inconclusive` — and note
that `inconclusive` from a geo block says nothing about whether the parsers still
work.


## AliExpress (added 2026-08-20)

Scope: one vantage point — a Russian residential IP, an anonymous scraping
profile, 2026-08-20. From a datacenter IP or another geo the pattern will be
stricter or different (a challenge on the search page itself is likely); the
numbers below are a snapshot, not a constant. Prices are RUB-locale by the
profile's region; a different ship-to region shifts both prices and the shape
of challenges. Before quoting any of this today, run `aliexpress_selfcheck`
from your own session.

**Anonymous HTTP — nothing usable:**

- item page: HTTP 200 with an x5sec challenge stub (`_____tmd_____/punish?x5secdata=…`,
  «Пройдите проверку»), ~1 889 bytes. Full browser headers and language do not help.
- search / category: 200, but a hollow SPA shell — `__AER_DATA__` carries zero
  catalog data (empty `widgets.paths`), tiles are client-rendered. Why the shell
  is empty was not isolated: it can be SPA architecture or the server declining
  to ship data to a low-trust client — both read the same from the outside.
- h5api item route (`acs.aliexpress.ru/h5/aapi/sc-item-detail/2.0`): route gone —
  `FAIL_SYS_API_NOT_FOUNDED` (single probe; an API-rename, not proven anti-bot).
- reviews (`feedback.aliexpress.ru/display/evaluationProductDetailAjaxService.htm`):
  HTTP 500 (single probe; server error, not proven a block).

**Real Chrome, challenge-passed session:**

- search page renders fully: 31-96 tiles with prices, discount badges, ratings and
  «N купили» lines; the landing search never challenges. The queries used were
  topical («smart watch», «realme watch»); a distinctive-query relevance test
  (the Detsky Mir lesson) was NOT run separately, so "search actually searches"
  is evidenced by topical output, not by a controlled probe.
- DIRECT navigation to an item page is challenged even in the warm profile. The
  trigger is the navigation level, not the IP: in the SAME session a same-origin
  in-page fetch of the item URL answers 200. Do not "fix" this by retrying
  direct gotos — that only burns profile reputation. The working path is
  search → open the card in a new tab.
- in-page `fetch(itemUrl)` from the loaded search page: 200, 486 KB SSR shell, no
  challenge — but the SSR carries no price (h1, og/meta description with the rating
  and «N заказов» present).
- `window.open(itemUrl)` from the loaded search page: the real card renders in
  the new tab, prices included. This is the transport the connector uses.

**Load behaviour (question 5 of the doctrine):** after many rapid probes x5sec
degrades quietly — the search grid keeps working, but new-tab card renders come
back with the price module stripped (title present, no ₽ anywhere). The measured
threshold is not established (qualitative, at double-digit burst counts). The
connector reports that state as `price_missing`, never as a fabricated number,
and the selfcheck marks it `inconclusive`. A full challenge wall on the landing
maps to a transport error telling the operator to pass the check by hand in the
scraping window. The connector cannot tell "anti-bot stripped the price" from
"this listing genuinely has no price" — either way the answer is None plus a
warning, and a run of price_missing answers means back off, not push harder.

**Not implemented (and why):** review TEXTS require navigating the review tab,
which is challenge-prone; the connector ships rating + order counts only — if the
tab ever stops challenging, a reviews tool becomes a small addition. The
mtop/h5api signature is not reversed: x5sec is not solved programmatically, and
per-request proof-of-work is precisely the case the doctrine calls "Neither".
The connector does not pretend otherwise: on a challenge it degrades into
`price_missing`/transport errors and tells the operator to pass the check by
hand.
