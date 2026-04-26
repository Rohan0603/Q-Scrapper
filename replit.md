# Workspace

## Overview

pnpm workspace monorepo using TypeScript. Each package manages its own dependencies.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- `pnpm --filter @workspace/api-server run dev` — run API server locally

See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details.

## Blinkit Hotwheels Stock Notifier (Python)

A standalone Python script at the project root that watches Blinkit for Hot Wheels availability and pushes Telegram alerts.

- **Entrypoint**: `main.py` (run by the `Stock Notifier` workflow as `python -u main.py`)
- **Dependencies**: `requirements.txt` — `requests`, `beautifulsoup4`, `python-telegram-bot`, `playwright` (uses system Chromium installed via Nix)
- **Required secrets**: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- **Optional env vars**: `HOTWHEELS_SEARCH_URL`, `CHECK_INTERVAL` (seconds, default 600), `BLINKIT_LAT`, `BLINKIT_LON`, `BLINKIT_LOCALITY`, `BLINKIT_LANDMARK`, `BLINKIT_CITY`, `BLINKIT_STATE` — used to set Blinkit's location cookies so the search returns real product listings
- Each check sends a single Telegram **summary message** listing every Hot Wheels product with `[IN STOCK]` / `[OUT OF STOCK]` status, name, qty, and price, followed by a **media group** of product images (each captioned with the same line). Re-sends only when any product's stock status, set, or price changes (deduplicated via a status fingerprint).
- Listens for the `/status` Telegram command (long-polling `getUpdates` in a background thread) and replies with the latest cached report (text + photos). Only responds to the configured `TELEGRAM_CHAT_ID`. Also handles `/start` and `/help`.
- Supports a watchlist (default `["hotwheels"]`) persisted in `watchlist.json`. Telegram commands `/watch <keyword>`, `/unwatch <keyword>`, `/list`, and `/status [<keyword>]` manage and report on it. Each scrape cycle iterates every watchlist keyword.
- Hosted on GitHub Actions via `.github/workflows/check.yml` (cron every ~15 min). When env var `RUN_ONCE=1` is set, `main.py` does a single scrape across the watchlist and exits — no Telegram listener, no sleep loop. Per-keyword dedup signatures are persisted in `state.json` (gitignored, restored/saved via `actions/cache` between runs).
