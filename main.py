"""Blinkit Stock Notifier (Playwright edition).

Watches Blinkit search results for a configurable list of keywords (the
"watchlist") with the user's location set, extracts every matching product
card from the rendered DOM, and sends Telegram messages whenever the stock
status of any product changes. Also supports interactive commands over
Telegram (/status, /watch, /unwatch, /list, /help).
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

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "600"))

LOCATION_LAT = float(os.environ.get("BLINKIT_LAT", "12.9784"))
LOCATION_LON = float(os.environ.get("BLINKIT_LON", "77.7506"))
LOCATION_LOCALITY = os.environ.get("BLINKIT_LOCALITY", "Pattandur Agrahara")
LOCATION_LANDMARK = os.environ.get("BLINKIT_LANDMARK", "Whitefield")
LOCATION_CITY = os.environ.get("BLINKIT_CITY", "Bengaluru")
LOCATION_STATE = os.environ.get("BLINKIT_STATE", "Karnataka")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)

WATCHLIST_PATH = os.environ.get("WATCHLIST_PATH", "watchlist.json")
DEFAULT_WATCHLIST = ["hotwheels"]

STATE_PATH = os.environ.get("STATE_PATH", "state.json")

# When set, do one scrape across the watchlist and exit (no Telegram
# command listener, no sleep loop). Designed for cron-style hosts like
# GitHub Actions.
RUN_ONCE = os.environ.get("RUN_ONCE", "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Watchlist persistence
# ---------------------------------------------------------------------------

def _normalize_keyword(kw: str) -> str:
    """Canonical form used for matching/dedup; preserves spaces for display."""
    return " ".join((kw or "").lower().split())


def load_watchlist() -> list[str]:
    if os.path.exists(WATCHLIST_PATH):
        try:
            with open(WATCHLIST_PATH, "r") as f:
                data = json.load(f)
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
        with open(WATCHLIST_PATH, "w") as f:
            json.dump(items, f, indent=2)
    except Exception as exc:
        logging.error("Failed to save watchlist: %s", exc)


def load_state() -> dict:
    """Load persisted per-keyword state (signatures + checked_at)."""
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r") as f:
                data = json.load(f)
            keywords = data.get("keywords") if isinstance(data, dict) else None
            if isinstance(keywords, dict):
                return {
                    k: {
                        "signature": v.get("signature") or "",
                        "checked_at": v.get("checked_at") or "",
                    }
                    for k, v in keywords.items()
                    if isinstance(v, dict)
                }
        except Exception as exc:
            logging.warning("Failed to read %s: %s", STATE_PATH, exc)
    return {}


def save_state(keywords: dict) -> None:
    """Persist per-keyword signatures so dedup survives restarts."""
    payload = {
        "keywords": {
            k: {
                "signature": v.get("signature") or "",
                "checked_at": v.get("checked_at") or "",
            }
            for k, v in keywords.items()
        }
    }
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(payload, f, indent=2)
    except Exception as exc:
        logging.error("Failed to save state: %s", exc)


def search_url_for(keyword: str) -> str:
    return "https://www.blinkit.com/s/?q=" + urllib.parse.quote(keyword)


# ---------------------------------------------------------------------------
# Browser-side extraction
# ---------------------------------------------------------------------------

EXTRACT_PRODUCTS_JS_TEMPLATE = r"""
(() => {
  const KEYWORD = __KEYWORD__;
  const normalize = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
  const targetNorm = normalize(KEYWORD);

  const titles = document.querySelectorAll('.tw-line-clamp-2');
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
    return EXTRACT_PRODUCTS_JS_TEMPLATE.replace("__KEYWORD__", json.dumps(keyword))


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
    chromium_path = shutil.which("chromium") or shutil.which("chromium-browser")
    launch_kwargs = {
        "headless": True,
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    }
    if chromium_path:
        launch_kwargs["executable_path"] = chromium_path
    return playwright.chromium.launch(**launch_kwargs)


def _new_context(browser):
    context = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1366, "height": 900},
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        geolocation={"latitude": LOCATION_LAT, "longitude": LOCATION_LON},
        permissions=["geolocation"],
    )
    cookie_base = {"domain": ".blinkit.com", "path": "/"}
    context.add_cookies([
        {"name": "gr_1_lat", "value": str(LOCATION_LAT), **cookie_base},
        {"name": "gr_1_lon", "value": str(LOCATION_LON), **cookie_base},
        {"name": "gr_1_locality", "value": LOCATION_LOCALITY, **cookie_base},
        {"name": "gr_1_landmark", "value": LOCATION_LANDMARK, **cookie_base},
        {"name": "gr_1_city", "value": LOCATION_CITY, **cookie_base},
        {"name": "gr_1_state", "value": LOCATION_STATE, **cookie_base},
    ])
    return context


def _autoscroll(page, steps: int = 10, delay_ms: int = 600) -> None:
    for _ in range(steps):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(delay_ms)


def get_products(page, keyword: str) -> list[dict]:
    url = search_url_for(keyword)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PWTimeout:
        logging.warning("[%s] search page timed out on initial load; continuing.", keyword)
    page.wait_for_timeout(4000)
    try:
        page.wait_for_function(
            "document.body && document.body.innerText.includes('ADD')",
            timeout=15000,
        )
    except PWTimeout:
        logging.warning("[%s] no ADD buttons appeared within 15s; continuing.", keyword)

    _autoscroll(page)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(1500)

    try:
        products = page.evaluate(_extraction_js(keyword))
    except Exception as exc:
        logging.error("[%s] failed to extract products: %s", keyword, exc)
        return []

    if not products:
        body_text = page.inner_text("body").lower()
        if "select your location" in body_text or "detect my location" in body_text:
            logging.warning(
                "[%s] Blinkit is asking for a delivery location. Location "
                "cookies may not have taken effect.", keyword,
            )
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


# ---------------------------------------------------------------------------
# Scrape loop
# ---------------------------------------------------------------------------

def run_once(browser, state: dict) -> None:
    with state["lock"]:
        keywords = list(state["watchlist"])
    if not keywords:
        logging.info("Watchlist is empty; nothing to scrape.")
        return

    context = _new_context(browser)
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


# ---------------------------------------------------------------------------
# Telegram command handling
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "Blinkit Stock Notifier\n"
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
    logging.info("Blinkit Stock Notifier (Playwright) started.")
    logging.info(
        "Location: %s, %s, %s (%.4f, %.4f)",
        LOCATION_LANDMARK, LOCATION_LOCALITY, LOCATION_CITY,
        LOCATION_LAT, LOCATION_LON,
    )
    logging.info("Check interval: %d seconds", CHECK_INTERVAL)

    watchlist = load_watchlist()
    logging.info("Watchlist (%d): %s", len(watchlist), ", ".join(watchlist))

    persisted = load_state()
    if persisted:
        logging.info("Loaded persisted state for %d keyword(s).", len(persisted))

    state: dict = {
        "lock": threading.Lock(),
        "watchlist": watchlist,
        # keyword -> {signature, checked_at, products?}
        "keywords": {k: dict(v) for k, v in persisted.items()},
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
