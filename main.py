import asyncio
import os
import signal
import threading
import time

from flask import Flask, jsonify, render_template
from hft_scalper import HFTScalper, ScalperConfig
from kraken_client import KrakenClient

app = Flask(__name__)

config = ScalperConfig(
    symbol="BTC/USD",
    ws_url="wss://ws.kraken.com/v2",
    ws_symbol="BTC/USD",
    rest_pair="XBTUSD",
    starting_capital=16.0,
    order_qty=0.001,
    max_spread_bps=10.0,
    stale_order_ms=500.0,
    max_position=0.01,
    max_open_orders=2,
    live_mode=True,
)

kraken = KrakenClient()
scalper = HFTScalper(config, kraken_client=kraken)


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
        "momentum": scalper.momentum_signal,
        "trade_history": scalper._trade_history[-20:],
        "updates_processed": scalper._update_count,
        "uptime": round(time.time() - start_time, 1),
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


start_time = time.time()
scalper_thread = threading.Thread(target=run_scalper, daemon=True)
scalper_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
