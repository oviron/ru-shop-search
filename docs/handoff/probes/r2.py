import sys, time, re
from curl_cffi import requests as cr
proxy = sys.argv[1] if len(sys.argv) > 1 else None
px = {"http": proxy, "https": proxy} if proxy else None
H = {"Accept-Language": "ru-RU,ru;q=0.9"}
def show(tag, r):
    b = r.text or ""
    print(tag, r.status_code, r.headers.get("server"), len(b), "json" if b.lstrip()[:1] in "{[" else "", "| hist:", [ (h.status_code, (h.headers.get("location") or "")[-40:]) for h in (r.history or [])][:6])
    return b
s = cr.Session(impersonate="chrome", proxies=px)
# Ozon: follow the __rr chain with a cookie jar
r = s.get("https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=%2Fsearch%2F%3Ftext%3Diphone%2B15", headers=H, allow_redirects=True, max_redirects=6)
b = show("ozon_composer_follow", r); print("   ", b[:160].replace("\n"," "), "| cookies:", sorted(s.cookies.keys()))
time.sleep(2)
r = s.get("https://www.ozon.ru/", headers=H, allow_redirects=True, max_redirects=6)
b = show("ozon_main_follow", r); print("   ", re.sub(r"\s+"," ",b[:200]), "| title:", (re.search(r"<title>(.*?)</title>", b, re.S) or [None,None])[1])
time.sleep(2)
r = s.get("https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=%2Fsearch%2F%3Ftext%3Diphone%2B15", headers=H, allow_redirects=True, max_redirects=6)
b = show("ozon_composer_after_main", r); print("   ", b[:160].replace("\n"," "))
time.sleep(2)
# Avito HTML search
a = cr.Session(impersonate="chrome", proxies=px)
r = a.get("https://www.avito.ru/moskva?q=iphone+15", headers=H)
b = show("avito_html", r); print("    markers:", b.count('data-marker="item"'), "items;", "Доступ ограничен" in b, "__initialData__" in b, (re.search(r"<title>(.*?)</title>", b, re.S) or [None,None])[1])
time.sleep(2)
# M.Video
m = cr.Session(impersonate="chrome", proxies=px)
r = m.get("https://www.mvideo.ru/search?q=iphone", headers=H)
b = show("mvideo_page", r); print("    title:", (re.search(r"<title>(.*?)</title>", b, re.S) or [None,None])[1], "| captcha-ish:", [k for k in ("captcha","qrator","servicepipe","challenge","Please wait") if k in b][:5])
time.sleep(2)
r = m.get("https://www.mvideo.ru/bff/products/search?query=iphone&offset=0&limit=24", headers={**H, "Accept":"application/json"})
b = show("mvideo_bff_search", r); print("   ", b[:200].replace("\n"," "))
time.sleep(2)
# Yandex Market: real results?
y = cr.Session(impersonate="chrome", proxies=px)
r = y.get("https://market.yandex.ru/search?text=iphone%2015", headers=H)
b = show("ym_search", r); print("    showcaptcha:", "showcaptcha" in b, "| zone blobs:", b.count("data-zone-data"), "| iPhone mentions:", b.count("iPhone 15"), "| ₽:", b.count("₽"))
