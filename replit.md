# WildBot HFT Scalper

## Overview
WildBot is a high-frequency trading (HFT) scalping bot that connects to cryptocurrency exchanges via WebSocket for real-time order book data. It implements market-making strategies with built-in risk management.

## Project Architecture

### Files
- `main.py` — Entry point. Configures the scalper and runs the async event loop with signal handling.
- `hft_scalper.py` — Core HFT module containing:
  - `HFTScalper` — Main orchestrator: WebSocket connection, tick processing, order lifecycle
  - `TopOfBook` — L1 data structure (best bid/ask, quantities, spread)
  - `RiskManager` — Position limits, spread filters, stale order detection (500ms timeout)
  - `OrderManager` — Order creation, cancellation, simulated fill checks
  - `ScalperConfig` — All tunable parameters (symbol, qty, spread threshold, etc.)

### Key Features
1. **WebSocket streaming** — Real-time order book depth via `websockets` library
2. **Market making** — Simultaneous limit buy at best bid and limit sell at best ask
3. **Risk management** — 500ms stale order cancellation, position limits, spread filter
4. **Async architecture** — `asyncio` for non-blocking high-speed data processing
5. **Reconnect logic** — Exponential backoff on disconnection

### Configuration (in main.py)
- `symbol` — Trading pair (default: BTC/USD)
- `ws_url` — WebSocket endpoint (default: Kraken v2)
- `ws_symbol` — Exchange-specific symbol for subscription
- `order_qty` — Order size per side
- `max_spread_bps` — Maximum acceptable spread in basis points
- `stale_order_ms` — Cancel orders older than this (default: 500ms)
- `max_position` — Maximum net position allowed

### Web Server
- Flask serves a JSON status dashboard on `/` and health check on `/health`
- Production deployment uses gunicorn
- Scalper runs in a background thread alongside the web server

## Tech Stack
- Python 3.11
- websockets (async WebSocket client)
- asyncio (concurrency)
- Flask + gunicorn (web server / deployment)

## Recent Changes
- 2026-02-12: Switched L1 data source from Binance to Kraken WebSocket v2 API (no IP restrictions)
- 2026-02-12: Added Flask web server and gunicorn deployment config
- 2026-02-12: Initial creation of HFT scalper module with WebSocket L1 monitoring, market making, and risk management
