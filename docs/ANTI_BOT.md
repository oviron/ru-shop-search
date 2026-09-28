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
| Avito | ❌ 403 firewall; с домашнего 429 (2026-09-27) | репутация IP и темп, капча файрвола | ⚠️ вкладка профиля, вживую не проверено |
| Lamoda | ❌ ServicePipe; с домашнего ✅ | ServicePipe: отказ по адресу | ✅ `curl_cffi` |
| DNS | ❌ 401 | Qrator JS proof-of-work | ✅ via your Chrome |
| Citilink | ❌ 429 | Qrator: JS-проверка на 429, вкладка проходит сама | ✅ вкладка профиля |
| М.Видео | не проверено | карточка по HTTP — 403 на `/bff/pdp/info`, вкладка проходит | ✅ поиск `curl_cffi`, карточка вкладкой |
| ВсеИнструменты | ❌ 403 ServicePipe; с домашнего ✅ | ServicePipe: JS-проверка после серии запросов без кук | ✅ `curl_cffi` |
| Regard | ✅ 200 (разведка) | не встречен | ✅ `curl_cffi` |
| Никс | не проверено | не встречен; поиск подписан JS страницы | ✅ поиск вкладкой, карточка `curl_cffi` |
| Холодильник | не проверено | не встречен | ✅ `curl_cffi` |
| AliExpress | ❌ x5sec (апстрим, август 2026) | x5sec: прямой переход на товар даёт проверку | ✅ вкладка профиля, карточка из вкладки выдачи |
| Pepper | ❌ 307 `?_ycch=1` и с дома, и с VPS (разведка) | SmartCaptcha: JS-проверка, вкладка проходит сама | ✅ вкладка профиля |

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
- `dest` обязателен: без него цены и остатки неверные. Плагин ставит в манифесте
  `WB_REGION=Санкт-Петербург`, это `-1205339` из снимка krein15 (`wb_connector.regions`);
  какой `dest` для Петербурга шлёт сам сайт, не сверено. Без `WB_REGION` — `-1257786`,
  как сайт шлёт для Москвы.
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
  36 товаров первой страницы, регион и подсказки поиска.
- **Регион страницы поиска — кука `geoCityDMIso`.** Без неё сайт отдаёт `RU-MOW`.
  Коннектор шлёт в ней `DETMIR_REGION`; манифест плагина ставит `RU-SPE`, и 2026-09-28
  поиск ответил регионом `RU-SPE`.
- **Нечёткий поиск.** Без совпадений сайт не отвечает «ничего не найдено», а отдаёт
  сотни популярных товаров. См. [ловушку поиска Детмира](#the-detsky-mir-search-trap).
- **HTTP 200 у API может нести `{"status": 404}`.** Одному коду статуса верить нельзя.
- **Листинги только `/v4/`.** Старого `/v2/products?filter=` нет.
- **Регион API только через `filter=withregion:`.** Параметр `?withregion=RU-SPE` API
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
  популярности) и куками города отвечает 200 по HTTP через `curl_cffi`. На запрос
  без совпадений — 204 без тела. Цена — `price.salePrice`, зачёркнутая —
  `price.basePrice`, если она больше. `bonusAmount` и значок «+1500 бонусов» — бонусные
  рубли, не цена. Название с «(МП)» — товар партнёра-маркетплейса.
- **Карточка** по HTTP закрыта: `/bff/pdp/info` и `/bff/location` отвечают 403 с HTML
  вместо JSON. Вкладка профиля открывает `/products/<id>` в 200, и сайт сам получает
  `/bff/pdp/info` с ответом 200. Коннектор читает schema.org `Product` (цена `offers.price`,
  наличие, рейтинг) и блок цены на странице: `.prices-base` — цена, `.prices-discount` —
  зачёркнутая. Если цена в блоке расходится с `offers.price`, берётся цена блока и
  появляется предупреждение `price_mismatch`.
