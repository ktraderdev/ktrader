#!/usr/bin/env python3
"""
Kalshi Trading Bot — Orchestrator
Main entry point. Runs the scan → analyze → trade loop.

Usage:
    python main.py              # Single scan cycle
    python main.py --loop       # Continuous scanning
    python main.py --status     # Show portfolio + open trades
    python main.py --test-llm   # Test LM Studio connection
"""
import os
import sys
import time
import signal
import logging
import argparse
from datetime import datetime, timezone

from config import config
from kalshi_client import KalshiClient
from scanner import MarketScanner
from llm_client import llm_client
from trade_journal import journal
from position_manager import PositionManager

try:
    from data_enrichment import enrich_market
except ImportError:
    enrich_market = lambda m: ''

try:
    from sports_scanner import scan_sports_arb
    from sports_enrichment import enrich_sports_market
    _sports_available = True
except ImportError:
    _sports_available = False

# Collective intelligence (opt-in)
_collective_client = None
if config.collective.enabled and config.collective.api_key:
    try:
        from collective.client import CollectiveClient
        _collective_client = CollectiveClient(
            server_url=config.collective.server_url,
            api_key=config.collective.api_key,
            instance_id=config.collective.instance_id,
        )
    except ImportError:
        pass


def _fetch_market_context(client, market: dict) -> str:
    """Fetch orderbook and recent trades to give the LLM real data."""
    ticker = market.get("ticker", "")
    parts = []
    try:
        ob = client.get_orderbook(ticker, depth=5)
        orderbook = ob.get("orderbook", {})
        yes_bids = orderbook.get("yes", [])
        no_bids = orderbook.get("no", [])
        if yes_bids:
            parts.append(f"Orderbook YES bids (price, qty): {yes_bids[:5]}")
        if no_bids:
            parts.append(f"Orderbook NO bids (price, qty): {no_bids[:5]}")
    except Exception:
        pass
    try:
        trades_data = client.get_trades(ticker=ticker, limit=10)
        recent = trades_data.get("trades", [])
        if recent:
            prices = [float(t.get("yes_price", 0)) / 100 for t in recent if t.get("yes_price")]
            if not prices:
                prices = [float(t.get("yes_price_dollars", 0)) for t in recent if t.get("yes_price_dollars")]
            if prices:
                parts.append(
                    f"Recent trades ({len(prices)}): avg=${sum(prices)/len(prices):.2f}, "
                    f"range=${min(prices):.2f}-${max(prices):.2f}"
                )
    except Exception:
        pass
    # Add real-world data from external APIs
    try:
        real_data = enrich_market(market)
        if real_data:
            parts.append(real_data)
    except Exception as e:
        pass

    return "\n".join(parts) if parts else ""


# ── Logging Setup ──────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, config.log_level),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(os.path.dirname(os.path.abspath(__file__)), "kalshi_trader.log")),
    ],
)
logger = logging.getLogger("kalshi-trader")

# ── Signal Handling ────────────────────────────────────────────────

_shutdown = False

def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Shutdown signal received — finishing current cycle...")
    _shutdown = True

signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ── Core Logic ─────────────────────────────────────────────────────

