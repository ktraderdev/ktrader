"""
Kalshi Trading Bot — LLM Client
Calls LM Studio (local) for analysis, Claude API for final arbiter decisions.
"""
import json
import re
import logging
from typing import Optional

import requests

from config import config

_calibration_cache = {'text': '', 'ts': 0}

logger = logging.getLogger(__name__)

# ── Agent System Prompts ───────────────────────────────────────────

SCANNER_PROMPT = """You are a prediction market analyst. Given market data, estimate the 
probability of the YES outcome. Respond ONLY with valid JSON:
{"probability": 0.XX, "confidence": "low|medium|high", "reasoning": "one sentence"}

Rules:
- Be calibrated — a 70% estimate should resolve YES ~70% of the time.
- If you believe YES is unlikely, give a LOW probability (e.g. 0.10) to create NO trade edge.
- Base your estimate on the REAL DATA in the context section (orderbook, recent trades).
- If you lack concrete data to form a view, set confidence to "low".
- Do NOT simply echo the market price back — that provides zero edge.

IMPORTANT: The market price reflects the consensus of all traders. 
Start from the assumption that the market is correct.
Only deviate if you have SPECIFIC, VERIFIABLE information.
"I think this is overpriced" is NOT evidence.
"Current S&P 500 is at 5,642, above the 5,500 strike" IS evidence."""

BULL_PROMPT = """You are a bull-case analyst. Find reasons YES is MORE likely than the 
market price suggests. Look for signals the market is missing.
Respond ONLY with valid JSON:
{"probability": 0.XX, "confidence": "low|medium|high", "reasoning": "2-3 sentences"}"""

BEAR_PROMPT = """You are a bear-case analyst. Find reasons YES is LESS likely than the 
market price suggests. Look for risks the market ignores.
Respond ONLY with valid JSON:
{"probability": 0.XX, "confidence": "low|medium|high", "reasoning": "2-3 sentences"}"""

ARBITER_PROMPT = """You are the final decision-maker for a prediction market trading bot.
You receive bull and bear analyses. Decide whether to trade.

CRITICAL: Consider BOTH sides:
- If your probability is HIGHER than the YES price → buy YES (edge = your_prob - yes_price)
- If your probability is LOWER than the YES price → buy NO (edge = yes_price - your_prob)
Do NOT default to YES. Buying NO when the market overprices YES is equally valid.

STRATEGY: YES trades ONLY. NO trades are disabled (0/40 historical win rate).
Only recommend trades where the YES price is below $0.50.
Higher-priced entries have poor risk/reward in binary prediction markets.

Respond ONLY with valid JSON:
{
  "final_probability": 0.XX,
  "edge": 0.XX,
  "trade": true|false,
  "side": "yes|no|none",
  "confidence": "low|medium|high",
  "reasoning": "2-3 sentences"
}"""


