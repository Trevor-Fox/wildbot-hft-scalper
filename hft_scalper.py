import asyncio
import json
import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("HFTScalper")


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
    symbol: str = "BTC/USDT"
    ws_url: str = "wss://stream.binance.com:9443/ws/btcusdt@depth5@100ms"
    order_qty: float = 0.001
    max_spread_bps: float = 10.0
    stale_order_ms: float = 500.0
    max_position: float = 0.01
    max_open_orders: int = 2
    tick_size: float = 0.01
    reconnect_delay: float = 1.0
    max_reconnect_delay: float = 30.0


class RiskManager:
    def __init__(self, config: ScalperConfig):
        self.config = config
        self.position: float = 0.0
        self.pnl: float = 0.0
        self.trade_count: int = 0

    def can_place_buy(self) -> bool:
        return self.position + self.config.order_qty <= self.config.max_position

    def can_place_sell(self) -> bool:
        return self.position - self.config.order_qty >= -self.config.max_position

    def is_spread_acceptable(self, tob: TopOfBook) -> bool:
        if tob.mid_price <= 0:
            return False
        spread_bps = (tob.spread / tob.mid_price) * 10_000
        return spread_bps <= self.config.max_spread_bps

    def record_fill(self, side: OrderSide, price: float, qty: float):
        if side == OrderSide.BUY:
            self.position += qty
            self.pnl -= price * qty
        else:
            self.position -= qty
            self.pnl += price * qty
        self.trade_count += 1
        logger.info(
            f"Fill: {side.value} {qty}@{price:.2f} | "
            f"pos={self.position:.6f} pnl={self.pnl:.4f} trades={self.trade_count}"
        )

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
        self._running = False
        self._update_count = 0

    def _parse_depth_update(self, raw: str) -> bool:
        try:
            data = json.loads(raw)
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            if bids and asks:
                self.tob.best_bid = float(bids[0][0])
                self.tob.bid_qty = float(bids[0][1])
                self.tob.best_ask = float(asks[0][0])
                self.tob.ask_qty = float(asks[0][1])
                self.tob.timestamp = time.time()
                return True
        except (json.JSONDecodeError, KeyError, IndexError, ValueError) as exc:
            logger.warning(f"Parse error: {exc}")
        return False

    async def _handle_tick(self):
        self._update_count += 1

        filled = self.orders.simulate_fill_check(self.tob)
        for order in filled:
            self.risk.record_fill(order.side, order.price, order.quantity)

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
            and self.risk.can_place_buy()
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
            spread_bps = (
                (self.tob.spread / self.tob.mid_price) * 10_000
                if self.tob.mid_price
                else 0
            )
            logger.info(
                f"TOB bid={self.tob.best_bid:.2f} ask={self.tob.best_ask:.2f} "
                f"spread={spread_bps:.2f}bps | "
                f"pos={self.risk.position:.6f} pnl={self.risk.pnl:.4f} "
                f"orders={self.orders.open_count}"
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
                    logger.info("WebSocket connected — streaming order book")
                    delay = self.config.reconnect_delay
                    async for msg in ws:
                        if not self._running:
                            break
                        if self._parse_depth_update(msg):
                            await self._handle_tick()
            except websockets.exceptions.ConnectionClosed as exc:
                logger.warning(f"Connection closed: {exc}")
            except Exception as exc:
                logger.error(f"WebSocket error: {exc}")

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
        logger.info(
            f"Scalper stopped | trades={self.risk.trade_count} "
            f"pnl={self.risk.pnl:.4f} final_pos={self.risk.position:.6f}"
        )
