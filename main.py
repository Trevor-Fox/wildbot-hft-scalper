import asyncio
import os
import signal
import threading
import time

from flask import Flask, jsonify
from hft_scalper import HFTScalper, ScalperConfig

app = Flask(__name__)

config = ScalperConfig(
    symbol="BTC/USD",
    ws_url="wss://ws.kraken.com/v2",
    ws_symbol="BTC/USD",
    order_qty=0.001,
    max_spread_bps=10.0,
    stale_order_ms=500.0,
    max_position=0.01,
    max_open_orders=2,
)

scalper = HFTScalper(config)


def run_scalper():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(scalper.run())
    finally:
        loop.close()


@app.route("/")
def index():
    return jsonify({
        "status": "running" if scalper._running else "stopped",
        "symbol": scalper.config.symbol,
        "position": scalper.risk.position,
        "pnl": round(scalper.risk.pnl, 4),
        "trade_count": scalper.risk.trade_count,
        "open_orders": scalper.orders.open_count,
        "tob": {
            "best_bid": scalper.tob.best_bid,
            "best_ask": scalper.tob.best_ask,
            "spread": scalper.tob.spread,
            "mid_price": scalper.tob.mid_price,
        },
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
