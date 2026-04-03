"""
Kalshi Trading Bot — Sports Arbitrage Scanner

Compares ESPN/DraftKings odds to Kalshi prediction market prices
to find arbitrage opportunities on sports markets.

Uses ESPN's free public scoreboard API (no API key required).
"""
import logging
import re
import time
from typing import Optional

import requests

from kalshi_client import KalshiClient
from config import config
from espn_teams import lookup_team, ESPN_TEAM_MAP

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache layer — 2-minute TTL (ESPN updates frequently)
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 120  # seconds


def _cache_get(key: str):
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.time() - ts > _CACHE_TTL:
        del _cache[key]
        return None
    return value


def _cache_set(key: str, value) -> None:
    _cache[key] = (time.time(), value)


# ---------------------------------------------------------------------------
# ESPN API endpoints (free, no key needed)
# ---------------------------------------------------------------------------
ESPN_SPORTS = {
    "nba": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "nfl": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "mlb": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
    "nhl": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard",
    "epl": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
    "ncaab": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard",
}

# Abbreviation -> city/region for matching
_ABBREV_MAP: dict[str, str] = {
    # NBA
    "ATL": "atlanta", "BOS": "boston", "BKN": "brooklyn", "CHA": "charlotte",
    "CHI": "chicago", "CLE": "cleveland", "DAL": "dallas", "DEN": "denver",
    "DET": "detroit", "GS": "golden state", "GSW": "golden state",
    "HOU": "houston", "IND": "indiana", "LAC": "la clippers", "LAL": "la lakers",
    "MEM": "memphis", "MIA": "miami", "MIL": "milwaukee", "MIN": "minnesota",
    "NO": "new orleans", "NOP": "new orleans", "NY": "new york", "NYK": "new york",
    "OKC": "oklahoma city", "ORL": "orlando", "PHI": "philadelphia",
    "PHX": "phoenix", "POR": "portland", "SAC": "sacramento", "SA": "san antonio",
    "SAS": "san antonio", "TOR": "toronto", "UTA": "utah", "WAS": "washington",
    "WSH": "washington",
    # NFL
    "ARI": "arizona", "BAL": "baltimore", "BUF": "buffalo", "CAR": "carolina",
    "CIN": "cincinnati", "GB": "green bay", "JAX": "jacksonville",
    "KC": "kansas city", "LV": "las vegas", "LAR": "la rams",
    "NE": "new england", "NO": "new orleans", "NYG": "new york giants",
    "NYJ": "new york jets", "PIT": "pittsburgh", "SEA": "seattle",
    "SF": "san francisco", "TB": "tampa bay", "TEN": "tennessee",
    # MLB
    "CWS": "chicago white sox", "COL": "colorado", "SD": "san diego",
    "STL": "st. louis", "TEX": "texas",
    # NHL
    "CBJ": "columbus", "EDM": "edmonton", "FLA": "florida", "MTL": "montreal",
    "NSH": "nashville", "NJ": "new jersey", "OTT": "ottawa", "VAN": "vancouver",
    "VGK": "vegas", "WPG": "winnipeg", "CGY": "calgary", "ANA": "anaheim",
    "SJ": "san jose",
}

