# WildBot HFT Scalper

## Overview
WildBot is a high-frequency trading (HFT) scalping bot that connects to Kraken via WebSocket for real-time order book data. It implements market-making strategies with built-in risk management, multiple trading strategies (microprice, inventory skew, momentum filter), and comprehensive analytics. Supports both live trading on Kraken and paper trading modes with PnL updates sent to Telegram.

## Project Architecture

### Files
- `main.py` — Entry point. Configures the scalper, Flask web server (HTML dashboard on `/`, JSON API on `/api/status`), and runs the async event loop in a background thread. Sets `live_mode=True` for real Kraken trading.
- `hft_scalper.py` — Core HFT module containing:
  - `HFTScalper` — Main orchestrator: WebSocket connection, tick processing, order lifecycle, Telegram notifications, EMA/momentum tracking, live balance initialization
  - `TopOfBook` — L1 data structure (best bid/ask, quantities, spread, microprice)
  - `RiskManager` — Position limits, spread filters, stale order detection (500ms timeout), affordability checks (live-aware), mark-to-market equity, 50% max drawdown stop, average cost tracking, round-trip PnL, inventory skew pricing
  - `OrderManager` — Dual-mode order management: paper (simulated fills) and live (Kraken REST API orders with exponential backoff on errors)
  - `ScalperConfig` — All tunable parameters including `live_mode` and `rest_pair`
  - `TelegramNotifier` — Rate-limited notifications, alerts, daily summaries
- `kraken_client.py` — Kraken REST API client with HMAC-SHA512 authentication for AddOrder, CancelOrder, CancelAll, Balance, OpenOrders, QueryOrders. Includes 1s rate limiting between API calls.
- `templates/dashboard.html` — Live-updating dark-themed trading dashboard with LIVE/PAPER mode indicator (auto-refreshes every 2s)

### Key Features
1. **Live Trading** — Real order placement on Kraken via REST API with proper authentication
2. **Paper Trading** — Simulated fills for strategy testing without risk
3. **WebSocket streaming** — Real-time order book depth via `websockets` library (Kraken v2 API)
4. **Market making** — Simultaneous limit buy/sell with inventory-skewed pricing
5. **Microprice** — Volume-weighted fair price from order book for smarter order placement
6. **Inventory Skew** — Biases prices based on current position to reduce inventory risk
7. **Momentum Filter** — EMA-based trend detection; skips orders against strong moves
8. **Risk management** — 500ms stale order cancellation, position limits, spread filter, affordability checks, 50% drawdown auto-stop
9. **Fill cooldown** — 200ms minimum between fills to prevent cascading; max 1 fill per tick
10. **Average cost tracking** — Weighted average entry price for accurate unrealized PnL
11. **Round-trip PnL** — Win/loss counted on complete position round-trips
12. **Mark-to-market equity** — Balance + position_value for accurate return tracking
13. **Session statistics** — Win/loss tracking, win rate, peak equity, max drawdown monitoring
14. **Telegram notifications** — Rate-limited PnL updates (60s), critical alerts (5s cooldown), daily summary
15. **Trade history** — Last 50 trades stored, last 20 displayed on dashboard
16. **Live dashboard** — Professional dark-themed HTML dashboard with LIVE/PAPER badge, strategy indicators, trade table, auto-refresh
17. **Async architecture** — `asyncio` for non-blocking high-speed data processing
18. **Reconnect logic** — Exponential backoff on disconnection
19. **API error handling** — Exponential backoff on Kraken API errors (2s → 60s), graceful fallback to paper mode on connection failure

### Live Trading Mode
- Set `live_mode=True` in ScalperConfig to enable real trading
- Requires `KRAKEN_API_KEY` and `KRAKEN_API_SECRET` environment secrets
- API key needs "Create & modify orders" and "Query open orders & trades" permissions
- On startup, fetches real USD and BTC balance from Kraken
- Places real limit orders via Kraken REST API (`/0/private/AddOrder`)
- Cancels orders via Kraken REST API (`/0/private/CancelOrder`, `/0/private/CancelAll`)
- Polls for fills via QueryOrders every 1 second
- Falls back to paper mode automatically if API connection fails
- Live mode enforces strict affordability: buys require sufficient USD, sells require BTC position
- Exponential backoff on API errors prevents rate limit violations
- REST pair name `XBTUSD` (different from WebSocket `BTC/USD`)

