# WildBot HFT Scalper

## Overview
WildBot is a high-frequency trading (HFT) scalping bot that connects to cryptocurrency exchanges via WebSocket for real-time order book data. It implements market-making strategies with built-in risk management. Paper trading starts with $16 capital, with PnL updates sent to Telegram.

## Project Architecture

### Files
- `main.py` — Entry point. Configures the scalper, Flask web server (HTML dashboard on `/`, JSON API on `/api/status`), and runs the async event loop in a background thread.
- `hft_scalper.py` — Core HFT module containing:
  - `HFTScalper` — Main orchestrator: WebSocket connection, tick processing, order lifecycle, Telegram notifications
  - `TopOfBook` — L1 data structure (best bid/ask, quantities, spread)
  - `RiskManager` — Position limits, spread filters, stale order detection (500ms timeout), affordability checks, mark-to-market equity, 50% max drawdown stop
  - `OrderManager` — Order creation, cancellation, simulated fill checks
  - `ScalperConfig` — All tunable parameters (symbol, qty, spread threshold, etc.)
- `templates/dashboard.html` — Live-updating dark-themed trading dashboard (auto-refreshes every 2s)

### Key Features
1. **WebSocket streaming** — Real-time order book depth via `websockets` library (Kraken v2 API)
2. **Market making** — Simultaneous limit buy at best bid and limit sell at best ask
3. **Risk management** — 500ms stale order cancellation, position limits, spread filter, affordability checks, 50% drawdown auto-stop
4. **Mark-to-market equity** — Balance + unrealized PnL (position * mid_price) for accurate return tracking
5. **Session statistics** — Win/loss tracking, win rate, peak equity, max drawdown monitoring
6. **Telegram notifications** — Rate-limited PnL updates (60s min interval), critical event alerts (drawdown stop, disconnections)
7. **Live dashboard** — Professional dark-themed HTML dashboard with auto-refresh (Balance, Equity, Return%, PnL, Market Data, Activity)
8. **Async architecture** — `asyncio` for non-blocking high-speed data processing
9. **Reconnect logic** — Exponential backoff on disconnection

### Configuration (in main.py)
- `symbol` — Trading pair (default: BTC/USD)
- `ws_url` — WebSocket endpoint (default: Kraken v2)
- `ws_symbol` — Exchange-specific symbol for subscription
- `order_qty` — Order size per side (default: 0.001)
- `max_spread_bps` — Maximum acceptable spread in basis points
- `stale_order_ms` — Cancel orders older than this (default: 500ms)
- `max_position` — Maximum net position allowed
- `starting_capital` — Paper trading capital (default: $16)

### Web Server
- Flask serves HTML dashboard on `/` with live-updating stats
- JSON API on `/api/status` for programmatic access
- Health check on `/health`
- Cache-Control: no-cache headers on all responses
- Production deployment uses gunicorn
- Scalper runs in a background thread alongside the web server

### Telegram Integration
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` stored as Replit Secrets
- Rate-limited notifications (60s minimum between updates)
- Critical alerts bypass rate limiting (drawdown stops, disconnections)

## Tech Stack
- Python 3.11
- websockets (async WebSocket client)
- asyncio (concurrency)
- Flask + gunicorn (web server / deployment)
- requests (Telegram API)

## Recent Changes
- 2026-02-12: Built professional HTML live dashboard with dark theme and auto-refresh
- 2026-02-12: Added session statistics (win/loss, win rate, peak equity, max drawdown)
- 2026-02-12: Enhanced risk management with affordability checks, mark-to-market equity, 50% drawdown stop
- 2026-02-12: Integrated Telegram notifications with rate limiting
- 2026-02-12: Added $16 starting capital tracking with balance, realized PnL, and return %
- 2026-02-12: Switched L1 data source from Binance to Kraken WebSocket v2 API (no IP restrictions)
- 2026-02-12: Added Flask web server and gunicorn deployment config
- 2026-02-12: Initial creation of HFT scalper module with WebSocket L1 monitoring, market making, and risk management