# Common team-name aliases (lowercase) -> canonical full name
_TEAM_ALIASES: dict[str, str] = {
    # NBA
    "lakers": "los angeles lakers",
    "celtics": "boston celtics",
    "warriors": "golden state warriors",
    "bucks": "milwaukee bucks",
    "nuggets": "denver nuggets",
    "76ers": "philadelphia 76ers",
    "sixers": "philadelphia 76ers",
    "suns": "phoenix suns",
    "heat": "miami heat",
    "knicks": "new york knicks",
    "nets": "brooklyn nets",
    "bulls": "chicago bulls",
    "cavaliers": "cleveland cavaliers",
    "cavs": "cleveland cavaliers",
    "mavericks": "dallas mavericks",
    "mavs": "dallas mavericks",
    "timberwolves": "minnesota timberwolves",
    "wolves": "minnesota timberwolves",
    "thunder": "oklahoma city thunder",
    "clippers": "los angeles clippers",
    "raptors": "toronto raptors",
    "hawks": "atlanta hawks",
    "pacers": "indiana pacers",
    "magic": "orlando magic",
    "grizzlies": "memphis grizzlies",
    "pelicans": "new orleans pelicans",
    "kings": "sacramento kings",
    "spurs": "san antonio spurs",
    "rockets": "houston rockets",
    "pistons": "detroit pistons",
    "wizards": "washington wizards",
    "blazers": "portland trail blazers",
    "trail blazers": "portland trail blazers",
    "jazz": "utah jazz",
    "hornets": "charlotte hornets",
    # NFL
    "chiefs": "kansas city chiefs",
    "eagles": "philadelphia eagles",
    "49ers": "san francisco 49ers",
    "niners": "san francisco 49ers",
    "bills": "buffalo bills",
    "cowboys": "dallas cowboys",
    "ravens": "baltimore ravens",
    "bengals": "cincinnati bengals",
    "lions": "detroit lions",
    "dolphins": "miami dolphins",
    "jets": "new york jets",
    "patriots": "new england patriots",
    "packers": "green bay packers",
    "vikings": "minnesota vikings",
    "chargers": "los angeles chargers",
    "broncos": "denver broncos",
    "seahawks": "seattle seahawks",
    "steelers": "pittsburgh steelers",
    "texans": "houston texans",
    "bears": "chicago bears",
    "raiders": "las vegas raiders",
    "titans": "tennessee titans",
    "colts": "indianapolis colts",
    "jaguars": "jacksonville jaguars",
    "giants": "new york giants",
    "commanders": "washington commanders",
    "saints": "new orleans saints",
    "falcons": "atlanta falcons",
    "panthers": "carolina panthers",
    "buccaneers": "tampa bay buccaneers",
    "bucs": "tampa bay buccaneers",
    "rams": "los angeles rams",
    "cardinals": "arizona cardinals",
    # MLB
    "yankees": "new york yankees",
    "dodgers": "los angeles dodgers",
    "astros": "houston astros",
    "braves": "atlanta braves",
    "mets": "new york mets",
    "phillies": "philadelphia phillies",
    "red sox": "boston red sox",
    "cubs": "chicago cubs",
    "white sox": "chicago white sox",
    "padres": "san diego padres",
    "mariners": "seattle mariners",
    "guardians": "cleveland guardians",
    "orioles": "baltimore orioles",
    "rangers": "texas rangers",
    "twins": "minnesota twins",
    "brewers": "milwaukee brewers",
    "blue jays": "toronto blue jays",
    "rays": "tampa bay rays",
    "diamondbacks": "arizona diamondbacks",
    "d-backs": "arizona diamondbacks",
    "reds": "cincinnati reds",
    "royals": "kansas city royals",
    "tigers": "detroit tigers",
    "angels": "los angeles angels",
    "athletics": "oakland athletics",
    "pirates": "pittsburgh pirates",
    "nationals": "washington nationals",
    "rockies": "colorado rockies",
    "marlins": "miami marlins",
    # NHL
    "bruins": "boston bruins",
    "maple leafs": "toronto maple leafs",
    "leafs": "toronto maple leafs",
    "avalanche": "colorado avalanche",
    "oilers": "edmonton oilers",
    "panthers_nhl": "florida panthers",
    "hurricanes": "carolina hurricanes",
    "penguins": "pittsburgh penguins",
    "red wings": "detroit red wings",
    "blackhawks": "chicago blackhawks",
    "canadiens": "montreal canadiens",
    "habs": "montreal canadiens",
    "flames": "calgary flames",
    "canucks": "vancouver canucks",
    "wild": "minnesota wild",
    "predators": "nashville predators",
    "blues": "st. louis blues",
    "kraken": "seattle kraken",
    "golden knights": "vegas golden knights",
    "stars": "dallas stars",
    "islanders": "new york islanders",
    "sabres": "buffalo sabres",
    "senators": "ottawa senators",
    "sharks": "san jose sharks",
    "flyers": "philadelphia flyers",
    "coyotes": "arizona coyotes",
    "ducks": "anaheim ducks",
    # EPL
    "arsenal": "arsenal",
    "manchester city": "manchester city",
    "man city": "manchester city",
    "manchester united": "manchester united",
    "man united": "manchester united",
    "man utd": "manchester united",
    "liverpool": "liverpool",
    "chelsea": "chelsea",
    "tottenham": "tottenham hotspur",
    "spurs_epl": "tottenham hotspur",
    "newcastle": "newcastle united",
    "aston villa": "aston villa",
    "brighton": "brighton and hove albion",
    "west ham": "west ham united",
    "everton": "everton",
}

