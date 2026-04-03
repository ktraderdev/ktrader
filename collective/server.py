"""
ktrader Collective Intelligence Server
FastAPI + SQLite. Receives anonymized signals, serves crowd aggregations.
Deploy behind nginx at /collective/.
"""
import os
import sqlite3
import hashlib
import logging
import statistics
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("collective")

# ── Config ─────────────────────────────────────────────────────

DB_PATH = os.environ.get("COLLECTIVE_DB", str(Path(__file__).resolve().parent.parent / "data" / "collective.db"))
MEMBER_CAP = int(os.environ.get("COLLECTIVE_MEMBER_CAP", "500"))
RATE_LIMIT = int(os.environ.get("COLLECTIVE_RATE_LIMIT", "100"))  # signals/hour

# ── Database ───────────────────────────────────────────────────

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;

        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key_hash TEXT UNIQUE NOT NULL,
            instance_id TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            is_active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS signals (
            signal_id TEXT PRIMARY KEY,
            instance_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            predicted_prob REAL NOT NULL,
            side TEXT NOT NULL,
            confidence TEXT NOT NULL,
            model_source TEXT NOT NULL,
            market_price REAL NOT NULL,
            category TEXT,
            submitted_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS outcomes (
            signal_id TEXT PRIMARY KEY,
            instance_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            outcome TEXT NOT NULL,
            submitted_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS waitlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);
        CREATE INDEX IF NOT EXISTS idx_signals_submitted ON signals(submitted_at);
        CREATE INDEX IF NOT EXISTS idx_signals_instance ON signals(instance_id);
        CREATE INDEX IF NOT EXISTS idx_outcomes_ticker ON outcomes(ticker);
    """)
    conn.close()

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

# ── Auth ───────────────────────────────────────────────────────

def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()

def verify_member(x_collective_key: str = Header(...)) -> str:
    """Verify API key and return instance_id."""
    key_hash = hash_key(x_collective_key)
    with get_db() as db:
        row = db.execute(
            "SELECT instance_id FROM members WHERE api_key_hash = ? AND is_active = 1",
            (key_hash,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")
    return row["instance_id"]

# ── Rate Limiting ──────────────────────────────────────────────

_rate_counts: dict[str, list[float]] = {}

def check_rate_limit(instance_id: str, limit: int = RATE_LIMIT):
    now = datetime.now(timezone.utc).timestamp()
    hour_ago = now - 3600
    window = _rate_counts.get(instance_id, [])
    window = [t for t in window if t > hour_ago]
    if len(window) >= limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    window.append(now)
    _rate_counts[instance_id] = window

# ── Models ─────────────────────────────────────────────────────

class SignalIn(BaseModel):
    signal_id: str
    ticker: str
    predicted_prob: float = Field(ge=0.0, le=1.0)
    side: str
    confidence: str
    model_source: str
    market_price: float = Field(ge=0.0, le=1.0)
    category: str = ""

class OutcomeIn(BaseModel):
    signal_id: str
    ticker: str
    outcome: str

# ── App ────────────────────────────────────────────────────────

app = FastAPI(title="ktrader Collective", version="1.0")

@app.on_event("startup")
def startup():
    init_db()

@app.post("/collective/v1/signals", status_code=201)
def submit_signal(signal: SignalIn, instance_id: str = Depends(verify_member)):
    check_rate_limit(instance_id)
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        try:
            db.execute(
                "INSERT INTO signals (signal_id, instance_id, ticker, predicted_prob, side, "
                "confidence, model_source, market_price, category, submitted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (signal.signal_id, instance_id, signal.ticker, signal.predicted_prob,
                 signal.side, signal.confidence, signal.model_source,
                 signal.market_price, signal.category, now)
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Signal already exists")
    return {"status": "ok"}

@app.post("/collective/v1/outcomes", status_code=201)
def submit_outcome(outcome: OutcomeIn, instance_id: str = Depends(verify_member)):
    with get_db() as db:
        try:
            db.execute(
                "INSERT INTO outcomes (signal_id, instance_id, ticker, outcome, submitted_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (outcome.signal_id, instance_id, outcome.ticker, outcome.outcome,
                 datetime.now(timezone.utc).isoformat())
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Outcome already submitted")
    return {"status": "ok"}

@app.get("/collective/v1/crowd/{ticker}")
def crowd_signal(ticker: str, instance_id: str = Depends(verify_member)):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    with get_db() as db:
        rows = db.execute(
            "SELECT predicted_prob, side, confidence, model_source, instance_id "
            "FROM signals WHERE ticker = ? AND submitted_at > ?",
            (ticker, cutoff)
        ).fetchall()

    if not rows:
        return {"ticker": ticker, "n_signals": 0}

    probs = [r["predicted_prob"] for r in rows]
    sides = [r["side"] for r in rows]
    confs = [r["confidence"] for r in rows]
    unique_bots = len(set(r["instance_id"] for r in rows))

    return {
        "ticker": ticker,
        "n_signals": len(rows),
        "n_bots": unique_bots,
        "avg_prob": round(statistics.mean(probs), 3),
        "median_prob": round(statistics.median(probs), 3),
        "std_dev": round(statistics.stdev(probs), 3) if len(probs) > 1 else 0,
        "yes_count": sides.count("yes"),
        "no_count": sides.count("no"),
        "confidence_dist": {
            "high": confs.count("high"),
            "medium": confs.count("medium"),
            "low": confs.count("low"),
        },
    }

@app.get("/collective/v1/crowd/active")
def active_crowds(instance_id: str = Depends(verify_member)):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with get_db() as db:
        tickers = db.execute(
            "SELECT DISTINCT ticker FROM signals WHERE submitted_at > ?",
            (cutoff,)
        ).fetchall()

    results = {}
    for row in tickers:
        ticker = row["ticker"]
        results[ticker] = crowd_signal(ticker, instance_id)
    return {"active": results, "count": len(results)}

@app.get("/collective/v1/calibration")
def crowd_calibration():
    """Public endpoint: crowd-level Brier score and calibration curve."""
    with get_db() as db:
        rows = db.execute("""
            SELECT s.predicted_prob, s.side, o.outcome
            FROM signals s
            JOIN outcomes o ON s.signal_id = o.signal_id
        """).fetchall()

    if not rows:
        return {"n": 0, "message": "No resolved signals yet"}

    n = len(rows)
    brier_sum = 0.0
    wins = 0
    buckets: dict[float, dict] = {}

    for r in rows:
        prob = r["predicted_prob"]
        won = r["outcome"] == "won"
        if won:
            wins += 1

        actual = 1.0 if (r["side"] == "yes" and won) or (r["side"] == "no" and not won) else 0.0
        brier_sum += (prob - actual) ** 2

        bucket = round(prob, 1)
        if bucket not in buckets:
            buckets[bucket] = {"n": 0, "actual_yes": 0}
        buckets[bucket]["n"] += 1
        buckets[bucket]["actual_yes"] += actual

    return {
        "n": n,
        "brier": round(brier_sum / n, 4),
        "win_rate": round(wins / n, 4),
        "calibration": {
            f"{b:.0%}": {"n": v["n"], "actual_yes_rate": round(v["actual_yes"] / v["n"], 3)}
            for b, v in sorted(buckets.items())
        },
    }

@app.get("/collective/v1/members/count")
def member_count():
    """Public endpoint: how many members, what's the cap."""
    with get_db() as db:
        count = db.execute("SELECT COUNT(*) FROM members WHERE is_active = 1").fetchone()[0]
    return {"members": count, "cap": MEMBER_CAP, "open": count < MEMBER_CAP}


# ── Admin: Register a new member ───────────────────────────────

class AdminRegisterRequest(BaseModel):
    api_key: str = Field(..., min_length=16)

@app.post("/collective/v1/admin/register")
def register_member(
    req: AdminRegisterRequest,
    admin_secret: str = Header(...)
):
    """Admin-only: register a new member. Requires COLLECTIVE_ADMIN_SECRET header."""
    expected = os.environ.get("COLLECTIVE_ADMIN_SECRET", "")
    if not expected or admin_secret != expected:
        raise HTTPException(status_code=403, detail="Forbidden")

    with get_db() as db:
        count = db.execute("SELECT COUNT(*) FROM members WHERE is_active = 1").fetchone()[0]
        if count >= MEMBER_CAP:
            raise HTTPException(status_code=403, detail=f"Membership capped at {MEMBER_CAP}")

        key_hash = hash_key(req.api_key)
        instance_id = key_hash[:16]

        try:
            db.execute(
                "INSERT INTO members (api_key_hash, instance_id) VALUES (?, ?)",
                (key_hash, instance_id)
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Member already registered")

    return {"status": "registered", "instance_id": instance_id}


# ── Self-service join ─────────────────────────────────────────

class JoinRequest(BaseModel):
    api_key: str = Field(..., min_length=16, description="Collective API key (you choose)")

@app.post("/collective/v1/join")
def join_collective(req: JoinRequest, x_real_ip: str = Header("anonymous")):
    """Public self-service: join the collective. Membership is capped."""
    check_rate_limit(f"join:{x_real_ip}", limit=10)
    with get_db() as db:
        count = db.execute("SELECT COUNT(*) FROM members WHERE is_active = 1").fetchone()[0]
        if count >= MEMBER_CAP:
            raise HTTPException(status_code=403, detail=f"Membership is full ({MEMBER_CAP}/{MEMBER_CAP})")

        key_hash = hash_key(req.api_key)
        instance_id = key_hash[:16]

        try:
            db.execute(
                "INSERT INTO members (api_key_hash, instance_id) VALUES (?, ?)",
                (key_hash, instance_id)
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Already registered")

    return {"status": "registered", "instance_id": instance_id, "members": count + 1, "cap": MEMBER_CAP}


# ── Waitlist ──────────────────────────────────────────────────

class WaitlistRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=254, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

@app.post("/collective/v1/waitlist")
def join_waitlist(req: WaitlistRequest, x_real_ip: str = Header("anonymous")):
    """Public: join the waitlist when membership is full."""
    check_rate_limit(f"waitlist:{x_real_ip}", limit=5)
    with get_db() as db:
        try:
            db.execute("INSERT INTO waitlist (email) VALUES (?)", (req.email,))
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Already on the waitlist")
        pos = db.execute("SELECT COUNT(*) FROM waitlist").fetchone()[0]
    return {"status": "waitlisted", "position": pos}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=5200)
