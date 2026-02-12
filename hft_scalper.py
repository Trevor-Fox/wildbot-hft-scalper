import asyncio
import json
import os
import time
import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import requests
import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("HFTScalper")


class TelegramNotifier:
    def __init__(self, min_interval: float = 60.0):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id)
        self.min_interval = min_interval
        self._last_send_time: float = 0
        if self.enabled:
            logger.info("Telegram notifications enabled")
        else:
            logger.warning("Telegram notifications disabled — missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    def send(self, message: str):
        if not self.enabled:
            return
        now = time.time()
        if now - self._last_send_time < self.min_interval:
            return
        self._last_send_time = now
        threading.Thread(target=self._send_sync, args=(message,), daemon=True).start()

    def send_alert(self, message: str):
        if not self.enabled:
            return
        threading.Thread(target=self._send_sync, args=(message,), daemon=True).start()

    def _send_sync(self, message: str):
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            resp = requests.post(url, json={
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
            }, timeout=5)
            if not resp.ok:
                logger.warning(f"Telegram send failed: {resp.status_code} {resp.text}")
        except Exception as exc:
            logger.warning(f"Telegram error: {exc}")


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(Enum):
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"


@dataclass
class Order:
    order_id: str
    side: OrderSide
    price: float
    quantity: float
    status: OrderStatus = OrderStatus.PENDING
    placed_at: float = 0.0


@dataclass
class TopOfBook:
    best_bid: float = 0.0
    best_ask: float = 0.0
    bid_qty: float = 0.0
    ask_qty: float = 0.0
    timestamp: float = 0.0

    @property
    def spread(self) -> float:
        if self.best_ask > 0 and self.best_bid > 0:
            return self.best_ask - self.best_bid
        return float("inf")

    @property
    def mid_price(self) -> float:
        if self.best_ask > 0 and self.best_bid > 0:
            return (self.best_ask + self.best_bid) / 2.0
        return 0.0


@dataclass
class ScalperConfig:
    symbol: str = "BTC/USD"
    ws_url: str = "wss://ws.kraken.com/v2"
    ws_symbol: str = "BTC/USD"
    starting_capital: float = 16.0
    order_qty: float = 0.001
    max_spread_bps: float = 10.0
    stale_order_ms: float = 500.0
    max_position: float = 0.01
    max_open_orders: int = 2
    tick_size: float = 0.01
    reconnect_delay: float = 1.0
    max_reconnect_delay: float = 30.0
    max_drawdown_pct: float = 50.0


class RiskManager:
    def __init__(self, config: ScalperConfig):
        self.config = config
        self.starting_capital: float = config.starting_capital
        self.balance: float = config.starting_capital
        self.position: float = 0.0
        self.pnl: float = 0.0
        self.trade_count: int = 0
        self.wins: int = 0
        self.losses: int = 0
        self.total_profit: float = 0.0
        self.total_loss: float = 0.0
        self.peak_equity: float = config.starting_capital
        self.max_drawdown_pct_seen: float = 0.0
        self.is_stopped: bool = False
        self._prev_pnl: float = 0.0

    def unrealized_pnl(self, mid_price: float) -> float:
        return self.position * mid_price

    def equity(self, mid_price: float) -> float:
        return self.balance + self.unrealized_pnl(mid_price)

    def return_pct(self, mid_price: float) -> float:
        if self.starting_capital > 0:
            return ((self.equity(mid_price) - self.starting_capital) / self.starting_capital) * 100
        return 0.0

    def can_place_buy(self, price: float) -> bool:
        position_ok = self.position + self.config.order_qty <= self.config.max_position
        cost = price * self.config.order_qty
        affordable = cost <= (self.balance + self.starting_capital * 5)
        return position_ok and affordable

    def can_place_sell(self) -> bool:
        return self.position - self.config.order_qty >= -self.config.max_position

    def is_spread_acceptable(self, tob: TopOfBook) -> bool:
        if tob.mid_price <= 0:
            return False
        spread_bps = (tob.spread / tob.mid_price) * 10_000
        return spread_bps <= self.config.max_spread_bps

    def record_fill(self, side: OrderSide, price: float, qty: float):
        cost = price * qty
        if side == OrderSide.BUY:
            self.position += qty
            self.balance -= cost
            self.pnl -= cost
        else:
            self.position -= qty
            self.balance += cost
            self.pnl += cost
        self.trade_count += 1

        pnl_change = self.pnl - self._prev_pnl
        if pnl_change > 0:
            self.wins += 1
            self.total_profit += pnl_change
        elif pnl_change < 0:
            self.losses += 1
            self.total_loss += abs(pnl_change)
        self._prev_pnl = self.pnl

        logger.info(
            f"Fill: {side.value} {qty}@{price:.2f} | "
            f"bal=${self.balance:.2f} pos={self.position:.6f} "
            f"pnl={self.pnl:.4f} trades={self.trade_count}"
        )

    def update_drawdown(self, mid_price: float):
        eq = self.equity(mid_price)
        self.peak_equity = max(self.peak_equity, eq)
        if self.peak_equity > 0:
            drawdown_pct = ((self.peak_equity - eq) / self.peak_equity) * 100
            self.max_drawdown_pct_seen = max(self.max_drawdown_pct_seen, drawdown_pct)
            if drawdown_pct > self.config.max_drawdown_pct:
                self.is_stopped = True
                logger.warning(
                    f"DRAWDOWN STOP: {drawdown_pct:.2f}% drawdown exceeds "
                    f"max {self.config.max_drawdown_pct:.2f}% — stopping trading"
                )

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        if total > 0:
            return (self.wins / total) * 100
        return 0.0

    @property
    def avg_trade_pnl(self) -> float:
        if self.trade_count > 0:
            return self.pnl / self.trade_count
        return 0.0

    def check_stale_orders(self, open_orders: dict[str, Order]) -> list[str]:
        now = time.monotonic() * 1000
        stale_ids = []
        for oid, order in open_orders.items():
            if order.status == OrderStatus.OPEN:
                age_ms = now - (order.placed_at * 1000)
                if age_ms > self.config.stale_order_ms:
                    stale_ids.append(oid)
        return stale_ids


class OrderManager:
    def __init__(self):
        self._open_orders: dict[str, Order] = {}
        self._seq: int = 0

    @property
    def open_orders(self) -> dict[str, Order]:
        return self._open_orders

    @property
    def open_count(self) -> int:
        return len(self._open_orders)

    def create_order(self, side: OrderSide, price: float, qty: float) -> Order:
        self._seq += 1
        oid = f"WB-{self._seq:06d}"
        order = Order(
            order_id=oid,
            side=side,
            price=price,
            quantity=qty,
            status=OrderStatus.OPEN,
            placed_at=time.monotonic(),
        )
        self._open_orders[oid] = order
        logger.info(f"Placed {side.value} order {oid}: {qty}@{price:.2f}")
        return order

    def cancel_order(self, order_id: str) -> Optional[Order]:
        order = self._open_orders.pop(order_id, None)
        if order:
            order.status = OrderStatus.CANCELLED
            logger.info(f"Cancelled order {order_id}")
        return order

    def cancel_all(self) -> int:
        count = len(self._open_orders)
        for oid in list(self._open_orders):
            self._open_orders[oid].status = OrderStatus.CANCELLED
        self._open_orders.clear()
        if count:
            logger.info(f"Cancelled all {count} open orders")
        return count

    def simulate_fill_check(self, tob: TopOfBook) -> list[Order]:
        filled = []
        for oid in list(self._open_orders):
            order = self._open_orders[oid]
            if order.side == OrderSide.BUY and tob.best_ask <= order.price:
                order.status = OrderStatus.FILLED
                filled.append(self._open_orders.pop(oid))
            elif order.side == OrderSide.SELL and tob.best_bid >= order.price:
                order.status = OrderStatus.FILLED
                filled.append(self._open_orders.pop(oid))
        return filled

    def has_side(self, side: OrderSide) -> bool:
        return any(o.side == side for o in self._open_orders.values())


class HFTScalper:
    def __init__(self, config: Optional[ScalperConfig] = None):
        self.config = config or ScalperConfig()
        self.tob = TopOfBook()
        self.risk = RiskManager(self.config)
        self.orders = OrderManager()
        self.telegram = TelegramNotifier()
        self._running = False
        self._update_count = 0

    def _parse_depth_update(self, raw: str) -> bool:
        try:
            data = json.loads(raw)
            channel = data.get("channel")
            if channel == "ticker":
                tick_data = data.get("data", [{}])[0]
                bid = tick_data.get("bid")
                ask = tick_data.get("ask")
                bid_qty = tick_data.get("bid_qty")
                ask_qty = tick_data.get("ask_qty")
                if bid is not None and ask is not None:
                    self.tob.best_bid = float(bid)
                    self.tob.best_ask = float(ask)
                    self.tob.bid_qty = float(bid_qty) if bid_qty else 0.0
                    self.tob.ask_qty = float(ask_qty) if ask_qty else 0.0
                    self.tob.timestamp = time.time()
                    return True
            elif channel == "heartbeat":
                return False
            elif data.get("method") == "subscribe" and data.get("success"):
                logger.info(f"Subscribed to {data.get('result', {}).get('channel', 'unknown')}")
                return False
        except (json.JSONDecodeError, KeyError, IndexError, ValueError) as exc:
            logger.warning(f"Parse error: {exc}")
        return False

    async def _handle_tick(self):
        self._update_count += 1

        filled = self.orders.simulate_fill_check(self.tob)
        for order in filled:
            self.risk.record_fill(order.side, order.price, order.quantity)

        self.risk.update_drawdown(self.tob.mid_price)

        if self.risk.is_stopped:
            self.orders.cancel_all()
            self.telegram.send_alert(
                f"🚨 <b>WildBot DRAWDOWN STOP</b>\n"
                f"{self.config.symbol} trading halted!\n"
                f"Max drawdown {self.config.max_drawdown_pct:.1f}% exceeded.\n"
                f"Equity: ${self.risk.equity(self.tob.mid_price):,.2f}\n"
                f"Realized PnL: ${self.risk.pnl:,.4f}\n"
                f"Trades: {self.risk.trade_count}"
            )
            return

        stale = self.risk.check_stale_orders(self.orders.open_orders)
        for oid in stale:
            self.orders.cancel_order(oid)

        if not self.risk.is_spread_acceptable(self.tob):
            if self.orders.open_count > 0:
                self.orders.cancel_all()
                logger.debug("Spread too wide — cancelled all orders")
            return

        if (
            not self.orders.has_side(OrderSide.BUY)
            and self.risk.can_place_buy(self.tob.best_bid)
            and self.orders.open_count < self.config.max_open_orders
        ):
            self.orders.create_order(
                OrderSide.BUY, self.tob.best_bid, self.config.order_qty
            )

        if (
            not self.orders.has_side(OrderSide.SELL)
            and self.risk.can_place_sell()
            and self.orders.open_count < self.config.max_open_orders
        ):
            self.orders.create_order(
                OrderSide.SELL, self.tob.best_ask, self.config.order_qty
            )

        if self._update_count % 50 == 0:
            mid = self.tob.mid_price
            spread_bps = (
                (self.tob.spread / mid) * 10_000
                if mid
                else 0
            )
            logger.info(
                f"TOB bid={self.tob.best_bid:.2f} ask={self.tob.best_ask:.2f} "
                f"spread={spread_bps:.2f}bps | "
                f"equity=${self.risk.equity(mid):,.2f} "
                f"bal=${self.risk.balance:.2f} pos={self.risk.position:.6f} "
                f"unrealized_pnl=${self.risk.unrealized_pnl(mid):,.4f} "
                f"realized_pnl={self.risk.pnl:.4f} "
                f"return={self.risk.return_pct(mid):.2f}% "
                f"win_rate={self.risk.win_rate:.1f}% "
                f"max_dd={self.risk.max_drawdown_pct_seen:.2f}% "
                f"orders={self.orders.open_count}"
            )
            self.telegram.send(
                f"<b>WildBot Scalper</b>\n"
                f"{self.config.symbol} | Tick #{self._update_count}\n\n"
                f"Bid: ${self.tob.best_bid:,.2f}\n"
                f"Ask: ${self.tob.best_ask:,.2f}\n"
                f"Spread: {spread_bps:.2f} bps\n\n"
                f"Equity: ${self.risk.equity(mid):,.2f}\n"
                f"Balance: ${self.risk.balance:,.2f}\n"
                f"Unrealized PnL: ${self.risk.unrealized_pnl(mid):,.4f}\n"
                f"Realized PnL: ${self.risk.pnl:,.4f}\n"
                f"Return: {self.risk.return_pct(mid):.2f}%\n"
                f"Position: {self.risk.position:.6f}\n"
                f"Win Rate: {self.risk.win_rate:.1f}%\n"
                f"Max Drawdown: {self.risk.max_drawdown_pct_seen:.2f}%\n"
                f"Trades: {self.risk.trade_count}\n"
                f"Open Orders: {self.orders.open_count}"
            )

    async def _ws_loop(self):
        delay = self.config.reconnect_delay
        while self._running:
            try:
                logger.info(f"Connecting to {self.config.ws_url} ...")
                async with websockets.connect(
                    self.config.ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    logger.info("WebSocket connected — subscribing to ticker")
                    self.telegram.send_alert(
                        f"✅ <b>WildBot Reconnected</b>\n"
                        f"WebSocket connected to {self.config.ws_url}"
                    )
                    sub_msg = json.dumps({
                        "method": "subscribe",
                        "params": {
                            "channel": "ticker",
                            "symbol": [self.config.ws_symbol],
                            "event_trigger": "bbo",
                        },
                    })
                    await ws.send(sub_msg)
                    delay = self.config.reconnect_delay
                    async for msg in ws:
                        if not self._running:
                            break
                        if self._parse_depth_update(msg):
                            await self._handle_tick()
            except websockets.exceptions.ConnectionClosed as exc:
                logger.warning(f"Connection closed: {exc}")
                self.telegram.send_alert(
                    f"⚠️ <b>WildBot Disconnected</b>\n"
                    f"WebSocket connection closed: {exc}"
                )
            except Exception as exc:
                logger.error(f"WebSocket error: {exc}")
                self.telegram.send_alert(
                    f"⚠️ <b>WildBot Disconnected</b>\n"
                    f"WebSocket error: {exc}"
                )

            if self._running:
                self.orders.cancel_all()
                logger.info(f"Reconnecting in {delay:.1f}s ...")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.config.max_reconnect_delay)

    async def _stale_order_monitor(self):
        while self._running:
            stale = self.risk.check_stale_orders(self.orders.open_orders)
            for oid in stale:
                self.orders.cancel_order(oid)
            await asyncio.sleep(0.1)

    async def run(self):
        self._running = True
        logger.info(
            f"WildBot HFT Scalper starting | symbol={self.config.symbol} "
            f"capital=${self.config.starting_capital:.2f} "
            f"qty={self.config.order_qty} stale_ms={self.config.stale_order_ms}"
        )
        try:
            await asyncio.gather(
                self._ws_loop(),
                self._stale_order_monitor(),
            )
        except asyncio.CancelledError:
            logger.info("Scalper cancelled")
        finally:
            self.stop()

    def stop(self):
        self._running = False
        self.orders.cancel_all()
        mid = self.tob.mid_price if self.tob.mid_price > 0 else 0.0
        logger.info(
            f"Scalper stopped | equity=${self.risk.equity(mid):,.2f} "
            f"bal=${self.risk.balance:.2f} trades={self.risk.trade_count} "
            f"realized_pnl={self.risk.pnl:.4f} "
            f"unrealized_pnl={self.risk.unrealized_pnl(mid):.4f} "
            f"return={self.risk.return_pct(mid):.2f}% "
            f"win_rate={self.risk.win_rate:.1f}% "
            f"max_drawdown={self.risk.max_drawdown_pct_seen:.2f}% "
            f"final_pos={self.risk.position:.6f}"
        )
