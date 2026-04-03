#!/usr/bin/env python3
"""
Generate a realistic demo trade journal database for promotional screenshots.
Modest positive results: ~58% win rate, steady upward P&L with drawdowns.
"""
import sqlite3
import random
import os
from datetime import datetime, timedelta, timezone

random.seed(42)

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "demo_journal.db")

# ── Realistic market templates ─────────────────────────────────

MARKETS = {
    "economics": [
        ("KXNFP-26MAR-U{n}K", "Will nonfarm payrolls be under {n}K in March?"),
        ("KXUNRATE-26MAR-B{n}", "Will the unemployment rate be below {n}% in March?"),
        ("KXCPI-26MAR-B{n}", "Will CPI be below {n}% in March?"),
        ("KXGASPRICE-26MAR-B{n}", "Will the national avg gas price be below ${n} on Mar 31?"),
        ("KXGDP-26Q1-B{n}", "Will Q1 GDP growth be below {n}%?"),
        ("KXJOBLESS-26MAR27-U{n}K", "Will initial jobless claims be under {n}K?"),
        ("KXRETAIL-26MAR-B{n}", "Will retail sales growth beat {n}% in March?"),
        ("KXPCE-26MAR-B{n}", "Will core PCE be below {n}% in March?"),
        ("KXHOUSING-26MAR-A{n}K", "Will housing starts exceed {n}K in March?"),
    ],
    "financials": [
        ("KXINX-26MAR{d}-B{n}", "Will S&P 500 close below {n} on Mar {d}?"),
        ("KXNDX-26MAR{d}-A{n}", "Will Nasdaq close above {n} on Mar {d}?"),
        ("KXGOLDW-26MAR{d}-A{n}", "Will gold close above ${n} on Mar {d}?"),
        ("KXOILW-26MAR{d}-A{n}", "Will WTI oil close above ${n} on Mar {d}?"),
        ("KXBTC-26MAR{d}-A{n}K", "Will Bitcoin close above ${n}K on Mar {d}?"),
        ("KXTSLA-26MAR{d}-A{n}", "Will Tesla close above ${n} on Mar {d}?"),
        ("KXSILVERW-26MAR{d}-A{n}", "Will silver close above ${n} on Mar {d}?"),
    ],
    "climate": [
        ("KXHIGHTATL-26MAR{d}-T{n}", "Will the high temp in Atlanta be >{n}F on Mar {d}?"),
        ("KXHIGHTNYC-26MAR{d}-T{n}", "Will the high temp in NYC be >{n}F on Mar {d}?"),
        ("KXHIGHTLAX-26MAR{d}-T{n}", "Will the high temp in LA be >{n}F on Mar {d}?"),
        ("KXHIGHTCHI-26MAR{d}-T{n}", "Will the high temp in Chicago be >{n}F on Mar {d}?"),
        ("KXHIGHTDEN-26MAR{d}-T{n}", "Will the high temp in Denver be >{n}F on Mar {d}?"),
        ("KXLOWTNYC-26MAR{d}-T{n}", "Will the low temp in NYC be >{n}F on Mar {d}?"),
    ],
    "politics": [
        ("KXTRUMPX-26MAR{d}-{s}", "Will Trump post about {s} on X this week?"),
        ("KXEXECORDER-26MAR-A{n}", "Will Trump sign more than {n} executive orders in March?"),
        ("KXAPPROVAL-26MAR-B{n}", "Will Trump approval be below {n}% on Mar 31?"),
        ("KXSENATE-26MAR-{s}", "Will the Senate vote on {s} in March?"),
    ],
    "science": [
        ("KXSPACEX-26MAR{d}-{s}", "Will SpaceX launch {s} in March?"),
        ("KXFDA-26MAR-{s}", "Will FDA approve {s} in March?"),
    ],
}

PARAMS = {
    "economics": ["150", "175", "200", "225", "4.0", "4.2", "3.5", "3.8", "3.20", "3.40", "2.0", "2.5", "230", "250", "0.5", "1.0", "1400", "1450"],
    "financials": ["5700", "5750", "5800", "5850", "18000", "18500", "2300", "2350", "68", "70", "72", "85", "88", "90", "250", "270", "280", "30", "31", "32"],
    "climate": [str(n) for n in range(50, 85)],
    "politics": ["tariffs", "immigration", "NATO", "Ukraine", "TikTok", "5", "8", "10", "45", "48", "budget"],
    "science": ["Starship", "Falcon9", "CrewDragon", "Ozempic-generic", "Wegovy-OTC"],
}

