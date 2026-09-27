import sys, time, json, re
from curl_cffi import requests as cr
egress = sys.argv[1]; proxy = sys.argv[2] if len(sys.argv) > 2 else None
Q = "iphone%2015"
U = [
 ("wb_search_v9", f"https://search.wb.ru/exactmatch/ru/common/v9/search?appType=1&curr=rub&dest=-1257786&query={Q}&resultset=catalog&spp=30&page=1"),
 ("wb_usearch_v18", f"https://www.wildberries.ru/__internal/u-search/exactmatch/ru/common/v18/search?appType=1&curr=rub&dest=-1257786&query={Q}&resultset=catalog&page=1"),
 ("wb_card_v4", "https://card.wb.ru/cards/v4/detail?appType=1&curr=rub&dest=-1257786&spp=30&nm={NM}"),
 ("ozon_composer", "https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=%2Fsearch%2F%3Ftext%3Diphone%2B15"),
 ("ozon_entrypoint", "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url=%2Fsearch%2F%3Ftext%3Diphone%2B15"),
 ("ym_search", f"https://market.yandex.ru/search?text={Q}"),
 ("dns_search", "https://www.dns-shop.ru/search/?q=iphone"),
 ("citilink_search", "https://www.citilink.ru/search/?text=iphone"),
 ("avito_js_items", "https://www.avito.ru/web/1/js/items?q=iphone+15&p=1&locationId=637640&context=&updateListOnly=true"),
 ("megamarket", "https://megamarket.ru/catalog/?q=iphone"),
 ("mvideo", "https://www.mvideo.ru/search?q=iphone"),
 ("eldorado", "https://www.eldorado.ru/search/catalog.php?q=iphone"),
 ("onlinetrade", "https://www.onlinetrade.ru/sitesearch.html?query=iphone"),
 ("regard", "https://www.regard.ru/catalog?search=iphone"),
 ("nix", "https://www.nix.ru/"),
 ("holodilnik", "https://www.holodilnik.ru/search/?search=iphone"),
 ("vseinstrumenti", "https://www.vseinstrumenti.ru/search/?what=%D0%B4%D1%80%D0%B5%D0%BB%D1%8C"),
 ("detmir", "https://www.detmir.ru/"),
 ("lamoda", "https://www.lamoda.ru/catalogsearch/result/?q=%D0%BA%D1%80%D0%BE%D1%81%D1%81%D0%BE%D0%B2%D0%BA%D0%B8"),
 ("aliexpress", "https://aliexpress.ru/wholesale?SearchText=%D1%87%D0%B0%D1%81%D1%8B"),
 ("pepper", "https://www.pepper.ru/"),
]
MARK = ["showcaptcha","smartcaptcha","qrator","servicepipe","wbaas","Доступ ограничен","проблема с IP","__rr","ddos-guard","captcha","firewall","widgetStates","data-zone-data","catalog-product","Snippet__","data-marker"]
proxies = {"http": proxy, "https": proxy} if proxy else None
nm = "212343556"
out = []
for mode in ["chrome", None]:
  for name, url in U:
    if mode is None and name not in ("wb_search_v9","wb_card_v4","ozon_composer","avito_js_items","ym_search","citilink_search"): continue
    url = url.replace("{NM}", nm)
    t = time.time()
    try:
      r = cr.get(url, impersonate=mode, proxies=proxies, timeout=20, allow_redirects=False,
                 headers={"Accept-Language":"ru-RU,ru;q=0.9"})
      body = r.text or ""
      marks = [m for m in MARK if m.lower() in body.lower()]
      rec = dict(egress=egress, mode=mode or "plain", name=name, status=r.status_code, server=r.headers.get("server"),
                 loc=(r.headers.get("location") or "")[:90], ctype=(r.headers.get("content-type") or "")[:30],
                 len=len(body), json=body.lstrip()[:1] in "{[", marks=marks, ms=int((time.time()-t)*1000))
      if name == "wb_search_v9" and r.status_code == 200 and rec["json"]:
        try:
          d = r.json(); prods = d.get("products") or d.get("data",{}).get("products") or []
          if prods: nm = str(prods[0]["id"]); rec["nm"] = nm; rec["n"] = len(prods)
        except Exception as e: rec["err"] = str(e)[:60]
      if name == "wb_card_v4" and rec["json"]:
        try:
          d = r.json(); p = (d.get("products") or d.get("data",{}).get("products") or [])
          rec["n"] = len(p)
          if p: rec["price"] = p[0].get("sizes",[{}])[0].get("price")
        except Exception as e: rec["err"] = str(e)[:60]
      if name == "avito_js_items" and rec["json"]:
        try:
          d = r.json(); items = d.get("items") or (d.get("catalog") or {}).get("items") or []
          rec["n"] = len(items)
        except Exception as e: rec["err"] = str(e)[:60]
    except Exception as e:
      rec = dict(egress=egress, mode=mode or "plain", name=name, error=str(e)[:120])
    out.append(rec); print(json.dumps(rec, ensure_ascii=False), flush=True)
    time.sleep(2)
