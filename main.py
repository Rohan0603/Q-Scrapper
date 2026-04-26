"""Blinkit Hotwheels Stock Notifier (Playwright edition).

Polls a Blinkit Hotwheels category page using a headless Chromium browser,
finds product links, checks each product page for stock availability, and
sends a Telegram message the first time a product is detected in stock.
"""

import logging
import os
import shutil
import time

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

HOTWHEELS_CATEGORY_URL = os.environ.get(
    "HOTWHEELS_CATEGORY_URL", "https://www.blinkit.com/cn/hotwheels"
)
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "600"))

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)

OUT_OF_STOCK_PHRASES = (
    "out of stock",
    "currently unavailable",
    "notify me when available",
    "sold out",
)
IN_STOCK_PHRASES = ("add to cart", "add to bag")


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
    return browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1366, "height": 900},
        locale="en-IN",
        timezone_id="Asia/Kolkata",
    )


def _autoscroll(page, steps: int = 8, delay_ms: int = 700) -> None:
    for _ in range(steps):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(delay_ms)


def get_hotwheels_product_urls(page) -> list[str]:
    try:
        page.goto(HOTWHEELS_CATEGORY_URL, wait_until="domcontentloaded", timeout=30000)
    except PWTimeout:
        logging.warning("Category page timed out on initial load; continuing.")
    page.wait_for_timeout(2500)
    _autoscroll(page)

    hrefs = page.eval_on_selector_all(
        "a[href]", "els => els.map(e => e.href).filter(Boolean)"
    )
    product_urls = set()
    for href in hrefs:
        lowered = href.lower()
        if "blinkit.com" not in lowered:
            continue
        if "/prn/" in lowered or "/p/" in lowered:
            if "hotwheels" in lowered or "hot-wheels" in lowered or "/prn/" in lowered:
                product_urls.add(href.split("#")[0])
    logging.info("Found %d candidate product URLs.", len(product_urls))

    if not product_urls:
        body_text = page.inner_text("body").lower()
        if "select your location" in body_text or "detect my location" in body_text:
            logging.warning(
                "Blinkit is asking for a delivery location. The category page "
                "won't show products until a pincode/location is set."
            )
    return sorted(product_urls)


def is_in_stock(page, url: str) -> bool | None:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PWTimeout:
        logging.warning("Product page timed out: %s", url)
        return None
    page.wait_for_timeout(2000)
    _autoscroll(page, steps=2, delay_ms=400)

    try:
        text = page.inner_text("body").lower()
    except Exception as exc:
        logging.error("Failed to read product page %s: %s", url, exc)
        return None

    if any(phrase in text for phrase in OUT_OF_STOCK_PHRASES):
        return False
    if any(phrase in text for phrase in IN_STOCK_PHRASES):
        return True
    return None


def run_once(browser, notified: set[str]) -> None:
    context = _new_context(browser)
    page = context.new_page()
    try:
        urls = get_hotwheels_product_urls(page)
        for url in urls:
            try:
                stock = is_in_stock(page, url)
                if stock is True and url not in notified:
                    send_telegram_message(f"Hotwheels in stock! {url}")
                    notified.add(url)
                    logging.info("In stock: %s", url)
                elif stock is False:
                    logging.info("Out of stock: %s", url)
                else:
                    logging.info("Stock status unknown: %s", url)
            except Exception as exc:
                logging.error("Error checking %s: %s", url, exc)
    finally:
        context.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.info("Blinkit Hotwheels Stock Notifier (Playwright) started.")

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
