# ktrader: Multi-Model Prediction Market Trading System

## What It Is

ktrader is an autonomous trading system for Kalshi, the regulated US prediction market exchange. It combines real-time data feeds, multi-model AI consensus, and sports arbitrage detection to identify and execute trades across economics, politics, science, climate, and sports markets.

The system doesn't guess. It fetches live S&P 500 prices, FRED employment data, NWS weather forecasts, and ESPN/DraftKings odds — then asks four independent AI models whether the market is mispriced. It only trades when they agree.

Live dashboard: **ktrader.dev**

---

## Architecture

**Four AI models vote on every trade decision:**

- **Qwen 2.5 14B** — runs locally on a Mac Mini M4 (free, 4-second responses)
- **Claude Sonnet** — Anthropic's reasoning model
- **Grok-3 Mini** — xAI's model with real-time web knowledge
- **GPT-4o Mini** — OpenAI's fast inference model

Qwen screens every market first (free). If it finds no edge, cloud models are never called — cutting API costs by 80%. When Qwen detects potential edge, all four models analyze the market with real data and vote. A trade only executes when the majority agrees on direction, side, and probability within 20%.

**Five real-time data sources feed every analysis:**

| Source | Data | Cost |
|--------|------|------|
| yfinance | S&P 500, Nasdaq, oil, gold, silver, copper, forex, crypto | Free |
| FRED (Federal Reserve) | Nonfarm payrolls, unemployment, jobless claims, CPI, gas prices, SOFR, mortgage rates | Free |
| NWS (National Weather Service) | Temperature and precipitation forecasts for 21 US cities | Free |
| ESPN/DraftKings | Moneylines, spreads, and totals for NBA, NFL, MLB, NHL, EPL | Free |
| OpenWeatherMap | Global weather conditions and forecasts | Free tier |

Early results show data enrichment transforms performance: trades made with real-time data win 26% of the time vs 0% without it. The models stop hallucinating ("gold is around $2,500") and start reasoning ("gold is at $4,670, above the $4,530 strike").

**Sports arbitrage runs a completely separate algorithm** — no AI guessing involved. It compares ESPN/DraftKings implied probabilities to Kalshi market prices in real time, using a 144-team mapping table auto-generated from ESPN's own APIs. When DraftKings says a team has a 75% win probability but Kalshi prices it at 40%, the system flags the discrepancy. City-name validation eliminates false matches (no more "Ningbo Rockets" matching "Houston Rockets").

---

## Trading Rules

The current strategy was derived from backtesting across 67 real trades, simulating every combination of side filtering, entry price caps, exit rules, and position management:

| Scenario | Trades | P&L |
|----------|--------|-----|
| What actually happened (stop-losses, both sides, any entry price) | 67 | -$61.34 |
| Same trades, hold to settlement | 58 | +$45.23 |
| Hold to settlement + cap entry at $0.50 + one per ticker | 22 | +$44.92 |
| Hold to settlement + cap at $0.50 + YES only | 13 | **+$50.90** |

Three rules survived the backtest:

1. **YES side only** — NO trades had a 0/40 win rate. The models consistently underestimate high-probability events.
2. **Entry price under $0.50** — cheap contracts with asymmetric payoff (risk $0.50 to win $0.50-0.99). Expensive entries ($0.80+) produced the largest losses.
3. **Hold to settlement** — no stop-losses, no trailing stops, no early exits. Prediction markets pay $1.00 if correct, $0.00 if wrong. Mid-market price swings are noise. Stop-losses alone destroyed $106 of value by killing positions that ultimately paid out.

Additional guardrails: 10% minimum edge required, per-ticker position limits, $3 category exposure caps, confidence gates (low-confidence trades blocked), and a circuit breaker that halts trading if daily losses exceed 10% of balance.

---

## Calibration Feedback Loop

Every prediction is tracked against its actual outcome. The system computes Brier scores, win rates by category and confidence level, and probability calibration curves. This data is injected into every AI prompt:

> *"Your NO trade history is 0/40 (0% win rate). Your predicted 20% probability actually resolves YES 75% of the time."*

The models see their own track record before making each decision. When Claude says "I'm 80% confident," the calibration data shows whether Claude's 80% estimates actually resolve 80% of the time — or 95% (systematically underconfident) or 50% (overconfident). This self-awareness measurably improves decision quality over time.

---

## The Dashboard

Two dashboards at **ktrader.dev**, protected by SSL and basic auth:

**Main Dashboard** — real-time portfolio view with P&L charts (separated by bot vs manual trades), position close timeline, trade history with filtering (Won/Lost/Open/Dry Run/Manual/Bot Only), position exits with stop-loss reasons, and recent analyses showing each model's probability estimate. Every ticker links directly to its Kalshi market page.

**Calibration Dashboard** — probability calibration chart (predicted vs actual), performance breakdowns by side/confidence/category, data enrichment coverage rates, sports arbitrage scanner with live ESPN odds, and a paginated prediction history with full reasoning tooltips.

Both dashboards auto-refresh every 30 seconds. The system runs as systemd services that survive reboots, with nginx reverse proxy, Let's Encrypt SSL, and no-cache headers for instant updates during development.

---

## What's Next

The system is currently in dry-run mode, accumulating paper trades to validate the strategy before going live. The target: 50+ resolved dry-run trades with a positive win rate and Brier score under 0.25. Additional data sources (FiveThirtyEight polling, Congress.gov bill tracking, Chrono24 watch prices) are identified and ready to integrate. WebSocket streaming for live sports is architecturally planned but not yet implemented.

The infrastructure is built. The models are voting. The data is flowing. Now we find out if four AIs with real data can actually beat a prediction market.

---

*Built with Claude, Qwen, Grok, GPT-4o, and a lot of iteration.*
