#!/usr/bin/env python3
"""Deal Alert: watch public Telegram deal channels and forward only price-mistake / huge-discount
deals (Amazon, Flipkart, Myntra, mainly electronics) to the user's own Telegram bot.

Features
  - filters: price mistakes, very big discounts, electronics deals, user's watchlist
  - de-duplicates the same deal posted by several channels
  - bot commands: /add /remove /list /min /elec /pause /resume /top /help
  - loud phone alarm (ntfy, urgent priority) for price mistakes and watchlist hits
  - nightly top-10 summary at 21:00 IST

Usage:
  deals.py            one check
  deals.py --loop N   keep checking every 20 s for N minutes
  deals.py --test     print what would be sent from the current posts (sends nothing)
"""
import history
import datetime, html, json, os, re, sys, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")          # runtime state (Actions cache)
SETTINGS = os.path.join(HERE, "settings.json")    # user settings (committed to the repo)
CHANNELS = ["desidime", "dealsmagnet", "dealsheaven", "idoffers", "coolztricks", "coolzdeals",
            "GrabOnIndiaOfficial", "OfferLootApp", "ExtraPe", "pricehistoryapp", "desidime_deals"]
DEFAULTS = {"watch": [], "mute": [], "min_off": 90, "elec_off": 70, "paused": False}
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
SUMMARY_HOUR = 21

MISTAKE = re.compile(r"price\s*(error|mistake|glitch|bug)|\bglitch\b|pricing error|"
                     r"(?:at|@)\s*(?:₹|rs\.?)?\s*1(?![\d,.])", re.I)
LOOT = re.compile(r"\bloot\b", re.I)
SALE_BANNER = re.compile(r"\bup\s*to\b|\bupto\b|starts? at|starting", re.I)
ELECTRONICS = re.compile(r"phone|mobile|iphone|galaxy|redmi|poco|realme|oneplus|vivo|oppo|iqoo|pixel|motorola|"
                         r"laptop|macbook|ipad|tablet|monitor|\btv\b|television|qled|oled|led tv|earbud|earphone|"
                         r"headphone|neckband|tws|airpods|speaker|soundbar|smart ?watch|smartwatch|camera|gopro|"
                         r"\bssd\b|hard (disk|drive)|pendrive|pen drive|memory card|router|wifi|mesh|charger|"
                         r"power ?bank|keyboard|mouse|printer|projector|console|playstation|xbox|trimmer|"
                         r"refrigerator|fridge|washing machine|\bac\b|air conditioner|microwave|purifier|"
                         r"processor|graphics|gpu|\bram\b|electronics", re.I)
OTHER_STORE = re.compile(r"ajio|meesho|nykaa|tata ?cliq|croma|jiomart|shopsy|blinkit|zepto|instamart|swiggy|"
                         r"location specific", re.I)
STORES = (("amzn|amazon", "Amazon"), ("fkrt|fktr|flipkart", "Flipkart"), ("myntr", "Myntra"))
FILLER = set("off price error mistake loot deal deals now just mrp save buy read more link https http www com "
             "amazon flipkart myntra open grab only the and with for get flat apply coupon bank offer store "
             "posted limited time hurry checked available".split())


# ---------- parsing ----------

def money(s):
    return float(s.replace(",", ""))


def prices(text):
    return [money(p) for p in re.findall(r"(?:₹|rs\.?)\s*([\d,]+(?:\.\d+)?)", text, re.I) if p.strip(",")]


def discount(text):
    """Best guess of the discount % from '65% off' or price vs MRP."""
    best = 0.0
    for m in re.finditer(r"(\d{1,2})\s*%\s*off", text, re.I):
        best = max(best, float(m.group(1)))
    mrp = re.search(r"mrp\s*[:\-]?\s*(?:₹|rs\.?)\s*([\d,]+)", text, re.I)
    if mrp:
        m = money(mrp.group(1))
        lows = [p for p in prices(text) if 0 < p < m]
        if m > 0 and lows:
            best = max(best, 100 * (1 - min(lows) / m))
    return best