### Configuration (in main.py)
- `symbol` — Trading pair (default: BTC/USD)
- `ws_url` — WebSocket endpoint (default: Kraken v2)
- `ws_symbol` — Exchange-specific symbol for subscription
- `rest_pair` — REST API pair name (default: XBTUSD)
- `live_mode` — Enable live trading on Kraken (default: True)
- `order_qty` — Order size per side (default: 0.001)
- `max_spread_bps` — Maximum acceptable spread in basis points
- `stale_order_ms` — Cancel orders older than this (default: 500ms)
- `max_position` — Maximum net position allowed
- `starting_capital` — Paper trading capital / overridden by live balance
- `fill_cooldown_ms` — Minimum time between fills (default: 200ms)
- `skew_factor` — How aggressively to skew prices for inventory (default: 0.5)
- `ema_window` — EMA period for momentum detection (default: 50)
- `momentum_filter_enabled` — Enable/disable momentum filter (default: True)

### Web Server
- Flask serves HTML dashboard on `/` with live-updating stats
- JSON API on `/api/status` for programmatic access (includes live_mode, trade_history, microprice, ema, momentum)
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
- Critical alerts: drawdown stops, disconnections, live mode activation/failure
- Daily summary message sent once per UTC day

## Tech Stack
- Python 3.11
- websockets (async WebSocket client)
- asyncio (concurrency)
- Flask + gunicorn (web server / deployment)
- requests (Kraken REST API + Telegram API)

## Environment Secrets
- `KRAKEN_API_KEY` — Kraken API public key (required for live mode)
- `KRAKEN_API_SECRET` — Kraken API private key (required for live mode)
- `TELEGRAM_BOT_TOKEN` — Telegram bot token for notifications
- `TELEGRAM_CHAT_ID` — Telegram chat ID for notifications
- `SESSION_SECRET` — Flask session secret

## Recent Changes
- 2026-02-13: Added volatility gate: bot only enters new positions when rolling price volatility > 20bps, preventing entries in dead markets where exits can't fill
- 2026-02-13: Widened entry offset from 2bps to 20bps (maker_fee + profit) so exit target is only 16bps further instead of 34bps
- 2026-02-13: Added time-based stop-loss: force-exits positions held > 120s without reaching target
- 2026-02-13: Added price stop-loss: force-exits when adverse move > 20bps to limit downside
- 2026-02-13: Fixed exit offset to cover BOTH sides' maker fees: 2×16bps + 4bps profit = 36bps (~$239 on BTC). Previous 20bps offset only covered one side, causing guaranteed losses per round trip
- 2026-02-13: Fixed position churn: when long, only sell orders are placed (no buys); when short, only buy orders. Prevents the bot from adding to positions while trying to exit, which was generating massive fee losses
- 2026-02-12: Tightened exit offset from 2x maker fee (33bps/$216) to 1x maker fee + profit (20bps/$131) for faster trade completion
- 2026-02-12: Increased API rate limit from 200ms to 1s between calls to prevent Kraken rate limiting
- 2026-02-12: Increased fill poll interval from 1s to 5s and balance refresh from 60s to 300s to reduce API load
- 2026-02-12: Set up VM deployment for 24/7 operation (bot no longer sleeps when Replit is inactive)
- 2026-02-12: Fee-aware pricing: buy/sell prices offset by maker fee (16bps) + profit target (4bps) to ensure round trips are profitable after Kraken fees
- 2026-02-12: Fee tracking in record_fill: deducts estimated maker fee from balance/pnl, tracks total fees_paid, includes fees in trip_cost for accurate win/loss
- 2026-02-12: Increased stale_order_ms to 5000 (from 500) and max_spread_bps to 50 to accommodate wider fee-aware quotes
- 2026-02-12: Enhanced Telegram messages: fill alerts with fee info, periodic updates with fees paid, LIVE/PAPER mode indicators, total PnL breakdown
- 2026-02-12: Implemented live trading via Kraken REST API (AddOrder, CancelOrder, CancelAll, QueryOrders)
- 2026-02-12: Created kraken_client.py with HMAC-SHA512 authentication and rate limiting
- 2026-02-12: Added live_mode toggle with automatic fallback to paper on API failure
- 2026-02-12: Live balance initialization from Kraken (USD + BTC)
- 2026-02-12: Added exponential backoff on API errors (2s → 60s)
- 2026-02-12: Live-aware affordability checks (buys need USD, sells need BTC position)
- 2026-02-12: Dashboard shows LIVE/PAPER mode badge
- 2026-02-12: Telegram alerts for live mode activation/failure
- 2026-02-12: Fixed position-crossing logic for correct round-trip PnL on long↔short flips
- 2026-02-12: Fixed equity calculation bug (use position_value, not unrealized profit)
- 2026-02-12: Fixed drawdown alert spam (one-shot flag + 5s rate limit on send_alert)
- 2026-02-12: Added Microprice, Inventory Skew, Momentum Filter strategies
- 2026-02-12: Added fill cooldown (200ms), average cost tracking, round-trip PnL tracking
- 2026-02-12: Built professional HTML live dashboard with dark theme
- 2026-02-12: Initial creation of HFT scalper module