# Keywords that indicate sports markets
_SPORTS_KEYWORDS = [
    "championship", "champion", "win the", "wins the", "winner",
    "super bowl", "world series", "stanley cup", "nba finals",
    "nba championship", "nfl championship", "mvp", "title",
    "premier league", "masters", "playoff", "make the playoffs",
    "finals", "conference", "division", "win", "beat", "defeat",
    "moneyline", "spread", "over", "under", "game", "match",
]


# ---------------------------------------------------------------------------
# ESPN API helpers
# ---------------------------------------------------------------------------

def _parse_american_odds(odds_str: str) -> Optional[int]:
    """Parse American odds string like '+750' or '-1200' to int."""
    if not odds_str:
        return None
    try:
        return int(odds_str.replace("+", ""))
    except (ValueError, TypeError):
        return None


def _parse_espn_game(event: dict, sport: str) -> Optional[dict]:
    """Parse a single ESPN event into a standardized game dict with odds."""
    competitions = event.get("competitions", [])
    if not competitions:
        return None

    comp = competitions[0]
    competitors = comp.get("competitors", [])
    if len(competitors) < 2:
        return None

    # Identify home and away
    home_comp = None
    away_comp = None
    for c in competitors:
        if c.get("homeAway") == "home":
            home_comp = c
        elif c.get("homeAway") == "away":
            away_comp = c

    if not home_comp or not away_comp:
        # Fallback: first is home, second is away
        home_comp = competitors[0]
        away_comp = competitors[1]

    home_team = home_comp.get("team", {}).get("displayName", "Unknown")
    away_team = away_comp.get("team", {}).get("displayName", "Unknown")
    home_abbrev = home_comp.get("team", {}).get("abbreviation", "")
    away_abbrev = away_comp.get("team", {}).get("abbreviation", "")

    # Records
    home_records = home_comp.get("records", [])
    away_records = away_comp.get("records", [])
    home_record = home_records[0].get("summary", "") if home_records else ""
    away_record = away_records[0].get("summary", "") if away_records else ""

    # Parse DraftKings odds from the odds array
    odds_list = comp.get("odds", [])
    dk_odds = None
    for o in odds_list:
        provider = o.get("provider", {}).get("name", "")
        if "draft" in provider.lower() and "king" in provider.lower():
            dk_odds = o
            break
    # Fallback to first available odds provider
    if dk_odds is None and odds_list:
        dk_odds = odds_list[0]

    home_ml = None
    away_ml = None
    home_prob = None
    away_prob = None
    spread = None
    over_under = None

    if dk_odds:
        spread = dk_odds.get("spread")
        over_under = dk_odds.get("overUnder")

        # Parse moneylines
        ml = dk_odds.get("moneyline", {})
        home_ml_data = ml.get("home", {})
        away_ml_data = ml.get("away", {})

        # Prefer close odds, fall back to open
        home_close = home_ml_data.get("close", {}).get("odds")
        home_open = home_ml_data.get("open", {}).get("odds")
        away_close = away_ml_data.get("close", {}).get("odds")
        away_open = away_ml_data.get("open", {}).get("odds")

        home_ml = _parse_american_odds(home_close or home_open)
        away_ml = _parse_american_odds(away_close or away_open)

        if home_ml is not None:
            home_prob = american_to_implied_prob(home_ml)
        if away_ml is not None:
            away_prob = american_to_implied_prob(away_ml)

    return {
        "sport": sport,
        "game": event.get("name", f"{away_team} at {home_team}"),
        "home_team": home_team,
        "away_team": away_team,
        "home_abbrev": home_abbrev,
        "away_abbrev": away_abbrev,
        "home_record": home_record,
        "away_record": away_record,
        "home_odds": home_ml,
        "away_odds": away_ml,
        "home_prob": home_prob,
        "away_prob": away_prob,
        "spread": spread,
        "over_under": over_under,
    }


def fetch_espn_sport(sport: str) -> list[dict]:
    """Fetch and parse ESPN scoreboard for a single sport. Returns list of game dicts."""
    url = ESPN_SPORTS.get(sport)
    if not url:
        logger.warning("Unknown ESPN sport: %s", sport)
        return []

    cache_key = f"espn:{sport}"
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.debug("Cache hit for espn:%s", sport)
        return cached

    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        logger.error("ESPN API request failed for %s: %s", sport, e)
        return []
    except ValueError as e:
        logger.error("ESPN API returned invalid JSON for %s: %s", sport, e)
        return []

    events = data.get("events", [])
    games = []
    for event in events:
        game = _parse_espn_game(event, sport)
        if game is not None:
            games.append(game)

    logger.info("ESPN: fetched %d games for %s (%d with odds)",
                len(games), sport,
                sum(1 for g in games if g["home_odds"] is not None))

    _cache_set(cache_key, games)
    return games