- **Город** — Санкт-Петербург: пять кук на домене `.www.mvideo.ru` (`MVID_CITY_ID`
  `CityCZ_1638`, `MVID_REGION_ID` 6, `MVID_REGION_SHOP` S904, `MVID_KLADR_ID`,
  `MVID_TIMEZONE_OFFSET`). Поиск шлёт их с запросом, вкладка карточки получает их через
  CDP до навигации. Кука на `.mvideo.ru` ложится дублем рядом с кукой сайта, и сайт
  берёт московскую. Поле `city` — город запроса, сайт его в ответе не называет.
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

### Regard — страницы сайта по HTTP, антибота не видно

Проверено 2026-09-27 с домашнего адреса, `curl_cffi` как Chrome, без кук. За день ушло
18 запросов, отказов не было.

- **Поиск** `/catalog?search=` отвечает 200 за 0,8 с. Страница собрана на Next.js:
  данные в `<script id="__NEXT_DATA__">`, товары —
  `props.initialState.listing.data.bySearch.pages["0"].data` (24 на страницу), число
  найденных — `recordsFiltered`. Цена — `price`, зачёркнутая — `price_old`. `bonus` —
  бонусные рубли, `discounted_price` — цена уценённого экземпляра; ни то, ни другое в
  `price_rub` не идёт. `vendorcode` — артикул производителя, он приходит в `part_number`.
- **Запрос-категория.** На «видеокарта» сайт отвечает 301 на
  `/catalog/1013/videokarty?q=…&search=…`; товары там лежат под `listing.data["1013"]`.
  Коннектор идёт туда один раз и пишет предупреждение `category`. Любой другой
  редирект — отказ.
- **Наличие в поиске** явно не отдаётся. У строки есть `max_day` — срок получения в
  днях, по нему работает фильтр сайта «Сроки получения», — и `preorder`. Коннектор
  ставит `in_stock: true`, когда срок есть и это не предзаказ, иначе `null`. Это вывод
  по данным, сайт его так не называет. В карточке наличие берётся из schema.org
  `offers.availability`.
- **Карточка** `/product/<id>/<slug>` отвечает 200, товар — `product.data`, та же
  форма, что у строки поиска. Цена сверяется с `offers.price`, расхождение —
  `price_mismatch`. Голый `/product/<id>` отвечает 301 на полный адрес, коннектор
  идёт туда один раз, если id совпадает. Отсутствующий товар — 410, это `not_found`
  без стопа.
- **Город** задаёт httpOnly-кука `city_id`: 44 — Москва (так же и без куки), 137 —
  Санкт-Петербург. Коннектор шлёт 137 с каждым запросом. Город сайт называет сам, он
  приходит в поле `city`.
- **Отказы.** Антибота не встретили. На случай его появления 401/403/418/429, чужой
  редирект и страница антибота на 200 останавливают Regard на 30 минут.

### Никс — поиск во вкладке, карточка по HTTP

Проверено 2026-09-27 с домашнего адреса. HTTP — `curl_cffi` как Chrome без кук, вкладка —
анонимный профиль плагина. За день ушло 23 запроса, из них два — за JS на `static.nix.ru`,
не считая XHR самой страницы во вкладке. Отказов не было.

- **Поиск по HTTP закрыт подписью, а не антиботом.** Выдачу по словам отдаёт XHR
  `POST /scripts/action.php/FastSearch/goods`, его параметр `sign` — md5 от
  `PHPSESSID` и отсортированных параметров в cp1251, считает его JS страницы.
  `price_list.html?section=…` по HTTP отдаёт 100 строк категории без фильтра по словам,
  `/scripts/autocomplete.php` — три товара. Подпись не подделываем.
- **Вкладка** открывает `/price/search_panel_ajax.html#keywords=<запрос>`: документ 200,
  страница сама шлёт 13 XHR к `nix.ru/scripts/action.php`, `FastSearch/goods` — 200
  за 0,6–0,9 с. Это обычный трафик страницы, как у человека в браузере. Запрос в hash
  сайт читает как UTF-8, кириллица проходит.
