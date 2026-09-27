import sys, os, time
from curl_cffi import requests as cr
tok, uid = os.environ["T"], os.environ["U"]
UA_BR = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Claude/2.9939.2 Chrome/152.0.7977.130 Safari/537.36"
url = "https://www.wildberries.ru/__internal/u-search/exactmatch/ru/common/v18/search?appType=1&curr=rub&dest=-1257786&lang=ru&locale=ru&query=iphone+15&resultset=catalog&sort=popular&spp=30"
cases = [("home, UA браузера", None, UA_BR), ("home, UA по умолчанию curl_cffi", None, None), ("vps, UA браузера", "socks5://127.0.0.1:18080", UA_BR), ("home, без токена", None, UA_BR)]
for name, px, ua in cases:
    h = {"deviceid": "site_" + __import__("uuid").uuid4().hex, "Accept": "*/*", "Accept-Language": "ru-RU,ru;q=0.9", "Referer": "https://www.wildberries.ru/catalog/0/search.aspx?search=iphone%2015"}
    if ua: h["User-Agent"] = ua
    r = cr.get(url, impersonate="chrome", headers=h, cookies=({} if "без токена" in name else {"x_wbaas_token": tok, "_wbauid": uid}), proxies={"https": px} if px else None, timeout=20)
    n = None
    try: n = len(r.json().get("products", []))
    except Exception: pass
    print(f"{name}: {r.status_code} products={n}")
    time.sleep(2)
