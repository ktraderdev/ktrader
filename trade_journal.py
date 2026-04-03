"""
Kalshi Trading Bot — Trade Journal
SQLite persistence for every decision. This feeds the calibration/learning loop.
"""
import sqlite3
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import config

logger = logging.getLogger(__name__)


class TradeJournal:
    """SQLite-backed trade journal for logging all bot activity."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or config.db_path
        self._conn = None
        self._init_db()

    def _init_db(self):
        """Create tables if they don't exist."""
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                ticker TEXT NOT NULL,
                title TEXT,
                category TEXT,
                market_yes_price REAL,
                market_no_price REAL,
                volume REAL,
                close_time TEXT,
                scanner_prob REAL,
                scanner_confidence TEXT,
                scanner_reasoning TEXT,
                passed_filter INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                scan_id INTEGER REFERENCES scans(id),
                ticker TEXT NOT NULL,
                bull_prob REAL,
                bull_confidence TEXT,
                bull_reasoning TEXT,
                bear_prob REAL,
                bear_confidence TEXT,
                bear_reasoning TEXT,
                arbiter_prob REAL,
                arbiter_edge REAL,
                arbiter_trade INTEGER,
                arbiter_side TEXT,
                arbiter_confidence TEXT,
                arbiter_reasoning TEXT,
                arbiter_source TEXT
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                analysis_id INTEGER REFERENCES analyses(id),
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                action TEXT NOT NULL DEFAULT 'buy',
                count INTEGER NOT NULL DEFAULT 1,
                price REAL,
                order_type TEXT DEFAULT 'market',
                order_id TEXT,
                status TEXT DEFAULT 'pending',
                fill_price REAL,
                pnl REAL,
                resolved_at TEXT,
                outcome TEXT
            );

            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                markets_scanned INTEGER DEFAULT 0,
                analyses_run INTEGER DEFAULT 0,
                trades_placed INTEGER DEFAULT 0,
                trades_won INTEGER DEFAULT 0,
                trades_lost INTEGER DEFAULT 0,
                gross_pnl REAL DEFAULT 0.0,
                api_cost REAL DEFAULT 0.0,
                balance_start REAL,
                balance_end REAL
            );

            CREATE TABLE IF NOT EXISTS exits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                trade_id INTEGER REFERENCES trades(id),
                ticker TEXT NOT NULL,
                reason TEXT NOT NULL,
                exit_price REAL,
                pnl REAL
            );

            CREATE INDEX IF NOT EXISTS idx_scans_ticker ON scans(ticker);
            CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE INDEX IF NOT EXISTS idx_exits_trade ON exits(trade_id);
        """)
        self._conn.commit()
        logger.info(f"Trade journal initialized at {self.db_path}")

    def log_scan(self, market: dict, scanner_result: Optional[dict],
                 passed: bool) -> int:
        """Log a market scan. Returns scan ID."""
        scanner = scanner_result or {}
        cur = self._conn.execute("""
            INSERT INTO scans (ticker, title, category, market_yes_price,
                             market_no_price, volume, close_time,
                             scanner_prob, scanner_confidence,
                             scanner_reasoning, passed_filter)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            market.get("ticker"),
            market.get("title"),
            market.get("category"),
            market.get("yes_bid_dollars"),
            market.get("no_bid_dollars"),
            market.get("volume_fp"),
            market.get("close_time"),
            scanner.get("probability"),
            scanner.get("confidence"),
            scanner.get("reasoning"),
            1 if passed else 0,
        ))
        self._conn.commit()
        return cur.lastrowid

    def log_analysis(self, scan_id: int, ticker: str, bull: dict,
                     bear: dict, arbiter: dict) -> int:
        """Log a full analysis with all agent results. Returns analysis ID."""
        cur = self._conn.execute("""
            INSERT INTO analyses (scan_id, ticker,
                                bull_prob, bull_confidence, bull_reasoning,
                                bear_prob, bear_confidence, bear_reasoning,
                                arbiter_prob, arbiter_edge, arbiter_trade,
                                arbiter_side, arbiter_confidence,
                                arbiter_reasoning, arbiter_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            scan_id, ticker,
            bull.get("probability"), bull.get("confidence"),
            bull.get("reasoning"),
            bear.get("probability"), bear.get("confidence"),
            bear.get("reasoning"),
            arbiter.get("final_probability"), arbiter.get("edge"),
            1 if arbiter.get("trade") else 0,
            arbiter.get("side"),
            arbiter.get("confidence"),
            arbiter.get("reasoning"),
            arbiter.get("arbiter_source"),
        ))
        self._conn.commit()
        return cur.lastrowid

    def log_trade(self, analysis_id: int, ticker: str, side: str,
                  count: int, price: float, order_id: str = None,
                  model: str = "financial-llm") -> int:
        """Log a placed trade. Returns trade ID."""
        cur = self._conn.execute("""
            INSERT INTO trades (analysis_id, ticker, side, count, price, order_id, model)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (analysis_id, ticker, side, count, price, order_id, model))
        self._conn.commit()
        logger.info(f"Trade logged: {side} {count}x {ticker} @ ${price}")
        return cur.lastrowid

    def update_trade(self, trade_id: int, **kwargs):
        """Update trade fields (status, fill_price, pnl, outcome, etc.)."""
        valid = {"status", "fill_price", "pnl", "resolved_at", "outcome", "order_id"}
        updates = {k: v for k, v in kwargs.items() if k in valid}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [trade_id]
        self._conn.execute(
            f"UPDATE trades SET {set_clause} WHERE id = ?", values
        )
        self._conn.commit()

    def get_open_trades(self) -> list:
        """Get all trades that haven't resolved yet."""
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE status IN ('pending', 'filled') "
            "ORDER BY timestamp DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_today_stats(self) -> dict:
        """Get today's trading stats."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self._conn.execute(
            "SELECT * FROM daily_stats WHERE date = ?", (today,)
        ).fetchone()
        if row:
            return dict(row)
        return {
            "date": today, "markets_scanned": 0, "analyses_run": 0,
            "trades_placed": 0, "trades_won": 0, "trades_lost": 0,
            "gross_pnl": 0.0, "api_cost": 0.0,
        }

    def get_daily_pnl(self) -> float:
        """Get today's P&L from resolved trades."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self._conn.execute("""
            SELECT COALESCE(SUM(pnl), 0.0) as daily_pnl
            FROM trades WHERE date(resolved_at) = ? AND pnl IS NOT NULL
        """, (today,)).fetchone()
        return row["daily_pnl"] if row else 0.0

    def log_exit(self, trade_id: int, ticker: str, reason: str,
                 exit_price: float, pnl: float):
        """Log a position exit with reason."""
        self._conn.execute("""
            INSERT INTO exits (trade_id, ticker, reason, exit_price, pnl)
            VALUES (?, ?, ?, ?, ?)
        """, (trade_id, ticker, reason, exit_price, pnl))
        self._conn.commit()
        logger.info(f"Exit logged: {ticker} — {reason} (pnl=${pnl:+.2f})")

    def get_recent_exits(self, limit: int = 50) -> list:
        """Get recent position exits."""
        rows = self._conn.execute("""
            SELECT e.*, t.side, t.count, t.price as entry_price
            FROM exits e
            LEFT JOIN trades t ON e.trade_id = t.id
            ORDER BY e.id DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_calibration_data(self, limit: int = 100) -> list:
        """Get resolved analyses for calibration scoring.
        Returns predictions paired with outcomes."""
        rows = self._conn.execute("""
            SELECT a.ticker, a.arbiter_prob, a.arbiter_side,
                   t.outcome, t.pnl, a.arbiter_source,
                   a.bull_prob, a.bear_prob
            FROM analyses a
            JOIN trades t ON t.analysis_id = a.id
            WHERE t.outcome IS NOT NULL
            ORDER BY a.timestamp DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_calibration_summary(self) -> dict:
        """Compute calibration stats from resolved trades. Returns summary dict."""
        rows = self._conn.execute("""
            SELECT a.arbiter_prob, a.arbiter_side, a.arbiter_confidence,
                   t.side as traded_side, t.outcome, t.pnl,
                   s.category, s.market_yes_price
            FROM analyses a
            JOIN trades t ON t.analysis_id = a.id
            LEFT JOIN scans s ON a.scan_id = s.id
            WHERE t.outcome IN ('won', 'lost', 'stopped')
              AND COALESCE(t.source, 'bot') = 'bot'
        """).fetchall()

        if not rows:
            return {"n": 0, "text": "No resolved bot trades yet."}

        from collections import defaultdict
        total = len(rows)
        wins = 0
        brier_sum = 0.0
        side_stats = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
        cat_stats = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
        conf_stats = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
        prob_buckets = defaultdict(lambda: {"n": 0, "actual_yes": 0})

        for r in rows:
            prob = r["arbiter_prob"] or 0.5
            side = r["traded_side"] or "unknown"
            pnl = r["pnl"] or 0.0
            cat = r["category"] or "unknown"
            conf = r["arbiter_confidence"] or "unknown"
            won = r["outcome"] == "won" or (r["outcome"] == "stopped" and pnl > 0)

            if won:
                wins += 1

            # Brier: did YES actually happen?
            actual_yes = 1.0 if (side == "yes" and won) or (side == "no" and not won) else 0.0
            brier_sum += (prob - actual_yes) ** 2

            bucket = round(prob, 1)
            prob_buckets[bucket]["n"] += 1
            prob_buckets[bucket]["actual_yes"] += actual_yes

            side_stats[side]["n"] += 1
            side_stats[side]["wins"] += (1 if won else 0)
            side_stats[side]["pnl"] += pnl

            cat_stats[cat]["n"] += 1
            cat_stats[cat]["wins"] += (1 if won else 0)
            cat_stats[cat]["pnl"] += pnl

            conf_stats[conf]["n"] += 1
            conf_stats[conf]["wins"] += (1 if won else 0)
            conf_stats[conf]["pnl"] += pnl

        brier = brier_sum / total
        win_rate = wins / total

        # Build text summary for LLM prompt injection
        lines = []
        lines.append(f"CALIBRATION ({total} resolved trades, Brier={brier:.2f}):")
        lines.append(f"  Overall: {wins}/{total} correct ({win_rate:.0%}), ${sum(r['pnl'] or 0 for r in rows):+.2f}")

        lines.append("  By side:")
        for s in sorted(side_stats):
            st = side_stats[s]
            wr = st["wins"]/st["n"]*100 if st["n"] else 0
            lines.append(f"    {s.upper()}: {st['wins']}/{st['n']} ({wr:.0f}%), P&L ${st['pnl']:+.2f}")

        lines.append("  By confidence:")
        for c in sorted(conf_stats):
            ct = conf_stats[c]
            wr = ct["wins"]/ct["n"]*100 if ct["n"] else 0
            lines.append(f"    {c}: {ct['wins']}/{ct['n']} ({wr:.0f}%), P&L ${ct['pnl']:+.2f}")

        lines.append("  Predicted prob vs actual YES rate:")
        for b in sorted(prob_buckets):
            bp = prob_buckets[b]
            ar = bp["actual_yes"]/bp["n"] if bp["n"] else 0
            lines.append(f"    Predicted {b:.0%} -> actual YES {ar:.0%} (n={bp['n']})")

        # Identify strongest/weakest categories
        if cat_stats:
            best_cat = max(cat_stats, key=lambda c: cat_stats[c]["pnl"])
            worst_cat = min(cat_stats, key=lambda c: cat_stats[c]["pnl"])
            lines.append(f"  Best category: {best_cat} (${cat_stats[best_cat]['pnl']:+.2f})")
            lines.append(f"  Worst category: {worst_cat} (${cat_stats[worst_cat]['pnl']:+.2f})")

        text = "\n".join(lines)

        return {
            "n": total,
            "brier": brier,
            "win_rate": win_rate,
            "side_stats": dict(side_stats),
            "cat_stats": dict(cat_stats),
            "conf_stats": dict(conf_stats),
            "prob_buckets": dict(prob_buckets),
            "text": text,
        }

    def get_enrichment_impact(self) -> dict:
        """Compare win rates for trades that had enrichment data vs those that didn't."""
        rows = self._conn.execute("""
            SELECT s.enrichment_data IS NOT NULL AND s.enrichment_data != '' as had_enrichment,
                   COUNT(*) as n,
                   SUM(CASE WHEN t.outcome = 'won' OR (t.outcome = 'stopped' AND t.pnl > 0) THEN 1 ELSE 0 END) as wins,
                   COALESCE(SUM(t.pnl), 0) as pnl
            FROM trades t
            JOIN analyses a ON t.analysis_id = a.id
            LEFT JOIN scans s ON a.scan_id = s.id
            WHERE t.pnl IS NOT NULL AND COALESCE(t.source, 'bot') = 'bot'
            GROUP BY had_enrichment
        """).fetchall()
        result = {}
        for r in rows:
            key = "enriched" if r["had_enrichment"] else "not_enriched"
            result[key] = {"n": r["n"], "wins": r["wins"], "pnl": r["pnl"],
                          "win_rate": r["wins"] / r["n"] if r["n"] else 0}
        return result


    def get_trade_traces(self, n_wins: int = 3, n_losses: int = 3) -> str:
        """Return formatted winning/losing trade examples with reasoning for LLM injection."""
        wins = self._conn.execute("""
            SELECT s.title, t.side, t.price, t.pnl, a.arbiter_prob, a.arbiter_reasoning
            FROM trades t
            JOIN analyses a ON t.analysis_id = a.id
            LEFT JOIN scans s ON a.scan_id = s.id
            WHERE t.outcome = 'won' AND COALESCE(t.source, 'bot') = 'bot'
              AND a.arbiter_reasoning IS NOT NULL AND a.arbiter_reasoning != ''
            ORDER BY t.pnl DESC LIMIT ?
        """, (n_wins,)).fetchall()

        losses = self._conn.execute("""
            SELECT s.title, t.side, t.price, t.pnl, a.arbiter_prob, a.arbiter_reasoning
            FROM trades t
            JOIN analyses a ON t.analysis_id = a.id
            LEFT JOIN scans s ON a.scan_id = s.id
            WHERE t.outcome = 'lost' AND COALESCE(t.source, 'bot') = 'bot'
              AND a.arbiter_reasoning IS NOT NULL AND a.arbiter_reasoning != ''
            ORDER BY t.pnl ASC LIMIT ?
        """, (n_losses,)).fetchall()

        if not wins and not losses:
            return ""

        lines = ["EXAMPLES FROM YOUR PAST TRADES (learn from these):"]
        if wins:
            lines.append("  WINNING trades:")
            for r in wins:
                lines.append(
                    f"    {r['side'].upper()} @${r['price']:.2f} -> ${r['pnl']:+.2f} | "
                    f"{r['title'][:50]} | Reasoning: {r['arbiter_reasoning'][:120]}"
                )
        if losses:
            lines.append("  LOSING trades:")
            for r in losses:
                lines.append(
                    f"    {r['side'].upper()} @${r['price']:.2f} -> ${r['pnl']:+.2f} | "
                    f"{r['title'][:50]} | Reasoning: {r['arbiter_reasoning'][:120]}"
                )
        return "\n".join(lines)

    def log_prompt_variant(self, trade_id: int, variant: str):
        """Tag a trade with its prompt variant for A/B testing."""
        self._conn.execute(
            "UPDATE trades SET order_type = ? WHERE id = ?",
            (variant, trade_id)
        )
        self._conn.commit()

    def get_variant_stats(self) -> dict:
        """Compare performance of prompt variants."""
        rows = self._conn.execute("""
            SELECT order_type as variant,
                   COUNT(*) as n,
                   SUM(CASE WHEN outcome = 'won' THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN outcome = 'lost' THEN 1 ELSE 0 END) as losses,
                   COALESCE(SUM(pnl), 0) as pnl
            FROM trades
            WHERE outcome IN ('won', 'lost')
              AND order_type IS NOT NULL
            GROUP BY order_type
        """).fetchall()
        return {r["variant"]: dict(r) for r in rows}

    def has_open_trade(self, ticker: str) -> bool:
        """Check if there's any open/pending trade for this ticker."""
        row = self._conn.execute(
            "SELECT 1 FROM trades WHERE ticker = ? AND status IN ('pending', 'filled') LIMIT 1",
            (ticker,)
        ).fetchone()
        return row is not None

    def get_held_tickers(self) -> set:
        """Return set of tickers with open/unsettled trades (including dry runs)."""
        rows = self._conn.execute(
            "SELECT DISTINCT ticker FROM trades "
            "WHERE status IN ('pending', 'filled') "
            "OR (order_id = 'DRY_RUN' AND (pnl IS NULL OR outcome = 'unknown'))"
        ).fetchall()
        return {r["ticker"] for r in rows}

    def get_unsettled_dry_runs(self) -> list:
        """Get dry-run trades that haven't been resolved yet."""
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE order_id = 'DRY_RUN' "
            "AND (pnl IS NULL OR outcome = 'unknown') "
            "ORDER BY timestamp DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_category_exposure(self) -> dict:
        """Return dict of category -> total exposure for open trades."""
        rows = self._conn.execute("""
            SELECT s.category, t.price, t.count
            FROM trades t
            JOIN analyses a ON t.analysis_id = a.id
            JOIN scans s ON a.scan_id = s.id
            WHERE t.status IN ('pending', 'filled')
              AND s.category IS NOT NULL
        """).fetchall()
        from collections import defaultdict
        exposure = defaultdict(float)
        for r in rows:
            cat = r["category"]
            price = r["price"] or 0.0
            count = r["count"] or 0
            exposure[cat] += price * count
        return dict(exposure)

    def close(self):
        """Close database connection."""
        if self._conn:
            self._conn.close()


journal = TradeJournal()