NOT_PRICE = re.compile(r"\b(coupon|code|cashback|save|off|cc|card|bank|emi|mrp|exchange|above|orders?|min)\b", re.I)
DEAL_PRICE = re.compile(r"(?:@|offer price|deal price|price|now(?: just)?|at just|just|✅|💰|💵|only|at)\s*[:\-]?\s*"
                        r"(?:₹|rs\.?|inr)?\s*([\d,]{2,9})(?:\.\d+)?(?!\s*%)", re.I)


def deal_price(text):
    """The selling price of the deal (not MRP, coupon or bank-offer amounts)."""
    for m in DEAL_PRICE.finditer(text):
        before = text[max(0, m.start() - 25):m.start()]
        after = text[m.end():m.end() + 12]
        if NOT_PRICE.search(before) or re.match(r"\s*(off|cashback|coupon|instant)", after, re.I):
            continue
        v = money(m.group(1))
        if v >= 1:
            return v
    good = []
    for m in re.finditer(r"(?:₹|rs\.?)\s*([\d,]+)", text, re.I):
        ctx = text[max(0, m.start() - 25):m.end() + 12]
        if not NOT_PRICE.search(ctx):
            good.append(money(m.group(1)))
    return min(good) if good else None


def store_of(text):
    return next((n for k, n in STORES if re.search(k, text, re.I)), "")


def first_link(text):
    m = re.search(r"https?://\S+", text)
    return m.group(0).rstrip(").,") if m else ""


def title_of(text):
    for line in text.splitlines():
        words = re.findall(r"[A-Za-z]{3,}", line)
        if len([w for w in words if w.lower() not in FILLER]) >= 3:
            return re.sub(r"^[^\w]+", "", line).strip()[:90]
    return text.strip().splitlines()[0][:90] if text.strip() else ""


def fingerprint(text):
    words = [w for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in FILLER][:4]
    p = prices(text)
    return " ".join(words) + "|" + (str(int(min(p))) if p else "")


def watch_hit(text, settings):
    low = text.lower()
    for w in settings["watch"]:
        if all(part in low for part in w["word"].lower().split()):
            p = prices(text)
            if w.get("max") and p and min(p) > w["max"]:
                continue
            return w["word"]
    return None


def muted(text, settings):
    low = text.lower()
    return any(m.lower() in low for m in settings.get("mute", []))


def classify(text, settings):
    """Return (reason, urgent) if the post should be sent, else (None, False)."""
    if OTHER_STORE.search(text) or muted(text, settings):
        return None, False
    hit = watch_hit(text, settings)
    if hit:
        return f"⭐ WATCHLIST: {hit}", True
    if MISTAKE.search(text):
        return "🚨 PRICE MISTAKE", True
    if SALE_BANNER.search(text):          # "up to 80% off" sale pages are not real steals
        return None, False
    d = discount(text)
    if d >= settings["min_off"] or (LOOT.search(text) and d >= min(80, settings["min_off"])):
        return f"🔥 {d:.0f}% off", False
    if ELECTRONICS.search(text) and d >= settings["elec_off"]:
        return f"⚡ Electronics {d:.0f}% off", False
    return None, False


# ---------- I/O ----------

def http(url, data=None, headers=None, timeout=25):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0 DealAlert", **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout).read()


def fetch(channel):
    page = http(f"https://t.me/s/{channel}").decode("utf-8", "replace")
    posts = []
    for block in page.split('data-post="')[1:]:
        pid = block.split('"', 1)[0]
        m = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', block, re.S)
        if not m or "/" not in pid:
            continue
        t = re.sub(r"<br\s*/?>", "\n", m.group(1))
        t = html.unescape(re.sub(r"<[^>]+>", "", t)).strip()
        posts.append((int(pid.split("/")[1]), t))
    return posts