def get_espn_odds(sport: str = None) -> list[dict]:
    """Get all current ESPN odds.

    Args:
        sport: Optional sport key (nba, nfl, mlb, nhl, epl, ncaab).
               If None, fetches all sports.

    Returns:
        List of dicts with keys: sport, game, home_team, away_team,
        home_odds, away_odds, home_prob, away_prob, spread, over_under
    """
    if sport:
        return fetch_espn_sport(sport)

    all_games = []
    for sport_key in ESPN_SPORTS:
        games = fetch_espn_sport(sport_key)
        all_games.extend(games)
    return all_games


def fetch_all_espn_odds() -> dict[str, list[dict]]:
    """Fetch ESPN odds for all supported sports. Returns {sport: [games]}."""
    results = {}
    for sport_key in ESPN_SPORTS:
        games = fetch_espn_sport(sport_key)
        if games:
            results[sport_key] = games
    return results


# ---------------------------------------------------------------------------
# Odds conversion
# ---------------------------------------------------------------------------

def american_to_implied_prob(american_odds: int) -> float:
    """Convert American odds to implied probability (0-1).

    Negative odds (favorite): prob = |odds| / (|odds| + 100)
    Positive odds (underdog): prob = 100 / (odds + 100)
    """
    if american_odds > 0:
        return 100.0 / (american_odds + 100.0)
    elif american_odds < 0:
        return abs(american_odds) / (abs(american_odds) + 100.0)
    else:
        return 0.5  # Even odds


