"""
ktrader Collective Intelligence Client
Fire-and-forget signal submission + crowd signal fetching.
"""
import uuid
import logging
import requests
from typing import Optional

logger = logging.getLogger("collective.client")


class CollectiveClient:
    """Client for the ktrader collective intelligence API."""

    def __init__(self, server_url: str, api_key: str, instance_id: str):
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.instance_id = instance_id
        self.timeout = 3  # seconds — never block trading

    def _headers(self) -> dict:
        return {"X-Collective-Key": self.api_key}

    def submit_signal(self, ticker: str, predicted_prob: float, side: str,
                      confidence: str, model_source: str, market_price: float,
                      category: str = "") -> Optional[str]:
        """Submit a prediction signal. Returns signal_id or None on failure."""
        signal_id = str(uuid.uuid4())
        try:
            resp = requests.post(
                f"{self.server_url}/collective/v1/signals",
                json={
                    "signal_id": signal_id,
                    "ticker": ticker,
                    "predicted_prob": predicted_prob,
                    "side": side,
                    "confidence": confidence,
                    "model_source": model_source,
                    "market_price": market_price,
                    "category": category,
                },
                headers=self._headers(),
                timeout=self.timeout,
            )
            if resp.status_code == 201:
                return signal_id
            logger.debug(f"Signal submit {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            logger.debug(f"Signal submit failed: {e}")
        return None

    def submit_outcome(self, signal_id: str, ticker: str, outcome: str) -> bool:
        """Submit a settlement outcome. Returns True on success."""
        try:
            resp = requests.post(
                f"{self.server_url}/collective/v1/outcomes",
                json={
                    "signal_id": signal_id,
                    "ticker": ticker,
                    "outcome": outcome,
                },
                headers=self._headers(),
                timeout=self.timeout,
            )
            return resp.status_code == 201
        except Exception as e:
            logger.debug(f"Outcome submit failed: {e}")
            return False

    def get_crowd_signal(self, ticker: str) -> Optional[dict]:
        """Get crowd consensus for a specific ticker."""
        try:
            resp = requests.get(
                f"{self.server_url}/collective/v1/crowd/{ticker}",
                headers=self._headers(),
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("n_signals", 0) > 0:
                    return data
        except Exception as e:
            logger.debug(f"Crowd signal fetch failed: {e}")
        return None

    def get_active_crowds(self) -> dict:
        """Get all active crowd signals (last 24h). Returns {ticker: crowd_data}."""
        try:
            resp = requests.get(
                f"{self.server_url}/collective/v1/crowd/active",
                headers=self._headers(),
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                return resp.json().get("active", {})
        except Exception as e:
            logger.debug(f"Active crowds fetch failed: {e}")
        return {}

    def format_crowd_text(self, crowd: dict) -> str:
        """Format a crowd signal dict into text for LLM prompt injection."""
        if not crowd or crowd.get("n_signals", 0) == 0:
            return ""
        n = crowd["n_signals"]
        bots = crowd.get("n_bots", n)
        avg = crowd["avg_prob"]
        std = crowd.get("std_dev", 0)
        yes_n = crowd.get("yes_count", 0)
        no_n = crowd.get("no_count", 0)
        conf = crowd.get("confidence_dist", {})

        lines = [
            f"CROWD SIGNAL ({bots} independent bots analyzed this market):",
            f"  Avg probability: {avg:.0%} YES (std dev: {std:.0%})",
            f"  Consensus: {yes_n}/{n} YES, {no_n}/{n} NO",
        ]
        if conf:
            lines.append(
                f"  Confidence: {conf.get('high', 0)} high, "
                f"{conf.get('medium', 0)} medium, {conf.get('low', 0)} low"
            )
        return "\n".join(lines)
