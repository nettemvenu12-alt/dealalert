#!/usr/bin/env python3
"""Extra deal sources and advice, used by deals.py:

  amazon_scan()     reads Amazon's own Deals page and deal searches directly (slowly, Amazon throttles)
  fk_record/fk_history   our own Flipkart price history (Flipkart has no free history source)
  fk_daily_refresh()     re-reads known Flipkart products once a day so their history grows
  weekly_advice()   Sunday "buy now or wait?" for the user's tracked products
"""
import datetime, html, json, os, re, statistics, time

import deals, history, tracker

FK_FILE = os.path.join(deals.HERE, "fk_history.json")     # saved in the Actions cache with state.json
AMZ_SOURCES = [
    "https://www.amazon.in/deals",
    "https://www.amazon.in/s?i=electronics&rh=p_n_deal_type%3A26921224031",
    "https://www.amazon.in/deals",
    "https://www.amazon.in/s?i=kitchen&rh=p_n_deal_type%3A26921224031",
    "https://www.amazon.in/deals",
    "https://www.amazon.in/s?i=computers&rh=p_n_deal_type%3A26921224031",
    "https://www.amazon.in/deals",
    "https://www.amazon.in/s?i=jewelry&rh=p_n_deal_type%3A26921224031",
    "https://www.amazon.in/deals",
    "https://www.amazon.in/s?i=appliances&rh=p_n_deal_type%3A26921224031",
    "https://www.amazon.in/deals",
    "https://www.amazon.in/s?i=home-improvement&rh=p_n_deal_type%3A26921224031",
]
AMZ_EVERY = 6 * 60            # one Amazon listing page every 6 minutes
AMZ_MAX_CHECKS = 20           # history checks per page
IST = deals.IST


# ---------------- Amazon deals page ----------------

def _cards(page):
    """-> {asin: (price, pct_off, title)} from a deals page or a search results page."""
    out = {}
    for marker in ('data-csa-c-item-id="amzn1.asin.', 'data-asin="'):
        for c in page.split(marker)[1:]:
            asin = c[:10]
            if not re.match(r"B0[A-Z0-9]{8}$", asin) or asin in out:
                continue
            body = c[:7000]
            price = re.search(r'(?:a-offscreen|a-price-whole)">\s*₹?\s*([\d,]+)', body)
            off = re.search(r"(\d{1,2})%\s*off", body)
            title = (re.search(r'<h2[^>]*>.*?<span[^>]*>([^<]{10,200})</span>', body, re.S) or
                     re.search(r'(?:alt|title)="([^"]{15,200})"', body) or
                     re.search(r'href="/([^/"]{10,150})/dp/', body))
            if not price:
                continue
            t = html.unescape(title.group(1)).replace("-", " ") if title else asin
            out[asin] = (float(price.group(1).replace(",", "")), int(off.group(1)) if off else 0, t.strip()[:120])
    return out


def amazon_scan(state, settings):
    now = time.time()
    if now - state.get("amz_t", 0) < AMZ_EVERY:
        return
    state["amz_t"] = now
    i = state.get("amz_i", 0) % len(AMZ_SOURCES)
    state["amz_i"] = i + 1
    url = AMZ_SOURCES[i]
    try:
        _, page = tracker._get(url, timeout=25)
    except Exception as e:
        print("amazon page failed:", url, e)
        return
    cards = _cards(page)
    print(f"amazon scan {url}: {len(cards)} products", flush=True)
    if not cards:
        return
    seen = state.setdefault("amz_seen", {})
    need = deals.LEVELS.get(settings.get("level", "normal"), 55)
    checks = 0
    for asin, (price, off, title) in cards.items():
        if now - seen.get(asin, 0) < 12 * 3600:
            continue
        if checks >= AMZ_MAX_CHECKS:
            break
        seen[asin] = now
        text = f"{title}\n₹{price:,.0f}" + (f" ({off}% off)" if off else "")
        if deals.OTHER_STORE.search(title) or deals.muted(text, settings):
            continue
        checks += 1
        hist = deals.cached_history(state, asin, price)
        if not hist:
            continue
        cat = deals.category(title)
        sc, label = deals.score_deal(text, price, hist, cat, "")
        watch = deals.watch_hit(text, settings)
        saving = hist["median"] - price
        drop_ok = hist["drop"] >= settings.get("min_drop", 30) and saving >= 150
        if sc < need and not drop_ok and not watch:
            continue
        key = " ".join([w for w in re.findall(r"[a-z0-9]{2,}", title.lower()) if w not in deals.FILLER][:2])
        prev = state.setdefault("recent", {}).get(key)
        if key and prev and price >= 0.95 * prev["p"] and now - prev["t"] < 12 * 3600:
            continue
        state["recent"][key] = {"p": price, "t": now}
        if watch:
            label = f"⭐ WATCHLIST: {watch}  ·  {label}"
        link = f"https://www.amazon.in/dp/{asin}"
        stars = "🟩" * round(sc / 20) + "⬜" * (5 - round(sc / 20))
        msg = (f"{label}  · Amazon Deals page\n{cat}  ·  Deal score {sc:.0f}/100 {stars}\n"
               f"📊 90 rojula history: low ₹{hist['low']:,.0f} · usual ₹{hist['median']:,.0f} · high ₹{hist['high']:,.0f}\n"
               f"💰 Ippudu ₹{price:,.0f}  ({hist['drop']:.0f}% usual kante takkuva)\n\n{title}\n\n"
               f"⏱ Amazon lo direct ga dorikindi – fast ga open cheyyandi.")
        deals.remember(state, sc, label, msg, link)
        deals.stat(state, "sent")
        deals.stat(state, "verified")
        if not settings.get("paused"):
            deals.send_photo(history.graph_url(asin), msg, deals.buttons_for(link, asin, title))
            if sc >= 95 or watch:
                deals.alarm(f"{label} Amazon"[:80], title, link)
            time.sleep(1)
    for k in [k for k, t in seen.items() if now - t > 2 * 86400]:
        del seen[k]