def run_cycle(client: KalshiClient, scanner: MarketScanner,
              pos_manager: PositionManager) -> dict:
    """Run one scan → analyze → trade cycle. Returns stats."""
    stats = {"scanned": 0, "analyzed": 0, "traded": 0, "errors": 0,
             "exits": 0}

    # 0a. Resolve any settled markets first (record actual P&L)
    try:
        settled = pos_manager.resolve_settlements(collective_client=_collective_client)
        if settled > 0:
            logger.info(f"Resolved {settled} settled market(s)")
    except Exception as e:
        logger.error(f"Settlement resolution error: {e}", exc_info=True)

    # 0b. Fetch calibration summary (refreshed each cycle, used in arbiter prompt)
    calibration_text = ""
    try:
        cal = journal.get_calibration_summary()
        if cal["n"] >= 5:  # Only inject after 5+ resolved trades
            calibration_text = cal["text"]
            logger.debug(f"Calibration: {cal['n']} trades, Brier={cal.get('brier', '?'):.3f}, "
                        f"win_rate={cal.get('win_rate', '?'):.1%}")
    except Exception as e:
        logger.debug(f"Calibration fetch failed: {e}")

    # 0c. Fetch trade traces (winning/losing examples for LLM context)
    trade_traces = ""
    try:
        trade_traces = journal.get_trade_traces(n_wins=3, n_losses=3)
        if trade_traces:
            logger.debug(f"Trade traces loaded ({trade_traces.count(chr(10))+1} lines)")
    except Exception as e:
        logger.debug(f"Trade traces fetch failed: {e}")

    # 0d. Fetch crowd signals from collective
    crowd_signals = {}
    if _collective_client:
        try:
            crowd_signals = _collective_client.get_active_crowds()
            if crowd_signals:
                logger.debug(f"Crowd signals loaded for {len(crowd_signals)} tickers")
        except Exception as e:
            logger.debug(f"Crowd signal fetch failed: {e}")

    # 0e. Get current prompt variant
    variant_name, variant_cfg = llm_client.next_variant()
    inject_traces = variant_cfg.get("inject_traces", False)
    active_traces = trade_traces if inject_traces else ""
    logger.info(f"Prompt variant: {variant_name}")

    # 0e. Check open positions for stop-loss / trailing stop / time exits
    try:
        pm_stats = pos_manager.check_positions()
        stats["exits"] = pm_stats["exits"]
        if pm_stats["exits"] > 0:
            logger.info(
                f"Position manager: checked={pm_stats['checked']} "
                f"exits={pm_stats['exits']} errors={pm_stats['errors']}"
            )
    except Exception as e:
        logger.error(f"Position manager error: {e}", exc_info=True)

    # 1. Check circuit breaker (skip in dry-run mode)
    daily_pnl = journal.get_daily_pnl()
    if daily_pnl < 0 and not config.trading.dry_run:
        try:
            balance = client.get_balance()
            balance_dollars = balance.get("balance", 0) / 100.0
            if abs(daily_pnl) > balance_dollars * config.trading.circuit_breaker_pct:
                logger.warning(
                    f"CIRCUIT BREAKER: Daily loss ${daily_pnl:.2f} exceeds "
                    f"{config.trading.circuit_breaker_pct:.0%} of ${balance_dollars:.2f}"
                )
                return stats
        except Exception:
            logger.error("Cannot fetch balance for circuit breaker — skipping cycle")
            return stats

    # 2. Check open position limit (against live Kalshi positions, not just our journal)
    try:
        kalshi_pos = client.get_positions()
        live_positions = [
            p for p in kalshi_pos.get("market_positions", [])
            if float(p.get("position_fp", "0")) > 0
        ]
        total_open = len(live_positions)
    except Exception:
        # Fail closed — don't trade if we can't check positions
        logger.error("Cannot fetch positions — skipping cycle")
        return stats

    # Build held tickers set: Kalshi positions + journal open trades
    held_tickers = {p["ticker"] for p in live_positions}
    held_tickers.update(journal.get_held_tickers())

    if total_open >= config.trading.max_open_positions and not config.trading.dry_run:
        logger.info(
            f"Max positions reached ({total_open}/{config.trading.max_open_positions}) "
            f"— skipping scan (not blocking exits)"
        )
        return stats

    # 3. Scan markets
    candidates = scanner.scan()
    stats["scanned"] = len(candidates)

    if not candidates:
        logger.info("No candidates found")
        return stats

    logger.info(f"\n{scanner.get_market_summary(candidates)}")

    # 4. Analyze top candidates
    # Deduplicate: pick the best candidate per event to maximize diversity
    seen_events = set()
    to_analyze = []
    for market in candidates:
        event_ticker = market.get("event_ticker", market.get("ticker", ""))
        if event_ticker not in seen_events:
            seen_events.add(event_ticker)
            to_analyze.append(market)
        if len(to_analyze) >= 30:  # Grab more candidates since cooldown will skip many
            break

    max_analyze = len(to_analyze)
    for market in to_analyze:
        if _shutdown:
            break

        ticker = market.get("ticker", "?")

        # Skip if we already hold this ticker (per-ticker position limit)
        if ticker in held_tickers:
            continue

        logger.info(f"\n{'='*60}")
        logger.info(f"Analyzing: {market.get('title', ticker)}")
        logger.info(f"{'='*60}")

        try:
            # Fetch real market data for the LLM
            market_context = _fetch_market_context(client, market)

            if config.llm.use_dual_analysis:
                logger.info("  Running dual analysis (Qwen + Claude)...")
                crowd_text = ""
                if _collective_client and ticker in crowd_signals:
                    crowd_text = _collective_client.format_crowd_text(crowd_signals[ticker])
                arbiter = llm_client.analyze_dual(market, market_context, calibration_text, active_traces, crowd_text)
                if arbiter:
                    prob = arbiter.get("final_probability", arbiter.get("probability", "?"))
                    logger.info(
                        f"  Dual result: prob={prob} edge={arbiter.get('edge', '?')} "
                        f"trade={arbiter.get('trade', False)} side={arbiter.get('side', 'none')} "
                        f"[{arbiter.get('arbiter_source', '?')}]"
                    )
                    # Submit to collective (fire-and-forget)
                    if _collective_client:
                        try:
                            _collective_client.submit_signal(
                                ticker=ticker,
                                predicted_prob=float(arbiter.get("final_probability", arbiter.get("probability", 0.5))),
                                side=arbiter.get("side", "none"),
                                confidence=arbiter.get("confidence", "low"),
                                model_source=arbiter.get("arbiter_source", "unknown"),
                                market_price=float(market.get("yes_bid_dollars", 0.5)),
                                category=market.get("_category", ""),
                            )
                        except Exception:
                            pass

                    if not arbiter.get("trade"):
                        # Log the analysis even if no trade
                        scan_id = journal.log_scan(market, {"probability": prob, "confidence": arbiter.get("confidence", "low"), "reasoning": arbiter.get("reasoning", "")}, False)
                        journal.log_analysis(scan_id, ticker,
                            {"probability": arbiter.get("qwen_prob", prob), "confidence": "dual", "reasoning": "Qwen estimate"},
                            {"probability": arbiter.get("claude_prob", prob), "confidence": "dual", "reasoning": "Claude estimate"},
                            arbiter)
                        continue
                    # Has a trade — log and proceed to execution
                    if market.get("_category"):
                        market["category"] = market["_category"]
                    scan_id = journal.log_scan(market, {"probability": prob, "confidence": arbiter.get("confidence", "medium"), "reasoning": arbiter.get("reasoning", "")}, True)
                    analysis_id = journal.log_analysis(scan_id, ticker,
                        {"probability": arbiter.get("qwen_prob", prob), "confidence": "dual", "reasoning": "Qwen estimate"},
                        {"probability": arbiter.get("claude_prob", prob), "confidence": "dual", "reasoning": "Claude estimate"},
                        arbiter)
                    stats["analyzed"] += 1
                else:
                    stats["errors"] += 1
                    continue

            elif config.llm.use_single_agent:
                # Single informed prompt — skip multi-agent pipeline
                logger.info("  Running single-agent analysis (Claude)...")
                crowd_text = ""
                if _collective_client and ticker in crowd_signals:
                    crowd_text = _collective_client.format_crowd_text(crowd_signals[ticker])
                arbiter = llm_client.analyze_single(market, market_context, calibration_text, active_traces, crowd_text)
                if not arbiter:
                    logger.warning(f"  Single-agent analysis failed for {ticker}")
                    stats["errors"] += 1
                    continue

                scanner_prob = float(arbiter.get("probability", 0.5))
                logger.info(
                    f"  Single-agent: prob={scanner_prob:.2f} "
                    f"edge={arbiter.get('edge', '?')} "
                    f"trade={arbiter.get('trade', False)} "
                    f"side={arbiter.get('side', 'none')} "
                    f"[via {arbiter.get('arbiter_source', '?')}]"
                )

                # Use the single result as both scanner and arbiter
                scanner_result = arbiter
                passed = scanner.filter_by_edge(market, scanner_prob)
                if market.get("_category"):
                    market["category"] = market["_category"]
                scan_id = journal.log_scan(market, scanner_result, passed)

                if not passed and not arbiter.get("trade"):
                    logger.info(f"  No edge — skipping")
                    continue

                stats["analyzed"] += 1

                # Log analysis (use single result for bull/bear placeholders)
                placeholder = {"probability": scanner_prob, "confidence": arbiter.get("confidence", "low"),
                               "reasoning": "Single-agent mode — no separate bull/bear"}
                analysis_id = journal.log_analysis(scan_id, ticker, placeholder, placeholder, arbiter)

            else:
                # Multi-agent pipeline (scanner → bull → bear → arbiter)
                # Agent 1: Scanner estimate
                scanner_result = llm_client.scan_market(market, market_context)
                if not scanner_result:
                    logger.warning(f"Scanner failed for {ticker}")
                    stats["errors"] += 1
                    continue

                scanner_prob = scanner_result.get("probability", 0.5)
                logger.info(
                    f"  Scanner: prob={scanner_prob:.2f} "
                    f"conf={scanner_result.get('confidence', '?')}"
                )

                # Log the scan
                passed = scanner.filter_by_edge(market, scanner_prob)
                if market.get("_category"):
                    market["category"] = market["_category"]
                scan_id = journal.log_scan(market, scanner_result, passed)

                if not passed:
                    logger.info(f"  No edge — skipping")
                    continue

                stats["analyzed"] += 1

                # Agents 2 & 3: Bull and bear cases
                logger.info("  Running bull/bear analysis...")
                bull = llm_client.bull_case(market, market_context) or {
                    "probability": scanner_prob, "confidence": "low",
                    "reasoning": "Bull analysis failed"
                }
                bear = llm_client.bear_case(market, market_context) or {
                    "probability": scanner_prob, "confidence": "low",
                    "reasoning": "Bear analysis failed"
                }

                logger.info(
                    f"  Bull: prob={bull.get('probability', '?'):.2f} "
                    f"| Bear: prob={bear.get('probability', '?'):.2f}"
                )

                # Agent 4: Arbiter decision
                logger.info("  Arbiter deciding...")
                arbiter = llm_client.arbiter(market, scanner_result, bull, bear, calibration_text)
                if not arbiter:
                    logger.warning("  Arbiter failed — no trade")
                    journal.log_analysis(scan_id, ticker, bull, bear,
                                        {"trade": False, "reasoning": "Arbiter error"})
                    continue

                analysis_id = journal.log_analysis(scan_id, ticker, bull, bear, arbiter)

                logger.info(
                    f"  Arbiter: prob={arbiter.get('final_probability', '?')} "
                    f"edge={arbiter.get('edge', '?')} "
                    f"trade={arbiter.get('trade', False)} "
                    f"side={arbiter.get('side', 'none')} "
                    f"conf={arbiter.get('confidence', '?')} "
                    f"[via {arbiter.get('arbiter_source', '?')}]"
                )

            # 5a. Confidence gate: skip low-confidence trades
            if arbiter.get("confidence") == "low":
                logger.info("  Low confidence — skipping")
                continue

            # 5. Execute trade if recommended
            if arbiter.get("trade") and arbiter.get("side") == "yes":
                # Strategy D: YES only. NO trades had 0/40 win rate.
                side = arbiter["side"]
                edge = float(arbiter.get("edge", 0))

                # Kelly criterion position sizing
                yes_price = scanner._get_yes_price(market)
                if yes_price and edge > 0:
                    prob = float(arbiter.get("final_probability", 0.5))
                    odds = (1 / yes_price) - 1 if side == "yes" else (1 / (1 - yes_price)) - 1
                    kelly = max(0, (prob * odds - (1 - prob)) / odds)
                    bet_fraction = kelly * config.trading.kelly_fraction

                    # Get balance for sizing
                    try:
                        balance = client.get_balance()
                        bankroll = balance.get("balance", 0) / 100.0
                    except Exception:
                        bankroll = 30.0  # Fallback

                    bet_amount = min(
                        bankroll * bet_fraction,
                        config.trading.max_bet_amount,
                    )

                    if bet_amount < 0.50:
                        logger.info(f"  Bet too small (${bet_amount:.2f}) — skipping")
                        continue

                    # Strategy D: Cap entry at $0.65 — data showed mid-range entries outperform cheap ones
                    if price > 0.65:
                        logger.info(f"  Entry price ${price:.2f} too high (max $0.65) — skipping")
                        continue

                    # Category exposure limit check
                    trade_category = market.get("_category") or market.get("_event_category")
                    if trade_category:
                        cat_exposure = journal.get_category_exposure()
                        current_cat_exp = cat_exposure.get(trade_category, 0.0)
                        if current_cat_exp > 3.00:
                            logger.info(
                                f"  Category exposure limit reached: "
                                f"{trade_category} = ${current_cat_exp:.2f} (limit $3.00) — skipping"
                            )
                            continue

                    # Calculate contracts
                    price = yes_price if side == "yes" else (1 - yes_price)
                    contracts = max(1, int(bet_amount / price))

                    logger.info(
                        f"  >> TRADE: {side.upper()} {contracts}x {ticker} "
                        f"@ ${price:.2f} (${bet_amount:.2f} risk)"
                    )

                    if config.trading.dry_run:
                        logger.info(f"  [DRY RUN] Would trade: {side} {contracts}x {ticker} @ ${price:.2f}")
                        trade_id = journal.log_trade(
                            analysis_id, ticker, side, contracts, price, "DRY_RUN"
                        )
                        journal.log_prompt_variant(trade_id, variant_name)
                        stats["traded"] += 1
                        held_tickers.add(ticker)
                    else:
                        try:
                            limit_kwargs = {}
                            if side == "yes":
                                limit_kwargs["yes_price"] = price
                            else:
                                limit_kwargs["no_price"] = price
                            order = client.place_order(
                                ticker=ticker, side=side, action="buy",
                                count=contracts, order_type="limit",
                                **limit_kwargs,
                            )
                            order_id = order.get("order", {}).get("order_id", "?")
                            trade_id = journal.log_trade(
                                analysis_id, ticker, side, contracts, price, order_id
                            )
                            journal.log_prompt_variant(trade_id, variant_name)
                            stats["traded"] += 1
                            held_tickers.add(ticker)
                            logger.info(f"  Order placed: {order_id}")
                        except Exception as e:
                            logger.error(f"  Order failed: {e}")
                            stats["errors"] += 1
                else:
                    logger.info("  Trade signal but no valid price — skipping")

        except Exception as e:
            logger.error(f"  Error analyzing {ticker}: {e}", exc_info=True)
            stats["errors"] += 1

    return stats


