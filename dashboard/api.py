"""
Kalshi Trading Bot — Dashboard API
Read-only Flask API serving trade journal data from both bots.
"""
import sys
import re
import sqlite3
import os
from pathlib import Path
from flask import Flask, jsonify, request, redirect

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DB_QWEN = os.environ.get("DB_PATH", str(PROJECT_ROOT / "trade_journal.db"))
DB_CLAUDE = os.environ.get("DB_CLAUDE_PATH", str(PROJECT_ROOT / "trade_journal_claude.db"))
DB_DEMO = str(PROJECT_ROOT / "data" / "demo_journal.db")

import time as _time
import threading

# Simple cache: stores {key: (timestamp, data)}
_cache = {}
_cache_lock = threading.Lock()

def cached(key, ttl_seconds, fn):
    """Return cached result or compute and cache it."""
    with _cache_lock:
        if key in _cache:
            ts, data = _cache[key]
            if _time.time() - ts < ttl_seconds:
                return data
    # Compute outside lock (slow)
    result = fn()
    with _cache_lock:
        _cache[key] = (_time.time(), result)
    return result

app = Flask(__name__, static_folder="static", static_url_path="")


@app.before_request
def _block_sensitive_in_demo():
    """Block endpoints that could leak real data when in demo mode."""
    if not _is_demo():
        return None
    blocked = {"/api/config", "/api/balance"}
    if request.path in blocked:
        if request.path == "/api/balance":
            return jsonify({"cash": 638.47, "portfolio": 1120.74, "total": 1759.21})
        return jsonify({"error": "Not available in demo mode"}), 403
    if request.path == "/api/position-timeline":
        # Synthetic timeline from demo open positions (real endpoint uses Kalshi API)
        demo_timeline = [
            {"ticker": "KXNFP-26APR-U180K", "title": "Will nonfarm payrolls be under 180K in April?",
             "side": "yes", "count": 15, "exposure": 6.30, "close_time": "2026-04-26T16:00:00Z", "yes_price": 0.42},
            {"ticker": "KXEXECORDER-26APR-A12", "title": "Will Trump sign more than 12 executive orders in April?",
             "side": "yes", "count": 30, "exposure": 6.60, "close_time": "2026-04-30T23:59:00Z", "yes_price": 0.22},
            {"ticker": "KXGOLDW-26APR07-A2380", "title": "Will gold close above $2380 on Apr 7?",
             "side": "yes", "count": 12, "exposure": 5.76, "close_time": "2026-04-07T20:00:00Z", "yes_price": 0.48},
            {"ticker": "KXHIGHTNYC-26APR05-T68", "title": "Will the high temp in NYC be >68F on Apr 5?",
             "side": "yes", "count": 25, "exposure": 7.75, "close_time": "2026-04-05T23:59:00Z", "yes_price": 0.31},
            {"ticker": "KXINX-26APR07-B5850", "title": "Will S&P 500 close below 5850 on Apr 7?",
             "side": "yes", "count": 18, "exposure": 6.84, "close_time": "2026-04-07T20:00:00Z", "yes_price": 0.38},
            {"ticker": "KXCPI-26APR-B3.2", "title": "Will CPI be below 3.2% in April?",
             "side": "yes", "count": 20, "exposure": 7.00, "close_time": "2026-04-10T12:30:00Z", "yes_price": 0.35},
        ]
        return jsonify(demo_timeline)
    return None

# Lazy-init Kalshi client for balance checks
_kalshi_client = None

_series_cache = {}

def kalshi_web_url(client, ticker):
    try:
        market = client.get_market(ticker).get("market", {})
        event_ticker = market.get("event_ticker", "")
        event = client.get_event(event_ticker).get("event", {})
        series_ticker = event.get("series_ticker", "")
        if series_ticker and series_ticker not in _series_cache:
            series = client._request("GET", f"/series/{series_ticker}", authenticated=False)
            series_data = series.get("series", series)
            title = series_data.get("title", "")
            slug = re.sub(r"[^a-z0-9-]", "", title.lower().replace(" ", "-").replace("/", "-"))
            slug = re.sub(r"-+", "-", slug).strip("-")
            _series_cache[series_ticker] = slug
        slug = _series_cache.get(series_ticker, "")
        if series_ticker and slug and event_ticker:
            return f"https://kalshi.com/markets/{series_ticker.lower()}/{slug}/{event_ticker.lower()}"
    except Exception:
        pass
    return None


def get_kalshi():
    global _kalshi_client
    if _kalshi_client is None:
        from kalshi_client import KalshiClient
        _kalshi_client = KalshiClient()
    return _kalshi_client


