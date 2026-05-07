"""Multi-store grocery stock notifier (Playwright edition).

Watches search results across multiple Indian grocery/quick-commerce stores for a
configurable list of keywords at the user's location, extracts matching product
cards from the rendered DOM, and sends Telegram messages whenever the stock
status of any product changes.

Stores supported (best-effort, DOM-dependent): Blinkit, Zepto, Swiggy Instamart,
BigBasket.
"""

import json
import logging
import os
import re
import shutil
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def _load_json_file(path: str):
    data = Path(path).read_bytes()
    if not data or not data.strip():
        raise ValueError("empty file")
    # Handle common encodings (Windows editors often write UTF-16, and some tools write UTF-8 BOM).
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        text = data.decode("utf-16")
    else:
        text = data.decode("utf-8-sig")
    return json.loads(text)


def _env(name: str, default: str) -> str:
    """Like os.environ.get, but treats empty/whitespace as unset.

    GitHub Actions expands undefined ${{ vars.X }} to "", which would
    otherwise crash float() / int() parsing.
    """
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    return val


CHECK_INTERVAL = int(_env("CHECK_INTERVAL", "600"))

LOCATION_LAT = float(_env("BLINKIT_LAT", "12.9807"))  # 12°58'50.5"N
LOCATION_LON = float(_env("BLINKIT_LON", "77.7465"))  # 77°44'47.3"E
LOCATION_LOCALITY = _env("BLINKIT_LOCALITY", "Pattandur Agrahara")
LOCATION_LANDMARK = _env("BLINKIT_LANDMARK", "Whitefield")
LOCATION_CITY = _env("BLINKIT_CITY", "Bengaluru")
LOCATION_STATE = _env("BLINKIT_STATE", "Karnataka")

# Shared location defaults (optionally override via LAT/LON/LOCALITY/LANDMARK/CITY/STATE).
# If unset, they fall back to the Blinkit defaults above for backward compatibility.
SHARED_LOCATION_LAT = float(_env("LAT", str(LOCATION_LAT)))
SHARED_LOCATION_LON = float(_env("LON", str(LOCATION_LON)))
SHARED_LOCATION_LOCALITY = _env("LOCALITY", LOCATION_LOCALITY)
SHARED_LOCATION_LANDMARK = _env("LANDMARK", LOCATION_LANDMARK)
SHARED_LOCATION_CITY = _env("CITY", LOCATION_CITY)
SHARED_LOCATION_STATE = _env("STATE", LOCATION_STATE)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)

WATCHLIST_PATH = os.environ.get("WATCHLIST_PATH", "watchlist.json")
DEFAULT_WATCHLIST = ["hotwheels", "hot wheels", "Hot Wheels"]

STATE_PATH = os.environ.get("STATE_PATH", "state.json")

# When set, do one scrape across the watchlist and exit (no Telegram
# command listener, no sleep loop). Designed for cron-style hosts like
# GitHub Actions.
RUN_ONCE = os.environ.get("RUN_ONCE", "").strip().lower() in ("1", "true", "yes", "on")

SCRAPER_DEBUG = os.environ.get("SCRAPER_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")
DEBUG_DIR = os.environ.get("DEBUG_DIR", "debug")

ENABLED_STORES_ENV = os.environ.get("ENABLED_STORES", "").strip()


def _enabled_stores() -> list[str]:
    if not ENABLED_STORES_ENV:
        return ["blinkit", "zepto", "instamart", "bigbasket"]
    parts = [p.strip().lower() for p in re.split(r"[,\s]+", ENABLED_STORES_ENV) if p.strip()]
    aliases = {
        "swiggy": "instamart",
        "swiggyinstamart": "instamart",
        "insta": "instamart",
        "bb": "bigbasket",
    }
    wanted = {aliases.get(p, p) for p in parts}
    order = ["blinkit", "zepto", "instamart", "bigbasket"]
    return [s for s in order if s in wanted]


# ---------------------------------------------------------------------------
# Watchlist persistence
# ---------------------------------------------------------------------------

def _normalize_keyword(kw: str) -> str:
    """Canonical form used for matching/dedup; preserves spaces for display."""
    return " ".join((kw or "").lower().split())


def load_watchlist() -> list[str]:
    if os.path.exists(WATCHLIST_PATH):
        try:
            data = _load_json_file(WATCHLIST_PATH)
            if isinstance(data, list):
                cleaned = []
                seen = set()
                for x in data:
                    n = _normalize_keyword(str(x))
                    if n and n not in seen:
                        seen.add(n)
                        cleaned.append(n)
                return cleaned or list(DEFAULT_WATCHLIST)
        except Exception as exc:
            logging.warning("Failed to read %s: %s", WATCHLIST_PATH, exc)
    return list(DEFAULT_WATCHLIST)


def save_watchlist(items: list[str]) -> None:
    try:
        with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2)
    except Exception as exc:
        logging.error("Failed to save watchlist: %s", exc)


def load_state() -> dict:
    """Load persisted per-keyword-per-store state (signatures + checked_at).

    Backward compatible with the legacy shape:
      {"keywords": {"lego": {"signature": "...", "checked_at": "..."}}}
    which is treated as Blinkit-only state.
    """
    keywords = {}
    if os.path.exists(STATE_PATH):
        try:
            data = _load_json_file(STATE_PATH)
            raw_keywords = data.get("keywords") if isinstance(data, dict) else None
            if isinstance(raw_keywords, dict):
                upgraded: dict = {}
                for kw, v in raw_keywords.items():
                    if not isinstance(v, dict):
                        continue
                    if isinstance(v.get("stores"), dict):
                        upgraded[kw] = {
                            "stores": {
                                store: {
                                    "signature": (sv or {}).get("signature") or "",
                                    "checked_at": (sv or {}).get("checked_at") or "",
                                }
                                for store, sv in v["stores"].items()
                                if isinstance(sv, dict)
                            }
                        }
                        continue

                    # Legacy: treat as Blinkit.
                    upgraded[kw] = {
                        "stores": {
                            "blinkit": {
                                "signature": v.get("signature") or "",
                                "checked_at": v.get("checked_at") or "",
                            }
                        }
                    }
                keywords = upgraded
        except Exception as exc:
            logging.warning("Failed to read %s: %s", STATE_PATH, exc)
    return keywords

