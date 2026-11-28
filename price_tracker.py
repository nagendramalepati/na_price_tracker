"""
Price tracker: checks product prices, stores history for the dashboard,
and sends Telegram alerts when a price reaches your target.

Usage:
  python price_tracker.py                 # normal run (used by GitHub Actions)
  python price_tracker.py --check URL     # test: can we read the price from this link?
  python price_tracker.py --check URL --selector "p.price"
  python price_tracker.py --test-telegram # send a test message to your Telegram
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
PRODUCTS_FILE = ROOT / "products.json"
DATA_FILE = ROOT / "docs" / "data.json"

MAX_HISTORY_POINTS = 2000      # per product, keeps the data file small
FAILURES_BEFORE_WARNING = 3    # consecutive failed checks before a Telegram warning
DUE_SLACK = timedelta(minutes=20)  # a check that runs a little early still counts as due

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Known price locations for popular sites (sites change these from time to time).
SITE_SELECTORS = {
    "amazon.": [
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "#apex_desktop .a-price .a-offscreen",
        "#priceblock_dealprice",
        "#priceblock_ourprice",
        "span.a-price span.a-offscreen",
    ],
    "flipkart.": ["div.Nx9bqj.CxhGGd", "div.Nx9bqj", "div._30jeq3._16Jk6d", "div._30jeq3"],
}


# ---------------------------------------------------------------- price parsing

def parse_price(value):
    """Turn text like '₹1,23,456.00', '$1,299.99', 'Rs. 999' or '1.299,99 €' into a float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    match = re.search(r"\d[\d.,\s]*", str(value).replace(" ", " "))
    if not match:
        return None
    s = re.sub(r"\s", "", match.group(0)).rstrip(".,")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):      # European: 1.299,99
            s = s.replace(".", "").replace(",", ".")
        else:                                 # 1,299.99
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) == 2:   # 12,99 -> decimal comma
            s = s.replace(",", ".")
        else:                                         # 1,299 or 1,23,456
            s = s.replace(",", "")
    elif s.count(".") > 1:                            # 1.299.000
        s = s.replace(".", "")
    try:
        price = float(s)
    except ValueError:
        return None
    return price if price > 0 else None


def _prices_from_jsonld(obj):
    """Find offer prices inside schema.org JSON-LD data."""
    found = []
    if isinstance(obj, list):
        for item in obj:
            found += _prices_from_jsonld(item)
    elif isinstance(obj, dict):
        offers = obj.get("offers")
        if offers is not None:
            for offer in offers if isinstance(offers, list) else [offers]:
                if isinstance(offer, dict):
                    for key in ("price", "lowPrice"):
                        p = parse_price(offer.get(key))
                        if p:
                            found.append(p)
                            break
                    else:
                        spec = offer.get("priceSpecification")
                        for sp in spec if isinstance(spec, list) else [spec]:
                            if isinstance(sp, dict) and parse_price(sp.get("price")):
                                found.append(parse_price(sp.get("price")))
                                break
        for key, val in obj.items():
            if key != "offers" and isinstance(val, (dict, list)):
                found += _prices_from_jsonld(val)
    return found


def _text_or_content(el):
    return el.get("content") or el.get("value") or el.get_text(" ", strip=True)


def extract_price(html, url="", selector=None):
    """Return (price, method) or (None, None)."""
    soup = BeautifulSoup(html, "html.parser")

    # 1. Your own selector for this product (most reliable when set)
    if selector:
        el = soup.select_one(selector)
        if el:
            p = parse_price(_text_or_content(el))
            if p:
                return p, f"selector {selector}"
        return None, None

    # 2. Structured product data (used by most shops)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        prices = _prices_from_jsonld(data)
        if prices:
            return prices[0], "structured data (JSON-LD)"

    # 3. Price meta tags
    for attrs in (
        {"property": "product:price:amount"},
        {"property": "og:price:amount"},
        {"itemprop": "price"},
        {"name": "twitter:data1"},
    ):
        el = soup.find(attrs=attrs)
        if el:
            p = parse_price(_text_or_content(el))
            if p:
                return p, f"meta tag {attrs}"

    # 4. Known selectors for popular sites
    for domain, selectors in SITE_SELECTORS.items():
        if domain in url:
            for sel in selectors:
                el = soup.select_one(sel)
                if el:
                    p = parse_price(_text_or_content(el))
                    if p:
                        return p, f"site selector {sel}"
    return None, None


