"""
Kalshi Trading Bot — Configuration
All settings loaded from .env file or environment variables.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

# Load .env if present
def _load_dotenv(path: str = ".env"):
    env_path = Path(path)
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))

_load_dotenv()


@dataclass
class KalshiConfig:
    """Kalshi API configuration."""
    api_key_id: str = os.environ.get("KALSHI_API_KEY_ID", "")
    private_key_path: str = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")

    # Toggle between demo and production
    use_demo: bool = os.environ.get("KALSHI_USE_DEMO", "false").lower() == "true"

    @property
    def base_url(self) -> str:
        if self.use_demo:
            return "https://demo-api.kalshi.co/trade-api/v2"
        return "https://api.elections.kalshi.com/trade-api/v2"

    @property
    def ws_url(self) -> str:
        if self.use_demo:
            return "wss://demo-api.kalshi.co/trade-api/v2/ws"
        return "wss://api.elections.kalshi.com/trade-api/v2/ws"


@dataclass
class LLMConfig:
    """LM Studio / local LLM configuration."""
    # Mac Mini M4 running LM Studio (ethernet primary, WiFi fallback)
    endpoint: str = os.environ.get("LLM_ENDPOINT", "http://192.168.1.238:1234/v1")
    endpoint_fallback: str = os.environ.get("LLM_ENDPOINT_FALLBACK", "http://192.168.1.21:1234/v1")
    model: str = os.environ.get("LLM_MODEL", "qwen3.5-9b")
    temperature: float = float(os.environ.get("LLM_TEMPERATURE", "0.3"))
    max_tokens: int = int(os.environ.get("LLM_MAX_TOKENS", "1024"))

    # Fallback Claude API for arbiter decisions
    claude_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    claude_model: str = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
    use_claude_arbiter: bool = os.environ.get("USE_CLAUDE_ARBITER", "false").lower() == "true"
    use_claude_all: bool = os.environ.get("USE_CLAUDE_ALL", "false").lower() == "true"
    use_single_agent: bool = os.environ.get("USE_SINGLE_AGENT", "true").lower() == "true"
    use_dual_analysis: bool = os.environ.get("USE_DUAL_ANALYSIS", "true").lower() == "true"
    xai_api_key: str = os.environ.get("XAI_API_KEY", "")
    xai_model: str = os.environ.get("XAI_MODEL", "grok-3-mini")
    openai_api_key: str = os.environ.get("OPENAI_API_KEY", "")
    openai_model: str = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


@dataclass
class TradingConfig:
    """Risk management and trading parameters."""
    # Bankroll management
    max_bet_amount: float = float(os.environ.get("MAX_BET_AMOUNT", "5.0"))
    max_daily_loss: float = float(os.environ.get("MAX_DAILY_LOSS", "10.0"))
    max_open_positions: int = int(os.environ.get("MAX_OPEN_POSITIONS", "3"))

    # Kelly criterion
    kelly_fraction: float = float(os.environ.get("KELLY_FRACTION", "0.25"))

    # Minimum edge to trade (model prob - market prob)
    min_edge: float = float(os.environ.get("MIN_EDGE", "0.08"))

    # Market categories to scan (comma-separated)
    categories: list = field(default_factory=lambda: os.environ.get(
        "CATEGORIES", "climate,economics,financials"
    ).split(","))

    # Scan interval in seconds
    scan_interval: int = int(os.environ.get("SCAN_INTERVAL", "300"))

    # Circuit breaker — stop trading if daily loss exceeds this
    circuit_breaker_pct: float = float(os.environ.get("CIRCUIT_BREAKER_PCT", "0.15"))

    # Position manager — stop-loss triggers if price drops this % from entry
    stop_loss_pct: float = float(os.environ.get("STOP_LOSS_PCT", "0.50"))

    # Trailing stop — once in profit, exit if price drops this % from high
    trailing_stop_pct: float = float(os.environ.get("TRAILING_STOP_PCT", "0.30"))

    # Time-based exit — sell losing positions within this many hours of close
    time_exit_hours: float = float(os.environ.get("TIME_EXIT_HOURS", "2.0"))

    # Dry run — log trades without executing
    dry_run: bool = os.environ.get("DRY_RUN", "true").lower() == "true"



@dataclass
class SportsConfig:
    """Sports arbitrage scanner configuration."""
    enabled: bool = os.environ.get("SPORTS_ENABLED", "false").lower() == "true"
    # ESPN API is free, no key needed
    scan_interval: int = int(os.environ.get("SPORTS_SCAN_INTERVAL", "30"))
    odds_api_key: str = os.environ.get("ODDS_API_KEY", "")


@dataclass
class Config:
    """Master configuration."""
    """Master configuration."""
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    sports: SportsConfig = field(default_factory=SportsConfig)
    db_path: str = os.environ.get("DB_PATH", "trade_journal.db")
    log_level: str = os.environ.get("LOG_LEVEL", "INFO")


# Singleton
config = Config()