# ---------------------------------------------------------------------------
# Fuzzy matching: Kalshi market titles <-> ESPN teams
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Normalize text for matching: lowercase, strip punctuation."""
    text = text.lower()
    text = re.sub(r"[''\".,!?():]", "", text)
    return text.strip()


def _extract_team_from_title(title: str) -> Optional[str]:
    """Try to extract a team name from a Kalshi market title.

    Returns canonical team name or None.
    """
    title_norm = _normalize(title)

    best_match = None
    best_len = 0
    for alias, canonical in _TEAM_ALIASES.items():
        if alias in title_norm and len(alias) > best_len:
            best_match = canonical
            best_len = len(alias)

    return best_match


def _is_sports_market(title: str) -> bool:
    """Check if a market title is about sports."""
    title_lower = title.lower()
    return any(kw in title_lower for kw in _SPORTS_KEYWORDS)


def _detect_sport(title: str) -> Optional[str]:
    """Guess the ESPN sport key from a Kalshi market title."""
    title_lower = title.lower()
    mapping = {
        "nba": ["nba", "basketball"],
        "nfl": ["nfl", "super bowl", "football"],
        "mlb": ["mlb", "baseball", "world series"],
        "nhl": ["nhl", "hockey", "stanley cup"],
        "epl": ["premier league", "epl", "soccer"],
        "ncaab": ["college basketball", "ncaa", "march madness"],
    }
    for sport_key, keywords in mapping.items():
        for kw in keywords:
            if kw in title_lower:
                return sport_key
    return None


def _teams_match(canonical: str, espn_name: str) -> bool:
    """Check if a canonical team name matches an ESPN team displayName.
    
    Requires strong match to avoid false positives like
    'giants' matching 'Lotte Giants' or 'Lucknow Super Giants'.
    """
    c = canonical.lower().strip()
    e = espn_name.lower().strip()

    # Exact match
    if c == e:
        return True

    # Full canonical is a substring of ESPN name (e.g. "boston celtics" in "Boston Celtics")
    if c in e:
        return True

    # ESPN full name is substring of canonical (less common)
    if e in c:
        return True

    # City + mascot match: both words of a 2+ word canonical must appear in ESPN name
    c_words = c.split()
    if len(c_words) >= 2:
        e_words = set(e.split())
        if all(w in e_words for w in c_words):
            return True
    
    # Single-word canonical (e.g. "arsenal") — must be an exact word match,
    # not a substring, to avoid "giants" matching "super giants"
    if len(c_words) == 1:
        e_words = e.split()
        # Must match as a complete word in the ESPN name
        if c in e_words:
            return True

    return False


def match_kalshi_to_espn(market: dict, all_games: dict[str, list[dict]]) -> Optional[dict]:
    """Match a Kalshi market to an ESPN game using the official team mapping table.

    Returns dict with match info or None.
    """
    title = market.get("title", "")
    if not title:
        return None

    # Only match game-level markets, NOT season futures
    # Season futures should not be compared to today's game odds
    title_lower = title.lower()
    season_keywords = [
        "championship", "stanley cup", "super bowl", "world series",
        "premier league", "la liga", "champions league", "fa cup",
        "finals", "mvp", "award", "rookie", "cy young",
        "division winner", "division", "conference", "president",
        "top 2", "top 4", "top 6", "relegated", "promotion",
        "playoff", "clinch", "series winner", "ipl",
        "national rugby league champ",
    ]
    if any(kw in title_lower for kw in season_keywords):
        return None

    # Must look like a game market: "vs", "winner?", team "at" team, or specific date
    game_signals = ["vs", "winner?", " at ", "wins by", "score"]
    if not any(sig in title_lower for sig in game_signals):
        return None

    # Use the ESPN team lookup table (auto-generated from ESPN APIs)
    result = lookup_team(title)
    if not result:
        return None

    sport_key, espn_team_name = result
    espn_lower = espn_team_name.lower()

    # Find this team in today's games
    title_lower = title.lower()
    games = all_games.get(sport_key, [])
    for game in games:
        matched_side = None
        if game["home_team"].lower() == espn_lower:
            matched_side = "home"
        elif game["away_team"].lower() == espn_lower:
            matched_side = "away"

        if not matched_side:
            continue

        # VALIDATION: prevent false matches (e.g. "Ningbo Rockets" -> "Houston Rockets")
        # The Kalshi title must contain either:
        # 1. The city/location of the ESPN team (e.g. "Houston", "Golden State", "West Ham")
        # 2. OR the opponent team also appears in the title (confirms it's the same game)
        espn_team = game[f"{matched_side}_team"]  # e.g. "Houston Rockets"
        other_side = "away" if matched_side == "home" else "home"
        other_team = game[f"{other_side}_team"]  # e.g. "Milwaukee Bucks"

        # Extract city: everything except the last word (mascot)
        team_parts = espn_team.split()
        if len(team_parts) >= 2:
            city = " ".join(team_parts[:-1]).lower()  # "houston", "golden state", "west ham"
        else:
            city = espn_team.lower()

        other_parts = other_team.split()
        if len(other_parts) >= 2:
            other_city = " ".join(other_parts[:-1]).lower()
        else:
            other_city = other_team.lower()

        city_in_title = city in title_lower
        other_in_title = other_city in title_lower or other_parts[-1].lower() in title_lower

        if not city_in_title and not other_in_title:
            # Neither the team's city nor opponent appears — likely false match
            continue

        prob = game[f"{matched_side}_prob"]
        odds = game[f"{matched_side}_odds"]
        if prob is not None:
            return {
                "sport": sport_key,
                "game": game["game"],
                "matched_team": game[f"{matched_side}_team"],
                "matched_side": matched_side,
                "implied_prob": prob,
                "moneyline": odds,
                "spread": game["spread"],
                "over_under": game["over_under"],
                "home_team": game["home_team"],
                "away_team": game["away_team"],
                "home_record": game.get("home_record", ""),
                "away_record": game.get("away_record", ""),
            }

    return None


# ---------------------------------------------------------------------------
# Kalshi sports market filtering
# ---------------------------------------------------------------------------

def get_kalshi_sports_markets(client: KalshiClient) -> list[dict]:
    """Fetch Kalshi markets in Sports / Exotics categories."""
    try:
        all_events = client.get_all_open_events(with_nested_markets=True)
    except Exception as e:
        logger.error("Failed to fetch Kalshi events: %s", e)
        return []

    sports_markets = []
    for event in all_events:
        cat = event.get("category", "")
        if cat not in ("Sports", "Exotics"):
            continue

        for market in event.get("markets", []):
            if market.get("status") != "active":
                continue

            title = market.get("title", "")
            if not _is_sports_market(title):
                continue

            market["_event_category"] = cat
            market["_event_title"] = event.get("title", "")
            sports_markets.append(market)

    logger.info("Found %d active sports markets on Kalshi", len(sports_markets))
    return sports_markets


# ---------------------------------------------------------------------------
# Arbitrage calculation
# ---------------------------------------------------------------------------

def _get_yes_price(market: dict) -> Optional[float]:
    """Get the YES price as a float between 0 and 1."""
    price = market.get("yes_bid_dollars")
    if price is None:
        price = market.get("last_price")
    if price is None:
        return None
    try:
        p = float(price)
        if p > 1:
            p = p / 100.0
        return p
    except (ValueError, TypeError):
        return None


def calculate_arb(kalshi_price: float, espn_prob: float,
                  edge_threshold: float = 0.05) -> Optional[dict]:
    """Determine if an arbitrage opportunity exists.

    Only flag YES opportunities where entry < $0.50.
    """
    edge = espn_prob - kalshi_price

    if edge >= edge_threshold and kalshi_price < 0.50:
        confidence = "high" if edge >= 0.10 else "medium" if edge >= 0.07 else "low"
        return {
            "side": "yes",
            "edge": round(edge, 4),
            "confidence": confidence,
        }

    if edge <= -edge_threshold:
        logger.debug(
            "Potential NO arb (skipped, YES-only): edge=%.4f, kalshi=%.2f, espn=%.4f",
            edge, kalshi_price, espn_prob,
        )

    return None


# ---------------------------------------------------------------------------
# Main scanner entry point
# ---------------------------------------------------------------------------

def scan_sports_arb(client: KalshiClient) -> list[dict]:
    """Scan for sports arbitrage opportunities.

    Returns list of opportunities sorted by edge descending, each containing:
        ticker, title, kalshi_price, espn_prob, edge, side, confidence,
        sport, game, matched_team, moneyline
    """
    sports_cfg = getattr(config, "sports", None)
    if sports_cfg and not sports_cfg.enabled:
        logger.info("Sports scanning is disabled (SPORTS_ENABLED=false)")
        return []

    logger.info("Starting sports arbitrage scan (ESPN/DraftKings)...")
    start = time.time()

    # Step 1: Fetch ESPN odds for all supported sports
    all_games = fetch_all_espn_odds()
    if not all_games:
        logger.warning("No ESPN odds data retrieved from any sport")
        return []
    total_games = sum(len(v) for v in all_games.values())
    games_with_odds = sum(
        1 for games in all_games.values()
        for g in games if g["home_odds"] is not None
    )
    logger.info("Fetched %d total games across %d sports (%d with DraftKings odds)",
                total_games, len(all_games), games_with_odds)

    # Step 2: Fetch Kalshi sports markets
    kalshi_markets = get_kalshi_sports_markets(client)
    if not kalshi_markets:
        logger.info("No active Kalshi sports markets found")
        return []

    # Step 3: Match and calculate arbitrage
    opportunities = []
    matched_count = 0

    for market in kalshi_markets:
        match = match_kalshi_to_espn(market, all_games)
        if not match:
            continue

        matched_count += 1
        kalshi_price = _get_yes_price(market)
        if kalshi_price is None or kalshi_price <= 0.0:
            continue

        espn_prob = match["implied_prob"]
        arb = calculate_arb(kalshi_price, espn_prob)

        if arb:
            ml = match["moneyline"]
            ml_str = f"+{ml}" if ml and ml > 0 else str(ml) if ml else "N/A"
            opp = {
                "ticker": market.get("ticker", ""),
                "title": market.get("title", ""),
                "kalshi_price": round(kalshi_price, 2),
                "espn_prob": round(espn_prob, 4),
                "edge": arb["edge"],
                "side": arb["side"],
                "confidence": arb["confidence"],
                "sport": match["sport"],
                "game": match["game"],
                "matched_team": match["matched_team"],
                "moneyline": ml_str,
            }
            opportunities.append(opp)
            logger.info(
                "ARB FOUND: %s | %s @ $%.2f vs ESPN %.1f%% (%s ML) | edge=%.1f%% | %s",
                opp["ticker"], opp["title"], opp["kalshi_price"],
                opp["espn_prob"] * 100, ml_str, opp["edge"] * 100,
                opp["confidence"],
            )

    elapsed = time.time() - start
    logger.info(
        "Sports arb scan complete: %d Kalshi markets, %d matched to ESPN, "
        "%d opportunities found in %.1fs",
        len(kalshi_markets), matched_count, len(opportunities), elapsed,
    )

    # Sort by edge descending
    opportunities.sort(key=lambda x: x["edge"], reverse=True)
    return opportunities
