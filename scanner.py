"""
Kalshi Trading Bot — Market Scanner
Polls Kalshi events, filters by category, and identifies market candidates for analysis.
"""
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from kalshi_client import KalshiClient
from config import config

logger = logging.getLogger(__name__)

# Map Kalshi's event categories to our trading focus areas.
# Keys = our internal category names used in config (CATEGORIES env var).
# Values = list of Kalshi event category strings that map to each.
CATEGORY_MAP = {
    "climate": ["Climate and Weather"],
    "economics": ["Economics", "Financials"],
    "financials": ["Financials", "Companies"],
    "politics": ["Politics", "Elections"],
    "entertainment": ["Entertainment"],
    "science": ["Science and Technology"],
    "health": ["Health"],
    "sports": ["Sports", "Exotics"],
    "world": ["World"],
}

# Reverse lookup: Kalshi category -> our category
_KALSHI_TO_OUR = {}
for our_cat, kalshi_cats in CATEGORY_MAP.items():
    for kc in kalshi_cats:
        _KALSHI_TO_OUR.setdefault(kc, our_cat)


class MarketScanner:
    """Scans Kalshi for trading opportunities."""

    def __init__(self, client: KalshiClient):
        self.client = client

    def scan(self) -> list:
        """Fetch and filter open markets. Returns candidate list."""
        logger.info("Scanning Kalshi markets...")
        start = time.time()

        try:
            all_events = self.client.get_all_open_events(with_nested_markets=True)
        except Exception as e:
            logger.error(f"Failed to fetch events: {e}")
            return []

        # Extract markets from events, tagging each with event category
        candidates = []
        for event in all_events:
            kalshi_cat = event.get("category", "")
            our_cat = _KALSHI_TO_OUR.get(kalshi_cat)
            if not our_cat or our_cat not in config.trading.categories:
                continue

            for market in event.get("markets", []):
                if market.get("status") != "active":
                    continue
                market["_category"] = our_cat
                market["_event_category"] = kalshi_cat
                market["_event_title"] = event.get("title", "")
                candidates.append(market)

        # Filter: only markets closing within 7 days (actionable timeframe)
        now_ts = int(datetime.now(timezone.utc).timestamp())
        week_ts = now_ts + (7 * 24 * 3600)  # 7-day window — wider horizon for more opportunities
        candidates = [
            m for m in candidates
            if self._get_close_ts(m) and now_ts < self._get_close_ts(m) <= week_ts
        ]

        # Filter out illiquid markets (no volume = no fills)
        # and extreme-priced markets (no room for edge at $0.00/$0.01 or $0.99/$1.00)
        filtered = []
        for m in candidates:
            vol = float(m.get("volume_fp", "0") or "0")
            if vol < 500:  # Require real liquidity
                continue
            yes_price = self._get_yes_price(m)
            if yes_price is not None and not (0.03 < yes_price < 0.97):
                continue  # Skip truly dead markets; allow high-YES for NO trades
            filtered.append(m)
        candidates = filtered

        # Filter out wide-spread markets (spread > $0.10 means poor liquidity)
        pre_spread = len(candidates)
        spread_filtered = []
        for m in candidates:
            yes_bid = None
            no_bid = None
            try:
                yb = m.get("yes_bid_dollars")
                nb = m.get("no_bid_dollars")
                if yb is not None:
                    yes_bid = float(yb)
                    if yes_bid > 1:
                        yes_bid = yes_bid / 100.0
                if nb is not None:
                    no_bid = float(nb)
                    if no_bid > 1:
                        no_bid = no_bid / 100.0
            except (ValueError, TypeError):
                pass

            if yes_bid is not None and no_bid is not None:
                spread = 1.0 - yes_bid - no_bid
            elif yes_bid is not None:
                # Use yes_ask - yes_bid if available
                yes_ask = m.get("yes_ask_dollars")
                if yes_ask is not None:
                    try:
                        ya = float(yes_ask)
                        if ya > 1:
                            ya = ya / 100.0
                        spread = ya - yes_bid
                    except (ValueError, TypeError):
                        spread = None
                else:
                    spread = None
            else:
                spread = None

            if spread is None or spread > 0.10:
                continue
            spread_filtered.append(m)
        spread_skipped = pre_spread - len(spread_filtered)
        if spread_skipped > 0:
            logger.info(f"Spread filter: skipped {spread_skipped} markets (spread unknown or > $0.10)")
        candidates = spread_filtered

        # Sort by close time (soonest first), then volume as tiebreaker
        # Prioritizes short-dated markets for faster turnover
        candidates.sort(
            key=lambda m: (
                self._get_close_ts(m) or 9999999999,
                -float(m.get("volume_fp", "0") or "0"),
            ),
        )

        elapsed = time.time() - start
        total_markets = sum(len(ev.get("markets", [])) for ev in all_events)
        logger.info(
            f"Scan complete: {len(all_events)} events / {total_markets} markets → "
            f"{len(candidates)} candidates in {elapsed:.1f}s"
        )
        return candidates

    def filter_by_edge(self, market: dict, scanner_prob: float) -> bool:
        """Check if scanner's probability estimate shows enough edge."""
        yes_price = self._get_yes_price(market)
        if yes_price is None:
            return False

        edge = abs(scanner_prob - yes_price)
        if edge >= config.trading.min_edge:
            logger.debug(
                f"Edge found: {market['ticker']} — "
                f"scanner={scanner_prob:.2f} market={yes_price:.2f} "
                f"edge={edge:.2f}"
            )
            return True
        return False

    def _get_close_ts(self, market: dict) -> Optional[int]:
        """Extract close timestamp from market data."""
        close_time = market.get("close_time")
        if not close_time:
            return None
        try:
            if isinstance(close_time, (int, float)):
                return int(close_time)
            # ISO format string
            dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except (ValueError, TypeError):
            return None

    def _get_yes_price(self, market: dict) -> Optional[float]:
        """Get the YES price as a float between 0 and 1."""
        price = market.get("yes_bid_dollars")
        if price is None:
            price = market.get("last_price")
        if price is None:
            return None
        try:
            p = float(price)
            # Kalshi prices can be in dollars (0.55) or cents (55)
            if p > 1:
                p = p / 100.0
            return p
        except (ValueError, TypeError):
            return None

    def get_market_summary(self, candidates: list) -> str:
        """Pretty-print scan results."""
        lines = [f"{'Ticker':<30} {'Category':<12} {'YES$':<8} {'Vol':<10} {'Closes'}"]
        lines.append("-" * 85)
        for m in candidates[:20]:
            lines.append(
                f"{m.get('ticker', '?'):<30} "
                f"{m.get('_category', '?'):<12} "
                f"${self._get_yes_price(m) or 0:.2f}    "
                f"{m.get('volume_fp', '0'):<10} "
                f"{m.get('close_time', '?')[:16]}"
            )
        if len(candidates) > 20:
            lines.append(f"  ... and {len(candidates) - 20} more")
        return "\n".join(lines)
