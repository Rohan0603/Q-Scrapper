"""Blinkit Hotwheels Stock Notifier (Playwright edition).

Loads Blinkit search results for "hotwheels" with the user's location set,
extracts every Hot Wheels product card from the rendered DOM, and sends a
Telegram message the first time each product is detected in stock.
"""

import logging
import os
import shutil
import time

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
    // Must contain a price marker nearby, otherwise it's probably a header.

    // Walk up until we find the card container that holds an [role="button"].
    let card = t;
    let foundButton = false;
    for (let i = 0; i < 15; i++) {
      if (!card.parentElement) break;
      card = card.parentElement;
      if (card.querySelector('[role="button"]')) {
        foundButton = true;
        break;
      }
    }
    if (!foundButton) continue;

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

    const key = name + '|' + quantity + '|' + price;
    if (seen.has(key)) continue;
    seen.add(key);
    items.push({ name, price, quantity, inStock, outOfStock, buttonLabel });
  }
  return items;
}
"""


def send_telegram_message(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Telegram credentials missing; cannot send message.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        response = requests.post(url, data=payload, timeout=10)
        response.raise_for_status()
        logging.info("Sent Telegram message: %s", message)
    except Exception as exc:
        logging.error("Failed to send Telegram message: %s", exc)


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


def run_once(browser, notified: set[str]) -> None:
    context = _new_context(browser)
    page = context.new_page()
    try:
        products = get_hotwheels_products(page)
        logging.info("Found %d Hot Wheels product(s).", len(products))
        for product in products:
            name = product.get("name", "").strip()
            price = product.get("price", "")
            quantity = product.get("quantity", "")
            in_stock = bool(product.get("inStock"))
            label_parts = [name]
            if quantity:
                label_parts.append(quantity)
            if price:
                label_parts.append(price)
            label = " — ".join(label_parts)
            button_label = product.get("buttonLabel", "")

            key = f"{name}|{quantity}|{price}"
            if in_stock:
                if key in notified:
                    logging.info("Still in stock (already notified): %s", label)
                else:
                    msg = (
                        "Hot Wheels in stock on Blinkit!\n"
                        f"{label}\n{HOTWHEELS_SEARCH_URL}"
                    )
                    send_telegram_message(msg)
                    notified.add(key)
                    logging.info("In stock (notified): %s", label)
            else:
                logging.info("Not in stock [%s]: %s", button_label or "?", label)
                # Allow re-notification next time it comes back in stock.
                notified.discard(key)
    finally:
        context.close()


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

    notified: set[str] = set()
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            while True:
                try:
                    run_once(browser, notified)
                except Exception as exc:
                    logging.error("Run failed: %s", exc)
                logging.info("Sleeping %d seconds before next check...", CHECK_INTERVAL)
                time.sleep(CHECK_INTERVAL)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