# ---------------- our own Flipkart price history ----------------

def _fk_load():
    try:
        return json.load(open(FK_FILE))
    except Exception:
        return {}


def _fk_save(db):
    json.dump(db, open(FK_FILE, "w"), separators=(",", ":"))


def fk_id(url):
    m = re.search(r"pid=([A-Z0-9]+)", url) or re.search(r"/p/(itm[0-9a-z]+)", url)
    return m.group(1) if m else ""


def fk_record(url, price, title=""):
    """Remember today's lowest price of a Flipkart product."""
    pid = fk_id(url)
    if not pid or not price:
        return
    db = _fk_load()
    day = datetime.datetime.now(IST).strftime("%Y-%m-%d")
    it = db.setdefault(pid, {"u": url, "t": title[:80], "p": {}})
    if title and not it.get("t"):
        it["t"] = title[:80]
    it["p"][day] = min(price, it["p"].get(day, price))
    cutoff = (datetime.datetime.now(IST) - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
    it["p"] = {d: v for d, v in it["p"].items() if d >= cutoff}
    if len(db) > 3000:                     # drop the products we know least about
        for k in sorted(db, key=lambda k: len(db[k]["p"]))[:len(db) - 3000]:
            del db[k]
    _fk_save(db)


def fk_history(url, price):
    """History numbers + label like history.label(), once we have at least 5 days of prices."""
    it = _fk_load().get(fk_id(url))
    if not it:
        return None
    today = datetime.datetime.now(IST).strftime("%Y-%m-%d")
    past = [v for d, v in it["p"].items() if d != today]
    if len(past) < 5:
        return None
    h = {"low": min(past), "median": statistics.median(past), "high": max(past), "points": len(past)}
    return history.label(h, price)


def fk_daily_refresh(state, budget=4):
    """A few Flipkart products per loop, each at most once a day, so history keeps growing."""
    db = _fk_load()
    day = datetime.datetime.now(IST).strftime("%Y-%m-%d")
    todo = [k for k, v in db.items() if day not in v["p"]][:budget]
    for pid in todo:
        try:
            title, price, oos = tracker.read({"store": "Flipkart", "url": db[pid]["u"]})
        except Exception:
            continue
        if price and not oos:
            fk_record(db[pid]["u"], price, title)
        time.sleep(1)


# ---------------- weekly buy-or-wait advice ----------------

def weekly_advice(state, settings):
    now = datetime.datetime.now(IST)
    week = now.strftime("%G-W%V")
    if now.weekday() != 6 or now.hour < 10 or state.get("advice_week") == week:
        return
    state["advice_week"] = week
    track = settings.get("track", [])
    if not track or settings.get("paused"):
        return
    lines = []
    for i, it in enumerate(track, 1):
        price = it.get("last")
        name = (it.get("title") or it["id"])[:50]
        if not price:
            continue
        h = None
        if it["store"] == "Amazon":
            try:
                raw = history.analyse(history.fetch_graph(it["id"]))
                h = history.label(raw, price) if raw else None
            except Exception:
                h = None
        else:
            h = fk_history(it["url"], price)
        if not h:
            lines.append(f"{i}. {name}\n   ₹{price:,.0f} – history inka saripoledu, wait chesi chuddam")
            continue
        if price <= h["low"] * 1.03:
            adv = "✅ IPPUDE KONANDI – 90 rojula lowest"
        elif h["drop"] >= 10:
            adv = "👍 Manchi time – usual kante takkuva"
        elif price >= h["median"] * 1.05:
            adv = "⏳ WAIT – ippudu usual kante ekkuva"
        else:
            adv = "😐 Normal price – sale kosam wait cheyyochu"
        tgt = f" · 🎯 ₹{it['target']:,.0f}" if it.get("target") else ""
        lines.append(f"{i}. {name}\n   ₹{price:,.0f} (low ₹{h['low']:,.0f} · usual ₹{h['median']:,.0f}){tgt}\n   {adv}")
    if lines:
        deals.send("🗓 Weekly: konaala, wait cheyyala?\n\n" + "\n\n".join(lines))
