import asyncio
import json
import os
import time
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
        self._daily_summary_hour: int = 0
        self._last_daily_summary_date: str = ""
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
        now = time.time()
        if now - self._last_send_time < 5.0:
            return
        self._last_send_time = now
        threading.Thread(target=self._send_sync, args=(message,), daemon=True).start()

    def check_daily_summary(self, stats: dict):
        if not self.enabled:
            return
        now_utc = datetime.now(timezone.utc)
        today_str = now_utc.strftime("%Y-%m-%d")
        if today_str != self._last_daily_summary_date and now_utc.hour >= self._daily_summary_hour:
            self._last_daily_summary_date = today_str
            msg = (
                f"📊 <b>WildBot Daily Summary</b>\n"
                f"Date: {today_str}\n\n"
                f"Equity: ${stats.get('equity', 0):,.2f}\n"
                f"Balance: ${stats.get('balance', 0):,.2f}\n"
                f"Realized PnL: ${stats.get('pnl', 0):,.4f}\n"
                f"Return: {stats.get('return_pct', 0):.2f}%\n"
                f"Position: {stats.get('position', 0):.6f}\n"
                f"Trades: {stats.get('trade_count', 0)}\n"
                f"Win Rate: {stats.get('win_rate', 0):.1f}%\n"
                f"Wins: {stats.get('wins', 0)} | Losses: {stats.get('losses', 0)}\n"
                f"Max Drawdown: {stats.get('max_drawdown', 0):.2f}%\n"
                f"EMA: ${stats.get('ema', 0):,.2f}\n"
                f"Momentum: {stats.get('momentum', 'neutral')}\n"
                f"Ticks: {stats.get('ticks', 0)}"
            )
            threading.Thread(target=self._send_sync, args=(msg,), daemon=True).start()

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

    @property
    def microprice(self) -> float:
        if self.bid_qty + self.ask_qty > 0 and self.best_bid > 0 and self.best_ask > 0:
            return (self.best_bid * self.ask_qty + self.best_ask * self.bid_qty) / (self.bid_qty + self.ask_qty)
        return self.mid_price


