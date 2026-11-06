#!/usr/bin/env python3
"""Deal Alert: watch public Telegram deal channels and forward only price-mistake / huge-discount
deals (Amazon, Flipkart, Myntra, mainly electronics) to the user's own Telegram bot.

Usage:
  deals.py            one check
  deals.py --loop N   keep checking every 20 s for N minutes
  deals.py --test     print what would be sent from the current posts (sends nothing)
"""
import html, json, os, re, sys, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
CHANNELS = ["desidime", "dealsmagnet", "dealsheaven"]

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
STORE = re.compile(r"amazon|amzn|flipkart|fkrt|fktr|myntra|myntr", re.I)
OTHER_STORE = re.compile(r"ajio|meesho|nykaa|tata ?cliq|croma|jiomart|shopsy|blinkit|zepto|instamart|swiggy|"
                         r"location specific", re.I)


def money(s):
    return float(s.replace(",", ""))


def discount(text):
    """Best guess of the discount % from '65% off' or price vs MRP."""
    best = 0.0
    for m in re.finditer(r"(\d{1,2})\s*%\s*off", text, re.I):
        best = max(best, float(m.group(1)))
    mrp = re.search(r"mrp\s*[:\-]?\s*(?:₹|rs\.?)\s*([\d,]+)", text, re.I)
    if mrp:
        prices = [money(p) for p in re.findall(r"(?:₹|rs\.?)\s*([\d,]+)", text, re.I)]
        m = money(mrp.group(1))
        lows = [p for p in prices if 0 < p < m]
        if m > 0 and lows:
            best = max(best, 100 * (1 - min(lows) / m))
    return best


def worth_sending(text):
    """Return a short reason if the post looks like a price mistake / huge deal, else None."""
    if OTHER_STORE.search(text):
        return None
    if MISTAKE.search(text):
        return "🚨 PRICE MISTAKE"
    if SALE_BANNER.search(text):          # "up to 80% off" sale pages are not real steals
        return None
    d = discount(text)
    elec = bool(ELECTRONICS.search(text))
    if d >= 90 or (LOOT.search(text) and d >= 80):
        return f"🔥 {d:.0f}% off"
    if elec and d >= 70:
        return f"⚡ Electronics {d:.0f}% off"
    return None


def fetch(channel):
    req = urllib.request.Request(f"https://t.me/s/{channel}", headers={"User-Agent": "Mozilla/5.0 DealAlert"})
    page = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
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
    if os.path.exists(p):
        return json.load(open(p))
    return {"token": os.environ["TELEGRAM_TOKEN"], "chat_id": os.environ["TELEGRAM_CHAT_ID"]}


def send(text):
    c = cfg()
    data = urllib.parse.urlencode({"chat_id": c["chat_id"], "text": text[:3900],
                                   "disable_web_page_preview": "false"}).encode()
    r = json.loads(urllib.request.urlopen(f"https://api.telegram.org/bot{c['token']}/sendMessage",
                                          data, timeout=20).read())
    if not r.get("ok"):
        print("Telegram error:", r)


def check(state, test=False):
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
            why = worth_sending(text)
            if why:
                store = next((n for k, n in (("amzn|amazon", "Amazon"), ("fkrt|fktr|flipkart", "Flipkart"),
                                             ("myntr", "Myntra")) if re.search(k, text, re.I)), "")
                msg = f"{why}{'  · ' + store if store else ''}\n\n{text}\n\n⏱ Fast ga open cheyyandi, ivi nimishallo ayipothayi."
                print(msg[:300].replace("\n", " | "), "\n")
                if not test:
                    send(msg)
                    time.sleep(1)
        if not test:
            state[ch] = max(newest, last or 0)


def main():
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    if "--test" in sys.argv:
        check(state, test=True)
        return
    minutes = int(sys.argv[sys.argv.index("--loop") + 1]) if "--loop" in sys.argv else 0
    end = time.time() + minutes * 60
    while True:
        check(state)
        json.dump(state, open(STATE, "w"))
        if time.time() + 20 > end:
            break
        time.sleep(20)


if __name__ == "__main__":
    main()
