"""
Kalshi Trading Bot — Sports Data Enrichment

Provides real-time ESPN/DraftKings odds context for the LLM
to use when evaluating sports prediction markets.
"""
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from sports_scanner import (
    ESPN_SPORTS,
    _TEAM_ALIASES,
    _normalize,
    _detect_sport,
    _extract_team_from_title,
    _teams_match,
    fetch_espn_sport,
    get_espn_odds,
    american_to_implied_prob,
    _cache_get,
    _cache_set,
)

logger = logging.getLogger(__name__)


def _fmt_ml(odds: Optional[int]) -> str:
    """Format moneyline odds with sign."""
    if odds is None:
        return "N/A"
    return f"+{odds}" if odds > 0 else str(odds)


def _fmt_prob(prob: Optional[float]) -> str:
    """Format probability as percentage."""
    if prob is None:
        return "N/A"
    return f"{prob:.1%}"


def _find_team_game(games: list[dict], team_canonical: str) -> Optional[dict]:
    """Find the ESPN game containing a given team."""
    for game in games:
        if _teams_match(team_canonical, game["home_team"]):
            return game
        if _teams_match(team_canonical, game["away_team"]):
            return game
    return None


def _format_game_context(game: dict, team_canonical: Optional[str] = None) -> str:
    """Format a single ESPN game as a context string for the LLM."""
    lines = []
    lines.append(f"Game: {game['game']}")

    home = game["home_team"]
    away = game["away_team"]
    home_ml = _fmt_ml(game["home_odds"])
    away_ml = _fmt_ml(game["away_odds"])
    home_prob = _fmt_prob(game["home_prob"])
    away_prob = _fmt_prob(game["away_prob"])
    home_rec = game.get("home_record", "")
    away_rec = game.get("away_record", "")
    spread = game.get("spread")
    over_under = game.get("over_under")

    # Away team line
    away_line = f"{away}: {away_ml} ML ({away_prob} implied)"
    if away_rec:
        away_line += f", Record: {away_rec}"
    # If spread is provided, it's typically from the home perspective;
    # away spread is the negative of home spread
    if spread is not None:
        away_spread = -spread
        away_line += f", Spread: {'+' if away_spread > 0 else ''}{away_spread}"
    lines.append(away_line)

    # Home team line
    home_line = f"{home}: {home_ml} ML ({home_prob} implied)"
    if home_rec:
        home_line += f", Record: {home_rec}"
    if spread is not None:
        home_line += f", Spread: {'+' if spread > 0 else ''}{spread}"
    lines.append(home_line)

    # Over/Under
    if over_under is not None:
        lines.append(f"Over/Under: {over_under}")

    return "\n".join(lines)


def enrich_sports_market(market: dict) -> str:
    """Return ESPN odds + records as context for LLM.

    Args:
        market: A Kalshi market dict (must have 'title' key).

    Returns:
        Formatted string with ESPN/DraftKings odds data, or empty string if no match.
    """
    title = market.get("title", "")
    if not title:
        return ""

    cache_key = f"sports_enrich:{_normalize(title)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # Detect sport and team
    sport_key = _detect_sport(title)
    team = _extract_team_from_title(title)

    if not sport_key:
        # Try all sports if we can't detect from title
        result = _enrich_generic(title, team)
        _cache_set(cache_key, result)
        return result

    # Fetch ESPN data for the detected sport
    games = fetch_espn_sport(sport_key)
    if not games:
        result = f"SPORTS DATA (ESPN/DraftKings): No games available for {sport_key}"
        _cache_set(cache_key, result)
        return result

    lines = ["SPORTS DATA (ESPN/DraftKings):"]

    if team:
        # Find the specific game for this team
        game = _find_team_game(games, team)
        if game:
            lines.append(_format_game_context(game, team))
        else:
            lines.append(f"Team '{team}' not found in current {sport_key} scoreboard")
            lines.append("")
            lines.append("Available games:")
            for g in games[:10]:
                lines.append(f"  {g['game']}")
            if len(games) > 10:
                lines.append(f"  ... and {len(games) - 10} more")
    else:
        # No team detected -- show all available games for context
        lines.append(f"No specific team detected in market title.")
        lines.append(f"Available games ({len(games)}):")
        for g in games[:10]:
            lines.append(f"  {g['game']}")
        if len(games) > 10:
            lines.append(f"  ... and {len(games) - 10} more")

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"\nLast updated: {now_utc}")
    result = "\n".join(lines)
    _cache_set(cache_key, result)
    return result


def _enrich_generic(title: str, team: Optional[str]) -> str:
    """Fallback enrichment: search all sports for a team match."""
    if not team:
        return ""

    lines = ["SPORTS DATA (ESPN/DraftKings):"]

    for sport_key in ESPN_SPORTS:
        games = fetch_espn_sport(sport_key)
        if not games:
            continue

        game = _find_team_game(games, team)
        if game:
            lines.append(_format_game_context(game, team))
            now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            lines.append(f"\nLast updated: {now_utc}")
            return "\n".join(lines)

    return ""