def fetch_price(url, selector=None, attempts=2):
    """Download the page and read the price. Raises RuntimeError on failure."""
    last_error = "unknown error"
    for attempt in range(attempts):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code != 200:
                last_error = f"website returned HTTP {resp.status_code} (it may be blocking automated checks)"
            else:
                price, method = extract_price(resp.text, url, selector)
                if price:
                    return price, method
                lowered = resp.text.lower()
                if "captcha" in lowered or "robot" in lowered:
                    last_error = "website showed a captcha / bot check"
                else:
                    last_error = "price not found on page (try adding a 'selector' for this product)"
        except requests.RequestException as exc:
            last_error = f"network error: {exc.__class__.__name__}"
        if attempt + 1 < attempts:
            time.sleep(10)
    raise RuntimeError(last_error)


GRAMS_PER_TROY_OUNCE = 31.1034768


def fetch_metal_price(metal, currency, grams, premium_percent=0.0):
    """Value of `grams` of a metal (XAU gold, XAG silver, XPT, XPD) at live spot price,
    plus an optional dealer premium. Uses the free gold-api.com feed."""
    url = f"https://api.gold-api.com/price/{metal.upper()}/{currency.upper()}"
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        per_oz = float(r.json()["price"])
    except (requests.RequestException, KeyError, ValueError) as exc:
        raise RuntimeError(f"metal price feed error: {exc.__class__.__name__}")
    value = per_oz / GRAMS_PER_TROY_OUNCE * float(grams) * (1 + float(premium_percent) / 100)
    return round(value, 2), f"{metal.upper()} spot {per_oz:,.2f} {currency.upper()}/oz + {premium_percent}% premium"


def get_price(item):
    if item.get("metal"):
        return fetch_metal_price(item["metal"], item.get("currency", "USD"),
                                 item.get("grams", GRAMS_PER_TROY_OUNCE), item.get("premium_percent", 0))
    attempts = 3 if "amazon." in item["url"] else 2   # Amazon blocks some requests at random
    return fetch_price(item["url"], item.get("selector") or None, attempts=attempts)


# ---------------------------------------------------------------- telegram

def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[telegram not configured] would send:\n" + text)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": "true"},
            timeout=20,
        )
        if not r.ok:
            print(f"Telegram error {r.status_code}: {r.text}")
        return r.ok
    except requests.RequestException as exc:
        print(f"Telegram error: {exc}")
        return False


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def money(symbol, value):
    return f"{symbol}{value:,.2f}" if value is not None else "—"