ARBITER_SOURCES = ["claude-3.5", "grok-2", "multi-3/4", "multi-4/4", "openai-4o"]


def gen_ticker_title(category):
    tpl_ticker, tpl_title = random.choice(MARKETS[category])
    n = random.choice(PARAMS[category])
    d = str(random.randint(10, 31))
    ticker = tpl_ticker.replace("{n}", n).replace("{d}", d).replace("{s}", n)
    title = tpl_title.replace("{n}", n).replace("{d}", d).replace("{s}", n)
    return ticker, title


def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    if os.path.exists(OUT_PATH):
        os.remove(OUT_PATH)

    conn = sqlite3.connect(OUT_PATH)
    conn.executescript("""
        CREATE TABLE scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
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
            passed_filter INTEGER DEFAULT 0,
            enrichment_data TEXT
        );
        CREATE INDEX idx_scans_ticker ON scans(ticker);

        CREATE TABLE analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
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

        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
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
            outcome TEXT,
            source TEXT DEFAULT 'bot',
            model TEXT DEFAULT 'financial-llm'
        );
        CREATE INDEX idx_trades_ticker ON trades(ticker);
        CREATE INDEX idx_trades_status ON trades(status);

        CREATE TABLE daily_stats (
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

        CREATE TABLE exits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            trade_id INTEGER REFERENCES trades(id),
            ticker TEXT NOT NULL,
            reason TEXT NOT NULL,
            exit_price REAL,
            pnl REAL
        );
        CREATE INDEX idx_exits_trade ON exits(trade_id);
    """)

    # ── Generate 24 days of trading ──────────────────────────────
    start_date = datetime(2026, 2, 15, tzinfo=timezone.utc)
    end_date = datetime(2026, 4, 3, tzinfo=timezone.utc)
    n_days = (end_date - start_date).days + 1
    categories = ["economics", "financials", "climate", "politics", "science"]
    cat_weights = [0.30, 0.25, 0.25, 0.12, 0.08]

    # Win rates — modest edge, economics strongest
    cat_win_rates = {
        "economics": 0.66,
        "financials": 0.57,
        "climate": 0.55,
        "politics": 0.52,
        "science": 0.61,
    }

    balance = 500.0
    used_tickers = set()

    for day_offset in range(n_days):
        day = start_date + timedelta(days=day_offset)
        if day.weekday() >= 5:
            n_trades = random.randint(0, 3)
        else:
            n_trades = random.randint(3, 8)

        day_pnl = 0.0
        day_won = 0
        day_lost = 0
        day_scanned = random.randint(35, 100)
        day_analyzed = random.randint(n_trades + 2, n_trades + 10)

        for _ in range(n_trades):
            cat = random.choices(categories, weights=cat_weights, k=1)[0]

            # Unique ticker
            for __ in range(50):
                ticker, title = gen_ticker_title(cat)
                if ticker not in used_tickers:
                    used_tickers.add(ticker)
                    break

            hour = random.randint(9, 20)
            minute = random.randint(0, 59)
            ts = day.replace(hour=hour, minute=minute, second=random.randint(0, 59))
            ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
            close_time = (ts + timedelta(days=random.randint(1, 10))).isoformat()

            # Entry: $0.10 - $0.55
            entry_price = round(random.uniform(0.10, 0.55), 2)
            contracts = random.randint(3, max(3, int(8.00 / entry_price)))

            scanner_prob = round(min(0.92, entry_price + random.uniform(0.10, 0.22)), 2)
            bull_prob = round(min(0.95, scanner_prob + random.uniform(0.02, 0.10)), 2)
            bear_prob = round(max(0.08, scanner_prob - random.uniform(0.02, 0.10)), 2)
            arbiter_prob = round((bull_prob * 0.6 + bear_prob * 0.4) + random.uniform(-0.03, 0.03), 3)
            arbiter_prob = max(0.10, min(0.95, arbiter_prob))
            arbiter_edge = round(arbiter_prob - entry_price, 3)
            arbiter_source = random.choice(ARBITER_SOURCES)
            arbiter_conf = random.choice(["medium", "high"])

            won = random.random() < cat_win_rates[cat]
            if won:
                pnl = round(contracts * (1.0 - entry_price), 2)
                outcome = "won"
                day_won += 1
            else:
                pnl = round(-contracts * entry_price, 2)
                outcome = "lost"
                day_lost += 1

            day_pnl += pnl
            balance += pnl
            resolved_at = (ts + timedelta(days=random.randint(1, 5))).isoformat()

            # Scan
            conn.execute(
                "INSERT INTO scans (timestamp, ticker, title, category, market_yes_price, "
                "volume, close_time, scanner_prob, scanner_confidence, passed_filter) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (ts_str, ticker, title, cat, entry_price,
                 random.randint(500, 45000), close_time,
                 scanner_prob, random.choice(["medium", "high"]))
            )
            scan_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            # Analysis
            conn.execute(
                "INSERT INTO analyses (timestamp, scan_id, ticker, bull_prob, bull_confidence, "
                "bull_reasoning, bear_prob, bear_confidence, bear_reasoning, "
                "arbiter_prob, arbiter_edge, arbiter_trade, arbiter_side, "
                "arbiter_confidence, arbiter_reasoning, arbiter_source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'yes', ?, ?, ?)",
                (ts_str, scan_id, ticker,
                 bull_prob, "high" if bull_prob > 0.6 else "medium",
                 f"Favorable data alignment. {bull_prob:.0%} probability supported by enrichment signals.",
                 bear_prob, "medium" if bear_prob > 0.3 else "low",
                 f"Downside risks present. {bear_prob:.0%} probability reflects uncertainty.",
                 arbiter_prob, arbiter_edge, arbiter_conf,
                 f"Consensus at {arbiter_prob:.0%}. Edge of {arbiter_edge:.0%}. "
                 f"{'Strong' if arbiter_conf == 'high' else 'Moderate'} agreement across models.",
                 arbiter_source)
            )
            analysis_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            # Trade
            conn.execute(
                "INSERT INTO trades (timestamp, analysis_id, ticker, side, count, price, "
                "order_id, status, pnl, resolved_at, outcome, source) "
                "VALUES (?, ?, ?, 'yes', ?, ?, ?, 'closed', ?, ?, ?, 'bot')",
                (ts_str, analysis_id, ticker, contracts, entry_price,
                 f"demo-{random.randint(10000,99999)}", pnl, resolved_at, outcome)
            )

        # No-trade analyses
        for _ in range(day_analyzed - n_trades):
            cat = random.choices(categories, weights=cat_weights, k=1)[0]
            for __ in range(50):
                ticker, title = gen_ticker_title(cat)
                if ticker not in used_tickers:
                    used_tickers.add(ticker)
                    break

            ts = day.replace(hour=random.randint(9, 20), minute=random.randint(0, 59))
            ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
            market_price = round(random.uniform(0.25, 0.65), 2)

            conn.execute(
                "INSERT INTO scans (timestamp, ticker, title, category, market_yes_price, "
                "volume, close_time, scanner_prob, scanner_confidence, passed_filter) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (ts_str, ticker, title, cat, market_price,
                 random.randint(200, 25000),
                 (ts + timedelta(days=random.randint(1, 10))).isoformat(),
                 round(market_price + random.uniform(-0.04, 0.04), 2),
                 random.choice(["low", "medium"]))
            )
            scan_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            bp = round(random.uniform(0.30, 0.65), 2)
            brp = round(random.uniform(0.20, 0.50), 2)
            conn.execute(
                "INSERT INTO analyses (timestamp, scan_id, ticker, bull_prob, bull_confidence, "
                "bear_prob, bear_confidence, arbiter_prob, arbiter_edge, arbiter_trade, "
                "arbiter_side, arbiter_confidence, arbiter_source) "
                "VALUES (?, ?, ?, ?, 'low', ?, 'low', ?, ?, 0, 'yes', 'low', ?)",
                (ts_str, scan_id, ticker, bp, brp,
                 round((bp + brp) / 2, 2),
                 round(random.uniform(-0.03, 0.03), 3),
                 random.choice(ARBITER_SOURCES))
            )

        # Daily stats
        conn.execute(
            "INSERT INTO daily_stats (date, markets_scanned, analyses_run, trades_placed, "
            "trades_won, trades_lost, gross_pnl, balance_end) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (day.strftime("%Y-%m-%d"), day_scanned, day_analyzed,
             n_trades, day_won, day_lost, round(day_pnl, 2), round(balance, 2))
        )

    # ── Open positions (not yet resolved) ─────────────────────────
    now = datetime(2026, 4, 3, 14, 0, tzinfo=timezone.utc)
    open_markets = [
        ("economics", "KXNFP-26APR-U180K", "Will nonfarm payrolls be under 180K in April?", 0.42, 15),
        ("economics", "KXCPI-26APR-B3.2", "Will CPI be below 3.2% in April?", 0.35, 20),
        ("financials", "KXINX-26APR07-B5850", "Will S&P 500 close below 5850 on Apr 7?", 0.38, 18),
        ("climate", "KXHIGHTNYC-26APR05-T68", "Will the high temp in NYC be >68F on Apr 5?", 0.31, 25),
        ("politics", "KXEXECORDER-26APR-A12", "Will Trump sign more than 12 executive orders in April?", 0.22, 30),
        ("financials", "KXGOLDW-26APR07-A2380", "Will gold close above $2380 on Apr 7?", 0.48, 12),
    ]
    for cat, ticker, title, price, contracts in open_markets:
        ts = now - timedelta(hours=random.randint(2, 36))
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
        close_time = (now + timedelta(days=random.randint(3, 10))).isoformat()

        scanner_prob = round(price + random.uniform(0.12, 0.20), 2)
        arbiter_prob = round(price + random.uniform(0.14, 0.22), 2)

        conn.execute(
            "INSERT INTO scans (timestamp, ticker, title, category, market_yes_price, "
            "volume, close_time, scanner_prob, scanner_confidence, passed_filter) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'high', 1)",
            (ts_str, ticker, title, cat, price,
             random.randint(3000, 35000), close_time, scanner_prob)
        )
        scan_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        conn.execute(
            "INSERT INTO analyses (timestamp, scan_id, ticker, bull_prob, bull_confidence, "
            "bear_prob, bear_confidence, arbiter_prob, arbiter_edge, arbiter_trade, "
            "arbiter_side, arbiter_confidence, arbiter_reasoning, arbiter_source) "
            "VALUES (?, ?, ?, ?, 'high', ?, 'medium', ?, ?, 1, 'yes', 'high', ?, ?)",
            (ts_str, scan_id, ticker,
             round(arbiter_prob + 0.04, 2), round(arbiter_prob - 0.06, 2),
             arbiter_prob, round(arbiter_prob - price, 3),
             f"Strong multi-model consensus at {arbiter_prob:.0%}.",
             random.choice(ARBITER_SOURCES))
        )
        analysis_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        conn.execute(
            "INSERT INTO trades (timestamp, analysis_id, ticker, side, count, price, "
            "order_id, status, source) "
            "VALUES (?, ?, ?, 'yes', ?, ?, ?, 'pending', 'bot')",
            (ts_str, analysis_id, ticker, contracts, price,
             f"demo-{random.randint(10000,99999)}")
        )

    conn.commit()

    # ── Summary ───────────────────────────────────────────────────
    total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    won = conn.execute("SELECT COUNT(*) FROM trades WHERE outcome='won'").fetchone()[0]
    lost = conn.execute("SELECT COUNT(*) FROM trades WHERE outcome='lost'").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM trades WHERE outcome IS NULL").fetchone()[0]
    pnl = conn.execute("SELECT COALESCE(SUM(pnl), 0) FROM trades").fetchone()[0]

    print(f"Demo database: {OUT_PATH}")
    print(f"  Trades: {total} total ({won}W / {lost}L / {pending} open)")
    print(f"  Win rate: {won/(won+lost)*100:.1f}%")
    print(f"  Total P&L: ${pnl:+.2f}")

    cats = conn.execute(
        "SELECT s.category, COUNT(*), SUM(CASE WHEN t.outcome='won' THEN 1 ELSE 0 END), "
        "COALESCE(SUM(t.pnl),0) FROM trades t "
        "JOIN analyses a ON t.analysis_id=a.id JOIN scans s ON a.scan_id=s.id "
        "GROUP BY s.category"
    ).fetchall()
    print("  By category:")
    for cat, n, w, p in cats:
        print(f"    {cat}: {n} trades, {w}W, ${p:+.2f}")

    conn.close()


if __name__ == "__main__":
    main()
