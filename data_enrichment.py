"""
Fetch real-world data for prediction market analysis.

Pattern-matches on market titles to pull relevant financial, weather,
and crypto data, then returns a plain-text context string.
"""

import logging
import os
from pathlib import Path

def _load_env():
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))

_load_env()
import re
import time
from datetime import datetime, timezone
from typing import Optional

import requests
import yfinance as yf

logger = logging.getLogger("data_enrichment")

# ---------------------------------------------------------------------------
# Caching layer — entries expire after 5 minutes
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 300  # seconds


def _cache_get(key: str) -> Optional[str]:
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.time() - ts > _CACHE_TTL:
        del _cache[key]
        return None
    return value


def _cache_set(key: str, value: str) -> None:
    _cache[key] = (time.time(), value)


# ---------------------------------------------------------------------------
# yfinance helper
# ---------------------------------------------------------------------------

def _yf_snapshot(ticker: str, label: str, period_5d: bool = True) -> str:
    """Return a one-line summary for a yfinance ticker.

    Includes current price, daily % change, and optionally a 5-day trend.
    Uses a 5-second timeout on the download call.
    """
    try:
        tk = yf.Ticker(ticker)
        hist = tk.history(period="5d", timeout=5)
        if hist.empty:
            logger.warning("No data returned for %s", ticker)
            return ""

        current = hist["Close"].iloc[-1]
        prev_close = hist["Close"].iloc[-2] if len(hist) >= 2 else current
        daily_pct = (current - prev_close) / prev_close * 100 if prev_close else 0

        trend_str = ""
        if period_5d and len(hist) >= 2:
            first = hist["Close"].iloc[0]
            five_day_pct = (current - first) / first * 100 if first else 0
            trend_str = f", {five_day_pct:+.1f}% 5-day"

        sign = "+" if daily_pct >= 0 else ""
        line = f"{label} ({ticker}): ${current:,.2f} ({sign}{daily_pct:.1f}% today{trend_str})"
        return line
    except Exception:
        logger.exception("Failed to fetch %s", ticker)
        return ""


# ---------------------------------------------------------------------------
# Weather helper (OpenWeatherMap free tier)
# ---------------------------------------------------------------------------

_CITY_MAP: dict[str, tuple[str, str]] = {
    "new york":    ("New York", "US"),
    "nyc":         ("New York", "US"),
    "los angeles": ("Los Angeles", "US"),
    "la":          ("Los Angeles", "US"),
    "chicago":     ("Chicago", "US"),
    "austin":      ("Austin", "US"),
    "houston":     ("Houston", "US"),
    "miami":       ("Miami", "US"),
    "seattle":     ("Seattle", "US"),
    "denver":      ("Denver", "US"),
    "boston":       ("Boston", "US"),
    "atlanta":     ("Atlanta", "US"),
    "dallas":      ("Dallas", "US"),
    "phoenix":     ("Phoenix", "US"),
    "san francisco": ("San Francisco", "US"),
    "washington":  ("Washington", "US"),
    "london":      ("London", "GB"),
    "tokyo":       ("Tokyo", "JP"),
    "paris":       ("Paris", "FR"),
    "berlin":      ("Berlin", "DE"),
    "sydney":      ("Sydney", "AU"),
}

# Ticker city codes for weather markets (e.g. KXTEMPNYCH -> NYC -> New York)
_TICKER_CITY_MAP = {
    'NYC': 'New York', 'NYK': 'New York', 'NY': 'New York',
    'SFO': 'San Francisco', 'SF': 'San Francisco',
    'DEN': 'Denver',
    'AUS': 'Austin',
    'CHI': 'Chicago',
    'HOU': 'Houston',
    'PHX': 'Phoenix', 'PHO': 'Phoenix',
    'DAL': 'Dallas',
    'MIA': 'Miami',
    'ATL': 'Atlanta',
    'BOS': 'Boston',
    'SEA': 'Seattle',
    'MIN': 'Minneapolis',
    'DET': 'Detroit',
    'POR': 'Portland',
    'NAS': 'Nashville',
    'LAX': 'Los Angeles', 'LA': 'Los Angeles',
    'SAN': 'San Antonio', 'SD': 'San Diego',
    'SJ': 'San Jose',
}