# At runtime, construct the state object as:
# state = {
#     "keywords": load_state(),
#     "lock": threading.Lock(),
#     "watchlist": load_watchlist(),
# }


def save_state(keywords: dict) -> None:
    """Persist per-keyword-per-store signatures so dedup survives restarts."""
    payload = {
        "keywords": {
            k: {
                "stores": {
                    store: {
                        "signature": (sv or {}).get("signature") or "",
                        "checked_at": (sv or {}).get("checked_at") or "",
                    }
                    for store, sv in (v.get("stores") or {}).items()
                    if isinstance(sv, dict)
                }
            }
            for k, v in keywords.items()
            if isinstance(v, dict)
        }
    }
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as exc:
        logging.error("Failed to save state: %s", exc)


def search_url_for(keyword: str) -> str:
    return "https://www.blinkit.com/s/?q=" + urllib.parse.quote(keyword)


def _store_search_url(store: str, keyword: str) -> str:
    store = (store or "").lower()
    if store == "blinkit":
        return "https://www.blinkit.com/s/?q=" + urllib.parse.quote(keyword)
    if store == "zepto":
        return "https://www.zeptonow.com/search?query=" + urllib.parse.quote(keyword)
    if store == "instamart":
        return "https://www.swiggy.com/instamart/search?query=" + urllib.parse.quote(keyword)
    if store == "bigbasket":
        return "https://www.bigbasket.com/ps/?q=" + urllib.parse.quote(keyword)
    return ""


# ---------------------------------------------------------------------------
# Browser-side extraction
# ---------------------------------------------------------------------------

EXTRACT_PRODUCTS_JS_TEMPLATE = r"""
(() => {
  const KEYWORD = __KEYWORD__;
  const normalize = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
  const targetNorm = normalize(KEYWORD);

  const titles = document.querySelectorAll('.tw-line-clamp-2, [data-testid*=\"product-title\"], [class*=\"line-clamp\"]');
  const items = [];
  const seen = new Set();
  const SKIP_RE = /^(showing\s+results?|search\s+results?|showing\s+related)/i;

  const isProductImage = (im) => {
    const src = im.currentSrc || im.src || im.getAttribute('data-src') || '';
    if (!src) return false;
    if (/\/(eta-icons|icons|badges|store-icons|brand-images?)\//i.test(src)) return false;
    if ((im.naturalWidth && im.naturalWidth < 40) || (im.width && im.width < 40)) return false;
    return true;
  };

  for (const t of titles) {
    const name = (t.textContent || '').trim();
    if (!name || name.length < 3) continue;
    if (SKIP_RE.test(name)) continue;
    if (!normalize(name).includes(targetNorm)) continue;

    // Walk up until we find the smallest ancestor that contains BOTH a
    // [role="button"] (ADD/Notify Me) AND a product image (not an
    // eta/icon helper graphic). Falls back to the first ancestor with a
    // button if no image is available.
    let card = t;
    let cardEl = null;
    for (let i = 0; i < 20; i++) {
      if (!card.parentElement) break;
      card = card.parentElement;
      if (!card.querySelector('[role="button"]')) continue;
      const productImg = Array.from(card.querySelectorAll('img')).find(isProductImage);
      if (productImg) { cardEl = card; break; }
    }
    if (!cardEl) {
      let c = t;
      for (let i = 0; i < 15; i++) {
        if (!c.parentElement) break;
        c = c.parentElement;
        if (c.querySelector('[role="button"]')) { cardEl = c; break; }
      }
    }
    if (!cardEl) continue;
    card = cardEl;

    const cardText = (card.textContent || '').replace(/\s+/g, ' ').trim();
    if (!/₹/.test(cardText)) continue;

    const buttons = card.querySelectorAll('[role="button"]');
    let buttonLabel = '';
    for (const b of buttons) {
      const txt = (b.textContent || '').trim().toUpperCase();
      if (/^(ADD|NOTIFY ME|SOLD OUT|OUT OF STOCK)$/.test(txt)) {
        buttonLabel = txt;
        break;
      }
      if (!buttonLabel && txt.length <= 20) buttonLabel = txt;
    }
    const outOfStock =
      /notify me/i.test(buttonLabel) ||
      /out of stock/i.test(cardText) ||
      /sold out/i.test(cardText);
    const inStock = !outOfStock && /\bADD\b/.test(buttonLabel + ' ' + cardText);

    const priceMatch = cardText.match(/₹\s*([0-9,]+)/);
    const price = priceMatch ? '₹' + priceMatch[1] : '';
    const qtyMatch = cardText.match(/\b(\d+\s*(?:pcs|pc|pack|unit|units|g|kg|ml|l))\b/i);
    const quantity = qtyMatch ? qtyMatch[1] : '';

    let image = '';
    const productImg = Array.from(card.querySelectorAll('img')).find(isProductImage);
    if (productImg) {
      image = productImg.currentSrc || productImg.src
        || productImg.getAttribute('data-src') || '';
      if (!image && productImg.srcset) {
        image = productImg.srcset.split(',')[0].trim().split(' ')[0];
      }
    }

    const key = name + '|' + quantity + '|' + price;
    if (seen.has(key)) continue;
    seen.add(key);
    items.push({ name, price, quantity, inStock, outOfStock, buttonLabel, image });
  }
  return items;
})()
"""