def cfg():
    p = os.path.join(HERE, "telegram.json")
    c = json.load(open(p)) if os.path.exists(p) else {}
    return {"token": c.get("token") or os.environ["TELEGRAM_TOKEN"],
            "chat_id": str(c.get("chat_id") or os.environ["TELEGRAM_CHAT_ID"]),
            "ntfy": c.get("ntfy") or os.environ.get("NTFY_TOPIC", "")}


def tg(method, **params):
    c = cfg()
    r = json.loads(http(f"https://api.telegram.org/bot{c['token']}/{method}",
                        urllib.parse.urlencode(params).encode(), timeout=30))
    if not r.get("ok"):
        print("Telegram error:", r)
    return r


def send(text, buttons=None):
    p = {"chat_id": cfg()["chat_id"], "text": text[:3900]}
    if buttons:
        p["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    return tg("sendMessage", **p)


def send_photo(url, caption, buttons=None):
    """Photo (Keepa graph) with the alert as caption; long alerts go as a separate text."""
    p = {"chat_id": cfg()["chat_id"], "photo": url}
    if len(caption) <= 1000:
        p["caption"] = caption
        if buttons:
            p["reply_markup"] = json.dumps({"inline_keyboard": buttons})
        r = tg("sendPhoto", **p)
        return r if r.get("ok") else send(caption, buttons)
    tg("sendPhoto", **p)
    return send(caption, buttons)


def cached_history(state, asin, price):
    """Keepa graph analysis, cached for 6 h per product (the label is recomputed for the new price)."""
    cache = state.setdefault("hist", {})
    c = cache.get(asin)
    if not c or time.time() - c["t"] > 6 * 3600:
        try:
            h = history.analyse(history.fetch_graph(asin))
        except Exception as e:
            print("history failed:", asin, e)
            h = None
        c = {"t": time.time(), "h": h}
        cache[asin] = c
        for k in sorted(cache, key=lambda k: cache[k]["t"])[:-300]:
            del cache[k]
    if not c["h"]:
        return None
    return history.label(dict(c["h"]), price)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def dealsmagnet_store_link(page_url):
    """DealsMagnet hides the store link behind its Buy button (/buy?<data-code> -> 307 to the store)."""
    try:
        page = http(page_url, timeout=12).decode("utf-8", "replace")
        code = re.search(r'id="BuyNowButton"', page) and re.search(r'data-code="([^"]+)"[^>]*id="BuyNowButton"', page)
        if not code:
            return ""
        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request("https://www.dealsmagnet.com/buy?" + html.unescape(code.group(1)),
                                     headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14)"})
        try:
            opener.open(req, timeout=12)
        except urllib.error.HTTPError as e:
            return e.headers.get("Location", "")
    except Exception as e:
        print("dealsmagnet link failed:", e)
    return ""


def resolve(link):
    """Follow a short/affiliate link to the real store page. Returns (clean_url, store, asin)."""
    if not link:
        return "", "", ""
    try:
        req = urllib.request.Request(link, headers={"User-Agent": "Mozilla/5.0"})
        final = urllib.request.urlopen(req, timeout=8).geturl()
    except urllib.error.HTTPError as e:
        final = e.url or link
    except Exception:
        final = link
    if "dealsmagnet.com/deal/" in final:
        final = dealsmagnet_store_link(final) or final
    asin = re.search(r"amazon\.in/(?:.*?/)?(?:dp|gp/product)/([A-Z0-9]{10})", final)
    if asin:
        return f"https://www.amazon.in/dp/{asin.group(1)}", "Amazon", asin.group(1)
    fk = re.search(r"(https://www\.flipkart\.com/[^?]+)(?:\?.*?(pid=[A-Z0-9]+))?", final)
    if fk:
        return fk.group(1) + ("?" + fk.group(2) if fk.group(2) else ""), "Flipkart", ""
    my = re.search(r"https://www\.myntra\.com/[^?]+", final)
    if my:
        return my.group(0), "Myntra", ""
    host = urllib.parse.urlparse(final).netloc.lower()
    if OTHER_STORE.search(host):
        return final, "OTHER", ""
    return link, "", ""


def mute_word(text):
    for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]{3,}", title_of(text)):
        if w.lower() not in FILLER:
            return w
    return ""


