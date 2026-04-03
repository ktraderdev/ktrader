"""
Kalshi Trading Bot — Position Manager
Monitors open positions and executes exits based on stop-loss,
trailing stop, and time-based rules.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from kalshi_client import KalshiClient
from trade_journal import journal
from config import config

logger = logging.getLogger(__name__)


class PositionManager:
    """Manages open positions with stop-loss and trailing stop logic."""

    def __init__(self, client: KalshiClient):
        self.client = client
        # Track highest price seen per ticker for trailing stops
        # {ticker: highest_price}
        self._high_water = {}

    def check_positions(self) -> dict:
        """Check all open positions and exit any that hit thresholds.

        Returns stats dict with counts of actions taken.
        """
        stats = {"checked": 0, "exits": 0, "errors": 0}

        # Get open trades from our journal
        open_trades = journal.get_open_trades()
        if not open_trades:
            return stats

        # Get live positions from Kalshi to confirm we still hold them
        try:
            kalshi_positions = self.client.get_positions()
            live_tickers = {
                p["ticker"]: p
                for p in kalshi_positions.get("market_positions", [])
                if float(p.get("position_fp", "0")) > 0
            }
        except Exception as e:
            logger.error(f"Failed to fetch positions from Kalshi: {e}")
            stats["errors"] += 1
            return stats

        for trade in open_trades:
            ticker = trade["ticker"]
            stats["checked"] += 1

            # Skip dry-run trades — they were never placed on Kalshi
            if trade.get("order_id") == "DRY_RUN":
                continue

            # Skip if we don't actually hold this on Kalshi
            # (may have been manually closed)
            if ticker not in live_tickers:
                logger.info(f"Position {ticker} not found on Kalshi — marking closed")
                journal.update_trade(trade["id"], status="closed", outcome="unknown")
                continue

            # Get current market price
            try:
                market_data = self.client.get_market(ticker)
                market = market_data.get("market", market_data)
            except Exception as e:
                logger.error(f"Failed to fetch market {ticker}: {e}")
                stats["errors"] += 1
                continue

            # Determine current exit price (what we'd get if we sold now)
            exit_price = self._get_exit_price(market, trade["side"])
            if exit_price is None:
                continue

            entry_price = trade["price"]
            exit_reason = self._should_exit(
                ticker, trade["side"], entry_price, exit_price, market, trade
            )

            if exit_reason:
                success = self._execute_exit(trade, exit_price, exit_reason)
                if success:
                    stats["exits"] += 1
                else:
                    stats["errors"] += 1
            else:
                # Update high water mark for trailing stop
                self._update_high_water(ticker, exit_price)

        return stats

    def _should_exit(self, ticker: str, side: str, entry_price: float,
                     current_price: float, market: dict,
                     trade: dict) -> Optional[str]:
        """Exit rules disabled — hold to settlement strategy.
        Prediction markets pay $1 or $0 at expiry. Mid-market price
        fluctuations are noise. Early exits destroyed +$106 of value
        in backtesting vs holding to settlement."""
        return None  # Hold to settlement

    def _execute_exit(self, trade: dict, exit_price: float,
                      reason: str) -> bool:
        """Sell the position and log the exit."""
        ticker = trade["ticker"]
        side = trade["side"]
        count = trade["count"]

        logger.info(
            f"EXITING {ticker}: sell {count}x {side} @ ~${exit_price:.2f} — {reason}"
        )

        try:
            price_field = "yes_price" if side == "yes" else "no_price"
            order = self.client.place_order(
                ticker=ticker,
                side=side,
                action="sell",
                count=count,
                order_type="market",
                **{price_field: exit_price},
            )

            order_data = order.get("order", {})
            fees = float(order_data.get("taker_fees_dollars", "0"))
            entry_cost = trade["price"] * trade["count"]
            exit_revenue = exit_price * trade["count"]
            pnl = exit_revenue - entry_cost - fees

            logger.info(
                f"EXIT FILLED {ticker}: "
                f"entry=${entry_cost:.2f} exit=${exit_revenue:.2f} "
                f"fees=${fees:.2f} pnl=${pnl:+.2f} — {reason}"
            )

            journal.update_trade(
                trade["id"],
                status="closed",
                fill_price=exit_price,
                pnl=pnl,
                resolved_at=datetime.now(timezone.utc).isoformat(),
                outcome="stopped",
            )

            journal.log_exit(trade["id"], ticker, reason, exit_price, pnl)
            self._high_water.pop(ticker, None)

            return True

        except Exception as e:
            logger.error(f"Failed to exit {ticker}: {e}")
            return False

    def _get_exit_price(self, market: dict, side: str) -> Optional[float]:
        """Get the price we'd receive if selling now (the bid side)."""
        if side == "yes":
            price = market.get("yes_bid_dollars")
        else:
            price = market.get("no_bid_dollars")

        if price is None:
            return None
        try:
            p = float(price)
            return p if p > 0 else None
        except (ValueError, TypeError):
            return None

    def resolve_settlements(self, collective_client=None) -> int:
        """Check settled markets and record actual P&L for all unresolved trades
        (both real trades with status pending/filled AND dry-run trades).
        Optionally submits outcomes to collective."""
        # Get pending/filled trades (real trades)
        open_trades = journal.get_open_trades()
        # Also get unsettled dry-run trades
        dry_run_trades = journal.get_unsettled_dry_runs()
        all_trades = open_trades + dry_run_trades

        resolved = 0
        for trade in all_trades:
            ticker = trade["ticker"]
            try:
                data = self.client.get_market(ticker)
                market = data.get("market", data)
                result = market.get("result")
                if not result:
                    continue  # Not settled yet

                # Calculate settlement P&L
                entry_cost = trade["price"] * trade["count"]
                if trade["side"] == "yes":
                    payout = trade["count"] * 1.0 if result == "yes" else 0.0
                else:
                    payout = trade["count"] * 1.0 if result == "no" else 0.0
                pnl = payout - entry_cost
                outcome = "won" if pnl > 0 else "lost"

                journal.update_trade(
                    trade["id"],
                    status="closed",
                    pnl=pnl,
                    outcome=outcome,
                    resolved_at=market.get("close_time", ""),
                )
                logger.info(
                    f"SETTLED {ticker}: {trade['side']} {trade['count']}x "
                    f"result={result} pnl=${pnl:+.2f} ({outcome})"
                )
                # Submit outcome to collective
                if collective_client:
                    try:
                        # Use trade ID as signal_id fallback
                        collective_client.submit_outcome(
                            signal_id=str(trade.get("id", "")),
                            ticker=ticker,
                            outcome=outcome,
                        )
                    except Exception:
                        pass
                resolved += 1
            except Exception as e:
                logger.debug(f"Settlement check failed for {ticker}: {e}")
        return resolved

    def _update_high_water(self, ticker: str, current_price: float):
        """Track the highest price seen for trailing stop."""
        prev = self._high_water.get(ticker, 0)
        if current_price > prev:
            self._high_water[ticker] = current_price
