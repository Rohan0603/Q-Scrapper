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
- Notifies once per unique product (name + quantity + price). When a product goes out of stock and back in, it will notify again.