- **Готовность.** Метка «ничего не найдено» видна до ответа XHR; поэтому коннектор ждёт,
  пока сайт впишет запрос в `.fts-keywords`, и только тогда смотрит на строки или эту
  метку. Чипы разделов он ждёт до того же дедлайна в 10 с. Если к дедлайну у страницы
  нет даже заголовка, это `transport_down` без стопа, а не блок.
- **Нечёткий поиск.** Незнакомое слово сайт режет до префикса: `qzxwvkjh` превратился в
  `qz` и дал 22 посторонних товара DeWalt, «абырвалг» — один патч-корд. Названия у Никса
  без родовых слов: на «видеокарта» приходят 50 видеокарт, и ни одна не называется
  видеокартой. Зато над выдачей сайт показывает чипы разделов, здесь «Видеокарты».
  Если слово запроса не встречается ни в названиях строк, ни в чипах, коннектор отдаёт
  пустой список с `no_match`.
- **Цена.** На `www` в строке две колонки: «от» — СУПЕРЦЕНА (предоплата и самовывоз из
  магазина), «до» — цена. В `price_rub` идёт «до», в `superprice_rub` — «от»; если
  заполнена только «от», суперцены у товара нет, и это цена (карточка 999368:
  `itemprop="price"` 5 569, строки СУПЕРЦЕНА нет). На `spb` колонка одна (`cell-price`),
  СУПЕРЦЕНЫ в выдаче нет, 2026-09-28 не было её и в карточке 907082. В карточке цена — `itemprop="price"` в schema.org `Offer`, СУПЕРЦЕНА —
  строка `span.superprice` над ним. В `compare` ранжируется СУПЕРЦЕНА с
  `price_condition: superprice`.
- **Наличие.** Колонка «Наличие» — «ЕСТЬ», «ЗАВТРА», «ПОСЛЕЗАВТРА» или дата вида
  «07.10». Любое из этого — `in_stock: true`, сам текст приходит в `availability`.
- **Карточка** `/autocatalog/…/<имя>_<id>.html` отвечает 200 в windows-1251. Цена и
  наличие — из microdata, бренд и артикул производителя (`sku`) — оттуда же. Товара нет —
  404, это `not_found` без стопа.
- **Город** у Петербурга — свой поддомен `spb.nix.ru`, туда коннектор и ходит. На
  `www.nix.ru` город всегда Москва (`city_id` 1721): ни кука `CityKLADR`, ни кнопка «Да»
  в вопросе о городе его не держат, а `GeoService.getCityByIP()` отвечает Петербургом с
  `domain: "spb"`. Ссылку на `www` коннектор читает с `spb`. Город приходит в поле `city`.
- **Отказы.** Антибота не встретили. На случай его появления 401/403/418/429 во вкладке
  или по HTTP, чужой редирект карточки и страница антибота останавливают Никс на
  30 минут.

### Холодильник — страницы сайта по HTTP, антибота не видно

Проверено 2026-09-27 с домашнего адреса, `curl_cffi` как Chrome без кук. За день ушло
10 запросов, отказов не было.

- **Поиск** `/search/?search=<запрос>` отвечает 200 в windows-1251, запрос в UTF-8
  сайт понимает. Строки — блоки `<div class="catalog-item">`: ссылка, название, цена
  `price-value__actual`, зачёркнутая `price-value__old`, рейтинг «4.5 • 74 отзыва»,
  кнопка «В корзину» или «Нет в наличии», срок доставки. Число найденных — в
  «Найдено товаров». На странице 24 строки.
- **Код товара** — артикул сайта (`data-icon-link-favorite` в строке, `sku` в
  карточке, «артикул 107818» в заголовке). Внутренний id сайта (`34449`) коннектор
  не отдаёт: карточка его не показывает.
