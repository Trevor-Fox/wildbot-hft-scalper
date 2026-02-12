# WildBot HFT Scalper

## Overview
WildBot is a high-frequency trading (HFT) scalping bot that connects to cryptocurrency exchanges via WebSocket for real-time order book data. It implements market-making strategies with built-in risk management, multiple trading strategies (microprice, inventory skew, momentum filter), and comprehensive analytics. Paper trading starts with $16 capital, with PnL updates sent to Telegram.

## Project Architecture

### Files
- `main.py` — Entry point. Configures the scalper, Flask web server (HTML dashboard on `/`, JSON API on `/api/status`), and runs the async event loop in a background thread.
- `hft_scalper.py` — Core HFT module containing:
  - `HFTScalper` — Main orchestrator: WebSocket connection, tick processing, order lifecycle, Telegram notifications, EMA/momentum tracking
  - `TopOfBook` — L1 data structure (best bid/ask, quantities, spread, microprice)
  - `RiskManager` — Position limits, spread filters, stale order detection (500ms timeout), affordability checks, mark-to-market equity, 50% max drawdown stop, average cost tracking, round-trip PnL, inventory skew pricing
  - `OrderManager` — Order creation, cancellation, simulated fill checks with 200ms cooldown
  - `ScalperConfig` — All tunable parameters (symbol, qty, spread threshold, strategies, etc.)
  - `TelegramNotifier` — Rate-limited notifications, alerts, daily summaries
- `templates/dashboard.html` — Live-updating dark-themed trading dashboard (auto-refreshes every 2s)

### Key Features
1. **WebSocket streaming** — Real-time order book depth via `websockets` library (Kraken v2 API)
2. **Market making** — Simultaneous limit buy/sell with inventory-skewed pricing
3. **Microprice** — Volume-weighted fair price from order book for smarter order placement
4. **Inventory Skew** — Biases prices based on current position to reduce inventory risk
5. **Momentum Filter** — EMA-based trend detection; skips orders against strong moves
6. **Risk management** — 500ms stale order cancellation, position limits, spread filter, affordability checks, 50% drawdown auto-stop
7. **Fill cooldown** — 200ms minimum between fills to prevent cascading; max 1 fill per tick
8. **Average cost tracking** — Weighted average entry price for accurate unrealized PnL
9. **Round-trip PnL** — Win/loss counted on complete position round-trips (position returns to zero), not per-fill
10. **Mark-to-market equity** — Balance + position_value for accurate return tracking
11. **Session statistics** — Win/loss tracking, win rate, peak equity, max drawdown monitoring
12. **Telegram notifications** — Rate-limited PnL updates (60s), critical alerts (5s cooldown), daily summary
13. **Trade history** — Last 50 trades stored, last 20 displayed on dashboard
14. **Live dashboard** — Professional dark-themed HTML dashboard with strategy indicators, trade table, auto-refresh
15. **Async architecture** — `asyncio` for non-blocking high-speed data processing
16. **Reconnect logic** — Exponential backoff on disconnection

### Configuration (in main.py)
- `symbol` — Trading pair (default: BTC/USD)
- `ws_url` — WebSocket endpoint (default: Kraken v2)
- `ws_symbol` — Exchange-specific symbol for subscription
- `order_qty` — Order size per side (default: 0.001)
- `max_spread_bps` — Maximum acceptable spread in basis points
- `stale_order_ms` — Cancel orders older than this (default: 500ms)
- `max_position` — Maximum net position allowed
- `starting_capital` — Paper trading capital (default: $16)
- `fill_cooldown_ms` — Minimum time between fills (default: 200ms)
- `skew_factor` — How aggressively to skew prices for inventory (default: 0.5)
- `ema_window` — EMA period for momentum detection (default: 50)
- `momentum_filter_enabled` — Enable/disable momentum filter (default: True)

### Web Server
- Flask serves HTML dashboard on `/` with live-updating stats
- JSON API on `/api/status` for programmatic access (includes trade_history, microprice, ema, momentum)
- Health check on `/health`
- Cache-Control: no-cache headers on all responses
- Production deployment uses gunicorn
- Scalper runs in a background thread alongside the web server

### Trading Strategies
1. **Microprice-centered market making**: Orders placed around the volume-weighted microprice rather than simple mid-price
2. **Inventory skew**: When holding a position, prices shift to encourage inventory reduction (long → sell cheaper, short → buy cheaper)
3. **Momentum filter**: When EMA detects upward momentum, skip sell orders; when downward, skip buy orders
4. **Fill cooldown**: 200ms minimum between fills prevents rapid cascade fills that distort PnL

### Telegram Integration
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` stored as Replit Secrets
- Rate-limited notifications (60s minimum between updates)
- Alert messages have 5s rate limit
- Critical alerts: drawdown stops, disconnections (sent once, not repeated)
- Daily summary message sent once per UTC day

## Tech Stack
- Python 3.11
- websockets (async WebSocket client)
- asyncio (concurrency)
- Flask + gunicorn (web server / deployment)
- requests (Telegram API)

## Recent Changes
- 2026-02-12: Fixed position-crossing logic for correct round-trip PnL on long↔short flips
- 2026-02-12: Fixed equity calculation bug (use position_value, not unrealized profit)
- 2026-02-12: Fixed drawdown alert spam (one-shot flag + 5s rate limit on send_alert)
- 2026-02-12: Added Microprice strategy (volume-weighted fair price from order book)
- 2026-02-12: Added Inventory Skew pricing (bias prices to reduce position risk)
- 2026-02-12: Added Momentum Filter (EMA-based, skip orders against trend)
- 2026-02-12: Added fill cooldown (200ms) and single-fill-per-tick to prevent cascades
- 2026-02-12: Added average cost tracking with weighted average entry price
- 2026-02-12: Replaced per-fill win/loss with round-trip PnL tracking
- 2026-02-12: Added trade history (last 50 trades, 20 shown on dashboard)
- 2026-02-12: Added daily summary Telegram message
- 2026-02-12: Enhanced dashboard with Strategy Indicators section, trade history table
- 2026-02-12: Built professional HTML live dashboard with dark theme and auto-refresh
- 2026-02-12: Added session statistics (win/loss, win rate, peak equity, max drawdown)
- 2026-02-12: Enhanced risk management with affordability checks, mark-to-market equity, 50% drawdown stop
- 2026-02-12: Integrated Telegram notifications with rate limiting
- 2026-02-12: Added $16 starting capital tracking with balance, realized PnL, and return %
- 2026-02-12: Switched L1 data source from Binance to Kraken WebSocket v2 API (no IP restrictions)
- 2026-02-12: Added Flask web server and gunicorn deployment config
- 2026-02-12: Initial creation of HFT scalper module with WebSocket L1 monitoring, market making, and risk management
