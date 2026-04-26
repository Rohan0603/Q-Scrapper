"""Blinkit Hotwheels Stock Notifier (Playwright edition).

Loads Blinkit search results for "hotwheels" with the user's location set,
extracts every Hot Wheels product card from the rendered DOM, and sends a
Telegram message the first time each product is detected in stock.
"""

import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

HOTWHEELS_SEARCH_URL = os.environ.get(
    "HOTWHEELS_SEARCH_URL", "https://www.blinkit.com/s/?q=hotwheels"
)
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

EXTRACT_PRODUCTS_JS = r"""
() => {
  const titles = document.querySelectorAll('.tw-line-clamp-2');
  const items = [];
  const seen = new Set();
  const HOTWHEELS_RE = /hot[\s-]?wheels/i;
  // Header / breadcrumb patterns to ignore.
  const SKIP_RE = /^(showing\s+results?|search\s+results?|showing\s+related)/i;

  for (const t of titles) {
    const name = (t.textContent || '').trim();
    if (!name || name.length < 5) continue;
    if (SKIP_RE.test(name)) continue;
    if (!HOTWHEELS_RE.test(name)) continue;

    // Walk up until we find the smallest ancestor that contains BOTH
    // a [role="button"] (ADD/Notify Me) AND a product image (not an
    // eta/icon helper graphic). The button is in a deeper wrapper than
    // the image on Blinkit, so we have to keep climbing.
    const isProductImage = (im) => {
      const src = im.currentSrc || im.src || im.getAttribute('data-src') || '';
      if (!src) return false;
      if (/\/(eta-icons|icons|badges|store-icons|brand-images?)\//i.test(src)) return false;
      // Tiny rendered images are decorative.
      if ((im.naturalWidth && im.naturalWidth < 40) || (im.width && im.width < 40)) return false;
      return true;
    };
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
      // Fallback: any ancestor with a button (image-less card).
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
    if (!/₹/.test(cardText)) continue;  // Must look like a product card.

    // Stock detection: prefer button label, fall back to card text.
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

    // Extract product image URL (skip eta/icon helper graphics).
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
}
"""


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
                # Telegram caption limit is 1024 characters.
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


def get_hotwheels_products(page) -> list[dict]:
    try:
        page.goto(HOTWHEELS_SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
    except PWTimeout:
        logging.warning("Search page timed out on initial load; continuing.")
    page.wait_for_timeout(4000)
    # Wait for product cards (any ADD button) to appear before scrolling.
    try:
        page.wait_for_function(
            "document.body && document.body.innerText.includes('ADD')",
            timeout=15000,
        )
    except PWTimeout:
        logging.warning("No ADD buttons appeared within 15s; continuing anyway.")

    _autoscroll(page)
    # Scroll back to the top so all cards have rendered their interactive bits.
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(1500)

    try:
        products = page.evaluate(EXTRACT_PRODUCTS_JS)
    except Exception as exc:
        logging.error("Failed to extract products: %s", exc)
        return []

    if not products:
        body_text = page.inner_text("body").lower()
        if "select your location" in body_text or "detect my location" in body_text:
            logging.warning(
                "Blinkit is asking for a delivery location. Location cookies "
                "may not have taken effect."
            )
    return products


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


def _build_summary(products: list[dict], checked_at: str | None = None) -> str:
    location = f"{LOCATION_LANDMARK}, {LOCATION_CITY} ({LOCATION_LAT}, {LOCATION_LON})"
    header = f"Hot Wheels stock @ {location}"
    if checked_at:
        header += f"\nChecked: {checked_at}"
    if not products:
        return header + "\n(no Hot Wheels products found)"
    lines = [_format_product_line(p) for p in _ordered_products(products)]
    return header + "\n" + "\n".join(lines) + f"\n{HOTWHEELS_SEARCH_URL}"


def _build_media_items(products: list[dict]) -> list[dict]:
    items = []
    for p in _ordered_products(products):
        image = (p.get("image") or "").strip()
        if not image or not image.startswith("http"):
            continue
        items.append({"image": image, "caption": _format_product_line(p)})
    return items


def _summary_signature(products: list[dict]) -> str:
    """A stable string that changes whenever any product's stock status changes."""
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


def _send_full_report(products: list[dict], checked_at: str, chat_id: str | None = None) -> None:
    summary = _build_summary(products, checked_at=checked_at)
    send_telegram_message(summary, chat_id=chat_id)
    media = _build_media_items(products)
    if media:
        send_telegram_media_group(media, chat_id=chat_id)


def run_once(browser, state: dict) -> None:
    context = _new_context(browser)
    page = context.new_page()
    try:
        products = get_hotwheels_products(page)
        logging.info("Found %d Hot Wheels product(s).", len(products))
        for product in products:
            logging.info(_format_product_line(product))

        checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        signature = _summary_signature(products)

        # Cache for /status command.
        state["products"] = products
        state["checked_at"] = checked_at

        if signature != state.get("signature"):
            _send_full_report(products, checked_at)
            state["signature"] = signature
            logging.info("Stock status changed — Telegram report sent.")
        else:
            logging.info("No stock changes since last check; skipping Telegram.")
    finally:
        context.close()


def telegram_command_loop(state: dict) -> None:
    """Background thread: long-poll Telegram for /status commands."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram credentials missing; /status command disabled.")
        return

    # Make sure no webhook is set (otherwise getUpdates is rejected).
    try:
        requests.post(_telegram_api("deleteWebhook"), data={"drop_pending_updates": "false"}, timeout=10)
    except Exception as exc:
        logging.warning("deleteWebhook failed (continuing): %s", exc)

    # Skip backlog: only react to messages that arrive after startup.
    offset = None
    try:
        r = requests.get(_telegram_api("getUpdates"), params={"timeout": 0, "offset": -1}, timeout=15)
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
                # Only respond to the configured chat.
                if str(chat_id) != str(TELEGRAM_CHAT_ID):
                    logging.info("Ignoring message from unauthorized chat %s.", chat_id)
                    continue
                command = text.split()[0].lower().split("@")[0]
                if command == "/status":
                    handle_status_command(state, chat_id=str(chat_id))
                elif command == "/start" or command == "/help":
                    send_telegram_message(
                        "Hot Wheels stock notifier.\n"
                        "Commands:\n"
                        "  /status — show the latest stock report",
                        chat_id=str(chat_id),
                    )
        except requests.exceptions.ReadTimeout:
            continue
        except Exception as exc:
            logging.error("Telegram command loop error: %s", exc)
            time.sleep(5)


def handle_status_command(state: dict, chat_id: str) -> None:
    products = state.get("products")
    checked_at = state.get("checked_at")
    if products is None:
        send_telegram_message(
            "Still running the first stock check, please try /status again in a minute.",
            chat_id=chat_id,
        )
        return
    logging.info("/status requested — sending cached report.")
    _send_full_report(products, checked_at=checked_at or "unknown", chat_id=chat_id)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.info("Blinkit Hotwheels Stock Notifier (Playwright) started.")
    logging.info(
        "Location: %s, %s, %s (%.4f, %.4f)",
        LOCATION_LANDMARK, LOCATION_LOCALITY, LOCATION_CITY,
        LOCATION_LAT, LOCATION_LON,
    )
    logging.info("Check interval: %d seconds", CHECK_INTERVAL)

    state: dict = {"signature": None, "products": None, "checked_at": None}

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
