# ktrader

Autonomous prediction market trading system for [Kalshi](https://kalshi.com) with multi-model AI consensus, real-time data enrichment, and collective intelligence.

## How It Works

```
Scan Kalshi markets
    -> Enrich with real-time data (yfinance, FRED, NWS weather, ESPN odds)
    -> Multi-model AI consensus (Qwen local + Claude + Grok + OpenAI)
    -> Trade if majority agrees with sufficient edge
    -> Hold to settlement (Strategy D)
    -> Calibrate: feed outcomes back into future prompts
    -> Repeat
```

**Strategy D**: YES-only trades, entry under $0.65, hold to settlement. Backtested across 4 strategy variants. Stop-losses destroy value in binary prediction markets ($1 or $0 payout) -- mid-market fluctuations are noise.

**Multi-model consensus**: Qwen screens locally for free. If potential edge detected, Claude + Grok + OpenAI vote. Majority must agree on side, probabilities must be within 20%. Cuts cloud API costs ~80%.

**Calibration loop**: Brier scores, win rates by category/side/confidence, and winning/losing trade reasoning are injected into every LLM prompt. The system learns from its own history.

## Quick Start

### Docker

```bash
git clone https://github.com/ktraderdev/ktrader.git
cd ktrader
cp .env.template .env
# Edit .env: add your Kalshi API key + at least one LLM API key
# Place your kalshi-key.pem in the project root
docker-compose up -d
```

### Bare Metal

```bash
git clone https://github.com/ktraderdev/ktrader.git
cd ktrader
pip install -r requirements.txt
cp .env.template .env
# Edit .env with your credentials
python main.py --loop --dry-run
```

Dashboard: `python dashboard/api.py` (default: http://localhost:5100)

## Configuration

All configuration is via environment variables in `.env`. See `.env.template` for the full reference.

**Required:**
- `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH` -- your Kalshi API credentials
- At least one LLM: `ANTHROPIC_API_KEY`, `XAI_API_KEY`, `OPENAI_API_KEY`, or `LLM_ENDPOINT` (local)

**Recommended:**
- `FRED_API_KEY` -- free, dramatically improves economic market predictions
- `LLM_ENDPOINT` -- local Qwen via LM Studio for free screening (saves ~$4/day in API costs)

**Important:** The bot starts in `DRY_RUN=true` mode. It logs paper trades without placing real orders. Run in dry-run until you have 50+ resolved trades with a positive win rate before going live.

## Architecture

```
main.py              -- Orchestrator: scan -> analyze -> trade loop
llm_client.py        -- Multi-model LLM client (Qwen/Claude/Grok/OpenAI)
scanner.py           -- Kalshi market scanner with filters
data_enrichment.py   -- Real-time data from yfinance, FRED, NWS, etc.
position_manager.py  -- Settlement resolution, exit rules (disabled for Strategy D)
trade_journal.py     -- SQLite persistence: scans, analyses, trades, exits
config.py            -- Configuration dataclasses from .env
sports_scanner.py    -- ESPN/DraftKings odds -> Kalshi arbitrage
espn_teams.py        -- 443 team mappings for sports matching
dashboard/api.py     -- Flask API for the web dashboard
dashboard/static/    -- Dashboard UI (main + calibration pages)
collective/          -- Collective intelligence layer (see below)
```

## Collective Intelligence

ktrader includes an opt-in collective intelligence layer. Members share anonymized trade decisions -- predicted probability, side, confidence, and outcome -- with a central server. The aggregated "crowd signal" flows back into each member's LLM prompts.

**What gets shared:** market ticker, your bot's probability estimate, side (yes/no), confidence level, model source, and the eventual outcome. That's it.

**What is never shared:** entry price, P&L, balance, reasoning text, API keys, any PII.

**How it helps:** When 15 independent bots estimate a market at >70% YES and the price is 50%, that's a stronger signal than one bot alone. The crowd calibration data (historical accuracy by probability bucket) gives the LLM grounded evidence for trusting or discounting the consensus.

**Membership is capped.** The edge is inversely proportional to the number of participants. With hundreds of members, each gets meaningful alpha. At mass adoption, the collective signal converges to the market price itself. The cap keeps the collective useful. Apply at https://ktrader.dev/collective.

To enable: set `COLLECTIVE_ENABLED=true` and `COLLECTIVE_API_KEY=your-key` in `.env`.

## Data Enrichment Sources

| Source | Data | API Key Required |
|--------|------|-----------------|
| yfinance | S&P 500, Nasdaq, oil, gold, silver, copper, forex, crypto, Tesla | No |
| FRED | Payrolls, unemployment, CPI, gas prices, SOFR, mortgage rates, housing | Yes (free) |
| NWS | Weather forecasts for 21 US cities | No |
| ESPN | Live sports odds for NFL, NBA, MLB, NHL, MLS, EPL | No |
| OpenWeatherMap | Weather fallback | Yes (free) |

## Dashboard

Web dashboard at `http://localhost:5100` (or your configured host):

- **Main page**: P&L chart, position timeline, trade history with filters, live positions
- **Calibration page**: Brier score, probability calibration, performance by category/side/confidence, sports arb scanner, enrichment coverage

All market tickers link directly to their Kalshi market pages.

## A/B Testing

The bot includes a prompt variant testing system. Each scan cycle alternates between prompt variants (currently: `base` vs `traces`). Every trade is tagged with its variant. Use `/api/variants` to compare win rates between variants.

## License

MIT