@dataclass
class ScalperConfig:
    symbol: str = "BTC/USD"
    ws_url: str = "wss://ws.kraken.com/v2"
    ws_symbol: str = "BTC/USD"
    rest_pair: str = "XBTUSD"
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
    fill_cooldown_ms: float = 200.0
    skew_factor: float = 0.5
    ema_window: int = 50
    momentum_filter_enabled: bool = True
    live_mode: bool = False


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
        self.avg_entry_price: float = 0.0
        self._trip_cost: float = 0.0

    def unrealized_pnl(self, mid_price: float) -> float:
        if self.position == 0 or self.avg_entry_price == 0:
            return 0.0
        return self.position * (mid_price - self.avg_entry_price)

    def position_value(self, mid_price: float) -> float:
        return self.position * mid_price

    def equity(self, mid_price: float) -> float:
        return self.balance + self.position_value(mid_price)

    def return_pct(self, mid_price: float) -> float:
        if self.starting_capital > 0:
            return ((self.equity(mid_price) - self.starting_capital) / self.starting_capital) * 100
        return 0.0

    def can_place_buy(self, price: float) -> bool:
        position_ok = self.position + self.config.order_qty <= self.config.max_position
        cost = price * self.config.order_qty
        if self.config.live_mode:
            affordable = cost <= self.balance
        else:
            affordable = cost <= (self.balance + self.starting_capital * 5)
        return position_ok and affordable

    def can_place_sell(self) -> bool:
        position_ok = self.position - self.config.order_qty >= -self.config.max_position
        if self.config.live_mode and self.position <= 0:
            return False
        return position_ok

    def is_spread_acceptable(self, tob: TopOfBook) -> bool:
        if tob.mid_price <= 0:
            return False
        spread_bps = (tob.spread / tob.mid_price) * 10_000
        return spread_bps <= self.config.max_spread_bps

    def _close_trip(self):
        if self._trip_cost > 1e-10:
            self.wins += 1
            self.total_profit += self._trip_cost
        elif self._trip_cost < -1e-10:
            self.losses += 1
            self.total_loss += abs(self._trip_cost)
        self._trip_cost = 0.0

    def record_fill(self, side: OrderSide, price: float, qty: float):
        old_pos = self.position
        cost = price * qty

        if side == OrderSide.BUY:
            self.balance -= cost
            self.pnl -= cost

            if old_pos >= 0:
                total_cost = self.avg_entry_price * old_pos + price * qty
                self.position = old_pos + qty
                self.avg_entry_price = total_cost / self.position if self.position != 0 else 0.0
                self._trip_cost -= cost
            elif old_pos + qty <= 0:
                self.position = old_pos + qty
                self._trip_cost -= cost
            else:
                close_qty = abs(old_pos)
                open_qty = qty - close_qty
                self._trip_cost -= price * close_qty
                self._close_trip()
                self.position = open_qty
                self.avg_entry_price = price
                self._trip_cost = -(price * open_qty)
        else:
            self.balance += cost
            self.pnl += cost

            if old_pos <= 0:
                total_cost = abs(self.avg_entry_price * old_pos) + price * qty
                self.position = old_pos - qty
                self.avg_entry_price = total_cost / abs(self.position) if self.position != 0 else 0.0
                self._trip_cost += cost
            elif old_pos - qty >= 0:
                self.position = old_pos - qty
                self._trip_cost += cost
            else:
                close_qty = old_pos
                open_qty = qty - close_qty
                self._trip_cost += price * close_qty
                self._close_trip()
                self.position = -open_qty
                self.avg_entry_price = price
                self._trip_cost = price * open_qty

        self.trade_count += 1

        if abs(self.position) < 1e-12:
            self.position = 0.0
            self._close_trip()
            self.avg_entry_price = 0.0

        logger.info(
            f"Fill: {side.value} {qty}@{price:.2f} | "
            f"bal=${self.balance:.2f} pos={self.position:.6f} "
            f"pnl={self.pnl:.4f} trades={self.trade_count} "
            f"avg_entry={self.avg_entry_price:.2f}"
        )

    def calculate_skewed_prices(self, tob: TopOfBook) -> tuple[float, float]:
        inventory_ratio = self.position / self.config.max_position if self.config.max_position > 0 else 0
        skew = inventory_ratio * self.config.skew_factor
        half_spread = tob.spread / 2
        mid = tob.microprice
        buy_price = round(mid - half_spread - skew * tob.spread, 2)
        sell_price = round(mid + half_spread - skew * tob.spread, 2)
        return buy_price, sell_price

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
    def __init__(self, config: ScalperConfig, kraken_client=None):
        self._open_orders: dict[str, Order] = {}
        self._seq: int = 0
        self._last_fill_time: float = 0.0
        self._fill_cooldown_ms: float = config.fill_cooldown_ms
        self._live_mode: bool = config.live_mode
        self._kraken = kraken_client
        self._rest_pair: str = config.rest_pair
        self._txid_map: dict[str, str] = {}
        self._consecutive_errors: int = 0
        self._last_error_time: float = 0.0
        self._error_backoff: float = 2.0

    @property
    def open_orders(self) -> dict[str, Order]:
        return self._open_orders

    @property
    def open_count(self) -> int:
        return len(self._open_orders)

    def create_order(self, side: OrderSide, price: float, qty: float) -> Optional[Order]:
        if self._live_mode and self._kraken:
            return self._create_live_order(side, price, qty)
        return self._create_paper_order(side, price, qty)

    def _create_paper_order(self, side: OrderSide, price: float, qty: float) -> Order:
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

    def _create_live_order(self, side: OrderSide, price: float, qty: float) -> Optional[Order]:
        now = time.monotonic()
        if now - self._last_error_time < self._error_backoff:
            return None
        try:
            txid = self._kraken.add_order(
                pair=self._rest_pair,
                side=side.value,
                order_type="limit",
                volume=f"{qty:.8f}",
                price=f"{price:.1f}",
            )
            self._consecutive_errors = 0
            self._error_backoff = 2.0
            order = Order(
                order_id=txid,
                side=side,
                price=price,
                quantity=qty,
                status=OrderStatus.OPEN,
                placed_at=time.monotonic(),
            )
            self._open_orders[txid] = order
            logger.info(f"LIVE placed {side.value} order {txid}: {qty}@{price:.2f}")
            return order
        except Exception as exc:
            self._consecutive_errors += 1
            self._last_error_time = now
            self._error_backoff = min(60.0, 2.0 * (2 ** self._consecutive_errors))
            logger.warning(f"LIVE order failed ({self._consecutive_errors}x, backoff={self._error_backoff:.0f}s): {exc}")
            return None

    def cancel_order(self, order_id: str) -> Optional[Order]:
        order = self._open_orders.pop(order_id, None)
        if order:
            order.status = OrderStatus.CANCELLED
            if self._live_mode and self._kraken:
                try:
                    self._kraken.cancel_order(order_id)
                    logger.info(f"LIVE cancelled order {order_id}")
                except Exception as exc:
                    logger.warning(f"LIVE cancel failed for {order_id}: {exc}")
            else:
                logger.info(f"Cancelled order {order_id}")
        return order

    def cancel_all(self) -> int:
        count = len(self._open_orders)
        if self._live_mode and self._kraken and count > 0:
            try:
                cancelled = self._kraken.cancel_all()
                logger.info(f"LIVE cancelled {cancelled} orders on exchange")
            except Exception as exc:
                logger.warning(f"LIVE cancel_all failed: {exc}")
                for oid in list(self._open_orders):
                    try:
                        self._kraken.cancel_order(oid)
                    except Exception:
                        pass
        for oid in list(self._open_orders):
            self._open_orders[oid].status = OrderStatus.CANCELLED
        self._open_orders.clear()
        if count:
            logger.info(f"Cancelled all {count} open orders")
        return count

    def check_fills_live(self) -> list[Order]:
        if not self._live_mode or not self._kraken or not self._open_orders:
            return []
        now_ms = time.monotonic() * 1000
        if now_ms - self._last_fill_time < self._fill_cooldown_ms:
            return []
        try:
            txids = list(self._open_orders.keys())
            results = self._kraken.query_orders(txids)
            filled = []
            for txid, info in results.items():
                status = info.get("status", "")
                if status == "closed":
                    order = self._open_orders.pop(txid, None)
                    if order:
                        vol_exec = float(info.get("vol_exec", order.quantity))
                        avg_price = float(info.get("price", order.price))
                        if avg_price <= 0:
                            avg_price = order.price
                        order.status = OrderStatus.FILLED
                        order.quantity = vol_exec
                        order.price = avg_price
                        filled.append(order)
                        self._last_fill_time = now_ms
                        logger.info(f"LIVE fill detected: {order.side.value} {vol_exec}@{avg_price:.2f} txid={txid}")
                elif status == "canceled" or status == "expired":
                    order = self._open_orders.pop(txid, None)
                    if order:
                        order.status = OrderStatus.CANCELLED
                        logger.info(f"LIVE order {status}: {txid}")
            return filled
        except Exception as exc:
            logger.warning(f"LIVE fill check failed: {exc}")
            return []

    def simulate_fill_check(self, tob: TopOfBook) -> list[Order]:
        now_ms = time.monotonic() * 1000
        if now_ms - self._last_fill_time < self._fill_cooldown_ms:
            return []
        filled = []
        for oid in list(self._open_orders):
            order = self._open_orders[oid]
            if order.side == OrderSide.BUY and tob.best_ask <= order.price:
                order.status = OrderStatus.FILLED
                filled.append(self._open_orders.pop(oid))
                self._last_fill_time = now_ms
                break
            elif order.side == OrderSide.SELL and tob.best_bid >= order.price:
                order.status = OrderStatus.FILLED
                filled.append(self._open_orders.pop(oid))
                self._last_fill_time = now_ms
                break
        return filled

    def has_side(self, side: OrderSide) -> bool:
        return any(o.side == side for o in self._open_orders.values())


