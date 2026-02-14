# WildBot MultiPair HFT Scalper

## Overview
WildBot is a high-frequency trading (HFT) scalping bot designed for the Kraken exchange. Its primary purpose is to implement market-making strategies with robust risk management across multiple cryptocurrency pairs. The bot focuses on capturing small profits from bid-ask spread differences and dynamically adapting to market conditions. Key capabilities include concurrent multi-pair trading (up to 3 pairs) selected from 8 USDC pairs (BTC, ETH, SOL, DOGE, XRP, LINK, AVAX, ADA), dynamic pair selection based on volatility, and real-time PnL updates. It supports both live trading on Kraken and a paper trading mode for simulation.

## User Preferences
I prefer to work in an iterative development process, with frequent, small, and reversible changes. When implementing new features or making significant changes, please ask for my approval before proceeding. I prefer clear and concise explanations, avoiding overly technical jargon where simpler terms suffice. Ensure that all critical alerts and daily summaries are delivered via Telegram.

## System Architecture

### UI/UX Decisions
The project includes a live-updating, dark-themed HTML dashboard for real-time monitoring. It features a clear LIVE/PAPER mode indicator, strategy indicators, a trade table, and a pair scanner table ranking pairs by volatility. The dashboard automatically refreshes every 2 seconds.

### Technical Implementations
The core system is built with Python 3.11 using an `asyncio` architecture for non-blocking, high-speed data processing. It leverages `websockets` for real-time Kraken v2 API data streaming and `requests` for Kraken REST API interactions and Telegram notifications. A Flask web server, deployed with `gunicorn` for production, provides the dashboard and a JSON API for status. Error handling includes exponential backoff for API errors and reconnection logic.

### Feature Specifications
- **Multi-Pair Orchestration:** Manages up to 3 concurrent `PairTrader` instances across 8 USDC pairs, subscribing to all tickers simultaneously. It includes dynamic pair swapping based on volatility and spread, balance allocation, and thread-safe active trader management.
- **Per-Pair Trading Engine (`PairTrader`):** Each pair operates with isolated state, including its own `ScalperConfig`, `RiskManager`, `OrderManager`, `TopOfBook`, EMA tracking, and volatility tracking. It handles tick processing, fill checking, and various stop mechanisms.
- **Order Management:** Supports dual-mode operation:
    - **Paper Trading:** Simulated fills for strategy testing.
    - **Live Trading:** Real order placement and cancellation on Kraken via REST API with HMAC-SHA512 authentication and 1-second rate limiting per API call. Includes strict affordability checks and automatic fallback to paper mode on API connection failure.
- **Risk Management:** Incorporates position limits, spread filters, stale order detection (500ms), affordability checks, 50% max drawdown auto-stop, average cost tracking, and dynamic exit/hold targeting.
- **Dynamic Pair Selection:** A `PairScanner` component tracks volatility and spread for all 8 supported pairs, scoring them and dynamically selecting the most tradeable pairs with a 60-second hysteresis and 1.5x switch threshold.
- **Asynchronous Architecture:** Utilizes `asyncio` for efficient handling of WebSocket data streams and concurrent operations.
- **Telegram Integration:** Provides rate-limited (60s) PnL updates, critical alerts (5s cooldown) for events like drawdown stops or disconnections, and daily summaries.

### System Design Choices
- **Market Making Strategy:** Orders are placed around the volume-weighted microprice, with inventory-skewed pricing to manage position risk.
- **Momentum Filter:** An EMA-based trend detection mechanism prevents trades against strong market moves.
- **Fill Cooldown:** A 200ms minimum delay between fills prevents rapid, cascading order executions.
- **Order Placement:** All Kraken orders include the `post_only` flag to ensure maker fees and prevent accidental taker fills.
- **Web Server:** A Flask application serves a dashboard and API, running in a background thread alongside the scalper.

## External Dependencies
- **Kraken Exchange:**
    - **WebSocket API (v2):** Used for real-time ticker data and order book depth streaming.
    - **REST API:** Used for placing/cancelling orders, querying balances, open orders, and trades. Requires `KRAKEN_API_KEY` and `KRAKEN_API_SECRET`.
- **Telegram:**
    - **Bot API:** Used for sending notifications, alerts, and daily summaries. Requires `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
- **Python Libraries:**
    - `websockets`: Asynchronous WebSocket client.
    - `asyncio`: Asynchronous I/O framework.
    - `Flask`: Web framework for the dashboard and API.
    - `gunicorn`: WSGI HTTP Server for production deployment of Flask.
    - `requests`: HTTP library for interacting with REST APIs.