def get_db(path):
    """Open a read-only SQLite connection."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _is_demo():
    """Check if demo mode is requested via ?demo=1 query param."""
    return request.args.get("demo") == "1" and os.path.exists(DB_DEMO)


def _db_pairs():
    """Return (bot_name, db_path) pairs, swapping to demo DB when requested."""
    if _is_demo():
        return [("demo", DB_DEMO)]
    return [("qwen", DB_QWEN), ("claude", DB_CLAUDE)]


def query_both(query_fn):
    """Run a query function against both DBs, tag results with bot name."""
    results = []
    for bot, path in _db_pairs():
        db = get_db(path)
        if not db:
            continue
        try:
            rows = query_fn(db)
            for r in rows:
                d = dict(r)
                d["_bot"] = bot
                results.append(d)
        finally:
            db.close()
    return results


# ── API Routes ────────────────────────────────────────────────────

@app.route("/api/status")
def status():
    """Bot status for both bots."""
    bots = {}
    for bot, path in _db_pairs():
        db = get_db(path)
        if not db:
            bots[bot] = {"active": False}
            continue
        try:
            latest_scan = db.execute(
                "SELECT timestamp FROM scans ORDER BY id DESC LIMIT 1"
            ).fetchone()

            open_trades = db.execute(
                "SELECT COUNT(*) as n FROM trades WHERE status IN ('pending', 'filled')"
            ).fetchone()

            today_trades = db.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN outcome = 'won' THEN 1 ELSE 0 END) as won,
                       SUM(CASE WHEN outcome = 'lost' THEN 1 ELSE 0 END) as lost,
                       COALESCE(SUM(pnl), 0.0) as pnl
                FROM trades WHERE date(timestamp) = date('now')
            """).fetchone()

            today_bot = db.execute("""
                SELECT COALESCE(SUM(pnl), 0.0) as pnl, COUNT(*) as trades
                FROM trades WHERE date(timestamp) = date('now') 
                AND COALESCE(source, 'bot') = 'bot'
            """).fetchone()

            today_manual = db.execute("""
                SELECT COALESCE(SUM(pnl), 0.0) as pnl, COUNT(*) as trades
                FROM trades WHERE date(timestamp) = date('now')
                AND source = 'manual'
            """).fetchone()

            today_dryrun = db.execute("""
                SELECT COUNT(*) as trades
                FROM trades WHERE date(timestamp) = date('now')
                AND source = 'dry_run'
            """).fetchone()

            total_scans = db.execute(
                "SELECT COUNT(*) as n FROM scans WHERE date(timestamp) = date('now')"
            ).fetchone()

            all_trades = db.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN outcome = 'won' THEN 1 ELSE 0 END) as won,
                       SUM(CASE WHEN outcome = 'lost' THEN 1 ELSE 0 END) as lost,
                       COALESCE(SUM(pnl), 0.0) as pnl
                FROM trades WHERE pnl IS NOT NULL
            """).fetchone()

            bots[bot] = {
                "active": True,
                "last_scan": latest_scan["timestamp"] if latest_scan else None,
                "open_trades": open_trades["n"],
                "today": {
                    "scans": total_scans["n"],
                    "trades": today_trades["total"],
                    "won": today_trades["won"] or 0,
                    "lost": today_trades["lost"] or 0,
                    "pnl": round(today_trades["pnl"], 2),
                    "bot_pnl": round(today_bot["pnl"], 2),
                    "bot_trades": today_bot["trades"],
                    "manual_pnl": round(today_manual["pnl"], 2),
                    "manual_trades": today_manual["trades"],
                    "dryrun_trades": today_dryrun["trades"],
                },
                "all_time": {
                    "trades": all_trades["total"],
                    "won": all_trades["won"] or 0,
                    "lost": all_trades["lost"] or 0,
                    "pnl": round(all_trades["pnl"], 2),
                },
            }
        finally:
            db.close()

    # Combined stats
    combined_open = sum(b.get("open_trades", 0) for b in bots.values() if isinstance(b, dict))
    combined_pnl = sum(b.get("today", {}).get("pnl", 0) for b in bots.values() if isinstance(b, dict))
    last_scans = [b.get("last_scan") for b in bots.values() if isinstance(b, dict) and b.get("last_scan")]

    # In demo mode, make last_scan appear current
    if _is_demo():
        from datetime import datetime, timezone
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for b in bots.values():
            if isinstance(b, dict) and b.get("active"):
                b["last_scan"] = now_str
        last_scans = [now_str]

    return jsonify({
        "bots": bots,
        "combined": {
            "open_trades": combined_open,
            "today_pnl": round(combined_pnl, 2),
            "last_scan": max(last_scans) if last_scans else None,
        },
    })


@app.route("/api/trades")
def trades():
    """Recent trades from both bots."""
    limit = min(int(request.args.get("limit", 50)), 200)
    bot_filter = request.args.get("bot")

    results = query_both(lambda db: db.execute("""
        SELECT t.id, t.timestamp, t.ticker, t.side, t.count, t.price,
               t.status, t.fill_price, t.pnl, t.outcome, t.order_id,
               COALESCE(t.source, 'bot') as source,
               a.bull_prob, a.bear_prob, a.arbiter_prob, a.arbiter_edge,
               a.arbiter_reasoning, a.arbiter_source,
               s.title as market_title
        FROM trades t
        LEFT JOIN analyses a ON t.analysis_id = a.id
        LEFT JOIN scans s ON a.scan_id = s.id
        ORDER BY t.id DESC LIMIT ?
    """, (limit,)).fetchall())

    if bot_filter:
        results = [r for r in results if r["_bot"] == bot_filter]

    results.sort(key=lambda r: r.get("timestamp", ""), reverse=True)

    # Enrich with web URLs
    url_cache = {}
    try:
        client = get_kalshi()
        for r in results[:limit]:
            ticker = r.get("ticker", "")
            if ticker and ticker not in url_cache:
                url_cache[ticker] = kalshi_web_url(client, ticker)
            r["web_url"] = url_cache.get(ticker)
    except Exception:
        pass

    return jsonify(results[:limit])


@app.route("/api/scans")
def scans():
    """Recent market scans from both bots."""
    limit = min(int(request.args.get("limit", 50)), 200)
    passed_only = request.args.get("passed") == "1"
    bot_filter = request.args.get("bot")

    def run(db):
        query = """
            SELECT id, timestamp, ticker, title, category,
                   market_yes_price, volume, close_time,
                   scanner_prob, scanner_confidence, scanner_reasoning,
                   passed_filter
            FROM scans
        """
        if passed_only:
            query += " WHERE passed_filter = 1"
        query += " ORDER BY id DESC LIMIT ?"
        return db.execute(query, (limit,)).fetchall()

    results = query_both(run)
    if bot_filter:
        results = [r for r in results if r["_bot"] == bot_filter]
    results.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return jsonify(results[:limit])


@app.route("/api/pnl")
def pnl():
    """Daily P&L history per bot."""
    results = []
    for bot, path in _db_pairs():
        db = get_db(path)
        if not db:
            continue
        try:
            rows = db.execute("""
                SELECT date(timestamp) as date,
                       COALESCE(source, 'bot') as source,
                       COUNT(*) as trades,
                       SUM(CASE WHEN outcome='won' THEN 1 ELSE 0 END) as won,
                       SUM(CASE WHEN outcome='lost' THEN 1 ELSE 0 END) as lost,
                       COALESCE(SUM(pnl), 0.0) as pnl
                FROM trades
                WHERE pnl IS NOT NULL AND COALESCE(source, 'bot') != 'dry_run'
                GROUP BY date(timestamp), COALESCE(source, 'bot')
                ORDER BY date(timestamp)
            """).fetchall()
            for r in rows:
                d = dict(r)
                d["_bot"] = bot
                results.append(d)
        finally:
            db.close()
    return jsonify(results)


@app.route("/api/balance")
def balance():
    """Live account balance and portfolio value from Kalshi API."""
    try:
        client = get_kalshi()
        bal = client.get_balance()
        return jsonify({
            "cash": round(bal.get("balance", 0) / 100, 2),
            "portfolio": round(bal.get("portfolio_value", 0) / 100, 2),
            "total": round((bal.get("balance", 0) + bal.get("portfolio_value", 0)) / 100, 2),
        })
    except Exception as e:
        return jsonify({"error": "Internal server error"}), 500


@app.route("/api/positions")
def positions():
    """Open positions from both bots, enriched with close times from Kalshi."""
    results = query_both(lambda db: db.execute("""
        SELECT t.id, t.timestamp, t.ticker, t.side, t.count, t.price,
               t.status, t.order_id,
               COALESCE(t.source, 'bot') as source,
               s.title, s.market_yes_price as current_price
        FROM trades t
        LEFT JOIN analyses a ON t.analysis_id = a.id
        LEFT JOIN scans s ON a.scan_id = s.id
        WHERE t.status IN ('pending', 'filled')
        ORDER BY t.timestamp DESC
    """).fetchall())

    # Enrich with close times and web URLs from Kalshi API (skip in demo — fake tickers)
    ticker_cache = {}
    if _is_demo():
        return jsonify(results)
    try:
        client = get_kalshi()
        for r in results:
            ticker = r.get("ticker", "")
            if ticker and ticker not in ticker_cache:
                try:
                    market_data = client.get_market(ticker)
                    market = market_data.get("market", market_data)
                    ticker_cache[ticker] = {
                        "close_time": market.get("close_time") or market.get("expiration_time"),
                        "web_url": kalshi_web_url(client, ticker),
                    }
                except Exception:
                    ticker_cache[ticker] = {"close_time": None, "web_url": None}
            cached = ticker_cache.get(ticker, {})
            r["close_time"] = cached.get("close_time")
            r["web_url"] = cached.get("web_url")
    except Exception:
        pass

    return jsonify(results)


@app.route("/api/exits")
def exits():
    """Recent position exits from both bots."""
    limit = min(int(request.args.get("limit", 50)), 200)
    results = query_both(lambda db: db.execute("""
        SELECT e.id, e.timestamp, e.ticker, e.reason,
               e.exit_price, e.pnl,
               t.side, t.count, t.price as entry_price
        FROM exits e
        LEFT JOIN trades t ON e.trade_id = t.id
        ORDER BY e.id DESC LIMIT ?
    """, (limit,)).fetchall())
    results.sort(key=lambda r: r.get("timestamp", ""), reverse=True)

    # Enrich with web URLs
    url_cache = {}
    try:
        client = get_kalshi()
        for r in results[:limit]:
            ticker = r.get("ticker", "")
            if ticker and ticker not in url_cache:
                url_cache[ticker] = kalshi_web_url(client, ticker)
            r["web_url"] = url_cache.get(ticker)
    except Exception:
        pass

    return jsonify(results[:limit])


@app.route("/api/analyses")
def analyses():
    """Recent analyses from both bots."""
    limit = min(int(request.args.get("limit", 30)), 100)
    bot_filter = request.args.get("bot")

    results = query_both(lambda db: db.execute("""
        SELECT a.id, a.timestamp, a.ticker,
               a.bull_prob, a.bull_confidence, a.bull_reasoning,
               a.bear_prob, a.bear_confidence, a.bear_reasoning,
               a.arbiter_prob, a.arbiter_edge, a.arbiter_trade,
               a.arbiter_side, a.arbiter_confidence,
               a.arbiter_reasoning, a.arbiter_source,
               s.title, s.market_yes_price, s.category
        FROM analyses a
        LEFT JOIN scans s ON a.scan_id = s.id
        ORDER BY a.id DESC LIMIT ?
    """, (limit,)).fetchall())

    if bot_filter:
        results = [r for r in results if r["_bot"] == bot_filter]
    results.sort(key=lambda r: r.get("timestamp", ""), reverse=True)

    # Enrich with web URLs
    url_cache = {}
    try:
        client = get_kalshi()
        for r in results[:limit]:
            ticker = r.get("ticker", "")
            if ticker and ticker not in url_cache:
                url_cache[ticker] = kalshi_web_url(client, ticker)
            r["web_url"] = url_cache.get(ticker)
    except Exception:
        pass

    return jsonify(results[:limit])


@app.route("/api/daily-stats")
def daily_stats():
    """Daily stats from both bots."""
    results = query_both(lambda db: db.execute(
        "SELECT * FROM daily_stats ORDER BY date DESC LIMIT 30"
    ).fetchall())
    return jsonify(results)


@app.route("/api/position-timeline")
def position_timeline():
    """Open positions with their close times for timeline visualization."""
    try:
        client = get_kalshi()
        positions = client.get_positions()
        live = [
            p for p in positions.get("market_positions", [])
            if float(p.get("position_fp", "0")) != 0
        ]

        timeline = []
        for p in live:
            ticker = p["ticker"]
            try:
                market_data = client.get_market(ticker)
                market = market_data.get("market", market_data)
                pos_count = float(p.get("position_fp", "0"))
                side = "yes" if pos_count > 0 else "no"
                web_url = kalshi_web_url(client, ticker)
                timeline.append({
                    "ticker": ticker,
                    "title": market.get("title", ticker),
                    "side": side,
                    "count": abs(pos_count),
                    "exposure": float(p.get("market_exposure_dollars", "0")),
                    "close_time": market.get("close_time") or market.get("expiration_time"),
                    "yes_price": market.get("yes_bid_dollars"),
                    "web_url": web_url,
                })
            except Exception:
                continue

        timeline.sort(key=lambda x: x.get("close_time") or "9999")
        return jsonify(timeline)
    except Exception as e:
        return jsonify({"error": "Internal server error"}), 500


@app.route("/api/sectors")
def sectors():
    """Sector heatmap data: trades, win rate, P&L by category."""
    results = query_both(lambda db: db.execute("""
        SELECT
            COALESCE(LOWER(s.category), 'unknown') as category,
            COUNT(*) as trades,
            SUM(CASE WHEN t.outcome='won' THEN 1 ELSE 0 END) as won,
            SUM(CASE WHEN t.outcome='lost' THEN 1 ELSE 0 END) as lost,
            SUM(CASE WHEN t.outcome IS NULL THEN 1 ELSE 0 END) as pending,
            COALESCE(SUM(t.pnl), 0.0) as pnl,
            AVG(a.arbiter_prob) as avg_prob,
            AVG(a.arbiter_edge) as avg_edge,
            AVG(t.price) as avg_entry
        FROM trades t
        JOIN analyses a ON t.analysis_id = a.id
        JOIN scans s ON a.scan_id = s.id
        GROUP BY LOWER(s.category)
    """).fetchall())

    # Merge results from both bots by category
    merged = {}
    for r in results:
        cat = r.get("category", "unknown") or "unknown"
        if cat not in merged:
            merged[cat] = {"category": cat, "trades": 0, "won": 0, "lost": 0,
                           "pending": 0, "pnl": 0.0, "avg_prob": 0, "avg_edge": 0,
                           "avg_entry": 0, "_count": 0}
        m = merged[cat]
        m["trades"] += r.get("trades", 0)
        m["won"] += r.get("won", 0)
        m["lost"] += r.get("lost", 0)
        m["pending"] += r.get("pending", 0)
        m["pnl"] = round(m["pnl"] + (r.get("pnl", 0) or 0), 2)
        m["avg_prob"] = (m["avg_prob"] * m["_count"] + (r.get("avg_prob", 0) or 0)) / (m["_count"] + 1)
        m["avg_edge"] = (m["avg_edge"] * m["_count"] + (r.get("avg_edge", 0) or 0)) / (m["_count"] + 1)
        m["avg_entry"] = (m["avg_entry"] * m["_count"] + (r.get("avg_entry", 0) or 0)) / (m["_count"] + 1)
        m["_count"] += 1

    out = []
    for m in merged.values():
        resolved = m["won"] + m["lost"]
        m["win_rate"] = round(m["won"] / resolved, 3) if resolved > 0 else None
        m["avg_prob"] = round(m["avg_prob"], 3)
        m["avg_edge"] = round(m["avg_edge"], 3)
        m["avg_entry"] = round(m["avg_entry"], 2)
        del m["_count"]
        out.append(m)

    out.sort(key=lambda x: x["trades"], reverse=True)
    return jsonify(out)


@app.route("/api/calibration")
def calibration():
    """Calibration summary from trade journal."""
    import sys
    sys.path.insert(0, str(PROJECT_ROOT))
    from trade_journal import TradeJournal

    results = {}
    for bot, path in _db_pairs():
        try:
            j = TradeJournal(path)
            cal = j.get_calibration_summary()
            results[bot] = cal
            j.close()
        except Exception as e:
            results[bot] = {"n": 0, "error": "Unavailable"}
    return jsonify(results)


@app.route("/api/calibration/predictions")
def calibration_predictions():
    """Individual predictions with outcomes for detailed calibration analysis."""
    results = query_both(lambda db: db.execute("""
        SELECT a.timestamp, a.ticker, a.arbiter_prob, a.arbiter_side,
               a.arbiter_edge, a.arbiter_confidence, a.arbiter_reasoning,
               a.arbiter_source, a.bull_prob, a.bear_prob,
               t.side as traded_side, t.outcome, t.pnl,
               t.count, t.price,
               COALESCE(t.source, 'bot') as trade_source,
               s.title, s.category, s.market_yes_price
        FROM analyses a
        JOIN trades t ON t.analysis_id = a.id
        LEFT JOIN scans s ON a.scan_id = s.id
        WHERE t.outcome IN ('won', 'lost', 'stopped')
        ORDER BY a.timestamp DESC
    """).fetchall())
    return jsonify(results)


@app.route("/api/enrichment/test")
def enrichment_test():
    """Test data enrichment on current market candidates. Cached for 5 minutes."""
    def _compute():
        import sys
        sys.path.insert(0, str(PROJECT_ROOT))
        from data_enrichment import enrich_market
        from kalshi_client import KalshiClient
        from scanner import MarketScanner

        client = KalshiClient()
        scanner = MarketScanner(client)
        candidates = scanner.scan()

        results = []
        seen_enrichments = set()
        for m in candidates[:30]:
            title = m.get("title", m.get("ticker", ""))
            enriched = enrich_market(m)
            # Deduplicate identical enrichment results
            if enriched and enriched not in seen_enrichments:
                seen_enrichments.add(enriched)
                web_url = kalshi_web_url(client, m.get("ticker", ""))
                results.append({
                    "ticker": m.get("ticker"),
                    "title": title,
                    "category": m.get("_category", ""),
                    "yes_price": m.get("yes_bid_dollars"),
                    "enrichment": enriched,
                    "has_data": True,
                    "web_url": web_url,
                })
            elif not enriched and len(results) < 20:
                web_url = kalshi_web_url(client, m.get("ticker", ""))
                results.append({
                    "ticker": m.get("ticker"),
                    "title": title,
                    "category": m.get("_category", ""),
                    "yes_price": m.get("yes_bid_dollars"),
                    "enrichment": "",
                    "has_data": False,
                    "web_url": web_url,
                })
            if len(results) >= 20:
                break

        enriched_count = sum(1 for m in candidates if enrich_market(m))
        return {
            "total_candidates": len(candidates),
            "enriched_count": enriched_count,
            "enrichment_rate": round(enriched_count / max(len(candidates), 1) * 100, 1),
            "samples": results,
        }
    try:
        result = cached("enrichment", 300, _compute)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": "Internal server error"}), 500





@app.route("/api/collective/status")
def collective_status():
    """Collective intelligence status."""
    try:
        import requests as _requests
        server = os.environ.get("COLLECTIVE_SERVER", "https://ktrader.dev/collective")
        resp = _requests.get(f"{server}/collective/v1/members/count", timeout=3)
        if resp.status_code == 200:
            return jsonify(resp.json())
        return jsonify({"error": "Collective server unreachable"}), 502
    except Exception as e:
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/variants")
def variant_stats():
    """A/B test variant performance."""
    try:
        stats = journal.get_variant_stats()
        return jsonify({"variants": stats})
    except Exception as e:
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/sports/arb")
def sports_arb():
    """Sports arbitrage opportunities. Cached for 2 minutes."""
    def _compute():
        import sys
        sys.path.insert(0, str(PROJECT_ROOT))
        from sports_scanner import scan_sports_arb
        from kalshi_client import KalshiClient
        client = KalshiClient()
        opps = scan_sports_arb(client)
        return {"opportunities": opps, "count": len(opps)}
    try:
        result = cached("sports_arb", 120, _compute)
        return jsonify(result)
    except ImportError:
        return jsonify({"opportunities": [], "count": 0, "error": "Sports scanner not installed"})
    except Exception as e:
        return jsonify({"opportunities": [], "count": 0, "error": "Unavailable"})


# Allowed config keys -- only these can be read/written via the API
_ALLOWED_KEYS = {
    "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH", "KALSHI_USE_DEMO",
    "DRY_RUN", "MAX_BET_AMOUNT", "MAX_DAILY_LOSS", "MAX_OPEN_POSITIONS",
    "KELLY_FRACTION", "MIN_EDGE", "SCAN_INTERVAL", "CIRCUIT_BREAKER_PCT",
    "CATEGORIES",
    "LLM_ENDPOINT", "LLM_ENDPOINT_FALLBACK", "LLM_MODEL", "LLM_TEMPERATURE",
    "LLM_MAX_TOKENS",
    "ANTHROPIC_API_KEY", "CLAUDE_MODEL", "XAI_API_KEY", "OPENAI_API_KEY",
    "USE_DUAL_ANALYSIS", "USE_SINGLE_AGENT",
    "FRED_API_KEY", "OPENWEATHER_API_KEY",
    "SPORTS_ENABLED", "SPORTS_SCAN_INTERVAL", "ODDS_API_KEY",
    "COLLECTIVE_ENABLED", "COLLECTIVE_SERVER", "COLLECTIVE_API_KEY",
    "DB_PATH", "LOG_LEVEL", "DASHBOARD_HOST", "DASHBOARD_PORT", "DASHBOARD_MODE",
}
_SECRET_KEYS = {
    "KALSHI_API_KEY_ID", "ANTHROPIC_API_KEY", "XAI_API_KEY",
    "OPENAI_API_KEY", "FRED_API_KEY", "OPENWEATHER_API_KEY",
    "COLLECTIVE_API_KEY", "ODDS_API_KEY",
}


@app.route("/api/config")
def get_config():
    """Read current .env config, masking secrets."""
    env_path = PROJECT_ROOT / ".env"
    config = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("'\"")
                if key in _ALLOWED_KEYS:
                    config[key] = value
    # Mask secret values -- show only that a value is set, not the value itself
    masked = {}
    for k, v in config.items():
        if k in _SECRET_KEYS and v:
            masked[k] = "*" * 16
        else:
            masked[k] = v
    return jsonify({"config": masked, "has_env": env_path.exists()})


@app.route("/api/config", methods=["POST"])
def save_config():
    """Save config values to .env file."""
    data = request.get_json()
    if not data or "config" not in data:
        return jsonify({"error": "Missing config"}), 400

    env_path = PROJECT_ROOT / ".env"
    template_path = PROJECT_ROOT / ".env.template"

    # Read existing .env or template as base
    existing_lines = []
    if env_path.exists():
        existing_lines = env_path.read_text().splitlines()
    elif template_path.exists():
        existing_lines = template_path.read_text().splitlines()

    updates = data["config"]
    # Reject unknown keys and sanitize values
    for key, val in list(updates.items()):
        if key not in _ALLOWED_KEYS:
            return jsonify({"error": f"Unknown config key: {key}"}), 400
        if not isinstance(val, str):
            return jsonify({"error": f"Invalid value for {key}"}), 400
        # Strip newlines and carriage returns to prevent env injection
        val = val.replace("\n", "").replace("\r", "")
        updates[key] = val

    updated_keys = set()
    new_lines = []

    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                val = updates[key]
                # Skip masked values (don't overwrite with masked placeholder)
                if val == "*" * 16:
                    new_lines.append(line)  # keep original
                else:
                    new_lines.append(f"{key}={val}")
                updated_keys.add(key)
                continue
        new_lines.append(line)

    # Append allowed keys not already in the file
    for key, val in updates.items():
        if key not in updated_keys and val != "*" * 16:
            new_lines.append(f"{key}={val}")

    env_path.write_text("\n".join(new_lines) + "\n")
    return jsonify({"status": "saved"})


import json as _json

_TUNING_PATH = PROJECT_ROOT / "data" / "tuning.json"
_TUNING_DEFAULTS = {
    "category_weights": {
        "climate": 1.0, "economics": 1.0, "financials": 1.0,
        "politics": 1.0, "science": 1.0, "health": 1.0,
        "entertainment": 1.0, "sports": 1.0, "world": 1.0,
    },
    "min_confidence": "low",
    "max_entry_price": 0.65,
    "category_exposure_limit": 3.00,
    "crowd_weight": 1.0,
    "side_policy": "yes_only",
}


def _load_tuning() -> dict:
    if _TUNING_PATH.exists():
        try:
            return _json.loads(_TUNING_PATH.read_text())
        except Exception:
            pass
    return dict(_TUNING_DEFAULTS)


@app.route("/api/tuning", methods=["GET"])
def get_tuning():
    return jsonify({"tuning": _load_tuning()})


@app.route("/api/tuning", methods=["POST"])
def save_tuning():
    data = request.get_json(silent=True) or {}
    tuning = data.get("tuning")
    if not tuning or not isinstance(tuning, dict):
        return jsonify({"error": "Missing tuning object"}), 400

    # Validate category_weights
    weights = tuning.get("category_weights", {})
    if not isinstance(weights, dict):
        return jsonify({"error": "Invalid category_weights"}), 400
    for cat, w in weights.items():
        try:
            w = float(w)
        except (TypeError, ValueError):
            return jsonify({"error": f"Invalid weight for {cat}"}), 400
        if w < 0 or w > 3.0:
            return jsonify({"error": f"Weight for {cat} must be 0-3.0"}), 400
        weights[cat] = round(w, 2)

    # Validate min_confidence
    conf = tuning.get("min_confidence", "low")
    if conf not in ("low", "medium", "high"):
        return jsonify({"error": "min_confidence must be low, medium, or high"}), 400

    # Validate max_entry_price
    try:
        mep = float(tuning.get("max_entry_price", 0.65))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid max_entry_price"}), 400
    if mep < 0.05 or mep > 0.95:
        return jsonify({"error": "max_entry_price must be 0.05-0.95"}), 400

    # Validate category_exposure_limit
    try:
        cel = float(tuning.get("category_exposure_limit", 3.0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid category_exposure_limit"}), 400
    if cel < 0.5 or cel > 100.0:
        return jsonify({"error": "category_exposure_limit must be 0.5-100.0"}), 400

    # Validate crowd_weight
    try:
        cw = float(tuning.get("crowd_weight", 1.0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid crowd_weight"}), 400
    if cw < 0 or cw > 2.0:
        return jsonify({"error": "crowd_weight must be 0-2.0"}), 400

    # Validate side_policy
    sp = tuning.get("side_policy", "yes_only")
    if sp not in ("yes_only", "both"):
        return jsonify({"error": "side_policy must be yes_only or both"}), 400

    clean = {
        "category_weights": weights,
        "min_confidence": conf,
        "max_entry_price": round(mep, 2),
        "category_exposure_limit": round(cel, 2),
        "crowd_weight": round(cw, 2),
        "side_policy": sp,
    }

    os.makedirs(_TUNING_PATH.parent, exist_ok=True)
    _TUNING_PATH.write_text(_json.dumps(clean, indent=2) + "\n")
    return jsonify({"status": "ok"})


@app.route("/dashboard/calibration")
@app.route("/calibration")
def calibration_page():
    return app.send_static_file("calibration.html")


@app.route("/dashboard")
def dashboard():
    return app.send_static_file("index.html")


_DEMO_BANNER = '''<div style="background:#1a1e33;border-bottom:1px solid #252a40;padding:10px 24px;
text-align:center;font-family:Inter,Segoe UI,system-ui,sans-serif;font-size:13px;color:#a5b4fc;
letter-spacing:0.02em;position:fixed;top:0;left:0;right:0;z-index:9999">
Demo dashboard with sample data.
<a href="/" style="color:#818cf8;text-decoration:underline">Home</a> &middot;
<a href="https://github.com/ktraderdev/ktrader" style="color:#818cf8;text-decoration:underline">View source</a> &middot;
<a href="/collective" style="color:#818cf8;text-decoration:underline">Join the collective</a>
</div>
<style>body{padding-top:38px !important}</style>'''

_DEMO_FETCH_PATCH = '''<script>
const _origFetch = window.fetch;
window.fetch = function(url, opts) {
  if (typeof url === 'string' && url.startsWith('/api/')) {
    url = url + (url.includes('?') ? '&' : '?') + 'demo=1';
  }
  return _origFetch.call(this, url, opts);
};
</script>'''


def _serve_demo(source_file, link_rewrites=None):
    """Serve a dashboard page with demo fetch patching and banner injected."""
    html_path = app.static_folder + "/" + source_file
    with open(html_path, "r") as f:
        html = f.read()
    # Inject fetch patch before first script
    html = html.replace("<script>", _DEMO_FETCH_PATCH + "\n<script>", 1)
    # Inject banner after <body>
    html = html.replace("<body>", "<body>" + _DEMO_BANNER, 1)
    # Rewrite links
    if link_rewrites:
        for old, new in link_rewrites.items():
            html = html.replace(old, new)
    return html


@app.route("/demo")
def demo_page():
    return _serve_demo("index.html", {
        'href="/calibration"': 'href="/demo/calibration"',
    })


@app.route("/demo/calibration")
def demo_calibration_page():
    return _serve_demo("calibration.html", {
        'href="/"': 'href="/demo"',
    })


# Public-facing pages only served on ktrader.dev (DASHBOARD_MODE=public)
_PUBLIC_MODE = os.environ.get("DASHBOARD_MODE", "") == "public"


@app.route("/setup")
def setup_page():
    if _PUBLIC_MODE:
        return app.send_static_file("setup.html")
    return app.send_static_file("index.html")


@app.route("/faq")
def faq_page():
    if _PUBLIC_MODE:
        return app.send_static_file("faq.html")
    return redirect("https://ktrader.dev/faq")


@app.route("/collective")
@app.route("/collective/")
def collective_page():
    if _PUBLIC_MODE:
        return app.send_static_file("collective.html")
    return redirect("https://ktrader.dev/collective")


@app.route("/")
def landing():
    if _PUBLIC_MODE:
        return app.send_static_file("landing.html")
    return app.send_static_file("index.html")


if __name__ == "__main__":
    app.run(
        host=os.environ.get("DASHBOARD_HOST", "127.0.0.1"),
        port=int(os.environ.get("DASHBOARD_PORT", "5100")),
        debug=False
    )
