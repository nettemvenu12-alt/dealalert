#!/usr/bin/env python3
"""Real price history for an Amazon India product, read from Keepa's free price graph.

The graph image is public (no API key). We OCR the y-axis labels to get the price scale,
then trace the price line to get the 90-day low, median and high.

  history.py ASIN [current_price]
"""
import io, re, statistics, subprocess, sys, tempfile, urllib.request

from PIL import Image

W, H = 1000, 400


def graph_url(asin, days=90):
    return (f"https://graph.keepa.com/pricehistory.png?asin={asin}&domain=in&range={days}"
            f"&width={W}&height={H}&amazon=1&new=1&used=0&salesrank=0")


def fetch_graph(asin, days=90):
    req = urllib.request.Request(graph_url(asin, days), headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=20).read()


def _erase_rupee(crop):
    """Whiten the '₹' sign in front of every y-axis label (OCR reads it as a random digit).
    The sign is the first glyph of each label, followed by a space wider than the gaps between digits."""
    g = crop.convert("L")
    px = g.load()
    w, h = g.size
    dark = lambda x, y: px[x, y] < 160
    rows = [any(dark(x, y) for x in range(w)) for y in range(h)]
    y = 0
    while y < h:
        if not rows[y]:
            y += 1
            continue
        y0 = y
        while y < h and rows[y]:
            y += 1
        y1 = y                                   # one text line: rows y0..y1
        cols = [any(dark(x, yy) for yy in range(y0, y1)) for x in range(w)]
        x = next((i for i, c in enumerate(cols) if c), None)
        if x is None:
            continue
        gap, cut = 0, None
        for i in range(x, w):
            if cols[i]:
                if gap >= 3:
                    cut = i
                    break
                gap = 0
            else:
                gap += 1
        if cut:
            for xx in range(0, cut):
                for yy in range(y0, y1):
                    px[xx, yy] = 255
    return g


def _axis_labels(img, plot_left):
    """OCR the y-axis price labels -> list of (y_pixel, [price])."""
    crop = _erase_rupee(img.crop((0, 0, plot_left, img.height)))
    crop = crop.resize((crop.width * 4, crop.height * 4), Image.LANCZOS)
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        crop.save(f.name)
        tsv = subprocess.run(["tesseract", f.name, "-", "--psm", "6", "-c",
                              "tessedit_char_whitelist=0123456789,.k", "tsv"],
                             capture_output=True, text=True, timeout=30).stdout
    out = []
    for row in tsv.splitlines()[1:]:
        c = row.split("\t")
        if len(c) < 12 or not c[11].strip():
            continue
        txt = c[11].replace(",", "").replace(".", "").strip()
        mult = 1000 if txt.endswith("k") else 1
        txt = txt.rstrip("k")
        if not txt.isdigit():
            continue
        y = (int(c[7]) + int(c[9]) / 2) / 4
        out.append((y, [int(txt) * mult]))
    return out


def _fit(labels):
    """Linear map pixel-y -> price using labels that agree with each other (drop OCR mistakes).
    labels: [(y, [candidate values])]."""
    best = None
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            (y1, c1), (y2, c2) = labels[i], labels[j]
            if abs(y2 - y1) < 5:
                continue
            for p1 in c1:
                for p2 in c2:
                    if p1 == p2:
                        continue
                    a = (p2 - p1) / (y2 - y1)
                    if a >= 0:          # price must grow upwards
                        continue
                    b = p1 - a * y1
                    ok = sum(1 for y, cs in labels if any(abs(a * y + b - p) <= max(1, 0.02 * abs(p)) for p in cs))
                    if not best or ok > best[0] or (ok == best[0] and abs(a) < abs(best[1])):
                        best = (ok, a, b)
    if not best or best[0] < max(3, int(0.7 * len(labels))):
        return None
    return best[1], best[2]


def _is_line(px):
    r, g, b = px[:3]
    # Keepa draws the Amazon price orange and the "new" price blue/purple
    return (b > 150 and r < 170 and g < 170 and b - r > 50) or (r > 200 and 90 < g < 190 and b < 90)


def analyse(png):
    img = Image.open(io.BytesIO(png)).convert("RGB")
    px = img.load()
    w, h = img.size
    # plot area: columns that contain the line
    cols = {}
    for x in range(w):
        ys = [y for y in range(h - 28) if _is_line(px[x, y])]   # skip x-axis labels / Keepa logo
        if ys and not (x > 0.85 * w and max(ys) < 45):          # skip the legend dot (top right)
            cols[x] = ys
    if len(cols) < 20:
        return None
    xs = sorted(cols)
    # ignore the legend at the right (its marker sits away from the plot); keep the longest run
    plot_left = max(0, xs[0] - 2)
    labels = _axis_labels(img, plot_left)
    fit = _fit(labels)
    if not fit:
        return None
    a, b = fit
    # per column take the lowest price drawn (the best deal price at that time)
    series = []
    for x in xs:
        ys = cols[x]
        series.append(a * max(ys) + b)
    series = [p for p in series if p > 0]
    if len(series) < 20:
        return None
    return {"low": min(series), "median": statistics.median(series), "high": max(series),
            "points": len(series)}


def label(h, current):
    """Add drop % and a verdict label for the current deal price to history numbers h."""
    med, low = h["median"], h["low"]
    drop = 100 * (1 - current / med) if med else 0
    h["drop"] = drop
    if drop >= 60:
        h["label"] = "🚨 REAL PRICE MISTAKE"
    elif current <= low * 1.03 and drop >= 15:
        h["label"] = "✅ REAL LOWEST PRICE (90 days)"
    elif drop >= 25:
        h["label"] = "✅ REAL PRICE DROP"
    elif drop >= 8:
        h["label"] = "🟡 Small real drop"
    else:
        h["label"] = "❌ FAKE discount (usual price)"
    return h


def verdict(asin, current):
    """History numbers plus a label, or None if the history can't be read."""
    try:
        h = analyse(fetch_graph(asin))
    except Exception as e:
        print("history failed:", asin, e)
        return None
    return label(h, current) if h else None


if __name__ == "__main__":
    asin = sys.argv[1]
    if len(sys.argv) > 2:
        print(verdict(asin, float(sys.argv[2])))
    else:
        print(analyse(fetch_graph(asin)))
