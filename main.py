import asyncio
import logging
import os
import signal
import socket
import subprocess
import threading
import time

from flask import Flask, jsonify, render_template
from hft_scalper import MultiPairOrchestrator, ScalperConfig, PairScanner, SCAN_PAIRS
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
    stale_order_ms=15000.0,
    max_position=0.01,
    max_open_orders=2,
    live_mode=True,
    maker_fee_bps=16.0,
    min_profit_bps=2.0,
    target_exit_bps=8.0,
    min_volatility_bps=0.1,
    max_hold_seconds=120.0,
    stop_loss_bps=12.0,
    fill_cooldown_ms=100.0,
    volatility_exit_multiplier=0.8,
    min_hold_seconds=5.0,
    base_hold_seconds=120.0,
    max_hold_scaling=2.0,
    requote_threshold_bps=8.0,
)

kraken = KrakenClient()
scanner = PairScanner(SCAN_PAIRS)
orchestrator = MultiPairOrchestrator(
    base_config=config,
    kraken_client=kraken,
    scanner=scanner,
    max_active_pairs=3,
)


def run_scalper():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(orchestrator.run())
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
    portfolio = orchestrator.get_portfolio_status()
    total_wins = portfolio["total_wins"]
    total_losses = portfolio["total_losses"]
    total_trades = portfolio["total_trades"]
    win_rate = (total_wins / (total_wins + total_losses) * 100) if (total_wins + total_losses) > 0 else 0.0
    
    starting_cap = orchestrator._initial_capital
    total_equity = portfolio["total_equity"]
    return_pct = ((total_equity - starting_cap) / starting_cap * 100) if starting_cap > 0 else 0.0
    
    all_trade_history = []
    with orchestrator._traders_lock:
        traders_snapshot = list(orchestrator.active_traders.values())
    for trader in traders_snapshot:
        for t in trader._trade_history[-20:]:
            entry = dict(t)
            entry["symbol"] = trader.config.symbol
            all_trade_history.append(entry)
    all_trade_history.extend([dict(t) for t in orchestrator._trade_history[-20:]])
    all_trade_history.sort(key=lambda x: x.get("time", 0), reverse=True)
    all_trade_history = all_trade_history[:30]
    
    return jsonify({
        "status": "running" if orchestrator._running else "stopped",
        "live_mode": orchestrator.base_config.live_mode,
        "multi_pair": True,
        "max_active_pairs": orchestrator.max_active_pairs,
        "total_balance": round(portfolio["total_balance"], 2),
        "total_equity": round(portfolio["total_equity"], 2),
        "total_pnl": round(portfolio["total_pnl"], 4),
        "total_trades": total_trades,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "total_fees": round(portfolio["total_fees"], 4),
        "win_rate": round(win_rate, 2),
        "return_pct": round(return_pct, 2),
        "active_pairs": portfolio["active_pairs"],
        "scanner_data": portfolio["scanner_data"],
        "trade_history": all_trade_history,
        "uptime": round(time.time() - start_time, 1),
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


start_time = time.time()
scalper_thread = threading.Thread(target=run_scalper, daemon=True)
scalper_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=port)