class HFTScalper:
    def __init__(self, config: Optional[ScalperConfig] = None, kraken_client=None):
        self.config = config or ScalperConfig()
        self.tob = TopOfBook()
        self.risk = RiskManager(self.config)
        self._kraken = kraken_client
        self.orders = OrderManager(self.config, kraken_client=kraken_client)
        self.telegram = TelegramNotifier()
        self._running = False
        self._update_count = 0
        self._ema: float = 0.0
        self._ema_alpha: float = 2.0 / (self.config.ema_window + 1)
        self._trade_history: list[dict] = []
        self._drawdown_alerted: bool = False
        self._live_fill_poll_interval: float = 1.0
        self._last_live_fill_poll: float = 0.0

    @property
    def momentum_signal(self) -> str:
        if self._ema == 0:
            return "neutral"
        mid = self.tob.mid_price
        if mid <= 0:
            return "neutral"
        diff_pct = (mid - self._ema) / self._ema * 100
        if diff_pct > 0.01:
            return "up"
        elif diff_pct < -0.01:
            return "down"
        return "neutral"

    def _update_ema(self):
        mid = self.tob.mid_price
        if mid <= 0:
            return
        if self._ema == 0:
            self._ema = mid
        else:
            self._ema = self._ema_alpha * mid + (1 - self._ema_alpha) * self._ema

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
        self._update_ema()

        if self.config.live_mode:
            now = time.monotonic()
            if now - self._last_live_fill_poll >= self._live_fill_poll_interval:
                self._last_live_fill_poll = now
                filled = self.orders.check_fills_live()
            else:
                filled = []
        else:
            filled = self.orders.simulate_fill_check(self.tob)
        for order in filled:
            self.risk.record_fill(order.side, order.price, order.quantity)
            self._trade_history.append({
                "time": time.time(),
                "side": order.side.value,
                "price": order.price,
                "qty": order.quantity,
                "pnl": self.risk.pnl,
                "balance": self.risk.balance,
            })
            if len(self._trade_history) > 50:
                self._trade_history = self._trade_history[-50:]

        self.risk.update_drawdown(self.tob.mid_price)

        if self.risk.is_stopped:
            self.orders.cancel_all()
            if not self._drawdown_alerted:
                self._drawdown_alerted = True
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

        buy_price, sell_price = self.risk.calculate_skewed_prices(self.tob)
        momentum = self.momentum_signal

        place_buy = True
        place_sell = True
        if self.config.momentum_filter_enabled:
            if momentum == "up":
                place_sell = False
            elif momentum == "down":
                place_buy = False

        if (
            place_buy
            and not self.orders.has_side(OrderSide.BUY)
            and self.risk.can_place_buy(buy_price)
            and self.orders.open_count < self.config.max_open_orders
        ):
            self.orders.create_order(
                OrderSide.BUY, buy_price, self.config.order_qty
            )

        if (
            place_sell
            and not self.orders.has_side(OrderSide.SELL)
            and self.risk.can_place_sell()
            and self.orders.open_count < self.config.max_open_orders
        ):
            self.orders.create_order(
                OrderSide.SELL, sell_price, self.config.order_qty
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
                f"orders={self.orders.open_count} "
                f"microprice={self.tob.microprice:.2f} "
                f"ema={self._ema:.2f} momentum={momentum} "
                f"avg_entry={self.risk.avg_entry_price:.2f}"
            )
            self.telegram.send(
                f"<b>WildBot Scalper</b>\n"
                f"{self.config.symbol} | Tick #{self._update_count}\n\n"
                f"Bid: ${self.tob.best_bid:,.2f}\n"
                f"Ask: ${self.tob.best_ask:,.2f}\n"
                f"Spread: {spread_bps:.2f} bps\n"
                f"Microprice: ${self.tob.microprice:,.2f}\n\n"
                f"Equity: ${self.risk.equity(mid):,.2f}\n"
                f"Balance: ${self.risk.balance:,.2f}\n"
                f"Unrealized PnL: ${self.risk.unrealized_pnl(mid):,.4f}\n"
                f"Realized PnL: ${self.risk.pnl:,.4f}\n"
                f"Return: {self.risk.return_pct(mid):.2f}%\n"
                f"Position: {self.risk.position:.6f}\n"
                f"Avg Entry: ${self.risk.avg_entry_price:,.2f}\n"
                f"Win Rate: {self.risk.win_rate:.1f}%\n"
                f"Max Drawdown: {self.risk.max_drawdown_pct_seen:.2f}%\n"
                f"Trades: {self.risk.trade_count}\n"
                f"Open Orders: {self.orders.open_count}\n"
                f"EMA: ${self._ema:,.2f}\n"
                f"Momentum: {momentum}"
            )

        if self._update_count % 500 == 0:
            stats = {
                "equity": self.risk.equity(self.tob.mid_price),
                "balance": self.risk.balance,
                "pnl": self.risk.pnl,
                "return_pct": self.risk.return_pct(self.tob.mid_price),
                "position": self.risk.position,
                "trade_count": self.risk.trade_count,
                "win_rate": self.risk.win_rate,
                "wins": self.risk.wins,
                "losses": self.risk.losses,
                "max_drawdown": self.risk.max_drawdown_pct_seen,
                "ema": self._ema,
                "momentum": self.momentum_signal,
                "ticks": self._update_count,
            }
            self.telegram.check_daily_summary(stats)

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

    def _initialize_live_balance(self):
        if not self.config.live_mode or not self._kraken:
            return
        try:
            if not self._kraken.validate_connection():
                logger.error("LIVE MODE: Kraken API connection failed — falling back to paper mode")
                self.config.live_mode = False
                self.orders._live_mode = False
                self.telegram.send_alert(
                    "🚨 <b>WildBot LIVE MODE FAILED</b>\n"
                    "Could not connect to Kraken API.\n"
                    "Falling back to paper trading mode."
                )
                return

            balances = self._kraken.get_balance()
            usd_bal = balances.get("ZUSD", 0.0)
            btc_bal = balances.get("XXBT", 0.0)

            self.risk.starting_capital = usd_bal
            self.risk.balance = usd_bal
            self.risk.peak_equity = usd_bal
            self.risk.position = btc_bal
            self.risk.avg_entry_price = 0.0

            logger.info(
                f"LIVE MODE initialized | USD=${usd_bal:,.2f} BTC={btc_bal:.8f}"
            )
            self.telegram.send_alert(
                f"🟢 <b>WildBot LIVE MODE ACTIVE</b>\n"
                f"Exchange: Kraken\n"
                f"Pair: {self.config.symbol}\n"
                f"USD Balance: ${usd_bal:,.2f}\n"
                f"BTC Balance: {btc_bal:.8f}\n"
                f"Order Size: {self.config.order_qty} BTC\n"
                f"Max Position: {self.config.max_position} BTC"
            )
        except Exception as exc:
            logger.error(f"LIVE MODE initialization failed: {exc} — falling back to paper mode")
            self.config.live_mode = False
            self.orders._live_mode = False
            self.telegram.send_alert(
                f"🚨 <b>WildBot LIVE MODE FAILED</b>\n"
                f"Error: {exc}\n"
                f"Falling back to paper trading mode."
            )

    async def run(self):
        self._running = True
        mode_str = "LIVE" if self.config.live_mode else "PAPER"
        logger.info(
            f"WildBot HFT Scalper starting [{mode_str}] | symbol={self.config.symbol} "
            f"capital=${self.config.starting_capital:.2f} "
            f"qty={self.config.order_qty} stale_ms={self.config.stale_order_ms} "
            f"fill_cooldown_ms={self.config.fill_cooldown_ms} "
            f"skew_factor={self.config.skew_factor} "
            f"ema_window={self.config.ema_window} "
            f"momentum_filter={self.config.momentum_filter_enabled}"
        )

        self._initialize_live_balance()

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
            f"final_pos={self.risk.position:.6f} "
            f"avg_entry={self.risk.avg_entry_price:.2f} "
            f"ema={self._ema:.2f}"
        )