class LLMClient:
    """Client for local LM Studio and optional Claude API."""

    # Prompt variants for A/B testing — only one variable changes at a time
    VARIANTS = {
        "base": {},  # current prompt, no changes
        "traces": {"inject_traces": True},  # add winning/losing trade examples
    }
    _current_variant_idx = 0

    def __init__(self):
        self.local_endpoint = config.llm.endpoint
        self.local_endpoint_fallback = getattr(config.llm, 'endpoint_fallback', None)
        self.local_model = config.llm.model
        self.temperature = config.llm.temperature
        self.max_tokens = config.llm.max_tokens
        self._local_available = None
        self._local_check_ts = 0  # Last time we checked local availability
        self._local_consecutive_fails = 0

    def next_variant(self) -> tuple:
        """Cycle to next prompt variant. Returns (name, config)."""
        names = list(self.VARIANTS.keys())
        name = names[self._current_variant_idx % len(names)]
        cfg = self.VARIANTS[name]
        self._current_variant_idx += 1
        return name, cfg

    def check_local(self) -> bool:
        """Check if LM Studio is reachable (tries primary, then WiFi fallback)."""
        import time
        self._local_check_ts = time.time()
        endpoints = [self.local_endpoint]
        if self.local_endpoint_fallback:
            endpoints.append(self.local_endpoint_fallback)
        for ep in endpoints:
            try:
                resp = requests.get(f"{ep}/models", timeout=5)
                if resp.status_code == 200:
                    if ep != self.local_endpoint:
                        logger.info(f"LM Studio: primary unreachable, using WiFi fallback ({ep})")
                    self.local_endpoint = ep  # Use whichever worked
                    if self._local_consecutive_fails > 0:
                        logger.info(f"LM Studio reconnected after {self._local_consecutive_fails} failures")
                    self._local_consecutive_fails = 0
                    self._local_available = True
                    models = resp.json().get("data", [])
                    ids = [m.get("id", "?") for m in models]
                    logger.info(f"LM Studio connected — models: {ids}")
                    return True
            except Exception as e:
                logger.debug(f"LM Studio not reachable at {ep}: {e}")
        logger.warning(f"LM Studio unreachable on all endpoints")
        self._local_available = False
        return False

    def _is_local_available(self) -> bool:
        """Check local availability, rechecking periodically if it was down."""
        import time
        if self._local_available is None:
            return self.check_local()
        if not self._local_available:
            # Recheck every 60s when down (not every call)
            if time.time() - self._local_check_ts > 60:
                return self.check_local()
        return self._local_available

    def _call_local(self, system_prompt: str, user_prompt: str,
                    temperature: float = None) -> Optional[dict]:
        """Call LM Studio's OpenAI-compatible API."""
        if not self._is_local_available():
            return None
        try:
            resp = requests.post(
                f"{self.local_endpoint}/chat/completions",
                json={
                    "model": self.local_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": temperature or self.temperature,
                    "max_tokens": self.max_tokens,
                },
                timeout=120,
            )
            resp.raise_for_status()
            self._local_consecutive_fails = 0
            content = resp.json()["choices"][0]["message"]["content"]
            return self._parse_json(content)
        except requests.exceptions.Timeout:
            logger.error("LM Studio timed out (120s)")
            self._local_consecutive_fails += 1
            self._local_available = False
            return None
        except Exception as e:
            logger.error(f"LM Studio call failed: {e}")
            self._local_consecutive_fails += 1
            self._local_available = False
            return None

    def _call_claude(self, system_prompt: str, user_prompt: str) -> Optional[dict]:
        """Call Claude API for arbiter decisions."""
        api_key = config.llm.claude_api_key
        if not api_key:
            logger.warning("No Claude API key — skipping arbiter")
            return None
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": config.llm.claude_model,
                    "max_tokens": 512,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_prompt}],
                },
                timeout=30,
            )
            resp.raise_for_status()
            content = resp.json()["content"][0]["text"]
            return self._parse_json(content)
        except Exception as e:
            logger.error(f"Claude API failed: {e}")
            return None

    def _call_grok(self, system_prompt: str, user_prompt: str) -> Optional[dict]:
        """Call xAI Grok API (OpenAI-compatible endpoint)."""
        api_key = config.llm.xai_api_key
        if not api_key:
            return None
        try:
            resp = requests.post(
                "https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": config.llm.xai_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 512,
                },
                timeout=30,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return self._parse_json(content)
        except Exception as e:
            logger.error(f"Grok API failed: {e}")
            return None

    def _call_openai(self, system_prompt: str, user_prompt: str) -> Optional[dict]:
        """Call OpenAI API."""
        api_key = config.llm.openai_api_key
        if not api_key:
            return None
        try:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": config.llm.openai_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 512,
                },
                timeout=30,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return self._parse_json(content)
        except Exception as e:
            logger.error(f"OpenAI API failed: {e}")
            return None

    def _parse_json(self, text: str) -> Optional[dict]:
        """Extract JSON from LLM response, handling fences and think blocks."""
        text = text.strip()
        # Strip <think>...</think> (Qwen thinking mode)
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        # Strip markdown fences
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end])
                except json.JSONDecodeError:
                    pass
            logger.error(f"Failed to parse JSON: {text[:200]}")
            return None

    # ── Dispatch ───────────────────────────────────────────────────

    def _call(self, system_prompt: str, user_prompt: str,
              temperature: float = None) -> Optional[dict]:
        """Route to Claude or local based on config, with fallback."""
        if config.llm.use_claude_all:
            return self._call_claude(system_prompt, user_prompt)

        result = self._call_local(system_prompt, user_prompt, temperature)
        if result is not None:
            return result

        # Fallback to Claude if local is down and we have an API key
        if config.llm.claude_api_key:
            logger.warning("Local LLM failed — falling back to Claude API")
            return self._call_claude(system_prompt, user_prompt)

        return None

    # ── Agent Methods ──────────────────────────────────────────────

    def scan_market(self, market_data: dict, context: str = "") -> Optional[dict]:
        """Agent 1: Quick probability estimate."""
        return self._call(SCANNER_PROMPT, self._fmt(market_data, context))

    def bull_case(self, market_data: dict, context: str = "") -> Optional[dict]:
        """Agent 2: Bull case analysis."""
        return self._call(BULL_PROMPT, self._fmt(market_data, context))

    def bear_case(self, market_data: dict, context: str = "") -> Optional[dict]:
        """Agent 3: Bear case analysis."""
        return self._call(BEAR_PROMPT, self._fmt(market_data, context))

    def arbiter(self, market_data: dict, scanner: dict,
                bull: dict, bear: dict,
                calibration_text: str = "") -> Optional[dict]:
        """Agent 4: Final trade decision."""
        cal_section = ""
        if calibration_text:
            cal_section = f"""

YOUR TRACK RECORD (use this to adjust your confidence):
{calibration_text}
"""

        prompt = f"""MARKET: {market_data.get('title', '?')}
Ticker: {market_data.get('ticker', '?')}
YES price: ${market_data.get('yes_bid_dollars', '?')}
Volume: {market_data.get('volume_fp', '?')}
Closes: {market_data.get('close_time', '?')}

SCANNER: {json.dumps(scanner)}
BULL: {json.dumps(bull)}
BEAR: {json.dumps(bear)}

Min edge required: {config.trading.min_edge}
Max bet: ${config.trading.max_bet_amount}
{cal_section}
Should we trade?"""

        if config.llm.use_claude_arbiter or config.llm.use_claude_all:
            result = self._call_claude(ARBITER_PROMPT, prompt)
            if result:
                result["arbiter_source"] = "claude"
                return result
        result = self._call_local(ARBITER_PROMPT, prompt, temperature=0.2)
        if result:
            result["arbiter_source"] = "local"
            return result
        # Fallback: if local arbiter failed, try Claude
        if config.llm.claude_api_key:
            logger.warning("Local arbiter failed — falling back to Claude")
            result = self._call_claude(ARBITER_PROMPT, prompt)
            if result:
                result["arbiter_source"] = "claude-fallback"
                return result
        return None

    def analyze_single(self, market_data: dict, context: str = "",
                       calibration_text: str = "", trade_traces: str = "",
                       crowd_text: str = "") -> Optional[dict]:
        """Single-call analysis: estimate probability + trade decision in one prompt."""
        yes_bid = market_data.get('yes_bid_dollars', '?')
        no_bid = market_data.get('no_bid_dollars', '?')
        
        cal_section = ""
        if calibration_text:
            cal_section = f"""
YOUR TRACK RECORD (use this to adjust your confidence):
{calibration_text}
"""
        if trade_traces:
            cal_section += f"\n{trade_traces}\n"
        if crowd_text:
            cal_section += f"\n{crowd_text}\n"

        prompt = f"""You are a prediction market analyst for a trading bot. Analyze this market and decide whether to trade.

MARKET: {market_data.get('title', '?')}
Ticker: {market_data.get('ticker', '?')}
YES price: ${yes_bid}
NO price: ${no_bid}
Volume: {market_data.get('volume_fp', '?')}
Closes: {market_data.get('close_time', '?')}

REAL-TIME DATA:
{context}

{cal_section}

Analyze step by step:
1. What is this market asking?
2. What does the real-time data tell you?
3. What is your probability estimate for YES? (be specific)
4. How does your estimate compare to the market price?
5. Is the edge large enough to trade? (minimum 10%)

IMPORTANT RULES:
- The market price is usually correct. You need SPECIFIC evidence to disagree.
- If you don't have concrete data, DO NOT TRADE.
- ONLY recommend YES trades. NO trades are disabled (0/40 historical win rate).
- Only recommend trades where YES price is below $0.65.
- If the YES price is above $0.65, do NOT trade.

Respond ONLY with valid JSON:
{{
  "probability": 0.XX,
  "edge": 0.XX,
  "trade": true|false,
  "side": "yes|no|none",
  "confidence": "low|medium|high",
  "reasoning": "2-3 sentences with SPECIFIC data points"
}}"""

        system_prompt = "You are a calibrated prediction market analyst. Respond only with valid JSON."
        result = self._call_claude(system_prompt, prompt)
        if result:
            # Map fields to match arbiter output format for compatibility
            result.setdefault("final_probability", result.get("probability", 0.5))
            result["arbiter_source"] = "claude-single"
        return result

    def analyze_dual(self, market_data: dict, context: str = "",
                     calibration_text: str = "", trade_traces: str = "",
                     crowd_text: str = "") -> Optional[dict]:
        """Run both Qwen and Claude on the same market. Only trade if they agree."""
        # Build the prompt (same for both)
        title = market_data.get("title", "?")
        ticker = market_data.get("ticker", "?")
        yes_bid = market_data.get("yes_bid_dollars", "?")
        no_bid = market_data.get("no_bid_dollars", "?")
        volume = market_data.get("volume_fp", "?")
        close_time = market_data.get("close_time", "?")

        cal_section = ""
        if calibration_text:
            cal_section = f"\nYOUR TRACK RECORD:\n{calibration_text}\n"
        if trade_traces:
            cal_section += f"\n{trade_traces}\n"
        if crowd_text:
            cal_section += f"\n{crowd_text}\n"

        prompt = f"""MARKET: {title}
Ticker: {ticker}
YES price: ${yes_bid}
NO price: ${no_bid}
Volume: {volume}
Closes: {close_time}

REAL-TIME DATA:
{context}
{cal_section}
Analyze this market. Estimate the probability of YES and decide whether to trade.

RULES:
- The market price is usually correct. You need SPECIFIC evidence to disagree.
- ONLY recommend YES trades. NO trades are disabled.
- Only recommend trades where YES price is below $0.65.
- If you don't have concrete data, DO NOT TRADE.

Respond ONLY with valid JSON:
{{"probability": 0.XX, "edge": 0.XX, "trade": true|false, "side": "yes|no|none", "confidence": "low|medium|high", "reasoning": "2-3 sentences"}}"""

        system = SCANNER_PROMPT  # reuse the calibrated system prompt

        # Step 1: Screen with cheapest model (Qwen local, fallback GPT-4o-mini)
        models = {}
        screen_result = self._call_local(system, prompt, temperature=0.3)
        screen_name = "qwen"
        if not screen_result:
            logger.info("  Qwen unavailable — screening with GPT-4o-mini")
            screen_result = self._call_openai(system, prompt)
            screen_name = "openai"
        
        if screen_result:
            models[screen_name] = screen_result
            s_trade = screen_result.get("trade", False)
            s_prob = float(screen_result.get("probability", 0.5))
            yes_price_f = float(yes_bid) if yes_bid != "?" else 0.5
            s_edge = abs(s_prob - yes_price_f)

            # If screen says no trade AND edge < 5%, skip expensive models
            if not s_trade and s_edge < 0.05:
                logger.info(f"  {screen_name:8s}: prob={s_prob:.2f} no edge ({s_edge:.0%}) — skipping other models")
                return {
                    "probability": s_prob, "final_probability": s_prob,
                    "edge": 0, "trade": False, "side": "none",
                    "confidence": "low",
                    "reasoning": f"{screen_name} screen: no edge ({s_edge:.0%}), other models skipped",
                    "arbiter_source": f"{screen_name}-screen",
                    f"{screen_name}_prob": s_prob,
                }

        # Step 2: Screen found potential edge — get remaining model votes
        if "claude" not in models:
            claude_result = self._call_claude(system, prompt)
            if claude_result:
                models["claude"] = claude_result

        if "grok" not in models:
            grok_result = self._call_grok(system, prompt)
            if grok_result:
                models["grok"] = grok_result

        if "openai" not in models:
            openai_result = self._call_openai(system, prompt)
            if openai_result:
                models["openai"] = openai_result

        if not models:
            logger.warning("Multi-model analysis: all models failed")
            return None

        if len(models) == 1:
            name, result = next(iter(models.items()))
            logger.info(f"  Multi-model: only {name} responded")
            result["arbiter_source"] = f"{name}-solo"
            return result

        # Log each model's view
        for name, result in models.items():
            prob = float(result.get("probability", 0.5))
            trade = result.get("trade", False)
            side = result.get("side", "none")
            logger.info(f"  {name:8s}: prob={prob:.2f} trade={trade} side={side}")

        # Consensus check: majority must agree on trade=True AND same side
        trade_yes = {n: r for n, r in models.items() if r.get("trade") and r.get("side") == "yes"}
        trade_no = {n: r for n, r in models.items() if r.get("trade") and r.get("side") == "no"}
        no_trade = {n: r for n, r in models.items() if not r.get("trade")}

        total = len(models)
        majority = total / 2

        if len(trade_yes) > majority:
            # Majority says trade YES
            probs = [float(r.get("probability", 0.5)) for r in trade_yes.values()]
            avg_prob = sum(probs) / len(probs)
            all_probs = {n: float(r.get("probability", 0.5)) for n, r in models.items()}
            prob_spread = max(probs) - min(probs)

            if prob_spread > 0.30:
                logger.info(f"  Multi-model: YES majority but high spread ({prob_spread:.0%}) — no trade")
            else:
                yes_price = float(yes_bid) if yes_bid != "?" else 0
                edge = abs(avg_prob - yes_price)
                voters = ", ".join(f"{n}={p:.0%}" for n, p in all_probs.items())
                claude_reasoning = models.get("claude", next(iter(models.values()))).get("reasoning", "")
                return {
                    "probability": avg_prob,
                    "final_probability": avg_prob,
                    "edge": edge,
                    "trade": True,
                    "side": "yes",
                    "confidence": "high" if len(trade_yes) == total else "medium",
                    "reasoning": f"CONSENSUS {len(trade_yes)}/{total} ({voters}): {claude_reasoning}",
                    "arbiter_source": f"multi-{len(trade_yes)}/{total}",
                    **{f"{n}_prob": float(r.get("probability", 0.5)) for n, r in models.items()},
                }

        # No majority for trade
        all_probs = {n: float(r.get("probability", 0.5)) for n, r in models.items()}
        voters = ", ".join(f"{n}={p:.0%}" for n, p in all_probs.items())
        logger.info(f"  Multi-model: no consensus ({len(trade_yes)} YES, {len(trade_no)} NO, {len(no_trade)} pass)")

        anchor = models.get("claude") or next(iter(models.values()))
        return {
            "probability": float(anchor.get("probability", 0.5)),
            "final_probability": float(anchor.get("probability", 0.5)),
            "edge": 0,
            "trade": False,
            "side": "none",
            "confidence": "low",
            "reasoning": f"NO CONSENSUS {len(trade_yes)}/{total} ({voters})",
            "arbiter_source": "multi-disagree",
            **{f"{n}_prob": float(r.get("probability", 0.5)) for n, r in models.items()},
        }

    def _fmt(self, m: dict, ctx: str = "") -> str:
        """Format market data into a prompt."""
        parts = [
            f"Market: {m.get('title', '?')}",
            f"Ticker: {m.get('ticker', '?')}",
            f"Category: {m.get('category', '?')}",
            f"YES price: ${m.get('yes_bid_dollars', '?')}",
            f"NO price: ${m.get('no_bid_dollars', '?')}",
            f"Volume: {m.get('volume_fp', '?')}",
            f"Close time: {m.get('close_time', '?')}",
        ]
        if sub := m.get("subtitle", ""):
            parts.append(f"Details: {sub}")
        if ctx:
            parts.append(f"\nContext:\n{ctx}")
        return "\n".join(parts)


llm_client = LLMClient()