def run_sports_cycle(client: KalshiClient) -> dict:
    """Run one sports arbitrage scan cycle. Returns stats."""
    stats = {"scanned": 0, "matched": 0, "traded": 0, "errors": 0}

    if not _sports_available or not config.sports.enabled:
        return stats

    try:
        arbs = scan_sports_arb(client)
        stats["scanned"] = len(arbs)

        # Build held tickers to prevent duplicates
        held_tickers = {p["ticker"] for p in client.get_positions().get("market_positions", [])
                       if float(p.get("position_fp", "0")) > 0}
        held_tickers.update(journal.get_held_tickers())

        for opp in arbs:
            if _shutdown:
                break

            ticker = opp["ticker"]
            if ticker in held_tickers:
                continue

            # Strategy D filters: YES only, entry < $0.50, edge >= 10%
            if opp["side"] != "yes":
                continue
            if opp["kalshi_price"] > 0.65:
                continue
            if opp["edge"] < 0.10:
                continue

            stats["matched"] += 1
            price = opp["kalshi_price"]
            edge = opp["edge"]
            espn_prob = opp["espn_prob"]

            # Position sizing: $1 max bet
            if price <= 0:
                continue
            contracts = max(1, int(config.trading.max_bet_amount / price))

            logger.info(
                f"  SPORTS ARB: YES {contracts}x {ticker} @ ${price:.2f} "
                f"(ESPN={espn_prob*100:.0f}% edge={edge*100:.0f}% ML={opp['moneyline']})"
            )

            # Log scan entry
            scan_id = journal.log_scan(
                {"ticker": ticker, "title": opp["title"], "category": "sports",
                 "yes_bid_dollars": price, "volume_fp": 0,
                 "close_time": ""},
                {"probability": espn_prob, "confidence": "high",
                 "reasoning": f"ESPN/DraftKings ML {opp['moneyline']}, game: {opp['game']}"},
                True
            )

            # Log analysis entry
            analysis_id = journal.log_analysis(
                scan_id, ticker,
                {"probability": espn_prob, "confidence": "high",
                 "reasoning": f"DraftKings moneyline {opp['moneyline']} implies {espn_prob*100:.1f}%"},
                {"probability": 1 - espn_prob, "confidence": "high",
                 "reasoning": "Counter-side of ESPN consensus"},
                {"final_probability": espn_prob, "edge": edge, "trade": True,
                 "side": "yes", "confidence": opp.get("confidence", "high"),
                 "reasoning": f"Sports arb: Kalshi ${price:.2f} vs ESPN {espn_prob*100:.1f}% ({opp['moneyline']})",
                 "arbiter_source": "sports-arb"}
            )

            if config.trading.dry_run:
                logger.info(f"  [DRY RUN] Would trade: yes {contracts}x {ticker} @ ${price:.2f}")
                journal.log_trade(analysis_id, ticker, "yes", contracts, price, "DRY_RUN", model="sports-arb")
                stats["traded"] += 1
                held_tickers.add(ticker)
            else:
                try:
                    order = client.place_order(
                        ticker=ticker, side="yes", action="buy",
                        count=contracts, order_type="limit",
                        yes_price=price,
                    )
                    order_id = order.get("order", {}).get("order_id", "?")
                    journal.log_trade(analysis_id, ticker, "yes", contracts, price, order_id, model="sports-arb")
                    stats["traded"] += 1
                    held_tickers.add(ticker)
                    logger.info(f"  Order placed: {order_id}")
                except Exception as e:
                    logger.error(f"  Sports order failed: {e}")
                    stats["errors"] += 1

    except Exception as e:
        logger.error(f"Sports cycle error: {e}", exc_info=True)
        stats["errors"] += 1

    return stats


