"""
Kalshi Trading Bot — Dashboard API
Read-only Flask API serving trade journal data from both bots.
"""
import sys
import re
import sqlite3
import os
from pathlib import Path
from flask import Flask, jsonify, request

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DB_QWEN = os.environ.get("DB_PATH", str(PROJECT_ROOT / "trade_journal.db"))
DB_CLAUDE = os.environ.get("DB_CLAUDE_PATH", str(PROJECT_ROOT / "trade_journal_claude.db"))

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


def query_both(query_fn):
    """Run a query function against both DBs, tag results with bot name."""
    results = []
    for bot, path in [("qwen", DB_QWEN), ("claude", DB_CLAUDE)]:
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
    for bot, path in [("qwen", DB_QWEN), ("claude", DB_CLAUDE)]:
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
    for bot, path in [("qwen", DB_QWEN), ("claude", DB_CLAUDE)]:
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
        return jsonify({"error": str(e)}), 500


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

    # Enrich with close times and web URLs from Kalshi API
    ticker_cache = {}
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
        return jsonify({"error": str(e)}), 500


@app.route("/api/calibration")
def calibration():
    """Calibration summary from trade journal."""
    import sys
    sys.path.insert(0, str(PROJECT_ROOT))
    from trade_journal import TradeJournal

    results = {}
    for bot, path in [("qwen", DB_QWEN), ("claude", DB_CLAUDE)]:
        try:
            j = TradeJournal(path)
            cal = j.get_calibration_summary()
            results[bot] = cal
            j.close()
        except Exception as e:
            results[bot] = {"n": 0, "error": str(e)}
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
        return jsonify({"error": str(e)}), 500





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
        return jsonify({"error": str(e)}), 500

@app.route("/api/variants")
def variant_stats():
    """A/B test variant performance."""
    try:
        stats = journal.get_variant_stats()
        return jsonify({"variants": stats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"opportunities": [], "count": 0, "error": str(e)})


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
                config[key] = value
    # Mask sensitive values for display
    masked = {}
    secret_keys = {"KALSHI_API_KEY_ID", "ANTHROPIC_API_KEY", "XAI_API_KEY",
                   "OPENAI_API_KEY", "FRED_API_KEY", "OPENWEATHER_API_KEY",
                   "COLLECTIVE_API_KEY", "ODDS_API_KEY"}
    for k, v in config.items():
        if k in secret_keys and v and len(v) > 8:
            masked[k] = v[:4] + "*" * (len(v) - 8) + v[-4:]
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
    updated_keys = set()
    new_lines = []

    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                # Skip masked values (don't overwrite with masked string)
                val = updates[key]
                if "*" * 4 not in val:
                    new_lines.append(f"{key}={val}")
                else:
                    new_lines.append(line)  # keep original
                updated_keys.add(key)
                continue
        new_lines.append(line)

    # Append any new keys not in the file
    for key, val in updates.items():
        if key not in updated_keys and "*" * 4 not in val:
            new_lines.append(f"{key}={val}")

    env_path.write_text("\n".join(new_lines) + "\n")
    return jsonify({"status": "saved"})


@app.route("/dashboard/calibration")
def calibration_page():
    return app.send_static_file("calibration.html")


@app.route("/dashboard")
def dashboard():
    return app.send_static_file("index.html")


@app.route("/setup")
def setup_page():
    return app.send_static_file("setup.html")


@app.route("/")
def landing():
    return app.send_static_file("landing.html")


if __name__ == "__main__":
    app.run(
        host=os.environ.get("DASHBOARD_HOST", "127.0.0.1"),
        port=int(os.environ.get("DASHBOARD_PORT", "5100")),
        debug=False
    )