def _extraction_js(keyword: str) -> str:
    # Keep the original template above for reference; use a more tolerant extractor here.
    return _blinkit_extraction_js(keyword)


BLINKIT_EXTRACT_PRODUCTS_JS_TEMPLATE_V2 = r"""
(() => {
    const KEYWORD = __KEYWORD__;
    const normalize = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
    const targetNorm = normalize(KEYWORD);
    const PRICE_RE = /(?:₹|rs\.?|inr)\s*([0-9][0-9,]*)/i;
    const QTY_RE = /\b(\d+\s*(?:pcs|pc|pack|unit|units|g|kg|ml|l))\b/i;
    const SKIP_RE = /^(showing\s+results?|search\s+results?|showing\s+related)/i;
    const items = [];
    const seen = new Set();
    // More robust: look for all product cards with price and add button or image
    const candidates = Array.from(document.querySelectorAll('div, section, li, article'));
    for (const card of candidates) {
        const cardText = (card.textContent || '').replace(/\s+/g, ' ').trim();
        if (!cardText || cardText.length < 10) continue;
        if (!PRICE_RE.test(cardText)) continue;
        // Try to find a title
        let name = '';
        let titleEl = card.querySelector('.tw-line-clamp-2, [data-testid*="product-title"], [class*="line-clamp"], h3, h2, span, a, div');
        if (titleEl) {
            name = (titleEl.textContent || '').trim();
        } else {
            // fallback: first 40 chars of cardText
            name = cardText.slice(0, 40);
        }
        if (!name || name.length < 3) continue;
        if (SKIP_RE.test(name)) continue;
        if (!normalize(name).includes(targetNorm)) continue;
        const priceMatch = cardText.match(PRICE_RE);
        const price = priceMatch ? ('Rs ' + priceMatch[1]) : '';
        const qtyMatch = cardText.match(QTY_RE);
        const quantity = qtyMatch ? qtyMatch[1] : '';
        // Button detection
        let buttonLabel = '';
        const btns = card.querySelectorAll('button, [role="button"]');
        for (const b of btns) {
            const t = ((b.textContent || '').trim()).toUpperCase();
            if (!t) continue;
            if (/^(ADD|ADD TO CART|NOTIFY ME|SOLD OUT|OUT OF STOCK)$/.test(t)) { buttonLabel = t; break; }
            if (!buttonLabel && t.length <= 24) buttonLabel = t;
        }
        const lower = cardText.toLowerCase();
        const outOfStock =
            /notify me/i.test(buttonLabel) ||
            /out of stock/.test(lower) ||
            /sold out/.test(lower);
        const inStock = !outOfStock && /add/.test((buttonLabel + ' ' + cardText).toLowerCase());
        // Image detection
        let image = '';
        const imgs = Array.from(card.querySelectorAll('img'));
        for (const im of imgs) {
            const src = im.currentSrc || im.src || im.getAttribute('data-src') || '';
            if (!src) continue;
            if (/\/(eta-icons|icons|badges|store-icons|brand-images?)\//i.test(src)) continue;
            if ((im.naturalWidth && im.naturalWidth < 40) || (im.width && im.width < 40)) continue;
            image = src; break;
        }
        const key = name + '|' + quantity + '|' + price;
        if (seen.has(key)) continue;
        seen.add(key);
        items.push({ store: 'blinkit', name, price, quantity, inStock, outOfStock, buttonLabel, image });
    }
    // For debugging: if no items, dump all product-like nodes
    if (items.length === 0) {
        const debugNodes = [];
        for (const card of candidates) {
            const cardText = (card.textContent || '').replace(/\s+/g, ' ').trim();
            if (PRICE_RE.test(cardText)) {
                debugNodes.push({text: cardText.slice(0, 200), html: card.outerHTML.slice(0, 500)});
            }
        }
        return {__debug__: true, nodes: debugNodes};
    }
    return items;
})()
"""


def _blinkit_extraction_js(keyword: str) -> str:
    return BLINKIT_EXTRACT_PRODUCTS_JS_TEMPLATE_V2.replace("__KEYWORD__", json.dumps(keyword))