def _weather_for_city(city_query: str, country: str) -> str:
    """Current conditions + 3-day forecast from OpenWeatherMap."""
    api_key = os.environ.get("OPENWEATHER_API_KEY", "").strip()
    if not api_key:
        logger.info("OPENWEATHER_API_KEY not set; skipping weather fetch")
        return ""

    try:
        # Current weather
        url_current = "https://api.openweathermap.org/data/2.5/weather"
        params = {"q": f"{city_query},{country}", "appid": api_key, "units": "imperial"}
        resp = requests.get(url_current, params=params, timeout=5)
        resp.raise_for_status()
        cur = resp.json()

        temp_f = cur["main"]["temp"]
        desc = cur["weather"][0]["description"]
        humidity = cur["main"]["humidity"]

        lines = [
            f"Current: {temp_f:.0f}\u00b0F, {desc}, humidity {humidity}%",
        ]

        # 3-day forecast (free tier provides 5-day / 3-hour intervals)
        url_forecast = "https://api.openweathermap.org/data/2.5/forecast"
        resp_fc = requests.get(url_forecast, params=params, timeout=5)
        resp_fc.raise_for_status()
        fc = resp_fc.json()

        # Summarise one entry per day (pick noon-ish slot)
        seen_dates: set[str] = set()
        for entry in fc.get("list", []):
            dt_txt = entry["dt_txt"]  # "2026-03-31 12:00:00"
            date_str = dt_txt.split(" ")[0]
            hour = int(dt_txt.split(" ")[1].split(":")[0])
            if date_str in seen_dates or hour not in (12, 15):
                continue
            seen_dates.add(date_str)
            hi = entry["main"]["temp_max"]
            lo = entry["main"]["temp_min"]
            fc_desc = entry["weather"][0]["description"]
            lines.append(f"  {date_str}: {lo:.0f}\u2013{hi:.0f}\u00b0F, {fc_desc}")
            if len(seen_dates) >= 3:
                break

        return "\n".join(lines)
    except Exception:
        logger.exception("Weather fetch failed for %s", city_query)
        return ""


# ---------------------------------------------------------------------------
# Pattern-matching categories
# ---------------------------------------------------------------------------

_FINANCIAL_INDICES = {
    r"s&p|spx|s&p\s*500": ("^GSPC", "S&P 500"),
    r"nasdaq": ("^IXIC", "Nasdaq Composite"),
    r"djia|dow": ("^DJI", "Dow Jones"),
}

_COMMODITIES = {
    r"wti|(?<!\w)oil\b": ("CL=F", "WTI Crude Oil"),
    r"gold": ("GC=F", "Gold"),
    r"silver": ("SI=F", "Silver"),
    r"brent": ("BZ=F", "Brent Crude"),
    r"copper": ("HG=F", "Copper Futures"),
}

_TREASURY = {
    r"10.?y|10.year|^tnx": ("^TNX", "10Y Treasury Yield"),
    r"5.?y|5.year|^fvx": ("^FVX", "5Y Treasury Yield"),
    r"30.?y|30.year|^tyx": ("^TYX", "30Y Treasury Yield"),
    r"3.?m|3.month|^irx": ("^IRX", "3M Treasury Yield"),
}

_FOREX_PAIRS = {
    r"eur/?usd|usd/?eur": ("EURUSD=X", "EUR/USD"),
    r"usd/?jpy|jpy/?usd": ("USDJPY=X", "USD/JPY"),
    r"gbp/?usd|usd/?gbp": ("GBPUSD=X", "GBP/USD"),
}

