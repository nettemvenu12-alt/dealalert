#!/usr/bin/env python3
"""Direct price tracking of the user's own products on Amazon.in and Flipkart.

Products are stored in settings["track"] (list of dicts). Each is checked straight on the store page
every few minutes; an alert goes out when the price drops, hits the user's target, is the lowest we've
seen, or the product comes back in stock.
"""
import html, json, re, time, urllib.parse, urllib.request

UA = ("Mozilla/5.0 (Linux; Android 14; 2311DRK48I) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Mobile Safari/537.36")
CHECK_EVERY = 5 * 60          # seconds between checks of the same product
MAX_ITEMS = 40


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.geturl(), r.read().decode("utf-8", "replace")


def normalise(url):
    """Short or long link -> (store, product id, clean url) or None."""
    try:
        final, _ = _get(url) if not re.search(r"amazon\.in/.*/?dp/|flipkart\.com/.*/p/", url) else (url, "")
    except Exception:
        final = url
    m = re.search(r"amazon\.in/(?:.*?/)?(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", final)
    if m:
        return "Amazon", m.group(1), f"https://www.amazon.in/dp/{m.group(1)}"
    m = re.search(r"(https://(?:www|dl)\.flipkart\.com/[^?#]*/p/(itm[0-9a-z]+))(?:[^#]*?[?&](pid=[A-Z0-9]+))?", final)
    if m:
        path = m.group(1).replace("://dl.", "://www.")
        return "Flipkart", m.group(2), path + (f"?{m.group(3)}" if m.group(3) else "")
    return None


def read_amazon(page):
    title = re.search(r"<title>(?:Buy )?([^<]*?)(?: Online at Low Prices in India)?\s*[-|:]\s*Amazon\.in", page)
    price = None
    m = re.search(r'"priceAmount":\s*([\d.]+)', page) or \
        re.search(r'id="twister-plus-price-data-price"\s+value="([\d.]+)"', page) or \
        re.search(r'priceToPay[^>]*>\s*₹\s*([\d,]+(?:\.\d+)?)', page)
    if m:
        price = float(m.group(1).replace(",", ""))
    oos = bool(re.search(r"Currently unavailable|out of stock", page, re.I)) and price is None
    return (html.unescape(title.group(1)).strip()[:80] if title else ""), price, oos


def read_flipkart(page):
    title, price, oos = "", None, False
    for m in re.finditer(r'application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            d = json.loads(m.group(1))
        except Exception:
            continue
        for x in d if isinstance(d, list) else [d]:
            if isinstance(x, dict) and x.get("offers"):
                title = title or x.get("name", "")
                o = x["offers"] if isinstance(x["offers"], dict) else x["offers"][0]
                if o.get("price"):
                    price = float(o["price"])
                oos = "OutOfStock" in str(o.get("availability", ""))
    if not title:
        t = re.search(r"<title>([^<]*?)(?: Price in India| - Buy|\|)", page)
        title = html.unescape(t.group(1)) if t else ""
    if re.search(r"Sold Out|Currently out of stock|Coming Soon", page):
        oos = True
    return title.strip()[:80], price, oos


def read(item):
    """Fetch the product page -> (title, price, out_of_stock). Raises on network errors."""
    _, page = _get(item["url"])
    return read_amazon(page) if item["store"] == "Amazon" else read_flipkart(page)


def add(settings, url, target=None):
    """Add a product. Returns a message for the user."""
    n = normalise(url)
    if not n:
        return ("Ee link Amazon.in / Flipkart product laa ledu. Myntra, Ajio GitHub nunchi block chestayi, "
                "avi deal channels lo vaste vastayi.")
    store, pid, clean = n
    track = settings.setdefault("track", [])
    for it in track:
        if it["id"] == pid:
            if target:
                it["target"] = target
            return f"Already tracking: {it['title'] or pid}" + (f"\n🎯 Target ₹{target:,.0f}" if target else "")
    if len(track) >= MAX_ITEMS:
        return f"Max {MAX_ITEMS} products. /untrack tho konni teeseyandi."
    item = {"store": store, "id": pid, "url": clean, "title": "", "target": target,
            "last": None, "low": None, "oos": False, "t": 0}
    try:
        title, price, oos = read(item)
    except Exception as e:
        title, price, oos = "", None, False
        print("track add read failed:", e)
    item.update(title=title, last=price, low=price, oos=oos, t=time.time())
    track.append(item)
    now = f"₹{price:,.0f}" if price else ("Out of stock" if oos else "price ippudu chadavalekapoyanu, malli try chestanu")
    return (f"👀 Tracking ({store}): {title or pid}\n💰 Ippudu: {now}" +
            (f"\n🎯 Target: ₹{target:,.0f}" if target else "") +
            "\nPrice padithe / stock vaste ventane cheptanu. /track tho list.")


def listing(settings):
    track = settings.get("track", [])
    if not track:
        return "Inka em track cheyyatledu. Amazon / Flipkart product link ikkada paste cheyyandi."
    lines = []
    for i, it in enumerate(track, 1):
        p = "out of stock" if it.get("oos") else (f"₹{it['last']:,.0f}" if it.get("last") else "?")
        low = f" · low ₹{it['low']:,.0f}" if it.get("low") else ""
        tgt = f" · 🎯 ₹{it['target']:,.0f}" if it.get("target") else ""
        lines.append(f"{i}. {it['title'][:45] or it['id']} ({it['store']}) – {p}{low}{tgt}")
    return ("Tracking:\n" + "\n".join(lines) +
            "\n\n/untrack 2 – remove · /target 2 15000 – alert at/below ₹15000")


def check(settings, notify, budget=6):
    """Check up to `budget` products that are due. notify(text, url, store, pid, urgent)."""
    changed = False
    due = sorted((it for it in settings.get("track", []) if time.time() - it.get("t", 0) >= CHECK_EVERY),
                 key=lambda it: it.get("t", 0))[:budget]
    for it in due:
        it["t"] = time.time()
        changed = True
        try:
            title, price, oos = read(it)
        except Exception as e:
            print("track read failed:", it["id"], e)
            continue
        if title and not it.get("title"):
            it["title"] = title
        name = it["title"] or it["id"]
        old, low = it.get("last"), it.get("low")
        if it.get("oos") and not oos and price:
            notify(f"📦 BACK IN STOCK · {it['store']}\n{name}\n💰 ₹{price:,.0f}", it, True)
        elif price and old and price < old:
            drop = 100 * (1 - price / old)
            hit = it.get("target") and price <= it["target"]
            new_low = low and price < low
            if hit or drop >= 5 or new_low:
                head = ("🎯 TARGET PRICE REACHED" if hit else
                        "🚨 HUGE DROP (maybe price mistake)" if drop >= 40 else
                        "📉 LOWEST PRICE SO FAR" if new_low else "📉 PRICE DROP")
                notify(f"{head} · {it['store']}\n{name}\n💰 ₹{old:,.0f} → ₹{price:,.0f}  (-{drop:.0f}%)"
                       + (f"\n🎯 Mee target ₹{it['target']:,.0f}" if it.get("target") else "")
                       + (f"\n⬇️ Mundu low ₹{low:,.0f}" if low else ""), it, bool(hit or drop >= 40))
        if price:
            it["last"] = price
            it["low"] = min(low or price, price)
        it["oos"] = oos
        time.sleep(1)
    return changed