GENERIC_EXTRACT_PRODUCTS_JS_TEMPLATE = r"""
(() => {
  const STORE = __STORE__;
  const KEYWORD = __KEYWORD__;
  const normalize = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
  const targetNorm = normalize(KEYWORD);

  const RUPEE = '\\u20b9';
  const LEGACY_RUPEE = '\\u00e2\\u201a\\u00b9';
  const PRICE_RE = new RegExp('(?:' + RUPEE + '|' + LEGACY_RUPEE + '|rs\\\\.?)\\\\s*([0-9][0-9,]*)', 'i');
  const QTY_RE = /\\b(\\d+\\s*(?:pcs|pc|pack|unit|units|g|kg|ml|l))\\b/i;
  const SKIP_RE = /^(showing\\s+results?|search\\s+results?|showing\\s+related)/i;

  const isProductImage = (im) => {
    const src = im.currentSrc || im.src || im.getAttribute('data-src') || '';
    if (!src) return false;
    if (/\/(icons?|badges|store-icons|brand-images?)\//i.test(src)) return false;
    if ((im.naturalWidth && im.naturalWidth < 40) || (im.width && im.width < 40)) return false;
    return true;
  };

  const bestButtonLabel = (card) => {
    const btns = card.querySelectorAll('button, [role=\"button\"]');
    let label = '';
    for (const b of btns) {
      const t = ((b.textContent || '').trim()).toUpperCase();
      if (!t) continue;
      if (/^(ADD|ADD TO CART|NOTIFY ME|SOLD OUT|OUT OF STOCK|ADD\\s*\\+)$/.test(t)) return t;
      if (!label && t.length <= 24) label = t;
    }
    return label;
  };

  const items = [];
  const seen = new Set();

  const candidates = [];
  for (const el of document.querySelectorAll('a, div, span, h1, h2, h3, h4')) {
    const txt = (el.textContent || '').trim();
    if (!txt || txt.length < 3) continue;
    if (SKIP_RE.test(txt)) continue;
    if (!normalize(txt).includes(targetNorm)) continue;
    candidates.push(el);
  }

  for (const t of candidates) {
    const name = (t.textContent || '').trim().slice(0, 180);
    if (!name) continue;

    let card = t;
    let cardEl = null;
    for (let i = 0; i < 22; i++) {
      if (!card.parentElement) break;
      card = card.parentElement;
      const cardText = (card.textContent || '').replace(/\\s+/g, ' ').trim();
      if (!PRICE_RE.test(cardText)) continue;
      const hasBtn = !!card.querySelector('button, [role=\"button\"]');
      const hasImg = !!Array.from(card.querySelectorAll('img')).find(isProductImage);
      if (hasBtn || hasImg) { cardEl = card; break; }
    }
    if (!cardEl) continue;
    card = cardEl;

    const cardText = (card.textContent || '').replace(/\\s+/g, ' ').trim();
    const priceMatch = cardText.match(PRICE_RE);
    const price = priceMatch ? ('Rs ' + priceMatch[1]) : '';
    const qtyMatch = cardText.match(QTY_RE);
    const quantity = qtyMatch ? qtyMatch[1] : '';

    const buttonLabel = bestButtonLabel(card);
    const lower = cardText.toLowerCase();
    const outOfStock =
      /notify me/i.test(buttonLabel) ||
      /out of stock/.test(lower) ||
      /sold out/.test(lower);
    const inStock = !outOfStock && /add/.test((buttonLabel + ' ' + cardText).toLowerCase());

    let image = '';
    const productImg = Array.from(card.querySelectorAll('img')).find(isProductImage);
    if (productImg) {
      image = productImg.currentSrc || productImg.src || productImg.getAttribute('data-src') || '';
      if (!image && productImg.srcset) image = productImg.srcset.split(',')[0].trim().split(' ')[0];
    }

    const key = [STORE, name, quantity, price].join('|');
    if (seen.has(key)) continue;
    seen.add(key);
    items.push({ store: STORE, name, price, quantity, inStock, outOfStock, buttonLabel, image });
  }

  return items;
})()
"""


