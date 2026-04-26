# Blinkit Stock Notifier

A Python script that watches [Blinkit](https://www.blinkit.com) (Indian
grocery / quick-commerce) for products matching a list of keywords at your
specific delivery location, and pushes Telegram alerts whenever stock
status, prices, or the product set changes.

Default watchlist: `hotwheels`. You can add more keywords (e.g. `lego`,
`barbie`, anything Blinkit sells) over Telegram once it's running.

## What it does

- Loads Blinkit's search results in a real headless Chromium browser
  (using Playwright) so the page renders properly.
- Sets your delivery location via cookies + browser geolocation
  (Whitefield, Bengaluru by default — configurable).
- For every keyword in your watchlist, extracts each product's name,
  price, quantity, in-stock status, and image.
- Sends a single Telegram summary message per keyword followed by an
  album of product images, **only when something has changed** since the
  previous check (to avoid spamming you).
- Listens for these Telegram commands (when running continuously):
  - `/status` — show the latest report for every keyword
  - `/status <keyword>` — show the latest report for one keyword
  - `/watch <keyword>` — start watching a keyword
  - `/unwatch <keyword>` — stop watching a keyword
  - `/list` — show your current watchlist
  - `/help` — show command help

## Files

| File | Purpose |
| --- | --- |
| `main.py` | The entire script (scraper + Telegram bot in one file) |
| `requirements.txt` | Python dependencies |
| `watchlist.json` | Auto-created on first `/watch`; persists your keywords |
| `replit.md` | Workspace notes |
| `README.md` | You are here |

## Required secrets / environment variables

| Name | Required | Description |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | yes | From [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | yes | Your numeric chat ID (e.g. from [@userinfobot](https://t.me/userinfobot)) |
| `CHECK_INTERVAL` | no | Seconds between scrape cycles (default `600` = 10 min) |
| `BLINKIT_LAT` | no | Delivery latitude (default `12.9784` — Whitefield, Bengaluru) |
| `BLINKIT_LON` | no | Delivery longitude (default `77.7506`) |
| `BLINKIT_LOCALITY` | no | e.g. `Pattandur Agrahara` |
| `BLINKIT_LANDMARK` | no | e.g. `Whitefield` |
| `BLINKIT_CITY` | no | e.g. `Bengaluru` |
| `BLINKIT_STATE` | no | e.g. `Karnataka` |
| `WATCHLIST_PATH` | no | Path to the watchlist JSON file (default `watchlist.json`) |

## Run locally (laptop / desktop / Raspberry Pi)

Requires Python 3.11+ and a system Chromium / Chrome installation.

```bash
# 1. Get the code
git clone <your-repo-url>
cd <repo>

# 2. Create a virtualenv and install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. (One-time) Install a browser for Playwright if your system
#    doesn't already have Chromium on $PATH.
python -m playwright install chromium

# 4. Set the two required secrets
export TELEGRAM_BOT_TOKEN="..."   # from BotFather
export TELEGRAM_CHAT_ID="..."     # your chat ID

# 5. Run
python -u main.py
```

To keep it running after you close the terminal:
- macOS / Linux: `nohup python -u main.py > notifier.log 2>&1 &`
- Linux (production): wrap it in a `systemd` user service.
- Raspberry Pi: same as Linux above; works great on a Pi 4 / Zero 2 W.

## Hosting options compared

| Where | Cost | 24/7? | `/status` & `/watch` work? | Effort |
| --- | --- | --- | --- | --- |
| **Your own laptop** | Free | Only when on | Yes | Lowest |
| **Raspberry Pi at home** | One-time hardware | Yes | Yes | Low |
| **Oracle Cloud Free Tier VM** | Free forever | Yes | Yes | Medium |
| **GitHub Actions (cron)** | Free | Scheduled only | **No** (cron only) | Medium |
| **Google Colab** | Free | **No** (~90 min idle, 12 h max) | Only while tab is open | Not recommended |
| **Replit Autoscale Deploy** | Free tier | No (scales to zero) | No (no HTTP triggers) | Not suitable |
| **Replit Reserved VM** | Paid | Yes | Yes | Easiest paid option |

### Why not Google Colab

Colab is built for interactive notebooks. Free Colab sessions disconnect
after ~90 minutes of inactivity and cap at ~12 hours total. The notebook
also stops the moment you close the browser tab. `watchlist.json`
disappears between sessions unless you mount Google Drive, and even then
you'd be manually re-running the notebook daily. It is not a viable home
for an unattended background notifier.

### GitHub Actions (recommended free option for periodic alerts)

GitHub Actions can run `main.py` on a cron schedule (e.g. every 15
minutes) for free, indefinitely. The trade-off:

- ✅ **Periodic stock-change alerts** — fully supported.
- ❌ **Interactive `/status`, `/watch`, `/unwatch` commands** — won't work,
  because GHA only runs on a schedule, not continuously.

To make this script GHA-friendly we'd need to:
1. Add a "run once then exit" mode so the cron job doesn't loop forever.
2. Persist `watchlist.json` and the dedup signatures between runs (via
   `actions/cache` or by committing them back to the repo) so we don't
   re-send the same alert every cron tick.
3. Add `.github/workflows/check.yml` with a `schedule:` trigger and the
   two Telegram secrets as repository secrets.

If you'd like, ask the agent to wire this up — it's a small refactor
plus one workflow file.

### Oracle Cloud Always-Free VM

Oracle gives away a small ARM VM permanently. Push this repo to GitHub,
SSH into the VM, follow the "Run locally" steps above, and run under
`systemd` or `nohup`. Best free option if you want the full feature set
(periodic alerts **and** Telegram commands) running 24/7.

## Adjusting the location

The defaults target Whitefield, Bengaluru (pincode 560066). To watch a
different location, set `BLINKIT_LAT`, `BLINKIT_LON`, `BLINKIT_LOCALITY`,
`BLINKIT_LANDMARK`, `BLINKIT_CITY`, `BLINKIT_STATE` to your delivery
address. The lat/lon must match the area Blinkit serves; otherwise it
will show "select your delivery location" instead of products.

## Troubleshooting

- **No products found / "select your delivery location" warnings** — your
  lat/lon don't map to a Blinkit-serviced area. Try the coordinates of a
  known address in your city.
- **`Telegram credentials missing`** — `TELEGRAM_BOT_TOKEN` /
  `TELEGRAM_CHAT_ID` aren't exported to the process.
- **`/status` is silent** — make sure no other process is also polling
  the same bot (Telegram only delivers each message to one consumer).
- **Browser fails to launch** — install Chromium with
  `python -m playwright install chromium`.