- **Не цена.** Бейдж «Скидка 10%» — промокод в мобильном приложении, «Списывайте
  бонусы» — бонусная программа. Цена в них не меняется, коннектор их не читает.
- **Нечёткий поиск.** На бессмысленный запрос сайт показывает что-то постороннее
  (выключатель Aqara на «qzxwvkjh plmnbq»). Если ни одна строка не называет слов
  запроса, коннектор отдаёт пустой список с `no_match`.
- **Карточка** `/<раздел>/<вид>/<бренд>/<модель>/` — ровно четыре сегмента, так у всех
  49 товаров из проб. Данные — schema.org microdata: `sku`, `gtin`, бренд, рейтинг,
  `Offer` с ценой в рублях и наличием. Зачёркнутой цены в микроразметке нет, она только
  в видимом блоке `product-price__old`; коннектор берёт её, если текущая цена этого
  блока совпадает с ценой `Offer`. Два блока Product или Offer на странице означают
  чужой товар рядом, это `parser_drift`, а не смешанная карточка. Товара нет — 301 на
  родительскую категорию, это `not_found` без стопа.
- **Город** сайт выбирает по IP: с домашнего адреса — Санкт-Петербург. Он приходит в
  поле `city`.
- **Отказы.** Антибота не встретили. На случай его появления 401/403/418/429, чужой
  редирект и страница антибота останавливают Холодильник на 30 минут.

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

