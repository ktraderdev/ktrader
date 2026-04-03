"""
Kalshi Trading Bot — API Client
RSA-PSS authenticated client for Kalshi's REST API.
"""
import time
import base64
import logging
from typing import Optional
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

from config import config

logger = logging.getLogger(__name__)


class KalshiClient:
    """Authenticated Kalshi API client."""

    def __init__(self):
        self.cfg = config.kalshi
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self._private_key = None
        self._load_private_key()

    def _load_private_key(self):
        """Load RSA private key from PEM file."""
        key_path = Path(self.cfg.private_key_path)
        if not key_path.exists():
            logger.warning(f"Private key not found at {key_path} — API calls will fail")
            return
        with open(key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(
                f.read(), password=None, backend=default_backend()
            )
        logger.info("Kalshi RSA private key loaded")

    def _sign_request(self, method: str, path: str) -> dict:
        """Generate authentication headers for a request."""
        if not self._private_key:
            raise RuntimeError("No private key loaded — cannot sign requests")

        timestamp_ms = str(int(time.time() * 1000))
        clean_path = path.split("?")[0]
        message = f"{timestamp_ms}{method.upper()}{clean_path}"

        signature = self._private_key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY": self.cfg.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        }

    def _request(self, method: str, path: str, params: dict = None,
                 json_body: dict = None, authenticated: bool = True) -> dict:
        """Make an API request with optional authentication."""
        url = f"{self.cfg.base_url}{path}"

        headers = {}
        if authenticated:
            headers = self._sign_request(method, f"/trade-api/v2{path}")

        for attempt in range(3):
            try:
                resp = self.session.request(
                    method=method, url=url, params=params,
                    json=json_body, headers=headers, timeout=15,
                )
                if resp.status_code == 429:
                    wait = 2 ** attempt + 1
                    logger.warning(f"Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.HTTPError as e:
                logger.error(f"Kalshi API error {resp.status_code}: {resp.text}")
                raise
            except requests.exceptions.RequestException as e:
                logger.error(f"Kalshi API request failed: {e}")
                raise
        raise requests.exceptions.HTTPError(f"Rate limited after 3 retries")

    # ── Market Data (public — no auth needed) ───────────────────

    def get_markets(self, status: str = "open", limit: int = 200,
                    cursor: str = None, series_ticker: str = None,
                    event_ticker: str = None, min_close_ts: int = None,
                    max_close_ts: int = None) -> dict:
        """List markets with optional filters."""
        params = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if min_close_ts:
            params["min_close_ts"] = min_close_ts
        if max_close_ts:
            params["max_close_ts"] = max_close_ts
        return self._request("GET", "/markets", params=params, authenticated=False)

    def get_market(self, ticker: str) -> dict:
        """Get a single market by ticker."""
        return self._request("GET", f"/markets/{ticker}", authenticated=False)

    def get_event(self, event_ticker: str) -> dict:
        """Get event details."""
        return self._request("GET", f"/events/{event_ticker}", authenticated=False)

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        """Get orderbook for a market."""
        return self._request(
            "GET", f"/markets/{ticker}/orderbook",
            params={"depth": depth}, authenticated=False
        )

    def get_trades(self, ticker: str = None, limit: int = 100,
                   min_ts: int = None, max_ts: int = None) -> dict:
        """Get recent trades."""
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if min_ts:
            params["min_ts"] = min_ts
        if max_ts:
            params["max_ts"] = max_ts
        return self._request("GET", "/markets/trades", params=params, authenticated=False)

    def get_all_open_markets(self) -> list:
        """Paginate through ALL open markets."""
        all_markets = []
        cursor = None
        while True:
            data = self.get_markets(status="open", limit=1000, cursor=cursor)
            markets = data.get("markets", [])
            if not markets:
                break
            all_markets.extend(markets)
            cursor = data.get("cursor")
            if not cursor:
                break
            logger.debug(f"Fetched {len(all_markets)} markets so far...")
        logger.info(f"Total open markets: {len(all_markets)}")
        return all_markets

    def get_events(self, status: str = "open", limit: int = 100,
                   cursor: str = None,
                   with_nested_markets: bool = False) -> dict:
        """List events with optional filters."""
        params = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        if with_nested_markets:
            params["with_nested_markets"] = "true"
        return self._request("GET", "/events", params=params, authenticated=False)

    def get_all_open_events(self, with_nested_markets: bool = True) -> list:
        """Paginate through ALL open events (with their markets)."""
        all_events = []
        cursor = None
        while True:
            data = self.get_events(
                status="open", limit=100, cursor=cursor,
                with_nested_markets=with_nested_markets,
            )
            events = data.get("events", [])
            if not events:
                break
            all_events.extend(events)
            cursor = data.get("cursor")
            if not cursor:
                break
            logger.debug(f"Fetched {len(all_events)} events so far...")
        logger.info(f"Total open events: {len(all_events)}")
        return all_events

    # ── Portfolio (auth required) ───────────────────────────────

    def get_balance(self) -> dict:
        """Get account balance (in cents)."""
        return self._request("GET", "/portfolio/balance")

    def get_positions(self) -> dict:
        """Get current open positions."""
        return self._request("GET", "/portfolio/positions")

    def get_portfolio_settlements(self, limit: int = 100) -> dict:
        """Get settled positions."""
        return self._request("GET", "/portfolio/settlements", params={"limit": limit})

    # ── Order Management (auth required) ────────────────────────

    def place_order(self, ticker: str, side: str, action: str = "buy",
                    count: int = 1, order_type: str = "market",
                    yes_price: int = None, no_price: int = None,
                    expiration_ts: int = None) -> dict:
        """Place an order.

        Args:
            ticker: Market ticker (e.g., "KXHIGHNY-26MAR30-T58")
            side: "yes" or "no"
            action: "buy" or "sell"
            count: Number of contracts (fixed-point string like "10.00")
            order_type: "market" or "limit"
            yes_price: Limit price in dollars (e.g., "0.55")
            no_price: Limit price in dollars (e.g., "0.45")
        """
        body = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "type": order_type,
        }
        # Kalshi requires exactly one price field even for market orders
        if yes_price is not None:
            body["yes_price_dollars"] = str(yes_price)
        elif no_price is not None:
            body["no_price_dollars"] = str(no_price)
        elif side == "yes":
            body["yes_price_dollars"] = "0.99"  # Market order: willing to pay up to 99c
        else:
            body["no_price_dollars"] = "0.99"  # Market order: willing to pay up to 99c
        if expiration_ts:
            body["expiration_ts"] = expiration_ts

        logger.info(f"ORDER: {action} {count}x {side} on {ticker} "
                     f"@ {'market' if order_type == 'market' else f'{yes_price or no_price}'}")
        return self._request("POST", "/portfolio/orders", json_body=body)

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order."""
        return self._request("DELETE", f"/portfolio/orders/{order_id}")

    def get_orders(self, ticker: str = None, status: str = None) -> dict:
        """Get orders, optionally filtered."""
        params = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        return self._request("GET", "/portfolio/orders", params=params)