def buttons_for(link, asin, text):
    row = []
    if link:
        row.append({"text": "🛒 Buy now", "url": link})
    if asin:
        row.append({"text": "📈 Price history", "url": f"https://keepa.com/#!product/10-{asin}"})
    rows = [row] if row else []
    mw = mute_word(text)
    if mw:
        rows.append([{"text": f"🔕 Mute \"{mw}\"", "callback_data": f"m:{mw}"[:60]},
                     {"text": "⭐ Watch", "callback_data": f"w:{mw}"[:60]}])
    return rows


def alarm(title, text, link):
    """Loud phone notification through ntfy (urgent priority rings even on silent-ish settings)."""
    topic = cfg()["ntfy"]
    if not topic:
        return
    try:
        h = {"Title": title.encode("utf-8").decode("latin-1", "ignore") or "Deal", "Priority": "urgent",
             "Tags": "rotating_light"}
        if link:
            h["Click"] = link
        http(f"https://ntfy.sh/{topic}", text[:500].encode("utf-8"), h)
    except Exception as e:
        print("ntfy failed:", e)


def load(path, default):
    try:
        return {**default, **json.load(open(path))}
    except Exception:
        return dict(default)


def save(path, obj):
    json.dump(obj, open(path, "w"), indent=1, ensure_ascii=False)


# ---------- bot commands ----------

HELP = ("Deal Alert commands:\n"
        "/add iphone 15 – watch a product (any discount)\n"
        "/add airpods 9000 – watch, only if price ≤ ₹9000\n"
        "/remove iphone 15 – stop watching\n"
        "/list – your watchlist and settings\n"
        "/min 85 – send any deal with at least 85% off (now {min_off}%)\n"
        "/elec 60 – electronics with at least 60% off (now {elec_off}%)\n"
        "/mute saree – hide deals with a word (or tap 🔕 under a deal)\n"
        "/unmute saree – show them again\n"
        "/pause, /resume – stop or restart alerts\n"
        "/top – today's top 10 deals now")