**Verdict (upstream, July 2026):** the endpoint is right and the block is the whole
problem. Even from a residential IP search came back 429-class, and the public parsers
that drive the same `js/items` endpoint (`Duff89/parser_avito`, `ihydrad/avito-parser`,
`ergon73/avito-parser`) are dominated by cookie handling and HTTP 429. What the plugin
does today is in [the Avito section below](#авито--одна-навигация-стоп-на-первом-отказе).

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
> стопа. Город цен сайт выбирает сам по IP (с домашнего адреса — Санкт-Петербург), ответ
> несёт его в поле `city`. Город в шапке появляется позже цен, поэтому коннектор после цен
> ждёт его ещё до шести опросов.

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
- **Город** сайт выбирает по IP (с домашнего адреса — Санкт-Петербург), ответ несёт его
  в поле `city`.
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


## AliExpress — вкладка профиля, x5sec останавливает магазин

Проверено 2026-09-27 с домашнего адреса во вкладке анонимного профиля плагина, Chrome 153
через Playwright. За вечер ушло до 20 навигаций, проверки x5sec не было. Пункты про HTTP
и нагрузку — разведка апстрима 2026-08-20 с того же типа адреса, их не перепроверяли.

- **HTTP без браузера бесполезен** (апстрим). Страница товара отвечает 200 с заглушкой
  x5sec (`_____tmd_____/punish?x5secdata=…`, «Пройдите проверку»). Выдача — пустая
  оболочка SPA, `__AER_DATA__` без товаров. Маршрут h5api карточки исчез
  (`FAIL_SYS_API_NOT_FOUNDED`), отзывы ответили 500.
- **Выдача** `/wholesale?SearchText=<запрос>` рендерится во вкладке. Плитки товаров
  лежат в ленте `SnowSearchProductFeed_List`, после неё идут «Вы всё посмотрели» и
  «Подобрали для вас» с такими же плитками и ценами. Коннектор читает только ленту:
  на «абырвалгщзх» на странице было 59 ссылок на товары, в ленте — 5. Готовность —
  когда число плиток перестало расти, есть признак «ничего не найдено» или проверка
  x5sec; дедлайн 15 с.
- **Нечёткий поиск.** На бессмысленный запрос сайт предлагает исправление и показывает
  посторонние товары: монитор, водонагреватель, волосы для наращивания. Если ни одна
  плитка не называет слов запроса, коннектор отдаёт пустой список с `no_match`.
- **Цена.** В плитке базовая цена стоит первой, текущая — второй; коннектор берёт
  меньшую как цену, большую как зачёркнутую. Ценой считается только текст, который
  целиком число с «₽»: «5 348 ₽ с купоном» — цена по купону, она идёт не в `price_rub`,
  а в `coupon_price_rub` (в `compare` ранжируется с `price_condition: coupon`). Строки
  доставки вида «до 14 дней, от 109 ₽» — не цена. Строка доставки не считается и названием, хотя бывает длиннее его.
- **Карточка.** Прямой переход на `/item/<id>.html` даёт x5sec даже в прогретом профиле
  (апстрим), поэтому коннектор его не делает. Он открывает выдачу-лендинг и из неё
  вызывает `window.open` на товар с `noopener`, чтобы страница товара не могла управлять
  вкладкой выдачи; новая вкладка рендерит карточку с ценой. Принимается
  только вкладка с `/item/<id>` нужного товара, все новые вкладки закрываются. Этому
  нужна вкладка Playwright: на raw CDP (запасной путь при зависании подключения) у
  страницы нет контекста, и карточка отвечает `transport_down`.
- **Ссылка** — канонический `aliexpress.ru/item/<id>.html`, с `?sku_id=` варианта, если
  он был в плитке; остальные параметры плитки отбрасываются.
- **Нагрузка** (апстрим). После серии быстрых проб x5sec выдачу не трогал, но карточки
  в новых вкладках приходили без блока цены. Порог не измерен. Коннектор отдаёт такую
  карточку с пустой ценой и предупреждением `price_missing`, а не с выдуманным числом;
  серия таких ответов — повод остановиться.
- **Отказы.** Проверка x5sec — `/punish`, `_____tmd_____` или `x5secdata` в адресе
  вкладки, либо заголовок «Пройдите проверку» — останавливает AliExpress на 30 минут
  с `challenge_required`. Проверку не проходим ни руками, ни кликом. 401/403/418/429
  при навигации и уход вкладки с `aliexpress.ru` тоже стоп. Прочие сбои вкладки —
  `transport_down` без стопа.
- **Не сделано.** Тексты отзывов: вкладка отзывов провоцирует проверку, коннектор
  отдаёт только рейтинг и число заказов. Подпись mtop/h5api не вскрываем.

## Pepper — вкладка профиля, цена сделки не цена магазина

Проверено 2026-09-27 с домашнего адреса во вкладке анонимного профиля плагина, Chrome 153
через Playwright. Ушло 8 навигаций, отказов и капчи не было.

- **HTTP без браузера** (разведка того же дня) встречает редиректом 307 на `?_ycch=1`:
  это JS-проверка Yandex SmartCaptcha, и с домашнего адреса, и с VPS. Вкладка проходит
  её сама, без клика. Поэтому коннектор не шлёт `fetch` из вкладки, как предполагал
  HANDOFF, а открывает во вкладке саму страницу: она серверная, данные уже в HTML.
- **Выдача** `/selective_search?keyword=<запрос>&search_list=deals`. Сделки — блоки
  `article[data-gtm-deal-id]` внутри `#search-results-content`; магазин — в
  `data-gtm-store`, ссылка — `/deals/<slug>-<id>`. Первая строка блока — возраст
  («10 дней назад») или «Истекло», температура — голоса сообщества в градусах, бывает
  отрицательной. Цена — `div.text-primary` в строке под названием, зачёркнутая —
  `div.line-through` там же. «Бесплатная доставка» и «0₽» в той же строке — доставка,
  не цена. Пустая выдача — «Упс! Похоже, что ничего не нашлось...».
- **Цена сделки — не цена магазина.** Её назвал автор поста в момент публикации, и
  условия часто стоят в названии: «с подпиской», «с ВБ кошельком», «цена с
  ozon-картой», «из-за рубежа». Выдача не прячет старые сделки: на «iphone 15» все пять
  почти двухлетней давности, пометки «Истекло» нет ни у одной. Поэтому в `compare` у
  сделки `price_rub` пустой, цена лежит в `offer.deal` и в ранжирование не идёт.
- **Карточка** `/deals/<slug>-<id>`: JSON-LD `Product` с `Offer` (цена, рубли,
  `priceValidUntil`, `InStock` или `OutOfStock`), где `brand` — магазин, и `Article` с
  датой публикации. Истёкшая сделка — `OutOfStock` или строка «срок действия этой
  скидки истек». Зачёркнутой цены нет ни в одной снятой карточке, карточка её не
  отдаёт. Ссылка на товар в магазине ведёт только через трекер `/visit/…`; по нему
  коннектор не ходит.
- **Нечёткий поиск.** Если ни одна сделка не называет слов запроса, коннектор отдаёт
  пустой список с `no_match`.
- **Отказы.** Капча во вкладке (`captcha` в пути адреса или форма SmartCaptcha) — стоп
  на 30 минут с `challenge_required`; капчу не проходим ни руками, ни кликом. JS-проверка
  `?_ycch=1` — не капча: коннектор ждёт, пока вкладка её пройдёт, и ставит тот же стоп,
  только если к дедлайну в 10 с адрес всё ещё с `_ycch`.
  401/403/418/429, уход вкладки с `pepper.ru` и страница без «Pepper» в заголовке и без
  сделок — тоже стоп. 404 карточки — `not_found` без стопа, прочие сбои —
  `transport_down` без стопа.

## Авито — одна навигация, стоп на первом отказе

Домашний IP получил флаг Авито ещё до плагина. Единственный проверочный запрос шага 7
(2026-09-27 23:14:20, вкладка профиля на выдачу `/moskva?q=rtx+5070`) ответил 429, так
что всё ниже проверено только офлайн.

- **Транспорт один: вкладка профиля.** HTTP-уровень через `curl_cffi`, `fetch` из
  прогретой вкладки и `RetryMiddleware` убраны: каждый из них после отказа делал второй
  запрос, а Авито банит именно за темп. Пауза между запросами — 5 с.
- **Поиск** открывает `/web/1/js/items?q=…&p=…&locationId=…` прямо во вкладке как
  документ, одной навигацией; JSON — это текст страницы (`body > pre` у Chrome). Разбор
  проверен на настоящем ответе `js/items` от 2026-08-29: объявления в `catalog.items[]`,
  цена в `priceDetailed.value`, ссылка в `urlPath`, рейтинг продавца в `rating`
  («721 отзыв»). Как Авито отвечает на открытие API документом, а не из скрипта
  страницы, не проверено.
- **Фильтры** уходят параметрами сайта: `pmin`, `pmax`, `s` (1 — дешевле, 2 — дороже,
  104 — по дате), `d=1` (с доставкой), `user=1|2` (частные или компании). Принимает ли их
  `js/items`, не проверено, поэтому границы цены и сортировку по цене коннектор
  применяет к ответу ещё раз сам и пишет `filters_not_verified_live`.
- **Карточка** — страница объявления, данные по `data-marker`
  (`item-view/title-info`, `item-view/item-price`, `item-view/item-params`,
  `item-view/item-description`, `seller-info/*`). Читатель проверен только на
  синтетической странице, отсюда предупреждение `card_not_verified_live`.
- **Имя продавца** не выходит ни из выдачи, ни из карточки: у частника это персональные
  данные. Остаются рейтинг, число отзывов, «компания или частное лицо» и год «На Авито с».
- **Отказы.** 401/403/418/429, уход вкладки с `avito.ru`, JSON без объявлений с
  `firewall` или `too-many-requests`, страница вместо JSON, «Доступ ограничен» и капча на
  странице объявления — стоп на 30 минут, без второго запроса. 404 карточки —
  `not_found` без стопа; пустой или обрезанный ответ и прочие сбои — `transport_down`
  без стопа.
- **`avito_evaluate`** не ходит в Авито повторно, если карточка уже вернула стоп: лот и
  цены похожих объявлений тогда можно передать аргументами.

