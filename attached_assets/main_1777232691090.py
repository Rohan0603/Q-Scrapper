# Blinkit Hotwheels Stock Notifier
# Main script for Replit deployment

import time
import logging


import requests
import os

# --- Telegram Bot Config ---
# For local testing, you can set these directly. For Replit, use secrets.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8728684147:AAEv5DiF9ZdbPu4wuE08gQVPffaQfoKzCEM")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "7301099717")  # User's chat id

# --- Telegram Notification Function ---
def send_telegram_message(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        response = requests.post(url, data=payload, timeout=10)
        response.raise_for_status()
        logging.info(f"Sent Telegram message: {message}")
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")

# --- Blinkit Hotwheels Scraper ---
from bs4 import BeautifulSoup

# Replace with the actual Blinkit Hotwheels category URL
HOTWHEELS_CATEGORY_URL = "https://www.blinkit.com/cn/hotwheels"

def get_hotwheels_product_urls():
    """
    Scrape all Hotwheels product URLs from the Blinkit category page.
    Returns a list of product URLs.
    """
    try:
        resp = requests.get(HOTWHEELS_CATEGORY_URL, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        product_links = []
        # This selector may need adjustment based on actual Blinkit HTML structure
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/p/" in href and "hotwheels" in href:
                # Ensure full URL
                if href.startswith("http"):
                    product_links.append(href)
                else:
                    product_links.append("https://www.blinkit.com" + href)
        logging.info(f"Found {len(product_links)} Hotwheels product URLs.")
        return list(set(product_links))
    except Exception as e:
        logging.error(f"Error scraping Hotwheels URLs: {e}")
        return []

def check_stock_and_notify(product_urls, check_interval=600):
    """
    For each product URL, check if it's in stock. If so, send a Telegram notification.
    Runs in a loop with the specified interval (default: 600s = 10min).
    """
    notified = set()
    while True:
        for url in product_urls:
            try:
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")
                # Adjust selector as needed for Blinkit
                out_of_stock = False
                if soup.find(string=lambda t: t and "out of stock" in t.lower()):
                    out_of_stock = True
                if not out_of_stock and url not in notified:
                    msg = f"Hotwheels in stock! {url}"
                    send_telegram_message(msg)
                    notified.add(url)
                    logging.info(f"In stock: {url}")
                elif out_of_stock:
                    logging.info(f"Out of stock: {url}")
            except Exception as e:
                logging.error(f"Error checking stock for {url}: {e}")
        logging.info(f"Sleeping for {check_interval} seconds before next check...")
        time.sleep(check_interval)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.info("Blinkit Hotwheels Stock Notifier started.")
    # Scrape Hotwheels URLs
    urls = get_hotwheels_product_urls()
    if urls:
        check_stock_and_notify(urls, check_interval=600)  # 10 minutes
    else:
        logging.error("No Hotwheels URLs found. Exiting.")