def handle_commands(state, settings):
    """Read new messages to the bot and apply commands. Returns True if settings changed."""
    try:
        r = tg("getUpdates", offset=state.get("offset", 0), timeout=0)
    except Exception as e:
        print("getUpdates failed:", e)
        return False
    changed = False
    me = cfg()["chat_id"]
    for u in r.get("result", []):
        state["offset"] = u["update_id"] + 1
        cb = u.get("callback_query")
        if cb and str(cb.get("from", {}).get("id")) == me:
            kind, _, word = (cb.get("data") or "").partition(":")
            if kind == "m" and word:
                if word.lower() not in [m.lower() for m in settings["mute"]]:
                    settings["mute"].append(word)
                note = f"🔕 \"{word}\" deals ika raavu. /unmute {word} tho malli on."
            elif kind == "w" and word:
                settings["watch"] = [w for w in settings["watch"] if w["word"].lower() != word.lower()]
                settings["watch"].append({"word": word, "max": None})
                note = f"⭐ Watching \"{word}\"."
            else:
                note = ""
            try:
                tg("answerCallbackQuery", callback_query_id=cb["id"], text=note[:190])
            except Exception:
                pass
            if note:
                send(note)
                changed = True
            continue
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != me:
            continue
        text = (msg.get("text") or "").strip()
        cmd, _, arg = text.partition(" ")
        cmd, arg = cmd.lower().split("@")[0], arg.strip()
        if cmd == "/add" and arg:
            m = re.match(r"(.*?)\s+(?:₹|rs\.?)?\s*(\d{3,7})$", arg, re.I)
            word, mx = (m.group(1), int(m.group(2))) if m else (arg, None)
            settings["watch"] = [w for w in settings["watch"] if w["word"].lower() != word.lower()]
            settings["watch"].append({"word": word, "max": mx})
            send(f"⭐ Watching \"{word}\"" + (f" up to ₹{mx}" if mx else "") + ". Loud alarm vastundi.")
            changed = True
        elif cmd == "/remove" and arg:
            before = len(settings["watch"])
            settings["watch"] = [w for w in settings["watch"] if w["word"].lower() != arg.lower()]
            send("Removed." if len(settings["watch"]) < before else f"\"{arg}\" watchlist lo ledu. /list chudandi.")
            changed = True
        elif cmd == "/mute" and arg:
            if arg.lower() not in [m.lower() for m in settings["mute"]]:
                settings["mute"].append(arg)
            send(f"🔕 \"{arg}\" unna deals ika raavu.")
            changed = True
        elif cmd == "/unmute" and arg:
            settings["mute"] = [m for m in settings["mute"] if m.lower() != arg.lower()]
            send(f"🔔 \"{arg}\" malli on.")
            changed = True
        elif cmd == "/list":
            wl = "\n".join(f"• {w['word']}" + (f" (≤ ₹{w['max']})" if w.get("max") else "")
                           for w in settings["watch"]) or "• (empty) – /add iphone"
            send(f"Watchlist:\n{wl}\n\nAny deal ≥ {settings['min_off']}% off\nElectronics ≥ {settings['elec_off']}% off\n"
                 f"Muted: {', '.join(settings['mute']) or '-'}\nPrice mistakes: always\nAlerts: {'PAUSED' if settings['paused'] else 'ON'}")
        elif cmd in ("/min", "/elec") and arg.rstrip("%").isdigit():
            key = "min_off" if cmd == "/min" else "elec_off"
            settings[key] = max(30, min(99, int(arg.rstrip("%"))))
            send(f"OK, {'any deal' if cmd == '/min' else 'electronics'} ≥ {settings[key]}% off.")
            changed = True
        elif cmd in ("/pause", "/resume"):
            settings["paused"] = cmd == "/pause"
            send("⏸ Alerts paused. /resume to restart." if settings["paused"] else "▶️ Alerts ON.")
            changed = True
        elif cmd == "/top":
            send_summary(state, now=True)
        elif cmd in ("/help", "/start"):
            send(HELP.format(**settings))
    return changed


# ---------- summary ----------

def remember(state, d, why, text, link=""):
    today = datetime.datetime.now(IST).strftime("%Y-%m-%d")
    day = state.setdefault("today", {"date": today, "deals": []})
    if day["date"] != today:
        state["today"] = day = {"date": today, "deals": []}
    score = d + (100 if "MISTAKE" in why else 0)
    day["deals"].append({"s": score, "d": round(d), "why": why, "t": title_of(text),
                         "p": int(min(prices(text))) if prices(text) else None,
                         "store": store_of(text), "link": link or first_link(text)})
    day["deals"] = sorted(day["deals"], key=lambda x: -x["s"])[:30]


def send_summary(state, now=False):
    day = state.get("today") or {}
    deals = day.get("deals") or []
    if not deals:
        if now:
            send("Ivala inka pedda deals raaledu.")
        return
    lines = []
    for i, x in enumerate(deals[:10], 1):
        tag = "🚨" if "MISTAKE" in x["why"] else ("⭐" if "WATCH" in x["why"] else f"{x['d']}%")
        price = f" ₹{x['p']}" if x["p"] else ""
        lines.append(f"{i}. {tag}{price} {x['t']}{' · ' + x['store'] if x['store'] else ''}\n{x['link']}")
    send(f"🏆 Today's top {len(lines)} deals ({day['date']})\n\n" + "\n\n".join(lines))


# ---------- main loop ----------

