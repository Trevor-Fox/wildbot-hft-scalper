import os
import time
import hashlib
import hmac
import base64
import urllib.parse
import logging
import threading

import requests

logger = logging.getLogger("KrakenClient")


class KrakenAPIError(Exception):
    pass


class KrakenClient:
    BASE_URL = "https://api.kraken.com"

    def __init__(self, api_key: str = None, api_secret: str = None):
        self.api_key = api_key or os.environ.get("KRAKEN_API_KEY", "")
        self.api_secret = api_secret or os.environ.get("KRAKEN_API_SECRET", "")
        self._last_request_time: float = 0.0
        self._session = requests.Session()
        self._lock = threading.RLock()
        self._nonce_counter: int = 0
        self._last_nonce: int = 0
        self._nonce_lock = threading.Lock()
        if not self.api_key or not self.api_secret:
            logger.warning("Kraken API key or secret not configured")

    def _sign(self, urlpath: str, data: dict) -> dict:
        postdata = urllib.parse.urlencode(data)
        encoded = (str(data["nonce"]) + postdata).encode("utf-8")
        message = urlpath.encode("utf-8") + hashlib.sha256(encoded).digest()
        signature = hmac.new(
            base64.b64decode(self.api_secret),
            message,
            hashlib.sha512,
        )
        sigdigest = base64.b64encode(signature.digest()).decode("utf-8")
        return {
            "API-Key": self.api_key,
            "API-Sign": sigdigest,
        }

    def _private_request(self, endpoint: str, data: dict = None, _retry: int = 0) -> dict:
        with self._lock:
            if data is None:
                data = {}

            now = time.time()
            elapsed = now - self._last_request_time
            if elapsed < 1.0:
                time.sleep(1.0 - elapsed)

            with self._nonce_lock:
                nonce = int(time.time() * 1_000_000_000)
                if nonce <= self._last_nonce:
                    nonce = self._last_nonce + 1
                self._last_nonce = nonce
                data["nonce"] = nonce
            urlpath = f"/0/private/{endpoint}"
            url = f"{self.BASE_URL}{urlpath}"
            headers = self._sign(urlpath, data)

            logger.debug("POST %s data=%s", urlpath, data)

            try:
                resp = self._session.post(url, data=data, headers=headers, timeout=15)
                self._last_request_time = time.time()
                resp.raise_for_status()
                result = resp.json()
            except requests.RequestException as exc:
                logger.warning("Request failed for %s: %s", endpoint, exc)
                raise KrakenAPIError(f"Request failed: {exc}") from exc

            logger.debug("Response %s: %s", endpoint, result)

            errors = result.get("error", [])
            if errors:
                error_msg = "; ".join(errors)
                if "Invalid nonce" in error_msg and _retry < 2:
                    logger.info("Nonce error on %s, retrying (%d)...", endpoint, _retry + 1)
                    time.sleep(0.5)
                    self._last_request_time = 0.0
                    return self._private_request(endpoint, data, _retry=_retry + 1)
                logger.warning("Kraken API error on %s: %s", endpoint, error_msg)
                raise KrakenAPIError(error_msg)

            return result.get("result", {})

    def get_balance(self) -> dict:
        try:
            raw = self._private_request("Balance")
            return {k: float(v) for k, v in raw.items()}
        except KrakenAPIError:
            raise
        except Exception as exc:
            logger.warning("get_balance failed: %s", exc)
            raise KrakenAPIError(f"get_balance failed: {exc}") from exc

    def get_usd_balance(self) -> float:
        balances = self.get_balance()
        return balances.get("ZUSD", 0.0)

    def get_btc_balance(self) -> float:
        balances = self.get_balance()
        return balances.get("XXBT", 0.0)

    def add_order(self, pair: str, side: str, order_type: str, volume: str, price: str = None, oflags: str = "post") -> str:
        data = {
            "pair": pair,
            "type": side,
            "ordertype": order_type,
            "volume": volume,
        }
        if price is not None:
            data["price"] = price
        if oflags:
            data["oflags"] = oflags

        try:
            result = self._private_request("AddOrder", data)
            txids = result.get("txid", [])
            if txids:
                txid = txids[0]
                logger.debug("Order placed: %s %s %s %s @ %s -> %s", pair, side, order_type, volume, price, txid)
                return txid
            raise KrakenAPIError("No txid returned from AddOrder")
        except KrakenAPIError:
            raise
        except Exception as exc:
            logger.warning("add_order failed: %s", exc)
            raise KrakenAPIError(f"add_order failed: {exc}") from exc

    def cancel_order(self, txid: str) -> bool:
        try:
            self._private_request("CancelOrder", {"txid": txid})
            logger.debug("Order cancelled: %s", txid)
            return True
        except KrakenAPIError:
            raise
        except Exception as exc:
            logger.warning("cancel_order failed: %s", exc)
            raise KrakenAPIError(f"cancel_order failed: {exc}") from exc

    def cancel_all(self) -> int:
        try:
            result = self._private_request("CancelAll")
            count = result.get("count", 0)
            logger.debug("Cancelled %d orders", count)
            return count
        except KrakenAPIError:
            raise
        except Exception as exc:
            logger.warning("cancel_all failed: %s", exc)
            raise KrakenAPIError(f"cancel_all failed: {exc}") from exc

    def get_open_orders(self) -> dict:
        try:
            result = self._private_request("OpenOrders")
            return result.get("open", {})
        except KrakenAPIError:
            raise
        except Exception as exc:
            logger.warning("get_open_orders failed: %s", exc)
            raise KrakenAPIError(f"get_open_orders failed: {exc}") from exc

    def query_orders(self, txids: list[str]) -> dict:
        try:
            data = {"txid": ",".join(txids)}
            return self._private_request("QueryOrders", data)
        except KrakenAPIError:
            raise
        except Exception as exc:
            logger.warning("query_orders failed: %s", exc)
            raise KrakenAPIError(f"query_orders failed: {exc}") from exc

    def validate_connection(self) -> bool:
        try:
            self.get_balance()
            logger.debug("Connection validated successfully")
            return True
        except Exception as exc:
            logger.warning("Connection validation failed: %s", exc)
            return False