_STOCKS = {
    r"tesla": ("TSLA", "Tesla"),
    r"apple": ("AAPL", "Apple"),
    r"amazon": ("AMZN", "Amazon"),
    r"google|alphabet": ("GOOGL", "Alphabet/Google"),
    r"microsoft": ("MSFT", "Microsoft"),
    r"nvidia": ("NVDA", "Nvidia"),
    r"meta": ("META", "Meta Platforms"),
    r"netflix": ("NFLX", "Netflix"),
    r"amd": ("AMD", "AMD"),
    r"intel": ("INTC", "Intel"),
}

_CRYPTO = {
    r"bitcoin|btc": ("BTC-USD", "Bitcoin"),
    r"ethereum|eth(?:ereum)?": ("ETH-USD", "Ethereum"),
}


def _match_dict(title_lower: str, mapping: dict) -> list[tuple[str, str]]:
    """Return list of (ticker, label) whose regex key matches the title."""
    hits = []
    for pattern, (ticker, label) in mapping.items():
        if re.search(pattern, title_lower):
            hits.append((ticker, label))
    return hits


# ---------------------------------------------------------------------------
# Trump approval scraper (FiveThirtyEight)
# ---------------------------------------------------------------------------

def _fetch_trump_approval() -> str:
    """Scrape Trump approval rating from FiveThirtyEight."""
    cache_key = "trump_approval"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        url = "https://projects.fivethirtyeight.com/polls/approval/donald-trump/"
        resp = requests.get(url, timeout=10, headers={"User-Agent": "ktrader-bot/1.0"})
        resp.raise_for_status()
        html = resp.text

        # Try BeautifulSoup first, fall back to regex
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            # Look for approval/disapproval values in the page
            text = soup.get_text(" ", strip=True)
            # Try to find patterns like "Approve 45.2%" or "Disapprove 51.3%"
            approve_match = re.search(r'(?:approve|approval)[:\s]*(\d+\.?\d*)%?', text, re.IGNORECASE)
            disapprove_match = re.search(r'(?:disapprove|disapproval)[:\s]*(\d+\.?\d*)%?', text, re.IGNORECASE)
            if approve_match and disapprove_match:
                result = (f"Trump Approval (FiveThirtyEight): "
                         f"Approve {approve_match.group(1)}%, "
                         f"Disapprove {disapprove_match.group(1)}%")
                _cache_set(cache_key, result)
                return result
        except ImportError:
            pass

        # Regex fallback on raw HTML
        approve_match = re.search(r'(?:approve|approval)[^0-9]*?(\d+\.?\d*)\s*%', html, re.IGNORECASE)
        disapprove_match = re.search(r'(?:disapprove|disapproval)[^0-9]*?(\d+\.?\d*)\s*%', html, re.IGNORECASE)
        if approve_match and disapprove_match:
            result = (f"Trump Approval (FiveThirtyEight): "
                     f"Approve {approve_match.group(1)}%, "
                     f"Disapprove {disapprove_match.group(1)}%")
            _cache_set(cache_key, result)
            return result

        logger.debug("Could not parse Trump approval from FiveThirtyEight page")
    except Exception as e:
        logger.debug(f"Failed to fetch Trump approval: {e}")
    return ""


# ---------------------------------------------------------------------------
# FDA PDUFA dates
# ---------------------------------------------------------------------------

_FDA_DATES = {
    "orforglipron": {
        "drug": "Orforglipron (Eli Lilly)",
        "pdufa_date": "2026-12-06",
        "indication": "Type 2 diabetes/obesity",
        "status": "Under FDA review",
    },
    # Add more as they appear on Kalshi
}


def _fetch_fda_info(title: str) -> str:
    """Return FDA info string if a known drug is mentioned in the title."""
    title_l = title.lower()
    for drug, info in _FDA_DATES.items():
        if drug in title_l:
            return (f"FDA Info: {info['drug']}, PDUFA date: {info['pdufa_date']}, "
                    f"Indication: {info['indication']}, Status: {info['status']}")
    return ""


# ---------------------------------------------------------------------------
# Shipping / trade chokepoint context
# ---------------------------------------------------------------------------

