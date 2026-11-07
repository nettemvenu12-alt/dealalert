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
  deals.py --loop N   keep checking every ~10 s for N minutes
  deals.py --test     print what would be sent from the current posts (sends nothing)
"""
import history, tracker
import datetime, html, json, os, re, sys, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")          # runtime state (Actions cache)
SETTINGS = os.path.join(HERE, "settings.json")    # user settings (committed to the repo)
CHANNELS = ["desidime", "dealsmagnet", "dealsheaven", "idoffers", "coolztricks", "coolzdeals",
            "GrabOnIndiaOfficial", "OfferLootApp", "ExtraPe", "pricehistoryapp", "desidime_deals"]
DEFAULTS = {"level": "normal", "min_drop": 30, "watch": [], "mute": [], "track": [], "banks": ["HDFC", "ICICI", "Axis", "SBI", "IDFC", "Kotak"], "min_off": 90, "elec_off": 70, "paused": False}
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
SUMMARY_HOUR = 21

MISTAKE = re.compile(r"price\s*(error|mistake|glitch|bug)|\bglitch\b|pricing error|"
                     r"(?:at|@)\s*(?:₹|rs\.?)?\s*1(?![\d,.])", re.I)
LOOT = re.compile(r"\bloot\b", re.I)
SALE_BANNER = re.compile(r"\bup\s*to\b|\bupto\b|starts? at|starting", re.I)
ELECTRONICS = re.compile(r"phone|mobile|iphone|galaxy|redmi|poco|realme|oneplus|vivo|oppo|iqoo|pixel|motorola|"
                         r"laptop|macbook|ipad|tablet|monitor|\btv\b|television|qled|oled|led tv|earbud|earphone|"
                         r"headphone|neckband|tws|airpods|airdopes|\bbuds\b|earpods|speaker|soundbar|smart ?watch|smartwatch|camera|gopro|"
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


BANK = r"hdfc|icici|axis|sbi|kotak|idfc|bob|au|onecard|amex|federal|hsbc|rbl|yes|indusind|canara|pnb|citi"
BANK_FLAT = re.compile(r"(?:₹|rs\.?)?\s*([\d,]+(?:\.\d+)?k?)\s*(?:instant\s*)?(?:off|discount|cashback)\s*"
                       r"(?:with|on|using|via|for)?\s*((?:" + BANK + r")(?:\s*[/&,]\s*(?:" + BANK + r"))*)\b", re.I)
BANK_PCT = re.compile(r"(\d{1,2})\s*%\s*(?:instant\s*)?(?:off|discount|cashback)\s*(?:with|on|using|via)?\s*"
                      r"((?:" + BANK + r")(?:\s*[/&,]\s*(?:" + BANK + r"))*)\b", re.I)
COUPON = re.compile(r"(?:apply|use|clip)?\s*(?:₹|rs\.?)\s*([\d,]+)\s*(?:off\s*)?(?:cpn|coupon)|"
                    r"(\d[\d,]*k?)\s*(?:₹|rs\.?)?\s*(?:off\s*)?(?:cpn|coupon)|(?:cpn|coupon)\s*(?:of)?\s*(?:₹|rs\.?)\s*([\d,]+)", re.I)


def _amt(s):
    s = s.replace(",", "").lower()
    return float(s[:-1]) * 1000 if s.endswith("k") else float(s)


def final_price_line(text, price, banks):
    """'💳 ...' line with the price after coupon and the best bank offer the user has, or ''."""
    if not price or re.search(r"\bincl", text, re.I):     # bank offer already included in the shown price
        return ""
    coupon = 0.0
    for m in COUPON.finditer(text):
        v = next(g for g in m.groups() if g)
        if _amt(v) < price:
            coupon = max(coupon, _amt(v))
    mine = {b.lower() for b in banks}
    best, best_bank = 0.0, ""
    for m in BANK_FLAT.finditer(text):
        have = [b.upper() if len(b) <= 4 else b.title() for b in re.split(r"\s*[/&,]\s*", m.group(2)) if b.lower() in mine]
        if have and _amt(m.group(1)) < price and _amt(m.group(1)) > best:
            best, best_bank = _amt(m.group(1)), "/".join(have)
    for m in BANK_PCT.finditer(text):
        v = (price - coupon) * int(m.group(1)) / 100
        for b in re.split(r"\s*[/&,]\s*", m.group(2)):
            if b.lower() in mine and v > best:
                best, best_bank = v, b.upper() if len(b) <= 4 else b.title()
    if not coupon and not best:
        return ""
    parts = (["coupon"] if coupon else []) + ([f"{best_bank} card"] if best else [])
    return f"\n💳 {' + '.join(parts)} tho final ≈ ₹{max(price - coupon - best, 0):,.0f}"


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


def send(text, buttons=None, reply_to=None):
    p = {"chat_id": cfg()["chat_id"], "text": text[:3900]}
    if reply_to:
        p["reply_to_message_id"] = reply_to
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


STORE_URL = re.compile(r"https?://(?:www\.|dl\.|m\.)?(?:amazon\.in|flipkart\.com|myntra\.com|ajio\.com)/\S*", re.I)


def _hops(link, max_hops=7):
    """Follow redirects one by one; return every URL seen (affiliate trackers hide the store URL in a dl= param)."""
    seen, url = [link], link
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(max_hops):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/130 Mobile"})
            r = opener.open(req, timeout=8)
            body = r.read(200000).decode("utf-8", "replace")
            m = re.search(r"(?:location(?:\.href)?\s*=\s*|url=)[\"']?(https?://[^\"' ;>]+)", body, re.I)
            if m and m.group(1) not in seen:
                url = html.unescape(m.group(1))
                seen.append(url)
                continue
            break
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            if e.code in (301, 302, 303, 307, 308) and loc:
                url = urllib.parse.urljoin(url, loc)
                seen.append(url)
                continue
            break
        except Exception:
            break
    out = []
    for u in seen:
        out.append(u)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
        for key in ("dl", "url", "u", "redirect", "target", "ulp"):
            for v in q.get(key, []):
                if v.startswith("http"):
                    out.append(v)
    return out


def _clean(url):
    asin = re.search(r"amazon\.in/(?:.*?/)?(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", url)
    if asin:
        return f"https://www.amazon.in/dp/{asin.group(1)}", "Amazon", asin.group(1)
    fk = re.search(r"https://(?:www\.|dl\.)?flipkart\.com(/[^?#]*)(?:\?[^#]*?\b(pid=[A-Z0-9]+))?", url)
    if fk and ("/p/" in fk.group(1) or fk.group(2)):
        path = fk.group(1) if "/p/itm" in fk.group(1) else "/product/p/itm"
        return "https://www.flipkart.com" + path + ("?" + fk.group(2) if fk.group(2) else ""), "Flipkart", ""
    my = re.search(r"https://www\.myntra\.com/[^?#]+", url)
    if my:
        return my.group(0), "Myntra", ""
    aj = re.search(r"https://www\.ajio\.com/[^?#]+", url)
    if aj:
        return aj.group(0), "OTHER", ""
    if OTHER_STORE.search(urllib.parse.urlparse(url).netloc):
        return url, "OTHER", ""
    return None


def resolve(link):
    """Follow a short/affiliate link to the real store product page.
    Returns (clean_url, store, asin). Clean store URLs open straight in the Amazon / Flipkart app."""
    if not link:
        return "", "", ""
    for u in _hops(link):
        if "dealsmagnet.com/deal/" in u:
            u = dealsmagnet_store_link(u) or u
        c = _clean(u)
        if c:
            return c
    try:                                  # last try: let urllib follow everything
        req = urllib.request.Request(link, headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/130 Mobile"})
        c = _clean(urllib.request.urlopen(req, timeout=10).geturl())
        if c:
            return c
    except Exception:
        pass
    return link, "", ""


def best_link(text):
    """Resolve the post's links (Buy Now first) and return the first real store link."""
    links = [l.rstrip(").,") for l in re.findall(r"https?://\S+", text)]
    buy = re.search(r"buy\s*now\W{0,6}(https?://\S+)", text, re.I)
    if buy:
        links.insert(0, buy.group(1).rstrip(").,"))
    tried = []
    for l in links[:4]:
        if l in tried or "t.me/" in l or "whatsapp.com" in l:
            continue
        tried.append(l)
        r = resolve(l)
        if r[1]:
            return r
    return (links[0] if links else ""), "", ""


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
        "/drop 30 – usual price kante 30%+ takkuva unna anni deals\n"
        "/level best | normal | more – entha manchi deals kavali (Deal score 70+ / 55+ / 45+)\n"
        "/mute saree – hide deals with a word (or tap 🔕 under a deal)\n"
        "/unmute saree – show them again\n"
        "/pause, /resume – stop or restart alerts\n"
        "/top – today's top 10 deals now\n\n"
        "📦 Mee products direct ga track: Amazon/Flipkart product link ikkada paste cheyyandi "
        "(link tarvata price raste target, e.g. 'link 15000')\n"
        "/track – tracked list · /untrack 2 · /target 2 15000\n"
        "/banks HDFC SBI – mee cards (final price lekka kosam)")


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
        link = re.search(r"https?://\S+", text)
        if link and not cmd.startswith("/"):
            tm = re.search(r"(?:₹|rs\.?)?\s*(\d{3,7})\s*$", text[link.end():], re.I)
            send(tracker.add(settings, link.group(0), float(tm.group(1)) if tm else None))
            changed = True
            continue
        if cmd == "/track":
            send(tracker.listing(settings))
        elif cmd == "/untrack" and arg.isdigit():
            tr = settings.get("track", [])
            if 1 <= int(arg) <= len(tr):
                it = tr.pop(int(arg) - 1)
                send(f"Removed: {it['title'] or it['id']}")
                changed = True
            else:
                send("Number tappu. /track chudandi.")
        elif cmd == "/target":
            m = re.match(r"(\d+)\s+(?:₹|rs\.?)?\s*([\d,]+)", arg, re.I)
            tr = settings.get("track", [])
            if m and 1 <= int(m.group(1)) <= len(tr):
                tr[int(m.group(1)) - 1]["target"] = money(m.group(2))
                send(f"🎯 Target set: ₹{money(m.group(2)):,.0f}")
                changed = True
            else:
                send("Ila: /target 2 15000")
        elif cmd == "/drop" and arg.rstrip("%").isdigit():
            settings["min_drop"] = max(10, min(90, int(arg.rstrip("%"))))
            send(f"OK: usual price kante {settings['min_drop']}%+ takkuva unna anni deals vastayi (plus best score deals).")
            changed = True
        elif cmd == "/level" and arg.lower() in LEVELS:
            settings["level"] = arg.lower()
            send({"best": "🏆 BEST only – score 70+ (takkuva alerts, top deals matrame)",
                  "normal": "👍 NORMAL – score 55+ (manchi deals anni)",
                  "more": "📬 MORE – score 45+ (ekkuva alerts)"}[arg.lower()])
            changed = True
        elif cmd == "/banks" and arg:
            settings["banks"] = [b.strip() for b in re.split(r"[ ,]+", arg) if b.strip()]
            send("💳 Mee cards: " + ", ".join(settings["banks"]))
            changed = True
        elif cmd == "/add" and arg:
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
            send(f"Watchlist:\n{wl}\n\nLevel: {settings.get('level', 'normal')} (Deal score {LEVELS.get(settings.get('level', 'normal'), 55)}+)\n"
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

def stat(state, key, n=1):
    today = datetime.datetime.now(IST).strftime("%Y-%m-%d")
    st = state.setdefault("stats", {})
    if st.get("date") != today:
        st.clear()
        st["date"] = today
    st[key] = st.get(key, 0) + n


def remember(state, d, why, text, link=""):
    today = datetime.datetime.now(IST).strftime("%Y-%m-%d")
    day = state.setdefault("today", {"date": today, "deals": []})
    if day["date"] != today:
        state["today"] = day = {"date": today, "deals": []}
    score = d
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
        tag = "🚨" if "MISTAKE" in x["why"] else ("⭐" if "WATCH" in x["why"] else f"[{x['d']}/100]")
        price = f" ₹{x['p']}" if x["p"] else ""
        lines.append(f"{i}. {tag}{price} {x['t']}{' · ' + x['store'] if x['store'] else ''}\n{x['link']}")
    st = state.get("stats", {})
    footer = (f"\n\n📈 Ivala: {st.get('posts', 0)} posts scan chesanu · {st.get('sent', 0)} alerts pampanu "
              f"({st.get('verified', 0)} history tho verify) · {st.get('fake', 0)} fake discounts, "
              f"{st.get('expired', 0)} expired deals skip chesanu.")
    send(f"🏆 Today's top {len(lines)} deals ({day['date']})\n\n" + "\n\n".join(lines) + footer)


# ---------- main loop ----------

GOLD = re.compile(r"\bgold\b|\bsilver\b|24\s*k|22\s*k|\b999\b|mmtc|augmont|tanishq|kalyan|malabar|caratlane|"
                  r"joyalukkas|senco|bullion|\bcoin\b", re.I)
HOME = re.compile(r"mixer|grinder|juicer|cooker|kettle|\biron\b|\bfan\b|cooler|vacuum|chimney|induction|"
                  r"purifier|geyser|heater|bedsheet|mattress|pillow|curtain|cookware|kadai|tawa|pan\b|"
                  r"dinner set|bottle|flask|storage|container|bulb|led\b|inverter|battery|tools?\b|drill|"
                  r"sofa|chair|table|bed\b|wardrobe|shoe rack|detergent|cleaner|towel|bucket|appliance", re.I)
FASHION = re.compile(r"shoe|sneaker|t-?shirt|shirt|jeans|kurta|saree|dress|watch\b|bag\b|wallet|sunglass|"
                     r"jacket|trouser|sandal|slipper|perfume", re.I)
LEVELS = {"best": 70, "normal": 55, "more": 45}


def category(text):
    if GOLD.search(text) and re.search(r"gold|silver|coin|jewel", text, re.I):
        return "🪙 Gold/Silver"
    if ELECTRONICS.search(text):
        return "📱 Electronics"
    if FASHION.search(text):
        return "👟 Fashion"
    if HOME.search(text):
        return "🏠 Home"
    return "🛍 Other"


def score_deal(text, price, hist, cat, final_line):
    """0-100 'how good is this deal'. Uses real price history when we have it, else the post's own claims."""
    bank = bool(final_line)
    if hist:
        drop, med, low = hist["drop"], hist["median"], hist["low"]
        saving = med - (price or med)
        if drop >= 60 and saving >= 150:
            return 100, "🚨 REAL PRICE MISTAKE"
        sc = drop * 1.4
        at_low = price and price <= low * 1.03
        if at_low and drop >= 5:
            sc += 15
        sc += min(15, max(saving, 0) / 400)
        if cat == "📱 Electronics" or cat == "🏠 Home":
            sc += 8
        if cat == "🪙 Gold/Silver":                 # gold rarely drops much: any real drop or bank offer is good
            sc += 25 + (15 if bank else 0)
        if saving < 150 and cat != "🪙 Gold/Silver":
            sc = min(sc - 30, 50)                   # cheap stuff, tiny saving: never a top deal
        if drop < 3 and not (cat == "🪙 Gold/Silver" and bank):
            return min(sc, 20), "❌ Fake discount (usual price)"
        label = ("✅ LOWEST PRICE in 90 days" if at_low else "✅ REAL PRICE DROP")
        return max(0, min(99, sc)), label
    # no history (Flipkart / Myntra / unknown): trust the post less
    if MISTAKE.search(text):
        return 95, "🚨 PRICE MISTAKE (verify)"
    if SALE_BANNER.search(text) and not price:
        return 0, ""
    d = discount(text)
    sc = d * 0.8
    if LOOT.search(text):
        sc += 10
    if cat in ("📱 Electronics", "🏠 Home"):
        sc += 10
    if cat == "🪙 Gold/Silver":
        sc += 30 + (20 if bank else 0)
    if bank:
        sc += 5
    p = price or 0
    mrp = re.search(r"mrp\s*[:\-]?\s*(?:₹|rs\.?)\s*([\d,]+)", text, re.I)
    if p and mrp and money(mrp.group(1)) - p < 150 and cat != "🪙 Gold/Silver":
        sc -= 30
    return max(0, min(94, sc)), f"🔥 {d:.0f}% off (history ledu)" if d else "🔥 Deal (history ledu)"


def _priority(text, settings):
    if MISTAKE.search(text) or watch_hit(text, settings):
        return 0
    if LOOT.search(text) or discount(text) >= 70:
        return 1
    return 2


def check(state, settings, test=False):
    """Read all channels at once, then handle new posts – price mistakes and watchlist hits first."""
    from concurrent.futures import ThreadPoolExecutor
    seen = state.setdefault("seen", [])
    need = LEVELS.get(settings.get("level", "normal"), 55)
    with ThreadPoolExecutor(len(CHANNELS)) as ex:
        futs = {ch: ex.submit(fetch, ch) for ch in CHANNELS}
    queue = []
    for ch, f in futs.items():
        try:
            posts = f.result()
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
            if test or pid > last:
                queue.append((_priority(text, settings), ch, pid, text))
        if not test:
            state[ch] = max(newest, last or 0)
    queue.sort(key=lambda q: q[0])
    for prio, ch, pid, text in queue:
        if not test:
            stat(state, "posts")
        if OTHER_STORE.search(text) or muted(text, settings):
            continue
        if SALE_BANNER.search(text) and not deal_price(text) and not MISTAKE.search(text):
            continue                       # "sale starts at / up to 80% off" banners
        watch = watch_hit(text, settings)
        fp = fingerprint(text)
        if fp in seen:
            continue
        seen.append(fp)
        # same product from another channel (titles differ): skip unless it's clearly cheaper
        key = " ".join([w for w in re.findall(r"[a-z0-9]{2,}", title_of(text).lower()) if w not in FILLER][:2])
        dp = deal_price(text) or 0
        prev = state.setdefault("recent", {}).get(key)
        if key and prev and dp and dp >= 0.95 * prev["p"] and time.time() - prev["t"] < 12 * 3600:
            continue
        if key and dp:
            state["recent"][key] = {"p": dp, "t": time.time()}
        link, rstore, asin = best_link(text) if not test else (first_link(text), "", "")
        if rstore == "OTHER":            # link really goes to Zepto/Ajio/Blinkit etc.
            continue
        store = rstore or store_of(text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        price = deal_price(text)
        hist, proof = None, ""
        fast_mid = None
        if MISTAKE.search(text) and not test and not settings["paused"]:
            # price mistakes vanish in minutes: send right away, verify after and reply with the result
            r = send(f"🚨⚡ PRICE MISTAKE – FAST ALERT · {store or '?'}\n"
                     f"(ippude post ayyindi – live check chesi 10 sec lo confirm chestanu)\n\n{text}",
                     buttons_for(link, asin, text))
            fast_mid = (r.get("result") or {}).get("message_id")
            alarm(f"PRICE MISTAKE {store}".strip(), title_of(text), link)
        if store in ("Amazon", "Flipkart") and link and not test:
            live = None
            try:
                _, live, oos = tracker.read({"store": store, "url": link})
                if oos:
                    stat(state, "expired")
                    if fast_mid:
                        send("❌ Out of stock ayipoyindi.", reply_to=fast_mid)
                    continue
            except Exception as e:
                print("live check failed:", e)
            if live and price and live > price * 1.15:
                print("skip expired:", link, price, live)
                stat(state, "expired")
                if fast_mid:
                    send(f"❌ Ayipoyindi – ippudu live price ₹{live:,.0f}.", reply_to=fast_mid)
                continue
            if live and (not price or live < price):
                price = live
            if live:
                proof += f"\n🟢 Live ippudu: ₹{live:,.0f} ({store} lo direct check chesanu)"
        if asin and price and not test:
            hist = cached_history(state, asin, price)
        elif store == "Flipkart" and price and link and not test:
            import extras
            hist = extras.fk_history(link, price)
            extras.fk_record(link, price, title_of(text))
        cat = category(text)
        fline = final_price_line(text, price, settings.get("banks", []))
        sc, label = score_deal(text, price, hist, cat, fline)
        if watch:
            label = f"⭐ WATCHLIST: {watch}  ·  {label}"
        drop_ok = (hist and hist["drop"] >= settings.get("min_drop", 30) and hist["median"] - price >= 150) or \
                  (not hist and discount(text) >= settings.get("min_drop", 30) + 20 and not label.startswith("❌"))
        if sc < need and not watch and not drop_ok:
            if hist:
                stat(state, "fake")
            print(f"skip score {sc:.0f}: {title_of(text)[:50]}")
            if fast_mid:
                send(f"⚠️ Check chesanu: {label} (score {sc:.0f}/100) – nijam mistake laa ledu.", reply_to=fast_mid)
            continue
        urgent = sc >= 95 or bool(watch)
        if hist:
            src = "90 rojula" if asin else f"{hist.get('points', 0)} rojula (mana sontha Flipkart)"
            proof += (f"\n📊 {src} history: low ₹{hist['low']:,.0f} · usual ₹{hist['median']:,.0f} · "
                      f"high ₹{hist['high']:,.0f}\n💰 Ippudu ₹{price:,.0f}  ({hist['drop']:.0f}% usual kante takkuva)")
        proof += fline
        stars = "🟩" * round(sc / 20) + "⬜" * (5 - round(sc / 20))
        msg = (f"{label}  · {store or '?'}\n{cat}  ·  Deal score {sc:.0f}/100 {stars}{proof}\n\n{text}\n\n"
               f"⏱ Fast ga open cheyyandi, ivi nimishallo ayipothayi.")
        print(msg[:220].replace("\n", " | "), "\n")
        if test:
            continue
        remember(state, sc, label, text, link)
        stat(state, "sent")
        if hist:
            stat(state, "verified")
        if fast_mid:
            send(f"✅ VERIFIED: {label} · score {sc:.0f}/100{proof}", buttons_for(link, asin, text), reply_to=fast_mid)
        elif not settings["paused"]:
            if hist and asin:
                send_photo(history.graph_url(asin), msg, buttons_for(link, asin, text))
            else:
                send(msg, buttons_for(link, asin, text))
            if urgent:
                alarm(f"{label} {store}".strip()[:80], title_of(text), link)
            time.sleep(1)
    del seen[:-600]
    rec = state.get("recent", {})
    for k in [k for k, v in rec.items() if time.time() - v["t"] > 12 * 3600]:
        del rec[k]


def track_alert(text, it, urgent, settings):
    if settings.get("paused"):
        return
    btn = [[{"text": "🛒 Buy now", "url": it["url"]}]]
    if it["store"] == "Amazon":
        btn[0].append({"text": "📈 Price history", "url": f"https://keepa.com/#!product/10-{it['id']}"})
        send_photo(history.graph_url(it["id"]), text, btn)
    else:
        send(text + "\n" + it["url"], btn)
    if urgent:
        alarm(text.splitlines()[0], text.splitlines()[1] if "\n" in text else text, it["url"])


def main():
    state = load(STATE, {})
    settings = load(SETTINGS, DEFAULTS)
    if "--test" in sys.argv:
        check(state, settings, test=True)
        return
    try:                                   # self-test: can we still read Keepa price graphs?
        print("history self-test:", history.analyse(history.fetch_graph("B0CTZX85DY")), flush=True)
    except Exception as e:
        print("history self-test failed:", e, flush=True)
    minutes = int(sys.argv[sys.argv.index("--loop") + 1]) if "--loop" in sys.argv else 0
    end = time.time() + minutes * 60
    while True:
        if handle_commands(state, settings):
            save(SETTINGS, settings)
        check(state, settings)
        import extras
        for fn in (lambda: extras.amazon_scan(state, settings), lambda: extras.fk_daily_refresh(state),
                   lambda: extras.weekly_advice(state, settings)):
            try:
                fn()
            except Exception as e:
                print("extra failed:", e, flush=True)
        for it in settings.get("track", []):
            if it["store"] == "Flipkart" and it.get("last"):
                extras.fk_record(it["url"], it["last"], it.get("title", ""))
        if settings.get("track") and tracker.check(settings, lambda t, it, urgent: track_alert(t, it, urgent, settings)):
            save(SETTINGS, settings)
        now = datetime.datetime.now(IST)
        today = now.strftime("%Y-%m-%d")
        if now.hour >= SUMMARY_HOUR and state.get("summary_sent") != today:
            state["summary_sent"] = today
            if not settings["paused"]:
                send_summary(state)
        save(STATE, state)
        if time.time() + 8 > end:
            break
        time.sleep(8)


if __name__ == "__main__":
    main()