def check(state, settings, test=False):
    seen = state.setdefault("seen", [])
    for ch in CHANNELS:
        try:
            posts = fetch(ch)
        except Exception as e:
            print(ch, "fetch failed:", e)
            continue
        if not posts:
            continue
        last = state.get(ch)
        newest = max(p for p, _ in posts)
        if last is None and not test:          # first time: remember where we are, don't flood
            state[ch] = newest
            continue
        for pid, text in sorted(posts):
            if not test and pid <= last:
                continue
            why, urgent = classify(text, settings)
            amazonish = re.search(r"amzn|amazon", text, re.I)
            maybe = amazonish and not OTHER_STORE.search(text) and not muted(text, settings) and (
                discount(text) >= 40 or LOOT.search(text) or (ELECTRONICS.search(text) and discount(text) >= 25))
            if not why and not maybe:
                continue
            fp = fingerprint(text)
            if fp in seen:
                continue
            seen.append(fp)
            link, rstore, asin = resolve(first_link(text)) if not test else (first_link(text), "", "")
            if rstore == "OTHER":            # link really goes to Zepto/Ajio/etc.
                continue
            store = rstore or store_of(text)
            text = re.sub(r"\n{3,}", "\n\n", text)
            price = deal_price(text)
            hist, proof = None, ""
            if asin and price and not test:
                hist = cached_history(state, asin, price)
            if hist:
                watch = why and why.startswith("⭐")
                saving = hist["median"] - price
                real = hist["label"].startswith("🚨") or (hist["label"].startswith("✅") and (
                    hist["drop"] >= 30 or saving >= 1500 or (ELECTRONICS.search(text) and hist["drop"] >= 20)))
                if not real and not watch:
                    print("skip fake:", asin, price, hist["label"])
                    continue
                why = (why + "  ·  " + hist["label"]) if watch else hist["label"]
                urgent = urgent and watch or hist["label"].startswith("🚨")
                proof = (f"\n📊 90 rojula history: low ₹{hist['low']:,.0f} · usual ₹{hist['median']:,.0f} · "
                         f"high ₹{hist['high']:,.0f}\n💰 Ippudu ₹{price:,.0f}  ({hist['drop']:.0f}% usual kante takkuva)")
            elif not why:
                continue                       # Amazon candidate whose history we couldn't read
            elif store == "Amazon" and asin:
                why += "  ·  ⚠️ history check kaaledu"
            elif store != "Amazon":
                why += "  ·  ⚠️ history ledu (verify)"
            msg = (f"{why}{'  · ' + store if store else ''}{proof}\n\n{text}\n\n"
                   f"⏱ Fast ga open cheyyandi, ivi nimishallo ayipothayi.")
            print(msg[:200].replace("\n", " | "), "\n")
            if test:
                continue
            remember(state, discount(text), why, text, link)
            if not settings["paused"]:
                if hist:
                    send_photo(history.graph_url(asin), msg, buttons_for(link, asin, text))
                else:
                    send(msg, buttons_for(link, asin, text))
                if urgent:
                    alarm(f"{why} {store}".strip(), title_of(text), link)
                time.sleep(1)
        if not test:
            state[ch] = max(newest, last or 0)
    del seen[:-600]


def main():
    state = load(STATE, {})
    settings = load(SETTINGS, DEFAULTS)
    if "--test" in sys.argv:
        check(state, settings, test=True)
        return
    minutes = int(sys.argv[sys.argv.index("--loop") + 1]) if "--loop" in sys.argv else 0
    end = time.time() + minutes * 60
    while True:
        if handle_commands(state, settings):
            save(SETTINGS, settings)
        check(state, settings)
        now = datetime.datetime.now(IST)
        today = now.strftime("%Y-%m-%d")
        if now.hour >= SUMMARY_HOUR and state.get("summary_sent") != today:
            state["summary_sent"] = today
            if not settings["paused"]:
                send_summary(state)
        save(STATE, state)
        if time.time() + 20 > end:
            break
        time.sleep(20)


if __name__ == "__main__":
    main()