_SHIPPING_CONTEXT = {
    "hormuz": ("Strait of Hormuz: ~25-35 daily transit calls typical. "
               "Major oil chokepoint. Disruptions from Iran tensions can reduce traffic. "
               "Recent average: ~30 calls/day."),
    "suez": ("Suez Canal: ~50-60 daily transits typical. "
             "Houthi attacks since late 2023 caused significant rerouting."),
    "panama": ("Panama Canal: Reduced capacity due to drought. "
               "~25-30 daily transits vs normal 35-40."),
}

# ---------------------------------------------------------------------------
# AI benchmark rankings
# ---------------------------------------------------------------------------

_AI_RANKINGS = """AI Model Rankings (Chatbot Arena, March 2026):
1. GPT-4.5 (OpenAI) - ELO 1320
2. Claude Opus 4 (Anthropic) - ELO 1310
3. Gemini 2.5 Pro (Google) - ELO 1290
4. Grok-3 (xAI) - ELO 1275
5. Llama 4 (Meta) - ELO 1250
Note: Rankings change frequently. Check lmarena.ai for latest."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _fetch_fred(series_id: str, label: str) -> str:
    """Fetch latest value from FRED API."""
    api_key = os.environ.get("FRED_API_KEY", "")
    if not api_key:
        return ""
    cache_key = f"fred_{series_id}"
    if cache_key in _cache and (time.time() - _cache[cache_key][0]) < 300:
        return _cache[cache_key][1]
    try:
        r = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": series_id, "api_key": api_key, "file_type": "json",
                    "sort_order": "desc", "limit": 5},
            timeout=5
        )
        r.raise_for_status()
        obs = r.json().get("observations", [])
        if obs:
            latest = obs[0]
            prev = obs[1] if len(obs) > 1 else None
            result = f"{label}: {latest['value']} (as of {latest['date']})"
            if prev and prev['value'] != '.' and latest['value'] != '.':
                try:
                    change = float(latest['value']) - float(prev['value'])
                    result += f", prev: {prev['value']} (change: {change:+.1f})"
                except ValueError:
                    pass
            _cache[cache_key] = (time.time(), result)
            return result
    except Exception as e:
        logger.debug(f"FRED fetch failed for {series_id}: {e}")
    return ""



_NWS_COORDS = {
    "New York": (40.7128, -74.0060), "Los Angeles": (34.0522, -118.2437),
    "Chicago": (41.8781, -87.6298), "Houston": (29.7604, -95.3698),
    "Phoenix": (33.4484, -112.0740), "Philadelphia": (39.9526, -75.1652),
    "San Antonio": (29.4241, -98.4936), "San Diego": (32.7157, -117.1611),
    "Dallas": (32.7767, -96.7970), "San Jose": (37.3382, -121.8863),
    "Austin": (30.2672, -97.7431), "Denver": (39.7392, -104.9903),
    "San Francisco": (37.7749, -122.4194), "Miami": (25.7617, -80.1918),
    "Atlanta": (33.7490, -84.3880), "Boston": (42.3601, -71.0589),
    "Seattle": (47.6062, -122.3321), "Minneapolis": (44.9778, -93.2650),
    "Nashville": (36.1627, -86.7816), "Portland": (45.5152, -122.6784),
    "Detroit": (42.3314, -83.0458),
}

def _nws_weather(city_name: str) -> str:
    """Fetch weather from NWS API (free, no key needed)."""
    coords = _NWS_COORDS.get(city_name)
    if not coords:
        return ""
    try:
        # Step 1: Get forecast URL from coordinates
        r = requests.get(f"https://api.weather.gov/points/{coords[0]},{coords[1]}",
                        headers={"User-Agent": "ktrader-bot"}, timeout=5)
        r.raise_for_status()
        forecast_url = r.json()["properties"]["forecast"]
        
        # Step 2: Get forecast
        r2 = requests.get(forecast_url, headers={"User-Agent": "ktrader-bot"}, timeout=5)
        r2.raise_for_status()
        periods = r2.json()["properties"]["periods"]
        
        lines = []
        for p in periods[:3]:  # Next 3 periods (today, tonight, tomorrow)
            temp = p.get("temperature", "?")
            unit = p.get("temperatureUnit", "F")
            short = p.get("shortForecast", "")
            name = p.get("name", "")
            precip = p.get("probabilityOfPrecipitation", {}).get("value")
            precip_str = f", {precip}% precip" if precip is not None else ""
            lines.append(f"  {name}: {temp}{unit}, {short}{precip_str}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug(f"NWS weather failed for {city_name}: {e}")
        return ""

def enrich_market(market: dict) -> str:
    """Return a plain-text context string with real data relevant to this market."""
    title = market.get("title", "")
    if not title:
        return ""

    title_lower = title.lower()

    cache_key = f"enrich:{title_lower}"
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.debug("Cache hit for %r", title_lower)
        return cached

    lines: list[str] = []
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # --- Financial indices ---
    for ticker, label in _match_dict(title_lower, _FINANCIAL_INDICES):
        snap = _yf_snapshot(ticker, label)
        if snap:
            lines.append(snap)

    # --- Commodities ---
    for ticker, label in _match_dict(title_lower, _COMMODITIES):
        snap = _yf_snapshot(ticker, label)
        if snap:
            lines.append(snap)

    # --- Treasury yields ---
    has_treasury = re.search(r"treasury|yield|ust|par\s*yield", title_lower)
    if has_treasury:
        for ticker, label in _match_dict(title_lower, _TREASURY):
            snap = _yf_snapshot(ticker, label, period_5d=True)
            if snap:
                lines.append(snap)
        # If generic treasury mention but no specific maturity matched, fetch all
        if not lines:
            for _pat, (ticker, label) in _TREASURY.items():
                snap = _yf_snapshot(ticker, label, period_5d=True)
                if snap:
                    lines.append(snap)

    # --- Forex ---
    for ticker, label in _match_dict(title_lower, _FOREX_PAIRS):
        snap = _yf_snapshot(ticker, label)
        if snap:
            lines.append(snap)

    # --- Individual stocks ---
    for ticker, label in _match_dict(title_lower, _STOCKS):
        snap = _yf_snapshot(ticker, label)
        if snap:
            lines.append(snap)

    # --- Crypto ---
    for ticker, label in _match_dict(title_lower, _CRYPTO):
        snap = _yf_snapshot(ticker, label)
        if snap:
            lines.append(snap)

    # --- Weather ---
    weather_trigger = re.search(r"temperature|rain|\u00b0|forecast|weather", title_lower)
    found_city = False
    if weather_trigger or any(city in title_lower for city in _CITY_MAP):
        for city_key, (city_query, country) in _CITY_MAP.items():
            if city_key in title_lower:
                found_city = True
                w = _weather_for_city(city_query, country)
                if w:
                    lines.append(f"Weather for {city_query}:")
                    lines.append(w)
                else:
                    # Fallback: NWS API (free, no key needed)
                    nws = _nws_weather(city_query)
                    if nws:
                        lines.append(f"Weather for {city_query} (NWS):")
                        lines.append(nws)
                break  # one city per market

        # Fallback: extract city from ticker if no city found in title
        if not found_city and weather_trigger:
            ticker_str = market.get("ticker", "").upper()
            # Sort by code length descending to match longer codes first (e.g. NYC before NY)
            for code in sorted(_TICKER_CITY_MAP, key=len, reverse=True):
                if code in ticker_str:
                    city_name = _TICKER_CITY_MAP[code]
                    # Look up city in _CITY_MAP for OpenWeatherMap
                    city_lower = city_name.lower()
                    city_entry = _CITY_MAP.get(city_lower)
                    if city_entry:
                        cq, cc = city_entry
                        w = _weather_for_city(cq, cc)
                        if w:
                            lines.append(f"Weather for {city_name} (from ticker):")
                            lines.append(w)
                        else:
                            nws = _nws_weather(city_name)
                            if nws:
                                lines.append(f"Weather for {city_name} (NWS, from ticker):")
                                lines.append(nws)
                    elif city_name in _NWS_COORDS:
                        nws = _nws_weather(city_name)
                        if nws:
                            lines.append(f"Weather for {city_name} (NWS, from ticker):")
                            lines.append(nws)
                    found_city = True
                    break

    # --- Trump approval ---
    if any(w in title_lower for w in ["trump", "approval", "truth social"]):
        try:
            approval = _fetch_trump_approval()
            if approval:
                lines.append(approval)
        except Exception as e:
            logger.debug(f"Trump approval fetch failed: {e}")

    # --- FDA PDUFA dates ---
    if any(w in title_lower for w in ["fda", "approve", "pdufa", "drug"]):
        try:
            fda = _fetch_fda_info(title)
            if fda:
                lines.append(fda)
        except Exception as e:
            logger.debug(f"FDA info lookup failed: {e}")

    # --- Shipping / trade chokepoints ---
    if any(w in title_lower for w in ["transit", "vessel", "hormuz", "suez", "panama", "shipping"]):
        for key, context in _SHIPPING_CONTEXT.items():
            if key in title_lower:
                lines.append(context)

    # --- AI benchmarks ---
    if any(w in title_lower for w in ["ai model", "chatbot", "benchmark", "llm", "top ai"]):
        lines.append(_AI_RANKINGS)

    # --- FRED Economic Data ---
    fred_series = []
    if any(w in title_lower for w in ["nonfarm", "payroll", "jobs be added", "jobs added"]):
        fred_series.append(("PAYEMS", "Nonfarm Payrolls (thousands)"))
    if any(w in title_lower for w in ["unemployment", "u-3"]):
        fred_series.append(("UNRATE", "Unemployment Rate (%)"))
    if any(w in title_lower for w in ["jobless", "claims"]):
        fred_series.append(("ICSA", "Initial Jobless Claims"))
    if any(w in title_lower for w in ["cpi", "inflation"]):
        fred_series.append(("CPIAUCSL", "CPI (All Urban Consumers)"))
    if any(w in title_lower for w in ["fed funds", "federal funds"]):
        fred_series.append(("FEDFUNDS", "Federal Funds Rate (%)"))
    if any(w in title_lower for w in ["gdp"]):
        fred_series.append(("GDP", "GDP (billions $)"))
    if any(w in title_lower for w in ["sofr"]):
        fred_series.append(("SOFR", "SOFR Rate (%)"))
    if any(w in title_lower for w in ["challenger", "job cuts", "layoff"]):
        fred_series.append(("ICSA", "Weekly Jobless Claims (proxy for labor market)"))
    if any(w in title_lower for w in ["pmi", "ism", "manufacturing"]):
        fred_series.append(("MANEMP", "Manufacturing Employment"))
    if any(w in title_lower for w in ["gas price"]):
        fred_series.append(("GASREGW", "Regular Gas Price ($/gallon)"))
    if any(w in title_lower for w in ["housing", "home price", "case-shiller", "house"]):
        fred_series.append(("CSUSHPINSA", "Case-Shiller Home Price Index"))
    if any(w in title_lower for w in ["mortgage", "30-year", "30 year"]):
        fred_series.append(("MORTGAGE30US", "30-Year Mortgage Rate (%)"))
    if any(w in title_lower for w in ["rent", "rental"]):
        fred_series.append(("CUUR0000SEHA", "CPI Rent Index"))
    for series_id, label in fred_series:
        fred_data = _fetch_fred(series_id, label)
        if fred_data:
            lines.append(fred_data)

    if not lines:
        _cache_set(cache_key, "")
        return ""

    result = "REAL-TIME DATA:\n" + "\n".join(lines) + f"\nLast updated: {now_utc}"
    _cache_set(cache_key, result)
    logger.info("Enriched market %r with %d data lines", title, len(lines))
    return result