def show_status(client: KalshiClient):
    """Print portfolio status and open trades."""
    print("\n" + "=" * 60)
    print("  KALSHI TRADING BOT — STATUS")
    print("=" * 60)

    env = "DEMO" if config.kalshi.use_demo else "LIVE"
    print(f"  Environment: {env}")
    print(f"  LM Studio: {config.llm.endpoint}")

    try:
        balance = client.get_balance()
        bal = balance.get("balance", 0) / 100.0
        portfolio_val = balance.get("portfolio_value", 0) / 100.0
        print(f"  Balance: ${bal:.2f}")
        print(f"  Portfolio value: ${portfolio_val:.2f}")
    except Exception as e:
        print(f"  Balance: error ({e})")

    open_trades = journal.get_open_trades()
    print(f"\n  Open trades: {len(open_trades)}")
    for t in open_trades:
        print(f"    {t['side'].upper()} {t['count']}x {t['ticker']} @ ${t['price']:.2f}")

    stats = journal.get_today_stats()
    print(f"\n  Today: {stats['trades_placed']} trades, "
          f"P&L: ${stats['gross_pnl']:.2f}")
    print()


def test_llm():
    """Test LM Studio connectivity and model response."""
    print(f"\nTesting LM Studio at {config.llm.endpoint}...")

    if llm_client.check_local():
        print("  ✓ LM Studio is reachable")
    else:
        print("  ✗ LM Studio is NOT reachable")
        print(f"    Check that LM Studio is running on your Mac Mini")
        print(f"    and 'Serve on Local Network' is enabled")
        return

    print(f"\n  Testing inference with a sample market...")
    sample_market = {
        "title": "Will WTI oil close above $70 today?",
        "ticker": "TEST-OIL-70",
        "category": "financials",
        "yes_bid_dollars": "0.55",
        "no_bid_dollars": "0.45",
        "volume_fp": "1500",
        "close_time": "2026-03-30T21:00:00Z",
    }

    result = llm_client.scan_market(sample_market)
    if result:
        print(f"  ✓ Scanner response: {result}")
    else:
        print(f"  ✗ Scanner failed — check LM Studio logs")


