import asyncio
import signal

from hft_scalper import HFTScalper, ScalperConfig


def main():
    config = ScalperConfig(
        symbol="BTC/USDT",
        ws_url="wss://stream.binance.com:9443/ws/btcusdt@depth5@100ms",
        order_qty=0.001,
        max_spread_bps=10.0,
        stale_order_ms=500.0,
        max_position=0.01,
        max_open_orders=2,
    )

    scalper = HFTScalper(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, scalper.stop)

    try:
        loop.run_until_complete(scalper.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