def price_update_message(name, symbol, price, prev, target, method, now, tz):
    """Routine 'latest price' message sent after every check (notify_every_check)."""
    if prev:
        diff = price - prev
        arrow = "🔺" if diff > 0 else "🔻" if diff < 0 else "➖"
        change = f"{arrow} {'+' if diff > 0 else ''}{money(symbol, diff).replace(symbol + '-', '-' + symbol)} ({diff / prev:+.2%})"
    else:
        change = "first check"
    lines = [
        f"💰 <b>{esc(name)}</b>",
        f"Now <b>{money(symbol, price)}</b> · {change}",
    ]
    if target is not None:
        gap = price - float(target)
        lines.append(f"Target {money(symbol, float(target))} · "
                     + (f"{money(symbol, gap)} above" if gap > 0 else "✅ at or below target"))
    lines.append(f"<i>{esc(method)} · {now.astimezone(tz):%d %b %H:%M}</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------- main run

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def run():
    config = load_json(PRODUCTS_FILE, None)
    if config is None:
        sys.exit(f"Could not read {PRODUCTS_FILE.name}. Check that it is valid JSON.")
    settings = config.get("settings", {})
    tz = ZoneInfo(settings.get("timezone", "UTC"))
    default_symbol = settings.get("currency_symbol", "")

    old = {p["url"]: p for p in load_json(DATA_FILE, {}).get("products", [])}
    now = datetime.now(timezone.utc)
    out = []

    for item in config.get("products", []):
        url = item.get("url", "").strip()
        if not url:
            continue
        rec = old.get(url, {"history": [], "alerted": False, "failures": 0})
        symbol = item.get("currency_symbol", default_symbol)
        target = item.get("target_price")
        every = float(item.get("check_every_hours", 6))
        name = item.get("name") or url
        rec.update({
            "name": name, "url": url, "target_price": target,
            "currency_symbol": symbol, "check_every_hours": every,
            "track_until": item.get("track_until"),
        })

        # Tracking window finished?
        until = item.get("track_until")
        if until:
            end = datetime.fromisoformat(until).replace(tzinfo=tz) + timedelta(days=1)
            if now >= end:
                if rec.get("status") != "ended":
                    hist = [h["price"] for h in rec["history"]]
                    send_telegram(
                        f"🏁 <b>Tracking ended</b>: {esc(name)}\n"
                        f"Last: {money(symbol, rec.get('last_price'))} · "
                        f"Lowest: {money(symbol, min(hist) if hist else None)}\n{esc(url)}"
                    )
                rec["status"] = "ended"
                out.append(rec)
                continue

        # Is this product due for a check?
        last = rec.get("last_checked")
        if last and now - datetime.fromisoformat(last) < timedelta(hours=every) - DUE_SLACK:
            out.append(rec)
            continue

        # A failed check is retried on the next hourly run, not after the full interval.
        rec["last_attempt"] = now.isoformat(timespec="seconds")
        try:
            price, method = get_price(item)
        except RuntimeError as err:
            rec["failures"] = rec.get("failures", 0) + 1
            rec["last_error"] = str(err)
            rec["status"] = "error"
            print(f"✗ {name}: {err}")
            if rec["failures"] == FAILURES_BEFORE_WARNING:
                send_telegram(
                    f"⚠️ <b>Can't read price</b> for {esc(name)} "
                    f"({FAILURES_BEFORE_WARNING} checks in a row)\nReason: {esc(err)}\n{esc(url)}"
                )
            out.append(rec)
            continue

        print(f"✓ {name}: {price} via {method}")
        prev = rec.get("last_price")
        rec["last_checked"] = now.isoformat(timespec="seconds")
        rec.update({"last_price": price, "failures": 0, "last_error": None, "method": method})
        rec["history"] = (rec["history"] + [{"t": rec["last_checked"], "price": price}])[-MAX_HISTORY_POINTS:]

        hit = target is not None and price <= float(target)
        rec["status"] = "target" if hit else "tracking"
        sent = False
        if hit and not rec.get("alerted"):
            sent = send_telegram(
                f"🔔 <b>Price alert!</b> {esc(name)}\n"
                f"Now <b>{money(symbol, price)}</b> (your target: {money(symbol, float(target))})\n"
                f"{esc(url)}"
            )
            rec["alerted"] = True
        elif not hit:
            rec["alerted"] = False  # re-arm, so you're alerted again if it drops back down
            if item.get("notify_on_any_drop") and prev and price < prev:
                sent = send_telegram(
                    f"📉 <b>Price dropped</b>: {esc(name)}\n"
                    f"{money(symbol, prev)} → <b>{money(symbol, price)}</b>\n{esc(url)}"
                )
        if item.get("notify_every_check") and not sent:
            send_telegram(price_update_message(name, symbol, price, prev, target, method, now, tz))
        out.append(rec)

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps({"updated": now.isoformat(timespec="seconds"), "products": out},
                   indent=1, ensure_ascii=False),
        encoding="utf-8",
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", metavar="URL", help="test reading the price from one link")
    ap.add_argument("--selector", help="CSS selector to use with --check")
    ap.add_argument("--test-telegram", action="store_true", help="send a test Telegram message")
    args = ap.parse_args()

    if args.test_telegram:
        ok = send_telegram("✅ Price tracker is connected to Telegram.")
        sys.exit(0 if ok else 1)
    if args.check:
        try:
            price, method = fetch_price(args.check, args.selector)
            print(f"Price found: {price}  (via {method})")
        except RuntimeError as err:
            sys.exit(f"Could not read price: {err}")
        return
    run()


if __name__ == "__main__":
    main()
