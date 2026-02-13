import asyncio
import logging
import os
import signal
import socket
import subprocess
import threading
import time

from flask import Flask, jsonify, render_template
from hft_scalper import HFTScalper, ScalperConfig, PairScanner, SCAN_PAIRS
from kraken_client import KrakenClient

logging.getLogger("werkzeug").setLevel(logging.WARNING)

port = int(os.environ.get("PORT", 5000))
for attempt in range(5):
    try:
        result = subprocess.run(["fuser", f"{port}/tcp"], capture_output=True, text=True, timeout=5)
        if result.stdout.strip():
            subprocess.run(["fuser", "-k", "-9", f"{port}/tcp"], capture_output=True, timeout=5)
            time.sleep(2)
        else:
            break
    except Exception:
        pass
    time.sleep(1)

app = Flask(__name__)

import werkzeug.serving
original_make_server = werkzeug.serving.make_server
def patched_make_server(*args, **kwargs):
    server = original_make_server(*args, **kwargs)
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    return server
werkzeug.serving.make_server = patched_make_server

config = ScalperConfig(
    symbol="BTC/USDC",
    ws_url="wss://ws.kraken.com/v2",
    ws_symbol="BTC/USDC",
    rest_pair="XBTUSDC",
    starting_capital=16.0,
    order_qty=0.0001,
    max_spread_bps=50.0,
    stale_order_ms=2000.0,
    max_position=0.01,
    max_open_orders=2,
    live_mode=True,
    maker_fee_bps=16.0,
    min_profit_bps=2.0,
    target_exit_bps=8.0,
    min_volatility_bps=0.1,
    max_hold_seconds=60.0,
    stop_loss_bps=12.0,
    fill_cooldown_ms=100.0,
    volatility_exit_multiplier=0.8,
    min_hold_seconds=5.0,
    base_hold_seconds=60.0,
    max_hold_scaling=2.0,
    requote_threshold_bps=3.0,
)

kraken = KrakenClient()
scanner = PairScanner(SCAN_PAIRS)
scanner._active_pair = config.ws_symbol
scalper = HFTScalper(config, kraken_client=kraken, scanner=scanner)


def run_scalper():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(scalper.run())
    finally:
        loop.close()


@app.after_request
def add_cache_headers(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/status")
def api_status():
    mid = scalper.tob.mid_price
    return jsonify({
        "status": "running" if scalper._running else "stopped",
        "live_mode": scalper.config.live_mode,
        "symbol": scalper.config.symbol,
        "starting_capital": scalper.risk.starting_capital,
        "balance": round(scalper.risk.balance, 2),
        "equity": round(scalper.risk.equity(mid), 2),
        "unrealized_pnl": round(scalper.risk.unrealized_pnl(mid), 4),
        "return_pct": round(scalper.risk.return_pct(mid), 2),
        "position": scalper.risk.position,
        "pnl": round(scalper.risk.pnl, 4),
        "trade_count": scalper.risk.trade_count,
        "wins": scalper.risk.wins,
        "losses": scalper.risk.losses,
        "win_rate": round(scalper.risk.win_rate, 2),
        "fees_paid": round(scalper.risk.fees_paid, 4),
        "max_drawdown_pct": round(scalper.risk.max_drawdown_pct_seen, 2),
        "is_stopped": scalper.risk.is_stopped,
        "open_orders": scalper.orders.open_count,
        "avg_entry_price": round(scalper.risk.avg_entry_price, 2),
        "tob": {
            "best_bid": scalper.tob.best_bid,
            "best_ask": scalper.tob.best_ask,
            "spread": scalper.tob.spread,
            "mid_price": scalper.tob.mid_price,
            "microprice": scalper.tob.microprice,
        },
        "ema": round(scalper._ema, 2),
        "volatility_bps": round(scalper._volatility_bps, 2),
        "momentum": scalper.momentum_signal,
        "trade_history": scalper._trade_history[-20:],
        "updates_processed": scalper._update_count,
        "uptime": round(time.time() - start_time, 1),
        "status_reason": scalper._status_reason,
        "spread_bps": round((scalper.tob.spread / scalper.tob.mid_price * 10000) if scalper.tob.mid_price > 0 else 0, 2),
        "target_exit_bps": config.target_exit_bps,
        "min_edge_bps": round(scalper.risk.dynamic_exit_bps(scalper._volatility_bps), 2),
        "fee_bps": config.maker_fee_bps,
        "scanner_data": scalper.scanner.get_scanner_data() if scalper.scanner else [],
        "active_pair": scalper.config.ws_symbol,
        "dynamic_exit_bps": round(scalper.risk.dynamic_exit_bps(scalper._volatility_bps), 2),
        "dynamic_hold_seconds": round(scalper.risk.dynamic_hold_seconds(scalper.risk.dynamic_exit_bps(scalper._volatility_bps)), 1),
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


start_time = time.time()
scalper_thread = threading.Thread(target=run_scalper, daemon=True)
scalper_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=port)