def _generic_extraction_js(store: str, keyword: str) -> str:
    return (
        GENERIC_EXTRACT_PRODUCTS_JS_TEMPLATE
        .replace("__STORE__", json.dumps(store))
        .replace("__KEYWORD__", json.dumps(keyword))
    )


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def _telegram_api(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def send_telegram_message(message: str, chat_id: str | None = None) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Telegram credentials missing; cannot send message.")
        return
    payload = {"chat_id": chat_id or TELEGRAM_CHAT_ID, "text": message}
    try:
        response = requests.post(_telegram_api("sendMessage"), data=payload, timeout=15)
        response.raise_for_status()
        logging.info("Sent Telegram message (%d chars).", len(message))
    except Exception as exc:
        logging.error("Failed to send Telegram message: %s", exc)


def send_telegram_media_group(
    items: list[dict], chat_id: str | None = None
) -> None:
    """Send up to 10 photos at a time as a media group.

    Each item: {"image": <url>, "caption": <text>}.
    """
    if not items or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    target = chat_id or TELEGRAM_CHAT_ID
    for i in range(0, len(items), 10):
        chunk = items[i : i + 10]
        media = []
        for it in chunk:
            entry = {"type": "photo", "media": it["image"]}
            cap = it.get("caption", "")
            if cap:
                entry["caption"] = cap[:1024]
            media.append(entry)
        try:
            response = requests.post(
                _telegram_api("sendMediaGroup"),
                data={"chat_id": target, "media": json.dumps(media)},
                timeout=30,
            )
            response.raise_for_status()
            logging.info("Sent Telegram media group (%d photos).", len(chunk))
        except Exception as exc:
            logging.error(
                "Failed to send media group (%d photos): %s — falling back to text.",
                len(chunk), exc,
            )
            for it in chunk:
                send_telegram_message(
                    (it.get("caption") or "") + "\n" + it["image"], chat_id=target
                )


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------

def _launch_browser(playwright):
    launch_kwargs = {
        "headless": True,
        "channel": "chrome",
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    }
    return playwright.chromium.launch(**launch_kwargs)


def _get_store_location(store: str) -> dict:
    """Return location dict (lat/lon/locality/landmark/city/state) for a store."""
    store = (store or "").upper()
    return {
        "lat": float(_env(f"{store}_LAT", str(SHARED_LOCATION_LAT))),
        "lon": float(_env(f"{store}_LON", str(SHARED_LOCATION_LON))),
        "locality": _env(f"{store}_LOCALITY", SHARED_LOCATION_LOCALITY),
        "landmark": _env(f"{store}_LANDMARK", SHARED_LOCATION_LANDMARK),
        "city": _env(f"{store}_CITY", SHARED_LOCATION_CITY),
        "state": _env(f"{store}_STATE", SHARED_LOCATION_STATE),
    }


def _new_context(browser, store: str):
    loc = _get_store_location(store)
    context = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1366, "height": 900},
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        geolocation={"latitude": loc["lat"], "longitude": loc["lon"]},
        permissions=["geolocation"],
    )

    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });
    """)

    # Store-specific cookie injection (best-effort; some stores require UI flows).
    if store == "blinkit":
        cookie_base = {"domain": ".blinkit.com", "path": "/"}
        context.add_cookies([
            {"name": "gr_1_lat", "value": str(loc["lat"]), **cookie_base},
            {"name": "gr_1_lon", "value": str(loc["lon"]), **cookie_base},
            {"name": "gr_1_locality", "value": loc["locality"], **cookie_base},
            {"name": "gr_1_landmark", "value": loc["landmark"], **cookie_base},
            {"name": "gr_1_city", "value": loc["city"], **cookie_base},
            {"name": "gr_1_state", "value": loc["state"], **cookie_base},
        ])
    return context


def _autoscroll(page, steps: int = 10, delay_ms: int = 600) -> None:
    for _ in range(steps):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(delay_ms)


def _safe_filename(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9._-]+", "_", s)
    return (s.strip("_")[:80]) or "item"


def _classify_page(text: str, products: list = None) -> str:
    t = (text or "").lower()
    # If products found, never call it blocked/location-gated
    if products and len(products) > 0:
        return "ok"
    # If price pattern found in HTML, don't call it blocked/location-gated
    if re.search(r'(₹|rs\.?|inr)\s*[0-9][0-9,]*', t):
        return "ok"
    if any(x in t for x in ["select your location", "detect my location", "choose delivery", "set your location"]):
        return "location-gated"
    if any(x in t for x in ["select location", "login/ sign up", "login / sign up"]):
        # Many sites show search results only after a delivery location is set.
        return "location-gated"
    if any(x in t for x in ["captcha", "unusual traffic", "verify you are", "access denied", "blocked", "robot check"]):
        return "blocked"
    if any(x in t for x in ["cloudflare", "cloudfront", "request blocked", "403 error", "ray id"]):
        return "blocked"
    if any(x in t for x in ["something went wrong", "try again later", "our best minds are on this"]):
        # Often a soft block / WAF / generic error page on headless infra.
        return "blocked"
    return "render-or-extraction-miss"


def _maybe_write_debug(page, store: str, keyword: str, classification: str, url: str, force: bool) -> None:
    debug = SCRAPER_DEBUG or force
    if not debug:
        return

    root = Path(DEBUG_DIR)
    root.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = root / f"{ts}_{_safe_filename(store)}_{_safe_filename(keyword)}"
    folder.mkdir(parents=True, exist_ok=True)

    try:
        page.screenshot(path=str(folder / "page.png"), full_page=True)
    except Exception as exc:
        logging.warning("[%s/%s] screenshot failed: %s", store, keyword, exc)
    try:
        (folder / "page.html").write_text(page.content(), encoding="utf-8", errors="replace")
    except Exception as exc:
        logging.warning("[%s/%s] html capture failed: %s", store, keyword, exc)

    try:
        body = page.inner_text("body")
    except Exception:
        body = ""

    meta = {
        "store": store,
        "keyword": keyword,
        "url": url,
        "classification": classification,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "body_snippet": (body or "")[:2000],
    }
    try:
        (folder / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except Exception as exc:
        logging.warning("[%s/%s] meta write failed: %s", store, keyword, exc)


def _wait_for_any(page, js_predicates: list[str], timeout_ms: int) -> None:
    deadline = time.time() + (timeout_ms / 1000.0)
    last_err = None
    while time.time() < deadline:
        for js in js_predicates:
            try:
                ok = page.evaluate(js)
                if ok:
                    return
            except Exception as exc:
                last_err = exc
        page.wait_for_timeout(500)
    if last_err is not None:
        raise PWTimeout(str(last_err))
    raise PWTimeout("timeout")


def _scrape_store_products(page, store: str, keyword: str) -> tuple[list[dict], str]:
    """Return (products, classification)."""
    url = _store_search_url(store, keyword)
    try:
        if store == "instamart":
            page.goto("https://www.swiggy.com/instamart", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PWTimeout:
        logging.warning("[%s/%s] page timed out on initial load; continuing.", store, keyword)

    page.wait_for_timeout(2000)

    preds = [
        "document.body && document.body.innerText && (/\\badd\\b/i.test(document.body.innerText) || document.body.innerText.includes('ADD'))",
        "document.querySelectorAll('img').length > 10",
        "document.body && document.body.innerText && (document.body.innerText.includes('\\u20b9') || /rs\\.?\\s*\\d/i.test(document.body.innerText))",
        "document.body && document.body.innerText && (document.body.innerText.toLowerCase().includes('select your location') || document.body.innerText.toLowerCase().includes('detect my location'))",
    ]
    try:
        _wait_for_any(page, preds, timeout_ms=15000)
    except PWTimeout:
        logging.warning("[%s/%s] no obvious results signal within 15s; continuing.", store, keyword)

    products: list[dict] = []
    deadline = time.time() + 20.0
    attempt = 0
    debug_nodes = None
    while time.time() < deadline:
        attempt += 1
        try:
            result = None
            if store == "blinkit":
                result = page.evaluate(_extraction_js(keyword))
            else:
                result = page.evaluate(_generic_extraction_js(store, keyword))

            # If result is a dict with __debug__, handle as debug_nodes
            if isinstance(result, dict) and result.get("__debug__"):
                debug_nodes = result.get('nodes')
                products = []
            # If result is a list, assign to products
            elif isinstance(result, list):
                products = result
            else:
                # Unexpected type, log and treat as no products
                logging.error("[%s/%s] Extraction returned unexpected type: %r", store, keyword, type(result))
                products = []
        except Exception as exc:
            logging.warning("[%s/%s] extraction attempt %d failed: %s", store, keyword, attempt, exc)
            products = []

        if products:
            break

        _autoscroll(page, steps=3, delay_ms=400)
        page.wait_for_timeout(800)

    try:
        body_text = page.inner_text("body")
    except Exception:
        body_text = ""
    # If debug_nodes present, log them for diagnosis
    if debug_nodes is not None:
        logging.warning("[%s/%s] Extraction debug nodes: %s", store, keyword, json.dumps(debug_nodes)[:1000])
        # Save debug_nodes to debug folder
        try:
            root = Path(DEBUG_DIR)
            root.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            folder = root / f"{ts}_{_safe_filename(store)}_{_safe_filename(keyword)}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "debug_nodes.json").write_text(json.dumps(debug_nodes, indent=2), encoding="utf-8")
        except Exception as exc:
            logging.warning("[%s/%s] debug_nodes write failed: %s", store, keyword, exc)
    classification = _classify_page(body_text, products)

    force_debug = RUN_ONCE and not products
    if not products:
        _maybe_write_debug(page, store, keyword, classification, url=url, force=force_debug)

    # Ensure a store field is present for downstream formatting.
    for p in products:
        p.setdefault("store", store)

    return products, classification


def get_products(page, keyword: str) -> list[dict]:
    # Backward compatible API: Blinkit-only scrape.
    products, _ = _scrape_store_products(page, "blinkit", keyword)
    return products


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _status_label(product: dict) -> str:
    if product.get("inStock"):
        return "IN STOCK"
    if product.get("outOfStock"):
        return "OUT OF STOCK"
    return "UNKNOWN"


def _format_product_line(product: dict) -> str:
    name = product.get("name", "").strip()
    price = product.get("price", "")
    quantity = product.get("quantity", "")
    parts = [name]
    if quantity:
        parts.append(quantity)
    if price:
        parts.append(price)
    return f"[{_status_label(product)}] " + " — ".join(parts)


def _ordered_products(products: list[dict]) -> list[dict]:
    return sorted(
        products,
        key=lambda p: (0 if p.get("inStock") else 1, p.get("name", "")),
    )


def _build_summary(
    keyword: str,
    products: list[dict],
    checked_at: str | None = None,
) -> str:
    location = f"{LOCATION_LANDMARK}, {LOCATION_CITY} ({LOCATION_LAT}, {LOCATION_LON})"
    header = f"\"{keyword}\" stock @ {location}"
    if checked_at:
        header += f"\nChecked: {checked_at}"
    if not products:
        return header + f"\n(no \"{keyword}\" products found)\n{search_url_for(keyword)}"
    lines = [_format_product_line(p) for p in _ordered_products(products)]
    return header + "\n" + "\n".join(lines) + f"\n{search_url_for(keyword)}"


def _build_media_items(products: list[dict]) -> list[dict]:
    items = []
    for p in _ordered_products(products):
        image = (p.get("image") or "").strip()
        if not image or not image.startswith("http"):
            continue
        items.append({"image": image, "caption": _format_product_line(p)})
    return items


def _summary_signature(products: list[dict]) -> str:
    rows = []
    for p in products:
        rows.append(
            "|".join([
                p.get("name", ""),
                p.get("quantity", ""),
                p.get("price", ""),
                "1" if p.get("inStock") else ("0" if p.get("outOfStock") else "?"),
            ])
        )
    rows.sort()
    return "\n".join(rows)


def _send_full_report(
    keyword: str,
    products: list[dict],
    checked_at: str,
    chat_id: str | None = None,
) -> None:
    summary = _build_summary(keyword, products, checked_at=checked_at)
    send_telegram_message(summary, chat_id=chat_id)
    media = _build_media_items(products)
    if media:
        send_telegram_media_group(media, chat_id=chat_id)


def _format_product_line_with_store(product: dict) -> str:
    store = (product.get("store") or "").strip().upper()
    base = _format_product_line(product)
    if store:
        return f"({store}) {base}"
    return base


def _build_summary_multi(
    keyword: str,
    store_to_products: dict[str, list[dict]],
    checked_at: str | None = None,
) -> str:
    location = (
        f"{SHARED_LOCATION_LANDMARK}, {SHARED_LOCATION_CITY} "
        f"({SHARED_LOCATION_LAT:.4f}, {SHARED_LOCATION_LON:.4f})"
    )
    header = f"\"{keyword}\" stock @ {location}"
    if checked_at:
        header += f"\nChecked: {checked_at}"

    stores = [s for s in _enabled_stores() if s in (store_to_products or {})]
    if not stores:
        return header + "\n(no stores enabled)"

    blocks: list[str] = []
    for store in stores:
        products = store_to_products.get(store) or []
        url = _store_search_url(store, keyword)
        if not products:
            blocks.append(f"{store.upper()}:\n  (no matching products)\n  {url}")
            continue
        lines = ["  " + _format_product_line(p) for p in _ordered_products(products)]
        blocks.append(f"{store.upper()}:\n" + "\n".join(lines) + f"\n  {url}")

    return header + "\n\n" + "\n\n".join(blocks)


def _build_media_items_multi(store_to_products: dict[str, list[dict]]) -> list[dict]:
    all_products: list[dict] = []
    for store, products in (store_to_products or {}).items():
        for p in (products or []):
            p.setdefault("store", store)
        all_products.extend(products or [])

    items = []
    for p in _ordered_products(all_products):
        image = (p.get("image") or "").strip()
        if not image or not image.startswith("http"):
            continue
        items.append({"image": image, "caption": _format_product_line_with_store(p)})
    return items


def _send_full_report_multi(
    keyword: str,
    store_to_products: dict[str, list[dict]],
    checked_at: str,
    chat_id: str | None = None,
) -> None:
    summary = _build_summary_multi(keyword, store_to_products, checked_at=checked_at)
    send_telegram_message(summary, chat_id=chat_id)
    media = _build_media_items_multi(store_to_products)
    if media:
        send_telegram_media_group(media, chat_id=chat_id)


# ---------------------------------------------------------------------------
# Scrape loop
# ---------------------------------------------------------------------------

def _run_once_legacy(browser, state: dict) -> None:
    with state["lock"]:
        keywords = list(state["watchlist"])
    if not keywords:
        logging.info("Watchlist is empty; nothing to scrape.")
        return

    context = _new_context(browser, "blinkit")
    page = context.new_page()
    try:
        for keyword in keywords:
            try:
                products = get_products(page, keyword)
            except Exception as exc:
                logging.error("[%s] scrape failed: %s", keyword, exc)
                continue

            logging.info("[%s] found %d product(s).", keyword, len(products))
            for product in products:
                logging.info("  %s", _format_product_line(product))

            checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            signature = _summary_signature(products)

            with state["lock"]:
                prev = state["keywords"].get(keyword, {})
                state["keywords"][keyword] = {
                    "products": products,
                    "checked_at": checked_at,
                    "signature": signature,
                }
                changed = signature != prev.get("signature")

            if changed:
                _send_full_report(keyword, products, checked_at)
                logging.info("[%s] stock status changed — Telegram report sent.", keyword)
            else:
                logging.info("[%s] no stock changes; skipping Telegram.", keyword)
    finally:
        context.close()

    # Persist state so dedup survives across processes (e.g. GitHub Actions).
    with state["lock"]:
        save_state(state["keywords"])


def run_once(browser, state: dict) -> None:
    with state["lock"]:
        keywords = list(state["watchlist"])
    if not keywords:
        logging.info("Watchlist is empty; nothing to scrape.")
        return

    stores = _enabled_stores()
    if not stores:
        logging.warning("No stores enabled.")
        return

    changed_keywords: set[str] = set()

    for store in stores:
        context = _new_context(browser, store)
        page = context.new_page()
        try:
            for keyword in keywords:
                try:
                    products, classification = _scrape_store_products(page, store, keyword)
                except Exception as exc:
                    logging.error("[%s/%s] scrape failed: %s", store, keyword, exc)
                    continue

                logging.info("[%s/%s] found %d product(s) (%s).", store, keyword, len(products), classification)
                for product in products:
                    logging.info("  %s", _format_product_line_with_store(product))

                checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                with state["lock"]:
                    kw_entry = state["keywords"].setdefault(keyword, {"stores": {}})
                    stores_state = kw_entry.setdefault("stores", {})
                    prev = stores_state.get(store, {})

                    # If the page looks blocked or location-gated, do not overwrite
                    # the last known good state and do not trigger alerts.
                    if classification in ("blocked", "location-gated"):
                        stores_state[store] = {
                            "products": prev.get("products") or [],
                            "checked_at": checked_at,
                            "signature": prev.get("signature") or "",
                            "last_error": classification,
                        }
                        changed = False
                    else:
                        signature = _summary_signature(products)
                        stores_state[store] = {
                            "products": products,
                            "checked_at": checked_at,
                            "signature": signature,
                        }
                        changed = signature != (prev.get("signature") or "")

                if changed:
                    changed_keywords.add(keyword)
        finally:
            context.close()

    for keyword in sorted(changed_keywords):
        with state["lock"]:
            kw_entry = state["keywords"].get(keyword) or {}
            store_state = (kw_entry.get("stores") or {})
            store_to_products = {
                store: (store_state.get(store) or {}).get("products") or []
                for store in stores
            }
        checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _send_full_report_multi(keyword, store_to_products, checked_at)
        logging.info("[%s] changes detected in at least one store; Telegram report sent.", keyword)

    with state["lock"]:
        save_state(state["keywords"])


# ---------------------------------------------------------------------------
# Telegram command handling
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "Multi-store Stock Notifier\n"
    "Commands:\n"
    "  /status            — show the latest report for every keyword\n"
    "  /status <keyword>  — show the latest report for one keyword\n"
    "  /watch <keyword>   — start watching a keyword\n"
    "  /unwatch <keyword> — stop watching a keyword\n"
    "  /list              — show your current watchlist\n"
    "  /help              — show this help"
)


def _parse_argument(text: str) -> str:
    """Return the part after the command word, normalized."""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return ""
    return _normalize_keyword(parts[1])


def handle_status_command(state: dict, chat_id: str, arg: str = "") -> None:
    with state["lock"]:
        watchlist = list(state["watchlist"])
        keyword_state = {k: dict(v) for k, v in state["keywords"].items()}

    targets = []
    if arg:
        # Match against current watchlist (or any cached keyword) by exact
        # normalized form.
        if arg in watchlist or arg in keyword_state:
            targets = [arg]
        else:
            send_telegram_message(
                f"Not watching \"{arg}\". Use /watch {arg} to add it, or /list to "
                f"see what's being watched.",
                chat_id=chat_id,
            )
            return
    else:
        targets = watchlist or list(keyword_state.keys())

    if not targets:
        send_telegram_message(
            "Watchlist is empty. Use /watch <keyword> to start tracking something.",
            chat_id=chat_id,
        )
        return

    pending = []
    for kw in targets:
        data = keyword_state.get(kw)
        if not data:
            pending.append(kw)
            continue
        logging.info("/status requested for %r — sending cached report.", kw)
        stores_state = data.get("stores") if isinstance(data, dict) else None
        if isinstance(stores_state, dict):
            store_to_products = {
                store: (sv.get("products") or [])
                for store, sv in stores_state.items()
                if isinstance(sv, dict)
            }
            checked_at = max(
                [(sv.get("checked_at") or "") for sv in stores_state.values() if isinstance(sv, dict)],
                default="",
            ) or "unknown"
            _send_full_report_multi(
                kw, store_to_products,
                checked_at=checked_at,
                chat_id=chat_id,
            )
        else:
            _send_full_report(
                kw, data.get("products") or [],
                checked_at=data.get("checked_at") or "unknown",
                chat_id=chat_id,
            )

    if pending:
        send_telegram_message(
            "No data yet for: " + ", ".join(f'"{k}"' for k in pending)
            + ". The next check will populate it.",
            chat_id=chat_id,
        )


def handle_watch_command(state: dict, chat_id: str, arg: str) -> None:
    if not arg:
        send_telegram_message(
            "Usage: /watch <keyword>\nExample: /watch lego",
            chat_id=chat_id,
        )
        return
    if not re.search(r"[a-z0-9]", arg):
        send_telegram_message(
            f"Keyword \"{arg}\" doesn't look searchable.",
            chat_id=chat_id,
        )
        return
    with state["lock"]:
        if arg in state["watchlist"]:
            send_telegram_message(
                f"Already watching \"{arg}\".", chat_id=chat_id,
            )
            return
        state["watchlist"].append(arg)
        save_watchlist(state["watchlist"])
        count = len(state["watchlist"])
    send_telegram_message(
        f"Now watching \"{arg}\" ({count} keyword{'s' if count != 1 else ''} total). "
        f"It'll appear in the next check, or use /status {arg} after the cycle.",
        chat_id=chat_id,
    )
    logging.info("Added %r to watchlist (now %d).", arg, count)


def handle_unwatch_command(state: dict, chat_id: str, arg: str) -> None:
    if not arg:
        send_telegram_message(
            "Usage: /unwatch <keyword>\nUse /list to see what's being watched.",
            chat_id=chat_id,
        )
        return
    with state["lock"]:
        if arg not in state["watchlist"]:
            send_telegram_message(
                f"Not watching \"{arg}\". Use /list to see what's being watched.",
                chat_id=chat_id,
            )
            return
        state["watchlist"].remove(arg)
        state["keywords"].pop(arg, None)
        save_watchlist(state["watchlist"])
        count = len(state["watchlist"])
    send_telegram_message(
        f"Stopped watching \"{arg}\". {count} keyword{'s' if count != 1 else ''} left.",
        chat_id=chat_id,
    )
    logging.info("Removed %r from watchlist (now %d).", arg, count)


def handle_list_command(state: dict, chat_id: str) -> None:
    with state["lock"]:
        items = list(state["watchlist"])
    if not items:
        send_telegram_message(
            "Watchlist is empty. Use /watch <keyword> to add one.",
            chat_id=chat_id,
        )
        return
    send_telegram_message(
        "Watching:\n" + "\n".join(f"  • {k}" for k in items),
        chat_id=chat_id,
    )


def telegram_command_loop(state: dict) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram credentials missing; commands disabled.")
        return

    try:
        requests.post(
            _telegram_api("deleteWebhook"),
            data={"drop_pending_updates": "false"},
            timeout=10,
        )
    except Exception as exc:
        logging.warning("deleteWebhook failed (continuing): %s", exc)

    offset = None
    try:
        r = requests.get(
            _telegram_api("getUpdates"),
            params={"timeout": 0, "offset": -1},
            timeout=15,
        )
        last = r.json().get("result", [])
        if last:
            offset = last[-1]["update_id"] + 1
    except Exception as exc:
        logging.warning("Initial getUpdates failed: %s", exc)

    logging.info("Telegram command loop started (offset=%s).", offset)

    while True:
        try:
            params = {"timeout": 30, "allowed_updates": json.dumps(["message"])}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(_telegram_api("getUpdates"), params=params, timeout=60)
            data = r.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or {}
                text = (msg.get("text") or "").strip()
                chat = msg.get("chat") or {}
                chat_id = chat.get("id")
                if not text or chat_id is None:
                    continue
                if str(chat_id) != str(TELEGRAM_CHAT_ID):
                    logging.info("Ignoring message from unauthorized chat %s.", chat_id)
                    continue
                command = text.split()[0].lower().split("@")[0]
                arg = _parse_argument(text)
                if command == "/status":
                    handle_status_command(state, chat_id=str(chat_id), arg=arg)
                elif command == "/watch":
                    handle_watch_command(state, chat_id=str(chat_id), arg=arg)
                elif command == "/unwatch":
                    handle_unwatch_command(state, chat_id=str(chat_id), arg=arg)
                elif command in ("/list", "/watchlist"):
                    handle_list_command(state, chat_id=str(chat_id))
                elif command in ("/start", "/help"):
                    send_telegram_message(HELP_TEXT, chat_id=str(chat_id))
        except requests.exceptions.ReadTimeout:
            continue
        except Exception as exc:
            logging.error("Telegram command loop error: %s", exc)
            time.sleep(5)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.info("Multi-store Stock Notifier (Playwright) started.")
    logging.info("Enabled stores: %s", ", ".join(_enabled_stores()) or "(none)")
    logging.info(
        "Location: %s, %s, %s (%.4f, %.4f)",
        SHARED_LOCATION_LANDMARK, SHARED_LOCATION_LOCALITY, SHARED_LOCATION_CITY,
        SHARED_LOCATION_LAT, SHARED_LOCATION_LON,
    )
    logging.info("Check interval: %d seconds", CHECK_INTERVAL)

    watchlist = load_watchlist()
    logging.info("Watchlist (%d): %s", len(watchlist), ", ".join(watchlist))

    keywords = load_state()
    if keywords:
        logging.info("Loaded persisted state for %d keyword(s).", len(keywords))

    state: dict = {
        "lock": threading.Lock(),
        "watchlist": watchlist,
        # keyword -> {"stores": {store -> {signature, checked_at, products?}}}
        "keywords": keywords,
    }

    if RUN_ONCE:
        logging.info("RUN_ONCE mode: single scrape, then exit (no command listener).")
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            try:
                run_once(browser, state)
            finally:
                browser.close()
        return

    cmd_thread = threading.Thread(
        target=telegram_command_loop, args=(state,), daemon=True, name="telegram-cmd"
    )
    cmd_thread.start()

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            while True:
                try:
                    run_once(browser, state)
                except Exception as exc:
                    logging.error("Run failed: %s", exc)
                logging.info("Sleeping %d seconds before next check...", CHECK_INTERVAL)
                time.sleep(CHECK_INTERVAL)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