# ── Main Entry Point ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Kalshi Trading Bot")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--status", action="store_true", help="Show portfolio status")
    parser.add_argument("--test-llm", action="store_true", help="Test LM Studio")
    parser.add_argument("--scan-only", action="store_true", help="Scan without trading")
    parser.add_argument("--dry-run", action="store_true", help="Log trades without executing")
    args = parser.parse_args()

    client = KalshiClient()
    scanner_obj = MarketScanner(client)
    pos_manager = PositionManager(client)

    if args.test_llm:
        test_llm()
        return

    if args.status:
        show_status(client)
        return

    # Dry-run validation check: warn if going live without enough dry-run data
    if not config.trading.dry_run:
        try:
            import sqlite3 as _sqlite3
            _conn = _sqlite3.connect(config.db_path)
            _row = _conn.execute(
                "SELECT COUNT(*) FROM trades WHERE source='dry_run' AND outcome IS NOT NULL"
            ).fetchone()
            _resolved = _row[0] if _row else 0
            _conn.close()
            if _resolved < 50:
                logger.warning(
                    f"WARNING: Only {_resolved} dry-run trades resolved. "
                    f"Recommend 50+ before going live."
                )
                print(f"\n*** WARNING: Only {_resolved} resolved dry-run trades "
                      f"(recommend 50+). Proceed with caution. ***\n")
        except Exception as _e:
            logger.warning(f"Could not check dry-run trade count: {_e}")

    env = "DEMO" if config.kalshi.use_demo else "** LIVE **"
    logger.info(f"Kalshi Trading Bot starting [{env}]")
    logger.info(f"LLM endpoint: {config.llm.endpoint}")
    logger.info(f"Categories: {config.trading.categories}")
    logger.info(f"Min edge: {config.trading.min_edge}")
    if args.dry_run:
        config.trading.dry_run = True
    logger.info(f"Max bet: ${config.trading.max_bet_amount}")
    logger.info(f"Dry run: {config.trading.dry_run}")
    logger.info(f"Sports: {config.sports.enabled and _sports_available}")

    if args.loop:
        logger.info(f"Continuous mode — scanning every {config.trading.scan_interval}s")
        while not _shutdown:
            try:
                stats = run_cycle(client, scanner_obj, pos_manager)
                logger.info(
                    f"Cycle done: scanned={stats['scanned']} "
                    f"analyzed={stats['analyzed']} traded={stats['traded']} "
                    f"exits={stats['exits']} errors={stats['errors']}"
                )
            except Exception as e:
                logger.error(f"Cycle error: {e}", exc_info=True)

            # Sports arb cycle (runs every financial cycle)
            if config.sports.enabled and _sports_available:
                try:
                    sp = run_sports_cycle(client)
                    if sp["scanned"] > 0 or sp["traded"] > 0:
                        logger.info(
                            f"Sports cycle: scanned={sp['scanned']} "
                            f"matched={sp['matched']} traded={sp['traded']} "
                            f"errors={sp['errors']}"
                        )
                except Exception as e:
                    logger.error(f"Sports cycle error: {e}", exc_info=True)

            if not _shutdown:
                logger.info(f"Sleeping {config.trading.scan_interval}s...")
                for _ in range(config.trading.scan_interval):
                    if _shutdown:
                        break
                    time.sleep(1)
    else:
        stats = run_cycle(client, scanner_obj, pos_manager)
        logger.info(
            f"Done: scanned={stats['scanned']} analyzed={stats['analyzed']} "
            f"traded={stats['traded']} exits={stats['exits']} errors={stats['errors']}"
        )

    journal.close()
    logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
