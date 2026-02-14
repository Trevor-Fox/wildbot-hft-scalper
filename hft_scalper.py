import asyncio
import concurrent.futures
import copy
import json
import os
import statistics
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
            mode = stats.get("mode", "PAPER")
            equity = stats.get("equity", 0)
            rpnl = stats.get("pnl", 0)
            upnl = stats.get("unrealized_pnl", 0)
            total_pnl = rpnl + upnl
            pnl_emoji = "📈" if total_pnl >= 0 else "📉"
            msg = (
                f"📊 <b>WildBot Daily Summary</b> | {mode}\n"
                f"Date: {today_str}\n"
                f"{'─' * 20}\n"
                f"{pnl_emoji} <b>Total PnL: ${total_pnl:,.4f}</b>\n"
                f"Equity: ${equity:,.2f}\n"
                f"USDC: ${stats.get('balance', 0):,.2f} | BTC: {stats.get('position', 0):.6f}\n"
                f"{'─' * 20}\n"
                f"Realized: ${rpnl:,.4f}\n"
                f"Unrealized: ${upnl:,.4f}\n"
                f"Return: {stats.get('return_pct', 0):.2f}%\n"
                f"Trades: {stats.get('trade_count', 0)} | W/L: {stats.get('wins', 0)}/{stats.get('losses', 0)} ({stats.get('win_rate', 0):.0f}%)\n"
                f"Max DD: {stats.get('max_drawdown', 0):.2f}%\n"
                f"{'─' * 20}\n"
                f"Ticks processed: {stats.get('ticks', 0)}"
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


@dataclass
class PairConfig:
    ws_symbol: str
    rest_pair: str
    display_name: str
    min_qty: float
    price_decimals: int
    qty_decimals: int


SCAN_PAIRS = [
    PairConfig("BTC/USDC", "XBTUSDC", "BTC", 0.0001, 1, 8),
    PairConfig("ETH/USDC", "ETHUSDC", "ETH", 0.01, 2, 8),
    PairConfig("SOL/USDC", "SOLUSDC", "SOL", 0.25, 4, 8),
    PairConfig("DOGE/USDC", "DOGEUSDC", "DOGE", 50.0, 5, 2),
    PairConfig("XRP/USDC", "XRPUSDC", "XRP", 10.0, 5, 2),
    PairConfig("LINK/USDC", "LINKUSDC", "LINK", 0.2, 4, 8),
    PairConfig("AVAX/USDC", "AVAXUSDC", "AVAX", 0.1, 4, 8),
    PairConfig("ADA/USDC", "ADAUSDC", "ADA", 10.0, 6, 2),
]


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


class PairScanner:
    """Tracks volatility and spread across multiple pairs to find best trading opportunity."""

    def __init__(self, pairs: list[PairConfig], volatility_window: int = 100):
        self.pairs = {p.ws_symbol: p for p in pairs}
        self.tobs: dict[str, TopOfBook] = {p.ws_symbol: TopOfBook() for p in pairs}
        self._price_histories: dict[str, list[float]] = {p.ws_symbol: [] for p in pairs}
        self._volatilities: dict[str, float] = {p.ws_symbol: 0.0 for p in pairs}
        self._spreads_bps: dict[str, float] = {p.ws_symbol: 0.0 for p in pairs}
        self._volatility_window = volatility_window
        self._min_switch_interval = 20.0
        self._last_switch_time = 0.0
        self._active_pair: str = ""

    def update(self, ws_symbol: str, bid: float, ask: float, bid_qty: float, ask_qty: float):
        if ws_symbol not in self.tobs:
            return
        tob = self.tobs[ws_symbol]
        tob.best_bid = bid
        tob.best_ask = ask
        tob.bid_qty = bid_qty
        tob.ask_qty = ask_qty
        tob.timestamp = time.time()

        mid = tob.mid_price
        if mid <= 0:
            return

        self._spreads_bps[ws_symbol] = (tob.spread / mid) * 10_000

        history = self._price_histories[ws_symbol]
        history.append(mid)
        if len(history) > self._volatility_window:
            self._price_histories[ws_symbol] = history[-self._volatility_window:]
        if len(history) >= 20:
            mean = sum(history) / len(history)
            if mean > 0:
                self._volatilities[ws_symbol] = (statistics.stdev(history) / mean) * 10_000

    def get_best_pair(self, available_balance: float, maker_fee_bps: float, min_profit_bps: float) -> Optional[PairConfig]:
        candidates = []
        min_edge = 2 * maker_fee_bps + min_profit_bps

        for ws_sym, pair_cfg in self.pairs.items():
            tob = self.tobs[ws_sym]
            vol = self._volatilities[ws_sym]
            spread = self._spreads_bps[ws_sym]

            if tob.mid_price <= 0:
                continue

            min_notional = pair_cfg.min_qty * tob.mid_price
            if min_notional > available_balance * 0.95:
                continue

            if len(self._price_histories[ws_sym]) < 20:
                continue

            score = vol - spread * 0.1

            candidates.append((ws_sym, pair_cfg, vol, spread, score))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[4], reverse=True)
        return candidates[0][1]

    def should_switch(self, current_pair: str, best_pair: PairConfig) -> bool:
        if best_pair.ws_symbol == current_pair:
            return False

        now = time.time()
        if now - self._last_switch_time < self._min_switch_interval:
            return False

        current_vol = self._volatilities.get(current_pair, 0)
        best_vol = self._volatilities.get(best_pair.ws_symbol, 0)

        if current_vol > 0 and best_vol < current_vol * 1.2:
            return False

        return True

    def record_switch(self):
        self._last_switch_time = time.time()

    def get_scanner_data(self) -> list[dict]:
        result = []
        for ws_sym in sorted(self.pairs.keys()):
            pair_cfg = self.pairs[ws_sym]
            tob = self.tobs[ws_sym]
            result.append({
                "symbol": pair_cfg.display_name,
                "ws_symbol": ws_sym,
                "mid_price": round(tob.mid_price, pair_cfg.price_decimals) if tob.mid_price > 0 else 0,
                "spread_bps": round(self._spreads_bps.get(ws_sym, 0), 2),
                "volatility_bps": round(self._volatilities.get(ws_sym, 0), 2),
                "active": ws_sym == self._active_pair,
                "data_points": len(self._price_histories.get(ws_sym, [])),
            })
        return result


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
    maker_fee_bps: float = 16.0
    min_profit_bps: float = 2.0
    requote_threshold_bps: float = 5.0
    min_volatility_bps: float = 3.0
    volatility_window: int = 100
    max_hold_seconds: float = 120.0
    stop_loss_bps: float = 20.0
    volatility_exit_multiplier: float = 0.8
    min_hold_seconds: float = 10.0
    base_hold_seconds: float = 300.0
    max_hold_scaling: float = 3.0
    target_exit_bps: float = 8.0
    price_decimals: int = 1


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
        self.fees_paid: float = 0.0
        self._position_entry_time: float = 0.0
        self._dust_qty: float = config.order_qty / 10

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
        if self.config.live_mode and self.position < self._dust_qty:
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
        was_flat = abs(old_pos) < self._dust_qty
        cost = price * qty
        fee = cost * self.config.maker_fee_bps / 10_000
        self.fees_paid += fee
        self.balance -= fee
        self.pnl -= fee
        self._trip_cost -= fee

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

        if abs(self.position) < self._dust_qty:
            self.position = 0.0
            self._close_trip()
            self.avg_entry_price = 0.0
            self._position_entry_time = 0.0
        elif was_flat:
            self._position_entry_time = time.monotonic()

        dec = self.config.price_decimals
        logger.info(
            f"Fill: {side.value} {qty}@{price:.{dec}f} fee=${fee:.4f} | "
            f"bal=${self.balance:.2f} pos={self.position:.6f} "
            f"pnl={self.pnl:.4f} fees_total=${self.fees_paid:.4f} trades={self.trade_count} "
            f"avg_entry={self.avg_entry_price:.{dec}f}"
        )

    def dynamic_exit_bps(self, volatility_bps: float) -> float:
        vol_target = volatility_bps * self.config.volatility_exit_multiplier
        return max(self.config.target_exit_bps, vol_target)

    def dynamic_hold_seconds(self, exit_bps: float) -> float:
        ratio = exit_bps / self.config.target_exit_bps if self.config.target_exit_bps > 0 else 1.0
        hold = self.config.base_hold_seconds * ratio
        return max(self.config.min_hold_seconds, min(hold, self.config.base_hold_seconds * self.config.max_hold_scaling))

    def calculate_skewed_prices(self, tob: TopOfBook, volatility_bps: float = 0.0) -> tuple[float | None, float | None]:
        mid = tob.microprice

        dec = self.config.price_decimals
        if self.position > self._dust_qty:
            exit_bps = self.dynamic_exit_bps(volatility_bps)
            exit_offset = mid * exit_bps / 10_000
            buy_price = None
            sell_price = round(max(self.avg_entry_price + exit_offset, tob.best_ask), dec)
        elif self.position < -self._dust_qty:
            exit_bps = self.dynamic_exit_bps(volatility_bps)
            exit_offset = mid * exit_bps / 10_000
            buy_price = round(min(self.avg_entry_price - exit_offset, tob.best_bid), dec)
            sell_price = None
        else:
            inventory_ratio = self.position / self.config.max_position if self.config.max_position > 0 else 0
            skew = inventory_ratio * self.config.skew_factor
            buy_price = round(tob.best_bid - skew * tob.spread, dec)
            sell_price = round(tob.best_ask - skew * tob.spread, dec)

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
        self._filled_during_cancel: list[Order] = []
        self._needs_balance_sync: bool = False
        self._price_decimals: int = 1
        self._qty_decimals: int = 8

    @property
    def open_orders(self) -> dict[str, Order]:
        return self._open_orders

    @property
    def open_count(self) -> int:
        return len(self._open_orders)

    def create_order(self, side: OrderSide, price: float, qty: float, force_taker: bool = False) -> Optional[Order]:
        if self._live_mode and self._kraken:
            return self._create_live_order(side, price, qty, force_taker=force_taker)
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

    def _create_live_order(self, side: OrderSide, price: float, qty: float, force_taker: bool = False) -> Optional[Order]:
        now = time.monotonic()
        if now - self._last_error_time < self._error_backoff:
            return None
        try:
            oflags = "" if force_taker else "post"
            txid = self._kraken.add_order(
                pair=self._rest_pair,
                side=side.value,
                order_type="limit",
                volume=f"{qty:.{self._qty_decimals}f}",
                price=f"{price:.{self._price_decimals}f}",
                oflags=oflags,
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
            logger.info(f"LIVE placed {side.value} order {txid}: {qty}@{price:.{self._price_decimals}f}")
            return order
        except Exception as exc:
            self._consecutive_errors += 1
            self._last_error_time = now
            self._error_backoff = min(60.0, 2.0 * (2 ** self._consecutive_errors))
            logger.warning(f"LIVE order failed ({self._consecutive_errors}x, backoff={self._error_backoff:.0f}s): {exc}")
            if "Insufficient funds" in str(exc):
                self._needs_balance_sync = True
            return None

    def cancel_order(self, order_id: str) -> Optional[Order]:
        order = self._open_orders.get(order_id)
        if not order:
            return None
        if self._live_mode and self._kraken:
            try:
                self._kraken.cancel_order(order_id)
                order.status = OrderStatus.CANCELLED
                self._open_orders.pop(order_id, None)
                logger.info(f"LIVE cancelled order {order_id}")
            except Exception as exc:
                err_str = str(exc)
                if "Unknown order" in err_str:
                    try:
                        result = self._kraken.query_orders([order_id])
                        info = result.get(order_id, {})
                        status = info.get("status", "")
                        if status == "closed":
                            vol_exec = float(info.get("vol_exec", order.quantity))
                            avg_price = float(info.get("price", order.price))
                            if avg_price <= 0:
                                avg_price = order.price
                            order.status = OrderStatus.FILLED
                            order.quantity = vol_exec
                            order.price = avg_price
                            self._open_orders.pop(order_id, None)
                            self._filled_during_cancel.append(order)
                            logger.info(f"LIVE order {order_id} was FILLED before cancel: {order.side.value} {vol_exec}@{avg_price:.2f}")
                            return order
                    except Exception:
                        pass
                    order.status = OrderStatus.CANCELLED
                    self._open_orders.pop(order_id, None)
                    logger.info(f"LIVE order {order_id} gone from exchange (likely filled or expired)")
                else:
                    logger.warning(f"LIVE cancel failed for {order_id}: {exc}")
                    order.status = OrderStatus.CANCELLED
                    self._open_orders.pop(order_id, None)
        else:
            order.status = OrderStatus.CANCELLED
            self._open_orders.pop(order_id, None)
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
                        dec = self._price_decimals
                        logger.info(f"LIVE fill detected: {order.side.value} {vol_exec}@{avg_price:.{dec}f} txid={txid}")
                elif status == "canceled" or status == "expired":
                    order = self._open_orders.pop(txid, None)
                    if order:
                        order.status = OrderStatus.CANCELLED
                        logger.info(f"LIVE order {status}: {txid}")
            return filled
        except Exception as exc:
            if "Rate limit" in str(exc):
                self._fill_backoff = min(getattr(self, '_fill_backoff', 5.0) * 2, 30.0)
                logger.warning(f"LIVE fill check rate limited, backing off to {self._fill_backoff:.0f}s")
            else:
                self._fill_backoff = 5.0
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
    def __init__(self, config: Optional[ScalperConfig] = None, kraken_client=None, scanner: Optional[PairScanner] = None):
        self.config = config or ScalperConfig()
        self.tob = TopOfBook()
        self.risk = RiskManager(self.config)
        self._kraken = kraken_client
        self.orders = OrderManager(self.config, kraken_client=kraken_client)
        self.telegram = TelegramNotifier()
        self.scanner = scanner
        self._running = False
        self._update_count = 0
        self._ema: float = 0.0
        self._ema_alpha: float = 2.0 / (self.config.ema_window + 1)
        self._trade_history: list[dict] = []
        self._price_history: list[float] = []
        self._volatility_bps: float = 0.0
        self._drawdown_alerted: bool = False
        self._live_fill_poll_interval: float = 3.0
        self._last_live_fill_poll: float = 0.0
        self._balance_refresh_interval: float = 300.0
        self._last_balance_refresh: float = 0.0
        self._live_balance_deferred: bool = False
        self._status_reason: str = "initializing"
        self._last_force_exit_time: float = 0.0
        self._force_exit_order_time: float = 0.0

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

        self._price_history.append(mid)
        if len(self._price_history) > self.config.volatility_window:
            self._price_history = self._price_history[-self.config.volatility_window:]
        if len(self._price_history) >= 20:
            mean = sum(self._price_history) / len(self._price_history)
            if mean > 0:
                self._volatility_bps = (statistics.stdev(self._price_history) / mean) * 10_000

    def _switch_pair(self, new_pair: PairConfig):
        if abs(self.risk.position) > self.risk._dust_qty:
            logger.warning(f"Cannot switch pair while holding position: {self.risk.position}")
            return False

        old_symbol = self.config.symbol
        self.config.symbol = new_pair.ws_symbol
        self.config.ws_symbol = new_pair.ws_symbol
        self.config.rest_pair = new_pair.rest_pair
        self.config.order_qty = new_pair.min_qty
        self.config.max_position = new_pair.min_qty * 100
        self.risk._dust_qty = new_pair.min_qty / 10
        self.orders._rest_pair = new_pair.rest_pair
        self.config.price_decimals = new_pair.price_decimals
        self.orders._price_decimals = new_pair.price_decimals
        self.orders._qty_decimals = new_pair.qty_decimals

        self._ema = 0.0
        self._price_history.clear()
        self._volatility_bps = 0.0

        if self.scanner and new_pair.ws_symbol in self.scanner.tobs:
            scanner_tob = self.scanner.tobs[new_pair.ws_symbol]
            self.tob.best_bid = scanner_tob.best_bid
            self.tob.best_ask = scanner_tob.best_ask
            self.tob.bid_qty = scanner_tob.bid_qty
            self.tob.ask_qty = scanner_tob.ask_qty

        self.scanner._active_pair = new_pair.ws_symbol
        self.scanner.record_switch()

        logger.info(f"SWITCHED PAIR: {old_symbol} → {new_pair.ws_symbol} | qty={new_pair.min_qty}")
        self.telegram.send_alert(
            f"🔄 <b>Pair Switch</b>\n"
            f"{old_symbol} → {new_pair.ws_symbol}\n"
            f"Order Size: {new_pair.min_qty}"
        )
        return True

    def _parse_depth_update(self, raw: str) -> tuple[bool, str]:
        try:
            data = json.loads(raw)
            channel = data.get("channel")
            if channel == "ticker":
                tick_data = data.get("data", [{}])[0]
                symbol = tick_data.get("symbol", "")
                bid = tick_data.get("bid")
                ask = tick_data.get("ask")
                bid_qty = tick_data.get("bid_qty")
                ask_qty = tick_data.get("ask_qty")
                if bid is not None and ask is not None:
                    bid_f = float(bid)
                    ask_f = float(ask)
                    bid_qty_f = float(bid_qty) if bid_qty else 0.0
                    ask_qty_f = float(ask_qty) if ask_qty else 0.0

                    if self.scanner:
                        self.scanner.update(symbol, bid_f, ask_f, bid_qty_f, ask_qty_f)

                    if symbol == self.config.ws_symbol:
                        self.tob.best_bid = bid_f
                        self.tob.best_ask = ask_f
                        self.tob.bid_qty = bid_qty_f
                        self.tob.ask_qty = ask_qty_f
                        self.tob.timestamp = time.time()
                        return True, symbol
                    return False, symbol
            elif channel == "heartbeat":
                return False, ""
            elif data.get("method") == "subscribe" and data.get("success"):
                logger.info(f"Subscribed to {data.get('result', {}).get('channel', 'unknown')}")
                return False, ""
        except (json.JSONDecodeError, KeyError, IndexError, ValueError) as exc:
            logger.warning(f"Parse error: {exc}")
        return False, ""

    async def _handle_tick(self):
        self._update_count += 1
        self._update_ema()

        if self.config.live_mode and self._live_balance_deferred and self.tob.mid_price > 0:
            self._live_balance_deferred = False
            if self.risk.position > 0:
                btc_value = self.risk.position * self.tob.mid_price
                total = self.risk.balance + btc_value
                self.risk.starting_capital = total
                self.risk.peak_equity = total
                self.risk.avg_entry_price = self.tob.mid_price
                self.risk._position_entry_time = time.monotonic()
                logger.info(f"Deferred balance init: BTC value=${btc_value:,.2f} total=${total:,.2f}")

        if self.config.live_mode:
            now = time.monotonic()
            fill_interval = getattr(self.orders, '_fill_backoff', self._live_fill_poll_interval)
            if now - self._last_live_fill_poll >= fill_interval:
                self._last_live_fill_poll = now
                filled = self.orders.check_fills_live()
                if filled:
                    self.orders._fill_backoff = self._live_fill_poll_interval
            else:
                filled = []
            if self.orders._filled_during_cancel:
                filled.extend(self.orders._filled_during_cancel)
                self.orders._filled_during_cancel.clear()
            if self.orders._needs_balance_sync:
                self.orders._needs_balance_sync = False
                self._refresh_live_balance()
                logger.info("Balance synced after insufficient funds error")
            if now - self._last_balance_refresh >= self._balance_refresh_interval:
                self._last_balance_refresh = now
                self._refresh_live_balance()
        else:
            filled = self.orders.simulate_fill_check(self.tob)
        for order in filled:
            self.risk.record_fill(order.side, order.price, order.quantity)
            if abs(self.risk.position) < self.risk._dust_qty:
                self._force_exit_order_time = 0.0
            mid = self.tob.mid_price
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
            mode_tag = "LIVE" if self.config.live_mode else "PAPER"
            side_emoji = "🟢" if order.side == OrderSide.BUY else "🔴"
            fee_est = order.price * order.quantity * self.config.maker_fee_bps / 10_000
            self.telegram.send_alert(
                f"{side_emoji} <b>{mode_tag} Fill</b> | {self.config.symbol}\n"
                f"{order.side.value.upper()} {order.quantity} @ ${order.price:,.2f}\n"
                f"Fee: ~${fee_est:,.4f}\n\n"
                f"Position: {self.risk.position:.6f} BTC\n"
                f"Avg Entry: ${self.risk.avg_entry_price:,.2f}\n"
                f"Equity: ${self.risk.equity(mid):,.2f}\n"
                f"Realized PnL: ${self.risk.pnl:,.4f}\n"
                f"Total Fees: ${self.risk.fees_paid:,.4f}\n"
                f"W/L: {self.risk.wins}/{self.risk.losses}"
            )

        self.risk.update_drawdown(self.tob.mid_price)

        if self.risk.is_stopped:
            self._status_reason = "drawdown_stopped"
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

        if not self.risk.is_spread_acceptable(self.tob):
            self._status_reason = "spread_wide"
            if self.orders.open_count > 0:
                self.orders.cancel_all()
                logger.debug("Spread too wide — cancelled all orders")
            return

        now_mono = time.monotonic()
        reprice_after = 30.0
        if abs(self.risk.position) > self.risk._dust_qty and self.risk._position_entry_time > 0:
            hold_time = now_mono - self.risk._position_entry_time
            dynamic_hold = self.risk.dynamic_hold_seconds(self.risk.dynamic_exit_bps(self._volatility_bps))
            if hold_time > dynamic_hold:
                exit_side = OrderSide.SELL if self.risk.position > 0 else OrderSide.BUY
                has_exit_order = any(o.side == exit_side for o in self.orders.open_orders.values())
                if has_exit_order and (now_mono - self._force_exit_order_time) < reprice_after:
                    self._status_reason = "time_stop"
                    return
                self._last_force_exit_time = now_mono
                self._force_exit_order_time = now_mono
                self.orders.cancel_all()
                exit_price = None
                dec = self.config.price_decimals
                if self.risk.position > 0:
                    exit_price = round(self.tob.best_bid * 0.999, dec)
                    self.orders.create_order(OrderSide.SELL, exit_price, abs(self.risk.position), force_taker=True)
                elif self.risk.position < 0:
                    exit_price = round(self.tob.best_ask * 1.001, dec)
                    self.orders.create_order(OrderSide.BUY, exit_price, abs(self.risk.position), force_taker=True)
                if exit_price is not None:
                    self._status_reason = "time_stop"
                    logger.warning(f"TIME STOP: Position held {hold_time:.0f}s > max {dynamic_hold:.0f}s — force exit at {exit_price:.2f}")
                    self.telegram.send_alert(f"⏰ <b>TIME STOP</b>\nForce exit after {hold_time:.0f}s\nPrice: ${exit_price:,.2f}")
                self._status_reason = "time_stop"
                return

        if abs(self.risk.position) > self.risk._dust_qty and self.risk.avg_entry_price > 0:
            mid = self.tob.mid_price
            if self.risk.position > 0:
                adverse_bps = (self.risk.avg_entry_price - mid) / self.risk.avg_entry_price * 10_000
            else:
                adverse_bps = (mid - self.risk.avg_entry_price) / self.risk.avg_entry_price * 10_000
            if adverse_bps > self.config.stop_loss_bps:
                exit_side = OrderSide.SELL if self.risk.position > 0 else OrderSide.BUY
                has_exit_order = any(o.side == exit_side for o in self.orders.open_orders.values())
                if has_exit_order and (now_mono - self._force_exit_order_time) < reprice_after:
                    self._status_reason = "stop_loss"
                    return
                self._last_force_exit_time = now_mono
                self._force_exit_order_time = now_mono
                self.orders.cancel_all()
                exit_price = None
                dec = self.config.price_decimals
                if self.risk.position > 0:
                    exit_price = round(self.tob.best_bid * 0.999, dec)
                    self.orders.create_order(OrderSide.SELL, exit_price, abs(self.risk.position), force_taker=True)
                elif self.risk.position < 0:
                    exit_price = round(self.tob.best_ask * 1.001, dec)
                    self.orders.create_order(OrderSide.BUY, exit_price, abs(self.risk.position), force_taker=True)
                if exit_price is not None:
                    self._status_reason = "stop_loss"
                    logger.warning(f"STOP LOSS: Adverse move {adverse_bps:.1f}bps > max {self.config.stop_loss_bps:.1f}bps — force exit at {exit_price:.2f}")
                    self.telegram.send_alert(f"🛑 <b>STOP LOSS</b>\nAdverse move {adverse_bps:.1f}bps\nForce exit at ${exit_price:,.2f}")
                self._status_reason = "stop_loss"
                return

        if self.scanner and self._update_count % 50 == 0 and abs(self.risk.position) < self.risk._dust_qty:
            best = self.scanner.get_best_pair(
                self.risk.balance,
                self.config.maker_fee_bps,
                self.config.min_profit_bps
            )
            if best and self.scanner.should_switch(self.config.ws_symbol, best):
                self.orders.cancel_all()
                self._switch_pair(best)

        buy_price, sell_price = self.risk.calculate_skewed_prices(self.tob, self._volatility_bps)
        momentum = self.momentum_signal

        requote_thresh = self.tob.mid_price * self.config.requote_threshold_bps / 10_000
        for oid, order in list(self.orders.open_orders.items()):
            if order.status != OrderStatus.OPEN:
                continue
            target = buy_price if order.side == OrderSide.BUY else sell_price
            if target is None:
                self.orders.cancel_order(oid)
                continue
            drift = abs(order.price - target)
            age_ms = (time.monotonic() - order.placed_at) * 1000
            if drift > requote_thresh and age_ms > self.config.stale_order_ms:
                self.orders.cancel_order(oid)

        place_buy = buy_price is not None
        place_sell = sell_price is not None
        if self.config.momentum_filter_enabled and abs(self.risk.position) < self.risk._dust_qty:
            if momentum == "up":
                place_sell = False
            elif momentum == "down":
                place_buy = False

        if abs(self.risk.position) < self.risk._dust_qty and self._volatility_bps < self.config.min_volatility_bps:
            self._status_reason = "volatility_low"
            return

        if abs(self.risk.position) < self.risk._dust_qty:
            self._status_reason = "active"
        elif self.orders.open_count > 0:
            self._status_reason = "waiting_fill"
        else:
            self._status_reason = "active"

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
                f"avg_entry={self.risk.avg_entry_price:.2f} "
                f"vol={self._volatility_bps:.1f}bps"
            )
            mode_tag = "🔴 LIVE" if self.config.live_mode else "📝 PAPER"
            equity = self.risk.equity(mid)
            upnl = self.risk.unrealized_pnl(mid)
            rpnl = self.risk.pnl
            total_pnl = upnl + rpnl
            pnl_emoji = "📈" if total_pnl >= 0 else "📉"
            self.telegram.send(
                f"<b>WildBot {mode_tag}</b>\n"
                f"{self.config.symbol} | ${mid:,.2f}\n"
                f"{'─' * 20}\n"
                f"{pnl_emoji} <b>Total PnL: ${total_pnl:,.4f}</b>\n"
                f"Equity: ${equity:,.2f}\n"
                f"USDC: ${self.risk.balance:,.2f} | BTC: {self.risk.position:.6f}\n"
                f"{'─' * 20}\n"
                f"Realized: ${rpnl:,.4f}\n"
                f"Unrealized: ${upnl:,.4f}\n"
                f"Return: {self.risk.return_pct(mid):.2f}%\n"
                f"Trades: {self.risk.trade_count} | W/L: {self.risk.wins}/{self.risk.losses} ({self.risk.win_rate:.0f}%)\n"
                f"Fees Paid: ${self.risk.fees_paid:,.4f}\n"
                f"Max DD: {self.risk.max_drawdown_pct_seen:.2f}%\n"
                f"{'─' * 20}\n"
                f"Spread: {spread_bps:.2f} bps | Momentum: {momentum}"
            )

        if self._update_count % 500 == 0:
            stats = {
                "equity": self.risk.equity(self.tob.mid_price),
                "balance": self.risk.balance,
                "pnl": self.risk.pnl,
                "unrealized_pnl": self.risk.unrealized_pnl(self.tob.mid_price),
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
                "mode": "LIVE" if self.config.live_mode else "PAPER",
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
                    all_symbols = [self.config.ws_symbol]
                    if self.scanner:
                        all_symbols = list(self.scanner.pairs.keys())
                    sub_msg = json.dumps({
                        "method": "subscribe",
                        "params": {
                            "channel": "ticker",
                            "symbol": all_symbols,
                            "event_trigger": "bbo",
                        },
                    })
                    await ws.send(sub_msg)
                    delay = self.config.reconnect_delay
                    async for msg in ws:
                        if not self._running:
                            break
                        is_update, symbol = self._parse_depth_update(msg)
                        if is_update:
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
            await asyncio.sleep(1.0)

    def _refresh_live_balance(self):
        if not self._kraken:
            return
        try:
            balances = self._kraken.get_balance()
            usd_bal = 0.0
            for key in ["ZUSD", "USD", "USDT", "ZUSDT", "USDC", "ZUSDC"]:
                if key in balances and balances[key] > 0:
                    usd_bal += balances[key]

            asset_keys_map = {
                "BTC": ["XXBT", "XBT", "BTC"],
                "ETH": ["XETH", "ETH"],
                "SOL": ["SOL"],
                "DOGE": ["DOGE", "XXDG"],
                "XRP": ["XXRP", "XRP"],
                "LINK": ["LINK"],
                "AVAX": ["AVAX"],
                "ADA": ["ADA"],
            }
            symbol = self.config.symbol.split("/")[0] if "/" in self.config.symbol else "BTC"
            asset_keys = asset_keys_map.get(symbol, [symbol])
            asset_bal = 0.0
            for key in asset_keys:
                if key in balances:
                    asset_bal += balances[key]
            asset_bal = asset_bal if asset_bal > self.risk._dust_qty else 0.0

            old_bal = self.risk.balance
            old_pos = self.risk.position

            if abs(old_pos) > self.risk._dust_qty and asset_bal < self.risk._dust_qty:
                mid = self.tob.mid_price if self.tob.mid_price > 0 else 0
                logger.info(f"Position sync: Kraken {symbol}=0 but internal pos={old_pos:.8f} — position was filled on exchange")
                if mid > 0:
                    self.risk.record_fill(
                        OrderSide.SELL if old_pos > 0 else OrderSide.BUY,
                        mid,
                        abs(old_pos),
                    )
                else:
                    self.risk.position = 0.0
                    self.risk.avg_entry_price = 0.0
                    self.risk._position_entry_time = 0.0
                self.risk.balance = usd_bal
                logger.info(f"Balance synced after position close: ${old_bal:,.2f} → ${usd_bal:,.2f}")
            elif self.risk.position == 0 and self.risk.trade_count == 0:
                self.risk.balance = usd_bal
                self.risk.starting_capital = usd_bal
                self.risk.peak_equity = usd_bal

            if abs(usd_bal - old_bal) > 0.01 and self.risk.trade_count == 0:
                logger.info(f"Balance refreshed: ${old_bal:,.2f} → ${usd_bal:,.2f}")
        except Exception as exc:
            logger.warning(f"Balance refresh failed: {exc}")

    def _initialize_live_balance(self):
        if not self.config.live_mode or not self._kraken:
            return
        try:
            connected = False
            for attempt in range(7):
                if attempt > 0:
                    wait = 15 * (2 ** attempt)
                    logger.warning(f"Kraken API validation attempt {attempt}/7 failed, retrying in {wait}s...")
                    time.sleep(wait)
                if self._kraken.validate_connection():
                    connected = True
                    break
            if not connected:
                logger.error("LIVE MODE: Kraken API connection failed after 7 attempts — falling back to paper mode")
                self.config.live_mode = False
                self.orders._live_mode = False
                self.telegram.send_alert(
                    "🚨 <b>WildBot LIVE MODE FAILED</b>\n"
                    "Could not connect to Kraken API.\n"
                    "Falling back to paper trading mode."
                )
                return

            balances = self._kraken.get_balance()
            logger.info(f"Kraken balance response: {balances}")
            usd_bal = 0.0
            for key in ["ZUSD", "USD", "USDT", "ZUSDT", "USDC", "ZUSDC"]:
                if key in balances and balances[key] > 0:
                    usd_bal += balances[key]
                    logger.info(f"Found USD balance in {key}: {balances[key]}")

            try:
                self._kraken.cancel_all()
                logger.info("Cancelled any leftover orders on startup")
            except Exception:
                pass

            asset_keys_map = {
                "BTC": ["XXBT", "XBT", "BTC"],
                "ETH": ["XETH", "ETH"],
                "SOL": ["SOL"],
                "DOGE": ["DOGE", "XXDG"],
                "XRP": ["XXRP", "XRP"],
                "LINK": ["LINK"],
                "AVAX": ["AVAX"],
                "ADA": ["ADA"],
            }
            pair_rest_map = {
                "BTC": "XBTUSDC", "ETH": "ETHUSDC", "SOL": "SOLUSDC",
                "DOGE": "DOGEUSDC", "XRP": "XRPUSDC", "LINK": "LINKUSDC",
                "AVAX": "AVAXUSDC", "ADA": "ADAUSDC",
            }
            for asset_name, keys in asset_keys_map.items():
                asset_bal = sum(balances.get(k, 0.0) for k in keys)
                if asset_bal > 0:
                    pair_cfg = next((p for p in SCAN_PAIRS if p.display_name == asset_name), None)
                    min_qty = pair_cfg.min_qty if pair_cfg else 0.0001
                    if asset_bal >= min_qty:
                        rest_pair = pair_rest_map.get(asset_name, f"{asset_name}USDC")
                        logger.warning(f"Stranded {asset_name} detected: {asset_bal} — selling via {rest_pair}")
                        try:
                            dec = pair_cfg.price_decimals if pair_cfg else 2
                            qty_dec = pair_cfg.qty_decimals if pair_cfg else 8
                            qty_str = f"{asset_bal:.{qty_dec}f}"
                            result = self._kraken.add_order(
                                pair=rest_pair,
                                side="sell",
                                order_type="market",
                                volume=qty_str,
                                oflags="",
                            )
                            logger.info(f"Sold stranded {asset_name}: {result}")
                            time.sleep(2)
                            balances = self._kraken.get_balance()
                            usd_bal = 0.0
                            for key in ["ZUSD", "USD", "USDT", "ZUSDT", "USDC", "ZUSDC"]:
                                if key in balances and balances[key] > 0:
                                    usd_bal += balances[key]
                        except Exception as e:
                            logger.warning(f"Failed to sell stranded {asset_name}: {e}")

            self.risk.balance = usd_bal
            self.risk.position = 0.0
            self.risk.avg_entry_price = 0.0
            self.risk.starting_capital = usd_bal
            self.risk.peak_equity = usd_bal

            logger.info(
                f"LIVE MODE initialized | USD=${usd_bal:,.2f}"
            )
            self.telegram.send_alert(
                f"🟢 <b>WildBot LIVE MODE ACTIVE</b>\n"
                f"Exchange: Kraken\n"
                f"Pair: {self.config.symbol}\n"
                f"USD Balance: ${usd_bal:,.2f}\n"
                f"Order Size: {self.config.order_qty}\n"
                f"Max Position: {self.config.max_position}"
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


class PairTrader:
    def __init__(self, pair_config: PairConfig, base_config: ScalperConfig, kraken_client, telegram: TelegramNotifier, allocated_balance: float):
        self.pair_config = pair_config
        self.config = copy.deepcopy(base_config)
        self.config.symbol = pair_config.ws_symbol
        self.config.ws_symbol = pair_config.ws_symbol
        self.config.rest_pair = pair_config.rest_pair
        self.config.order_qty = pair_config.min_qty
        self.config.max_position = pair_config.min_qty * 100
        self.config.price_decimals = pair_config.price_decimals
        self.config.starting_capital = allocated_balance

        self.tob = TopOfBook()
        self.risk = RiskManager(self.config)
        self.risk.balance = allocated_balance
        self.risk.starting_capital = allocated_balance
        self.risk.peak_equity = allocated_balance

        self._kraken = kraken_client
        self.orders = OrderManager(self.config, kraken_client=kraken_client)
        self.orders._price_decimals = pair_config.price_decimals
        self.orders._qty_decimals = pair_config.qty_decimals
        self.telegram = telegram

        self._update_count: int = 0
        self._ema: float = 0.0
        self._ema_alpha: float = 2.0 / (self.config.ema_window + 1)
        self._trade_history: list[dict] = []
        self._price_history: list[float] = []
        self._volatility_bps: float = 0.0
        self._drawdown_alerted: bool = False
        self._live_fill_poll_interval: float = 5.0
        self._last_live_fill_poll: float = 0.0
        self._status_reason: str = "initializing"
        self._last_force_exit_time: float = 0.0
        self._force_exit_order_time: float = 0.0
        self._allocated_balance: float = allocated_balance
        self._tick_lock = threading.Lock()

    @property
    def allocated_balance(self) -> float:
        return self._allocated_balance

    @allocated_balance.setter
    def allocated_balance(self, value: float):
        self._allocated_balance = value
        self.risk.balance = value
        self.risk.starting_capital = value

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

        self._price_history.append(mid)
        if len(self._price_history) > self.config.volatility_window:
            self._price_history = self._price_history[-self.config.volatility_window:]
        if len(self._price_history) >= 20:
            mean = sum(self._price_history) / len(self._price_history)
            if mean > 0:
                self._volatility_bps = (statistics.stdev(self._price_history) / mean) * 10_000

    def update_tob(self, bid: float, ask: float, bid_qty: float, ask_qty: float):
        self.tob.best_bid = bid
        self.tob.best_ask = ask
        self.tob.bid_qty = bid_qty
        self.tob.ask_qty = ask_qty
        self.tob.timestamp = time.time()

    def handle_tick(self):
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            self._handle_tick_inner()
        finally:
            self._tick_lock.release()

    def _handle_tick_inner(self):
        self._update_count += 1
        self._update_ema()

        if self.config.live_mode:
            now = time.monotonic()
            fill_interval = getattr(self.orders, '_fill_backoff', self._live_fill_poll_interval)
            if now - self._last_live_fill_poll >= fill_interval:
                self._last_live_fill_poll = now
                filled = self.orders.check_fills_live()
                if filled:
                    self.orders._fill_backoff = self._live_fill_poll_interval
            else:
                filled = []
            if self.orders._filled_during_cancel:
                filled.extend(self.orders._filled_during_cancel)
                self.orders._filled_during_cancel.clear()
            if self.orders._needs_balance_sync:
                self.orders._needs_balance_sync = False
        else:
            filled = self.orders.simulate_fill_check(self.tob)

        for order in filled:
            self.risk.record_fill(order.side, order.price, order.quantity)
            if abs(self.risk.position) < self.risk._dust_qty:
                self._force_exit_order_time = 0.0
            mid = self.tob.mid_price
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
            mode_tag = "LIVE" if self.config.live_mode else "PAPER"
            side_emoji = "🟢" if order.side == OrderSide.BUY else "🔴"
            fee_est = order.price * order.quantity * self.config.maker_fee_bps / 10_000
            self.telegram.send_alert(
                f"{side_emoji} <b>{mode_tag} Fill</b> | {self.config.symbol}\n"
                f"{order.side.value.upper()} {order.quantity} @ ${order.price:,.2f}\n"
                f"Fee: ~${fee_est:,.4f}\n\n"
                f"Position: {self.risk.position:.6f}\n"
                f"Avg Entry: ${self.risk.avg_entry_price:,.2f}\n"
                f"Equity: ${self.risk.equity(mid):,.2f}\n"
                f"Realized PnL: ${self.risk.pnl:,.4f}\n"
                f"Total Fees: ${self.risk.fees_paid:,.4f}\n"
                f"W/L: {self.risk.wins}/{self.risk.losses}"
            )

        self.risk.update_drawdown(self.tob.mid_price)

        if self.risk.is_stopped:
            self._status_reason = "drawdown_stopped"
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

        if not self.risk.is_spread_acceptable(self.tob):
            self._status_reason = "spread_wide"
            if self.orders.open_count > 0:
                self.orders.cancel_all()
            return

        now_mono = time.monotonic()
        reprice_after = 30.0
        if abs(self.risk.position) > self.risk._dust_qty and self.risk._position_entry_time > 0:
            hold_time = now_mono - self.risk._position_entry_time
            dynamic_hold = self.risk.dynamic_hold_seconds(self.risk.dynamic_exit_bps(self._volatility_bps))
            if hold_time > dynamic_hold:
                exit_side = OrderSide.SELL if self.risk.position > 0 else OrderSide.BUY
                has_exit_order = any(o.side == exit_side for o in self.orders.open_orders.values())
                if has_exit_order and (now_mono - self._force_exit_order_time) < reprice_after:
                    self._status_reason = "time_stop"
                    return
                self._last_force_exit_time = now_mono
                self._force_exit_order_time = now_mono
                self.orders.cancel_all()
                exit_price = None
                dec = self.config.price_decimals
                if self.risk.position > 0:
                    exit_price = round(self.tob.best_bid * 0.999, dec)
                    self.orders.create_order(OrderSide.SELL, exit_price, abs(self.risk.position), force_taker=True)
                elif self.risk.position < 0:
                    exit_price = round(self.tob.best_ask * 1.001, dec)
                    self.orders.create_order(OrderSide.BUY, exit_price, abs(self.risk.position), force_taker=True)
                if exit_price is not None:
                    self._status_reason = "time_stop"
                    logger.warning(f"TIME STOP [{self.config.symbol}]: Position held {hold_time:.0f}s > max {dynamic_hold:.0f}s — force exit at {exit_price}")
                    self.telegram.send_alert(f"⏰ <b>TIME STOP</b> | {self.config.symbol}\nForce exit after {hold_time:.0f}s\nPrice: ${exit_price:,.2f}")
                self._status_reason = "time_stop"
                return

        if abs(self.risk.position) > self.risk._dust_qty and self.risk.avg_entry_price > 0:
            mid = self.tob.mid_price
            if self.risk.position > 0:
                adverse_bps = (self.risk.avg_entry_price - mid) / self.risk.avg_entry_price * 10_000
            else:
                adverse_bps = (mid - self.risk.avg_entry_price) / self.risk.avg_entry_price * 10_000
            if adverse_bps > self.config.stop_loss_bps:
                exit_side = OrderSide.SELL if self.risk.position > 0 else OrderSide.BUY
                has_exit_order = any(o.side == exit_side for o in self.orders.open_orders.values())
                if has_exit_order and (now_mono - self._force_exit_order_time) < reprice_after:
                    self._status_reason = "stop_loss"
                    return
                self._last_force_exit_time = now_mono
                self._force_exit_order_time = now_mono
                self.orders.cancel_all()
                exit_price = None
                dec = self.config.price_decimals
                if self.risk.position > 0:
                    exit_price = round(self.tob.best_bid * 0.999, dec)
                    self.orders.create_order(OrderSide.SELL, exit_price, abs(self.risk.position), force_taker=True)
                elif self.risk.position < 0:
                    exit_price = round(self.tob.best_ask * 1.001, dec)
                    self.orders.create_order(OrderSide.BUY, exit_price, abs(self.risk.position), force_taker=True)
                if exit_price is not None:
                    self._status_reason = "stop_loss"
                    logger.warning(f"STOP LOSS [{self.config.symbol}]: Adverse move {adverse_bps:.1f}bps > max {self.config.stop_loss_bps:.1f}bps — force exit at {exit_price}")
                    self.telegram.send_alert(f"🛑 <b>STOP LOSS</b> | {self.config.symbol}\nAdverse move {adverse_bps:.1f}bps\nForce exit at ${exit_price:,.2f}")
                self._status_reason = "stop_loss"
                return

        buy_price, sell_price = self.risk.calculate_skewed_prices(self.tob, self._volatility_bps)
        momentum = self.momentum_signal

        requote_thresh = self.tob.mid_price * self.config.requote_threshold_bps / 10_000
        for oid, order in list(self.orders.open_orders.items()):
            if order.status != OrderStatus.OPEN:
                continue
            target = buy_price if order.side == OrderSide.BUY else sell_price
            if target is None:
                self.orders.cancel_order(oid)
                continue
            drift = abs(order.price - target)
            age_ms = (time.monotonic() - order.placed_at) * 1000
            if drift > requote_thresh and age_ms > self.config.stale_order_ms:
                self.orders.cancel_order(oid)

        place_buy = buy_price is not None
        place_sell = sell_price is not None
        if self.config.momentum_filter_enabled and abs(self.risk.position) < self.risk._dust_qty:
            if momentum == "up":
                place_sell = False
            elif momentum == "down":
                place_buy = False

        if abs(self.risk.position) < self.risk._dust_qty and self._volatility_bps < self.config.min_volatility_bps:
            self._status_reason = "volatility_low"
            return

        if abs(self.risk.position) < self.risk._dust_qty:
            self._status_reason = "active"
        elif self.orders.open_count > 0:
            self._status_reason = "waiting_fill"
        else:
            self._status_reason = "active"

        if (
            place_buy
            and not self.orders.has_side(OrderSide.BUY)
            and self.risk.can_place_buy(buy_price)
            and self.orders.open_count < self.config.max_open_orders
        ):
            self.orders.create_order(OrderSide.BUY, buy_price, self.config.order_qty)

        if (
            place_sell
            and not self.orders.has_side(OrderSide.SELL)
            and self.risk.can_place_sell()
            and self.orders.open_count < self.config.max_open_orders
        ):
            self.orders.create_order(OrderSide.SELL, sell_price, self.config.order_qty)

        if self._update_count % 50 == 0:
            mid = self.tob.mid_price
            spread_bps = (self.tob.spread / mid) * 10_000 if mid else 0
            logger.info(
                f"[{self.config.symbol}] TOB bid={self.tob.best_bid} ask={self.tob.best_ask} "
                f"spread={spread_bps:.2f}bps | "
                f"equity=${self.risk.equity(mid):,.2f} "
                f"bal=${self.risk.balance:.2f} pos={self.risk.position:.6f} "
                f"unrealized_pnl=${self.risk.unrealized_pnl(mid):,.4f} "
                f"realized_pnl={self.risk.pnl:.4f} "
                f"return={self.risk.return_pct(mid):.2f}% "
                f"win_rate={self.risk.win_rate:.1f}% "
                f"max_dd={self.risk.max_drawdown_pct_seen:.2f}% "
                f"orders={self.orders.open_count} "
                f"vol={self._volatility_bps:.1f}bps"
            )

    def get_status(self) -> dict:
        mid = self.tob.mid_price
        return {
            "symbol": self.config.symbol,
            "balance": self.risk.balance,
            "equity": self.risk.equity(mid),
            "position": self.risk.position,
            "pnl": self.risk.pnl,
            "unrealized_pnl": self.risk.unrealized_pnl(mid),
            "trade_count": self.risk.trade_count,
            "status_reason": self._status_reason,
            "avg_entry_price": self.risk.avg_entry_price,
            "volatility_bps": self._volatility_bps,
            "open_orders": self.orders.open_count,
            "wins": self.risk.wins,
            "losses": self.risk.losses,
            "fees_paid": self.risk.fees_paid,
            "tob": {
                "bid": self.tob.best_bid,
                "ask": self.tob.best_ask,
                "spread": self.tob.spread,
                "mid": mid,
            },
        }

    def cancel_all_orders(self) -> int:
        return self.orders.cancel_all()

    def is_flat(self) -> bool:
        return abs(self.risk.position) < self.risk._dust_qty


class MultiPairOrchestrator:
    def __init__(self, base_config: ScalperConfig, kraken_client=None, scanner: Optional[PairScanner] = None, max_active_pairs: int = 3):
        self.base_config = base_config
        self._kraken = kraken_client
        self.scanner = scanner
        self.telegram = TelegramNotifier()
        self.max_active_pairs = max_active_pairs
        self.active_traders: dict[str, PairTrader] = {}
        self.total_balance: float = base_config.starting_capital
        self._initial_capital: float = base_config.starting_capital
        self._running: bool = False
        self._update_count: int = 0
        self._trade_history: list[dict] = []
        self._live_fill_poll_interval: float = 3.0
        self._balance_refresh_interval: float = 300.0
        self._last_balance_refresh: float = 0.0
        self._traders_lock = threading.Lock()

    def _allocate_balance(self):
        with self._traders_lock:
            usable = self.total_balance * 0.98
            per_pair = usable / self.max_active_pairs
            for trader in self.active_traders.values():
                trader.risk.balance = per_pair
                trader.risk.starting_capital = per_pair
                trader.risk.peak_equity = max(trader.risk.peak_equity, per_pair)
                trader._allocated_balance = per_pair

    def _select_pairs(self) -> list[PairConfig]:
        if not self.scanner:
            return []
        per_pair_balance = (self.total_balance * 0.98) / self.max_active_pairs
        candidates = []
        for ws_sym, pair_cfg in self.scanner.pairs.items():
            tob = self.scanner.tobs[ws_sym]
            vol = self.scanner._volatilities.get(ws_sym, 0.0)
            if tob.mid_price <= 0:
                continue
            if len(self.scanner._price_histories.get(ws_sym, [])) < 20:
                continue
            min_notional = pair_cfg.min_qty * tob.mid_price
            if min_notional > per_pair_balance * 0.95:
                continue
            candidates.append((ws_sym, pair_cfg, vol))
        candidates.sort(key=lambda x: x[2], reverse=True)
        return [c[1] for c in candidates[:self.max_active_pairs]]

    def _activate_pair(self, pair_config: PairConfig):
        with self._traders_lock:
            if pair_config.ws_symbol in self.active_traders:
                return
        per_pair_balance = (self.total_balance * 0.98) / self.max_active_pairs
        trader = PairTrader(
            pair_config=pair_config,
            base_config=self.base_config,
            kraken_client=self._kraken,
            telegram=self.telegram,
            allocated_balance=per_pair_balance,
        )
        if self.scanner and pair_config.ws_symbol in self.scanner.tobs:
            scanner_tob = self.scanner.tobs[pair_config.ws_symbol]
            trader.update_tob(scanner_tob.best_bid, scanner_tob.best_ask, scanner_tob.bid_qty, scanner_tob.ask_qty)
            scanner_history = self.scanner._price_histories.get(pair_config.ws_symbol, [])
            if scanner_history:
                trader._price_history = list(scanner_history)
                trader._volatility_bps = self.scanner._volatilities.get(pair_config.ws_symbol, 0.0)
                if scanner_tob.mid_price > 0:
                    trader._ema = scanner_tob.mid_price
        with self._traders_lock:
            self.active_traders[pair_config.ws_symbol] = trader
        logger.info(f"ACTIVATED pair {pair_config.ws_symbol} with ${per_pair_balance:.2f} allocated")

    def _deactivate_pair(self, ws_symbol: str):
        with self._traders_lock:
            trader = self.active_traders.get(ws_symbol)
        if not trader:
            return
        if not trader.is_flat():
            logger.warning(f"Cannot deactivate {ws_symbol}: position={trader.risk.position}")
            return
        trader.cancel_all_orders()
        self._trade_history.extend(trader._trade_history)
        if len(self._trade_history) > 200:
            self._trade_history = self._trade_history[-200:]
        with self._traders_lock:
            del self.active_traders[ws_symbol]
        logger.info(f"DEACTIVATED pair {ws_symbol}")

    def _parse_depth_update(self, raw: str) -> tuple[bool, float, float, float, float, str]:
        try:
            data = json.loads(raw)
            channel = data.get("channel")
            if channel == "ticker":
                tick_data = data.get("data", [{}])[0]
                symbol = tick_data.get("symbol", "")
                bid = tick_data.get("bid")
                ask = tick_data.get("ask")
                bid_qty = tick_data.get("bid_qty")
                ask_qty = tick_data.get("ask_qty")
                if bid is not None and ask is not None:
                    bid_f = float(bid)
                    ask_f = float(ask)
                    bid_qty_f = float(bid_qty) if bid_qty else 0.0
                    ask_qty_f = float(ask_qty) if ask_qty else 0.0

                    if self.scanner:
                        self.scanner.update(symbol, bid_f, ask_f, bid_qty_f, ask_qty_f)

                    return True, bid_f, ask_f, bid_qty_f, ask_qty_f, symbol
            elif channel == "heartbeat":
                return False, 0.0, 0.0, 0.0, 0.0, ""
            elif data.get("method") == "subscribe" and data.get("success"):
                logger.info(f"Subscribed to {data.get('result', {}).get('channel', 'unknown')}")
                return False, 0.0, 0.0, 0.0, 0.0, ""
        except (json.JSONDecodeError, KeyError, IndexError, ValueError) as exc:
            logger.warning(f"Parse error: {exc}")
        return False, 0.0, 0.0, 0.0, 0.0, ""

    def _handle_tick(self, symbol: str, bid: float, ask: float, bid_qty: float, ask_qty: float):
        with self._traders_lock:
            trader = self.active_traders.get(symbol)
        if trader:
            trader.update_tob(bid, ask, bid_qty, ask_qty)
            trader.handle_tick()

    def _maybe_swap_pairs(self):
        if not self.scanner:
            return

        with self._traders_lock:
            active_syms = set(self.active_traders.keys())
            current_count = len(self.active_traders)
        per_pair_balance = (self.total_balance * 0.98) / self.max_active_pairs

        if current_count < self.max_active_pairs:
            candidates = self._select_pairs()
            for pc in candidates:
                if pc.ws_symbol not in active_syms and len(self.active_traders) < self.max_active_pairs:
                    self._activate_pair(pc)
            self._allocate_balance()
            return

        flat_traders = []
        for ws_sym, trader in self.active_traders.items():
            if trader.is_flat():
                vol = self.scanner._volatilities.get(ws_sym, 0.0)
                flat_traders.append((ws_sym, vol))

        if not flat_traders:
            return

        flat_traders.sort(key=lambda x: x[1])
        worst_sym, worst_vol = flat_traders[0]

        best_inactive_sym = None
        best_inactive_vol = 0.0
        best_inactive_cfg = None
        for ws_sym, pair_cfg in self.scanner.pairs.items():
            if ws_sym in active_syms:
                continue
            tob = self.scanner.tobs[ws_sym]
            if tob.mid_price <= 0:
                continue
            if len(self.scanner._price_histories.get(ws_sym, [])) < 20:
                continue
            min_notional = pair_cfg.min_qty * tob.mid_price
            if min_notional > per_pair_balance * 0.95:
                continue
            vol = self.scanner._volatilities.get(ws_sym, 0.0)
            if vol > best_inactive_vol:
                best_inactive_vol = vol
                best_inactive_sym = ws_sym
                best_inactive_cfg = pair_cfg

        if best_inactive_cfg and best_inactive_vol > worst_vol * 1.5:
            logger.info(
                f"SWAP: deactivating {worst_sym} (vol={worst_vol:.1f}bps) "
                f"for {best_inactive_sym} (vol={best_inactive_vol:.1f}bps)"
            )
            self._deactivate_pair(worst_sym)
            self._activate_pair(best_inactive_cfg)
            self._allocate_balance()
            self.telegram.send_alert(
                f"🔄 <b>Pair Swap</b>\n"
                f"Out: {worst_sym} ({worst_vol:.1f}bps)\n"
                f"In: {best_inactive_sym} ({best_inactive_vol:.1f}bps)"
            )

    async def _ws_loop(self):
        delay = self.base_config.reconnect_delay
        while self._running:
            try:
                logger.info(f"MultiPair: Connecting to {self.base_config.ws_url} ...")
                async with websockets.connect(
                    self.base_config.ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    logger.info("MultiPair: WebSocket connected — subscribing to all tickers")
                    self.telegram.send_alert(
                        f"✅ <b>WildBot MultiPair Reconnected</b>\n"
                        f"WebSocket connected, trading up to {self.max_active_pairs} pairs"
                    )
                    all_symbols = list(self.scanner.pairs.keys()) if self.scanner else [self.base_config.ws_symbol]
                    sub_msg = json.dumps({
                        "method": "subscribe",
                        "params": {
                            "channel": "ticker",
                            "symbol": all_symbols,
                            "event_trigger": "bbo",
                        },
                    })
                    await ws.send(sub_msg)
                    delay = self.base_config.reconnect_delay
                    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
                    async for msg in ws:
                        if not self._running:
                            break
                        self._update_count += 1
                        is_update, bid, ask, bid_qty, ask_qty, symbol = self._parse_depth_update(msg)
                        if is_update and symbol:
                            _executor.submit(self._handle_tick, symbol, bid, ask, bid_qty, ask_qty)
            except websockets.exceptions.ConnectionClosed as exc:
                logger.warning(f"MultiPair: Connection closed: {exc}")
                self.telegram.send_alert(
                    f"⚠️ <b>WildBot MultiPair Disconnected</b>\n"
                    f"WebSocket connection closed: {exc}"
                )
            except Exception as exc:
                logger.error(f"MultiPair: WebSocket error: {exc}")
                self.telegram.send_alert(
                    f"⚠️ <b>WildBot MultiPair Disconnected</b>\n"
                    f"WebSocket error: {exc}"
                )

            if self._running:
                for trader in self.active_traders.values():
                    trader.cancel_all_orders()
                logger.info(f"MultiPair: Reconnecting in {delay:.1f}s ...")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.base_config.max_reconnect_delay)

    async def _pair_management_loop(self):
        await asyncio.sleep(30)
        self._maybe_swap_pairs()
        while self._running:
            await asyncio.sleep(120)
            self._maybe_swap_pairs()

            if self.base_config.live_mode and self._kraken:
                now = time.monotonic()
                if now - self._last_balance_refresh >= self._balance_refresh_interval:
                    self._last_balance_refresh = now
                    self._refresh_live_balance()

    def _initialize_live_balance(self):
        if not self.base_config.live_mode or not self._kraken:
            return
        try:
            connected = False
            for attempt in range(7):
                if attempt > 0:
                    wait = 15 * (2 ** attempt)
                    logger.warning(f"Kraken API validation attempt {attempt}/7 failed, retrying in {wait}s...")
                    time.sleep(wait)
                if self._kraken.validate_connection():
                    connected = True
                    break
            if not connected:
                logger.error("LIVE MODE: Kraken API connection failed after 7 attempts — falling back to paper mode")
                self.base_config.live_mode = False
                self.telegram.send_alert(
                    "🚨 <b>WildBot MultiPair LIVE MODE FAILED</b>\n"
                    "Could not connect to Kraken API.\n"
                    "Falling back to paper trading mode."
                )
                return

            balances = self._kraken.get_balance()
            logger.info(f"Kraken balance response: {balances}")
            usd_bal = 0.0
            for key in ["ZUSD", "USD", "USDT", "ZUSDT", "USDC", "ZUSDC"]:
                if key in balances and balances[key] > 0:
                    usd_bal += balances[key]
                    logger.info(f"Found USD balance in {key}: {balances[key]}")

            try:
                self._kraken.cancel_all()
                logger.info("Cancelled any leftover orders on startup")
            except Exception:
                pass

            asset_keys_map = {
                "BTC": ["XXBT", "XBT", "BTC"],
                "ETH": ["XETH", "ETH"],
                "SOL": ["SOL"],
                "DOGE": ["DOGE", "XXDG"],
                "XRP": ["XXRP", "XRP"],
                "LINK": ["LINK"],
                "AVAX": ["AVAX"],
                "ADA": ["ADA"],
            }
            pair_rest_map = {
                "BTC": "XBTUSDC", "ETH": "ETHUSDC", "SOL": "SOLUSDC",
                "DOGE": "DOGEUSDC", "XRP": "XRPUSDC", "LINK": "LINKUSDC",
                "AVAX": "AVAXUSDC", "ADA": "ADAUSDC",
            }
            for asset_name, keys in asset_keys_map.items():
                asset_bal = sum(balances.get(k, 0.0) for k in keys)
                if asset_bal > 0:
                    pair_cfg = next((p for p in SCAN_PAIRS if p.display_name == asset_name), None)
                    min_qty = pair_cfg.min_qty if pair_cfg else 0.0001
                    if asset_bal >= min_qty:
                        rest_pair = pair_rest_map.get(asset_name, f"{asset_name}USDC")
                        logger.warning(f"Stranded {asset_name} detected: {asset_bal} — selling via {rest_pair}")
                        try:
                            qty_dec = pair_cfg.qty_decimals if pair_cfg else 8
                            qty_str = f"{asset_bal:.{qty_dec}f}"
                            self._kraken.add_order(
                                pair=rest_pair,
                                side="sell",
                                order_type="market",
                                volume=qty_str,
                                oflags="",
                            )
                            logger.info(f"Sold stranded {asset_name}")
                            time.sleep(2)
                            balances = self._kraken.get_balance()
                            usd_bal = 0.0
                            for key in ["ZUSD", "USD", "USDT", "ZUSDT", "USDC", "ZUSDC"]:
                                if key in balances and balances[key] > 0:
                                    usd_bal += balances[key]
                        except Exception as e:
                            logger.warning(f"Failed to sell stranded {asset_name}: {e}")

            self.total_balance = usd_bal
            self._initial_capital = usd_bal
            logger.info(f"LIVE MODE MultiPair initialized | Total USD=${usd_bal:,.2f}")
            self.telegram.send_alert(
                f"🟢 <b>WildBot MultiPair LIVE MODE ACTIVE</b>\n"
                f"Exchange: Kraken\n"
                f"USD Balance: ${usd_bal:,.2f}\n"
                f"Max Active Pairs: {self.max_active_pairs}\n"
                f"Per-Pair Allocation: ${(usd_bal * 0.98 / self.max_active_pairs):,.2f}"
            )
        except Exception as exc:
            logger.error(f"LIVE MODE MultiPair initialization failed: {exc} — falling back to paper mode")
            self.base_config.live_mode = False
            self.telegram.send_alert(
                f"🚨 <b>WildBot MultiPair LIVE MODE FAILED</b>\n"
                f"Error: {exc}\n"
                f"Falling back to paper trading mode."
            )

    def _refresh_live_balance(self):
        if not self._kraken:
            return
        try:
            balances = self._kraken.get_balance()
            usd_bal = 0.0
            for key in ["ZUSD", "USD", "USDT", "ZUSDT", "USDC", "ZUSDC"]:
                if key in balances and balances[key] > 0:
                    usd_bal += balances[key]
            old_total = self.total_balance
            self.total_balance = usd_bal
            self._allocate_balance()
            if abs(usd_bal - old_total) > 0.01:
                logger.info(f"MultiPair balance refreshed: ${old_total:,.2f} → ${usd_bal:,.2f}")
        except Exception as exc:
            logger.warning(f"MultiPair balance refresh failed: {exc}")

    async def run(self):
        self._running = True
        mode_str = "LIVE" if self.base_config.live_mode else "PAPER"
        logger.info(
            f"WildBot MultiPair Orchestrator starting [{mode_str}] | "
            f"capital=${self.total_balance:.2f} "
            f"max_pairs={self.max_active_pairs}"
        )

        self._initialize_live_balance()

        initial_pairs = self._select_pairs()
        if initial_pairs:
            for pc in initial_pairs:
                self._activate_pair(pc)
            self._allocate_balance()
            logger.info(f"Initial pairs activated: {[p.ws_symbol for p in initial_pairs]}")
        else:
            logger.info("No pairs meet criteria yet — will activate once scanner has data")

        try:
            await asyncio.gather(
                self._ws_loop(),
                self._pair_management_loop(),
            )
        except asyncio.CancelledError:
            logger.info("MultiPair orchestrator cancelled")
        finally:
            self.stop()

    def stop(self):
        self._running = False
        with self._traders_lock:
            traders_to_stop = list(self.active_traders.items())
        for ws_sym, trader in traders_to_stop:
            trader.cancel_all_orders()
            logger.info(f"Stopped trader for {ws_sym}")
        logger.info("MultiPair orchestrator stopped")

    def get_portfolio_status(self) -> dict:
        with self._traders_lock:
            traders_snapshot = list(self.active_traders.values())
        
        total_balance = 0.0
        total_equity = 0.0
        total_pnl = 0.0
        total_trades = 0
        total_wins = 0
        total_losses = 0
        total_fees = 0.0
        active_pairs = []

        for trader in traders_snapshot:
            status = trader.get_status()
            total_balance += status["balance"]
            total_equity += status["equity"]
            total_pnl += status["pnl"] + status["unrealized_pnl"]
            total_trades += status["trade_count"]
            total_wins += status["wins"]
            total_losses += status["losses"]
            total_fees += status["fees_paid"]
            active_pairs.append(status)

        unallocated = self.total_balance - total_balance
        if unallocated > 0:
            total_balance += unallocated
            total_equity += unallocated

        return {
            "total_balance": total_balance,
            "total_equity": total_equity,
            "total_pnl": total_pnl,
            "total_trades": total_trades,
            "total_wins": total_wins,
            "total_losses": total_losses,
            "total_fees": total_fees,
            "active_pairs": active_pairs,
            "scanner_data": self.scanner.get_scanner_data() if self.scanner else [],
            "max_active_pairs": self.max_active_pairs,
        }
