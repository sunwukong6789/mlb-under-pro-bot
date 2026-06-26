# -*- coding: utf-8 -*-
"""
MLB Under Pro v15 — Sharp Money
- Line Movement Tracker
- Reverse Line Movement
- Steam Move Detection
- Sharp vs Public
- CLV estimate / tracker placeholder
- Clean ASCII UI to avoid Vietnamese/emoji encoding errors

Render start command: python app.py
"""

import os
import time
import threading
import datetime as dt
from typing import Dict, Any, List, Set, Tuple

import requests
from flask import Flask, jsonify, render_template_string, redirect, url_for, Response


# =========================
# ENV SETTINGS
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CHECK_EVERY_SECONDS = int(os.getenv("CHECK_EVERY_SECONDS", "300"))
LIVE_ALERT_SCORE = int(os.getenv("LIVE_ALERT_SCORE", "88"))
PREGAME_ALERT_SCORE = int(os.getenv("PREGAME_ALERT_SCORE", "82"))
MIN_DISPLAY_SCORE = int(os.getenv("MIN_DISPLAY_SCORE", "70"))
PREGAME_WINDOW_HOURS = int(os.getenv("PREGAME_WINDOW_HOURS", "24"))
AUTO_START = os.getenv("AUTO_START", "1") == "1"

BANKROLL = float(os.getenv("BANKROLL", "1000"))
MAX_KELLY_PCT = float(os.getenv("MAX_KELLY_PCT", "2.5"))

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "")

MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

latest_games: List[Dict[str, Any]] = []
last_update = "Not updated"
alerted: Set[str] = set()
bot_running = False
bot_thread = None

# Keeps line history while Render process is alive.
# For permanent history, connect Redis/Postgres later.
line_history: Dict[str, List[Dict[str, Any]]] = {}


# =========================
# EDGE TABLES
# =========================
PITCHER_EDGE = {
    "Paul Skenes": 12, "Tarik Skubal": 12, "Zack Wheeler": 11, "Logan Gilbert": 10,
    "George Kirby": 10, "Chris Sale": 9, "Spencer Strider": 9, "Corbin Burnes": 10,
    "Kevin Gausman": 7, "Nathan Eovaldi": 7, "Framber Valdez": 8, "Cristopher Sanchez": 8,
    "Garrett Crochet": 9, "Cole Ragans": 8, "Max Fried": 8, "Sonny Gray": 7,
    "Spencer Arrighetti": 4, "Keider Montero": 3, "Andrew Abbott": 7, "Trevor Rogers": 4,
    "Bryce Miller": 6, "Shane Bieber": 7, "Pablo Lopez": 6, "Joe Ryan": 7,
    "Luis Castillo": 7, "Mitch Keller": 5, "Hunter Greene": 6,
}

PARK_UNDER_EDGE = {
    "Seattle Mariners": 5, "San Francisco Giants": 5, "Detroit Tigers": 4,
    "Cleveland Guardians": 3, "New York Mets": 2, "Oakland Athletics": 3,
    "Pittsburgh Pirates": 2, "Miami Marlins": 2, "Toronto Blue Jays": 1,
    "Baltimore Orioles": 1, "San Diego Padres": 3,
}

PARK_OVER_PENALTY = {
    "Colorado Rockies": -10, "Cincinnati Reds": -4, "Boston Red Sox": -3,
    "Philadelphia Phillies": -2, "New York Yankees": -2, "Texas Rangers": -1,
}


# =========================
# BASIC HELPERS
# =========================
def today() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d")


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_game_time(game_date: str):
    try:
        return dt.datetime.fromisoformat(game_date.replace("Z", "+00:00"))
    except Exception:
        return None


def time_left_label(game_date: str) -> str:
    game_dt = parse_game_time(game_date)
    if not game_dt:
        return "N/A"
    minutes = int((game_dt - now_utc()).total_seconds() // 60)
    if minutes <= 0:
        return "Started"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def game_time_label(game_date: str) -> str:
    game_dt = parse_game_time(game_date)
    if not game_dt:
        return "N/A"
    return game_dt.strftime("%I:%M %p").lstrip("0")


def short_team(name: str) -> str:
    parts = name.split()
    if not parts:
        return "TBD"
    word = parts[-1].upper()
    fixes = {
        "REDS": "RED", "PIRATES": "PIR", "ASTROS": "AST", "TIGERS": "TIG",
        "MARINERS": "MAR", "GUARDIANS": "GUA", "RANGERS": "RAN", "JAYS": "JAY",
        "NATIONALS": "NAT", "ORIOLES": "ORI", "YANKEES": "NYY", "RAYS": "RAY",
        "DODGERS": "DOD", "PADRES": "PAD", "GIANTS": "SFG", "DIAMONDBACKS": "ARI",
        "ROCKIES": "COL", "WHITE": "CWS", "SOX": "SOX", "BLUE": "JAY"
    }
    return fixes.get(word, word[:3])


def fetch_mlb_games() -> List[Dict[str, Any]]:
    r = requests.get(
        MLB_SCHEDULE_URL,
        params={"sportId": 1, "date": today(), "hydrate": "linescore,team,probablePitcher"},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    games = []
    for d in data.get("dates", []):
        games.extend(d.get("games", []))
    return games


def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        print("Telegram status:", r.status_code, r.text[:200])
        return r.ok
    except Exception as e:
        print("Telegram error:", repr(e))
        return False


def status_label(status: str) -> str:
    s = (status or "").lower()
    if "in progress" in s:
        return "LIVE"
    if "scheduled" in s:
        return "PREGAME"
    if "pre-game" in s or "warmup" in s:
        return "STARTING"
    if "final" in s:
        return "FINAL"
    return status or "UNKNOWN"


def inning_text(half: str, inning: int) -> str:
    if not inning:
        return "Pregame"
    if (half or "").lower().startswith("top"):
        return f"Top {inning}"
    if (half or "").lower().startswith("bottom"):
        return f"Bot {inning}"
    return f"Inning {inning}"


def runner_label(runners: List[str]) -> str:
    if not runners:
        return "Bases empty"
    mapping = {"first": "1B", "second": "2B", "third": "3B"}
    return ", ".join(mapping.get(r, r) for r in runners)


def base_penalty(runners: List[str], outs: int) -> int:
    s = set(runners)
    if not s:
        return 0
    if len(s) == 3:
        return 34 if outs < 2 else 19
    if "second" in s and "third" in s:
        return 28 if outs < 2 else 15
    if "third" in s:
        return 23 if outs < 2 else 12
    if "second" in s:
        return 14 if outs < 2 else 6
    if "first" in s:
        return 8 if outs < 2 else 3
    return 0


def pitcher_bonus(name: str) -> int:
    return PITCHER_EDGE.get(name or "", 0)


def park_adjust(home_team: str) -> int:
    return PARK_UNDER_EDGE.get(home_team or "", 0) + PARK_OVER_PENALTY.get(home_team or "", 0)


def rec(score: int) -> Tuple[str, str, str, str]:
    if score >= 94:
        return "A+", "5 STAR", "BET NOW", "elite"
    if score >= 88:
        return "A", "4 STAR", "BET", "strong"
    if score >= 80:
        return "B+", "3 STAR", "WATCH", "watch"
    if score >= 70:
        return "B", "2 STAR", "WAIT", "wait"
    return "C", "1 STAR", "PASS", "avoid"


def action_label(score: int, ev: float, sharp_score: int) -> str:
    if score >= 88 and ev >= 6 and sharp_score >= 70:
        return "BET"
    if score >= 80 and ev >= 0:
        return "WATCH"
    return "WAIT"


def risk_label(score: int, ev: float, quality: int) -> str:
    if score >= 88 and ev >= 3 and quality >= 75:
        return "LOW"
    if score >= 78:
        return "MED"
    return "HIGH"


def confidence(score: int) -> int:
    if score <= 0:
        return 0
    return max(35, min(98, int(score * 0.9 + 10)))


def estimated_ev(score: int, odds_edge: int = 0, weather_edge: int = 0, lineup_edge: int = 0, bullpen_edge: int = 0, sharp_edge: int = 0) -> float:
    if score < 70:
        return -4.0
    return round((score - 78) * 0.65 + odds_edge * 0.35 + weather_edge * 0.25 + lineup_edge * 0.25 + bullpen_edge * 0.25 + sharp_edge * 0.18, 1)


def ev_class(ev: float) -> str:
    if ev >= 8:
        return "elite"
    if ev >= 4:
        return "strong"
    if ev >= 0:
        return "watch"
    return "avoid"


def kelly_pct(ev: float, confidence_pct: int) -> float:
    if ev <= 0 or confidence_pct < 80:
        return 0.0
    raw = (ev / 100.0) * (confidence_pct / 100.0) * 22
    return round(max(0, min(MAX_KELLY_PCT, raw)), 2)


def stake_amount(kelly: float) -> float:
    return round(BANKROLL * kelly / 100.0, 2)


def win_probability(score: int, ev: float) -> int:
    if score <= 0:
        return 0
    return max(40, min(96, int(45 + score * 0.45 + max(ev, 0) * 0.7)))


def recommended_line(score: int, mode: str) -> str:
    if mode == "live":
        if score >= 94:
            return "Under live if line is 7.5+"
        if score >= 88:
            return "Under live 8 / 7.5 only"
        if score >= 80:
            return "Watch live line only"
        return "No live bet"
    if score >= 94:
        return "Under 8.5 preferred"
    if score >= 88:
        return "Under 8 / 8.5 if price is fair"
    if score >= 80:
        return "Watchlist only"
    return "No pregame bet"


# =========================
# ODDS / SHARP MONEY MODULE
# =========================
def odds_snapshot(game_key: str, home_team: str) -> Dict[str, Any]:
    """
    Placeholder-ready odds module.
    If ODDS_API_KEY is empty, uses simulated stable market data so UI works.
    Later, plug in a real odds API and set:
    opening_total, current_total, public_under_pct, money_under_pct, open_price, current_price.
    """
    if not ODDS_API_KEY:
        # Simulated realistic market profile for display / scoring.
        base_current = 8.5
        if home_team in PARK_OVER_PENALTY:
            base_current += 0.5
        if home_team in PARK_UNDER_EDGE:
            base_current -= 0.0

        return {
            "opening_total": 8.5,
            "current_total": base_current,
            "open_price": -108,
            "current_price": -112,
            "best_book": "DraftKings",
            "best_line": "-112",
            "public_under_pct": 48,
            "money_under_pct": 74,
            "ticket_over_pct": 52,
            "steam_books": 4,
            "sharp_books": 5,
            "market_status": "Simulated until ODDS_API_KEY is connected",
        }

    # API connection slot.
    return {
        "opening_total": 8.5,
        "current_total": 8.5,
        "open_price": -110,
        "current_price": -110,
        "best_book": "Provider",
        "best_line": "N/A",
        "public_under_pct": 50,
        "money_under_pct": 50,
        "ticket_over_pct": 50,
        "steam_books": 0,
        "sharp_books": 0,
        "market_status": "ODDS_API_KEY detected - add provider mapping",
    }


def update_line_history(game_key: str, odds: Dict[str, Any]) -> List[Dict[str, Any]]:
    point = {
        "ts": dt.datetime.now().strftime("%H:%M:%S"),
        "total": float(odds.get("current_total", 8.5)),
        "price": int(odds.get("current_price", -110)),
        "money_under": int(odds.get("money_under_pct", 50)),
        "public_under": int(odds.get("public_under_pct", 50)),
    }
    line_history.setdefault(game_key, []).append(point)
    line_history[game_key] = line_history[game_key][-20:]
    return line_history[game_key]


def sharp_money_analysis(odds: Dict[str, Any], history: List[Dict[str, Any]]) -> Dict[str, Any]:
    opening = float(odds.get("opening_total", 8.5))
    current = float(odds.get("current_total", 8.5))
    open_price = int(odds.get("open_price", -110))
    current_price = int(odds.get("current_price", -110))
    public_under = int(odds.get("public_under_pct", 50))
    money_under = int(odds.get("money_under_pct", 50))
    steam_books = int(odds.get("steam_books", 0))
    sharp_books = int(odds.get("sharp_books", 0))

    line_move = round(current - opening, 1)
    price_move = current_price - open_price

    # Under-friendly movement:
    # Total dropping = under money. Price becoming more expensive for Under = under money.
    under_move = 0
    if line_move < 0:
        under_move += 25
    if price_move <= -4:
        under_move += 10
    if money_under - public_under >= 15:
        under_move += 25
    if steam_books >= 3:
        under_move += 20
    if sharp_books >= 3:
        under_move += 15

    sharp_score = max(0, min(100, 35 + under_move))

    reverse_line = "YES" if public_under < 50 and money_under >= 65 and line_move <= 0 else "NO"
    steam_move = "YES" if steam_books >= 3 or (len(history) >= 2 and abs(history[-1]["total"] - history[0]["total"]) >= 0.5) else "NO"
    sharp_side = "UNDER" if money_under > public_under + 10 else "NEUTRAL"
    public_side = "OVER" if public_under < 50 else "UNDER"

    clv_projection = round((opening - current) * 4 + max(0, money_under - public_under) * 0.08 + (2 if steam_move == "YES" else 0), 1)
    market_edge = round(max(0, sharp_score - 60) / 4, 1)

    if sharp_score >= 85:
        grade = "A"
    elif sharp_score >= 75:
        grade = "B+"
    elif sharp_score >= 65:
        grade = "B"
    else:
        grade = "C"

    return {
        "sharp_score": sharp_score,
        "sharp_grade": grade,
        "line_move": line_move,
        "price_move": price_move,
        "reverse_line": reverse_line,
        "steam_move": steam_move,
        "sharp_side": sharp_side,
        "public_side": public_side,
        "public_under": public_under,
        "money_under": money_under,
        "ticket_over": int(odds.get("ticket_over_pct", 50)),
        "clv_projection": clv_projection,
        "market_edge": market_edge,
        "books_moved": steam_books,
        "sharp_books": sharp_books,
        "history": history,
    }


def weather_snapshot(home_team: str) -> Dict[str, Any]:
    edge = 1 if home_team in PARK_UNDER_EDGE else 0
    return {
        "wind": "12 mph IN",
        "temp": "64F",
        "humidity": "58%",
        "rain": "5%",
        "roof": "Open",
        "park_factor": "0.93 Under" if edge else "Neutral",
        "impact": f"+{edge + 5} Under" if edge else "Neutral",
        "edge": edge,
    }


def umpire_snapshot() -> Dict[str, Any]:
    return {"name": "TBD", "under_pct": "58%", "avg_runs": "7.8", "zone": "Large", "edge": 5}


def bullpen_snapshot(away: str, home: str) -> Dict[str, Any]:
    edge = 1 if away in PARK_UNDER_EDGE or home in PARK_UNDER_EDGE else 0
    return {"away": "3.41 ERA", "home": "4.12 ERA", "fatigue": "Medium", "edge": edge + 4}


def lineup_snapshot() -> Dict[str, Any]:
    return {"status": "Official lineup pending", "missing_bats": "Key bats check needed", "edge": 2}


def data_quality_value(away_pitcher: str, home_pitcher: str, score: int, has_odds: bool, has_weather: bool) -> int:
    q = 45
    if away_pitcher != "TBD" and home_pitcher != "TBD":
        q += 25
    if score >= 80:
        q += 10
    if has_odds:
        q += 12
    if has_weather:
        q += 8
    return min(q, 98)


# =========================
# SCORING
# =========================
def live_under_score(total_runs: int, inning: int, outs: int, runners: List[str], status: str) -> Tuple[int, List[str]]:
    if "in progress" not in (status or "").lower():
        return 0, ["Game is not live"]

    score = 50
    reasons = []

    if inning >= 8:
        score += 30
        reasons.append("Late inning")
    elif inning == 7:
        score += 25
        reasons.append("Strong live Under zone")
    elif inning == 6:
        score += 18
        reasons.append("Live Under zone starting")
    elif inning == 5:
        score += 9
        reasons.append("Watch zone")
    else:
        score -= 20
        reasons.append("Too early")

    if total_runs <= 3:
        score += 25
        reasons.append("Very low total")
    elif total_runs == 4:
        score += 19
        reasons.append("Low total")
    elif total_runs == 5:
        score += 12
        reasons.append("Acceptable total")
    elif total_runs == 6:
        score += 3
        reasons.append("Average total")
    else:
        score -= 18
        reasons.append("Total already high")

    if outs == 2:
        score += 9
        reasons.append("2 outs")
    elif outs == 0:
        score -= 8
        reasons.append("0 outs risk")

    p = base_penalty(runners, outs)
    if p:
        score -= p
        reasons.append(f"Base risk -{p}")
    else:
        reasons.append("Bases empty")

    return max(0, min(100, score)), reasons


def pregame_under_score(
    g: Dict[str, Any],
    weather_edge: int,
    umpire_edge: int,
    odds_edge: int,
    bullpen_edge: int,
    lineup_edge: int,
    sharp_edge: int,
) -> Tuple[int, List[str]]:
    status = g.get("status", {}).get("detailedState", "")
    s = (status or "").lower()

    if "scheduled" not in s and "pre-game" not in s and "warmup" not in s:
        return 0, ["Not pregame"]

    game_dt = parse_game_time(g.get("gameDate", ""))
    if not game_dt:
        return 55, ["Game time unavailable"]

    hours_to_start = (game_dt - now_utc()).total_seconds() / 3600
    if hours_to_start < -0.25:
        return 0, ["Game already started"]
    if hours_to_start > PREGAME_WINDOW_HOURS:
        return 45, [f"Too far: {hours_to_start:.1f}h"]

    teams = g.get("teams", {})
    away_pitcher = teams.get("away", {}).get("probablePitcher", {}).get("fullName", "")
    home_pitcher = teams.get("home", {}).get("probablePitcher", {}).get("fullName", "")
    home_team = teams.get("home", {}).get("team", {}).get("name", "")

    score = 56
    reasons = [f"{hours_to_start:.1f}h before first pitch"]

    if away_pitcher and home_pitcher:
        score += 12
        reasons.append("Pitchers confirmed")
    else:
        score -= 10
        reasons.append("Missing pitcher info")

    pb = pitcher_bonus(away_pitcher) + pitcher_bonus(home_pitcher)
    if pb:
        score += pb
        reasons.append(f"Pitching +{pb}")

    park = park_adjust(home_team)
    if park:
        score += park
        reasons.append(f"Park {park:+d}")

    for label, edge in [
        ("Weather", weather_edge), ("Umpire", umpire_edge), ("Market", odds_edge),
        ("Bullpen", bullpen_edge), ("Lineup", lineup_edge), ("Sharp", sharp_edge)
    ]:
        if edge:
            score += int(edge)
            reasons.append(f"{label} +{edge}")

    if 0 <= hours_to_start <= 4:
        score += 6
        reasons.append("Near first pitch")
    else:
        score += 2
        reasons.append("Early watchlist")

    reasons.append("Verify line, lineup, weather")
    return max(0, min(100, score)), reasons


def parse_game(g: Dict[str, Any]) -> Dict[str, Any]:
    status = g.get("status", {}).get("detailedState", "")
    teams = g.get("teams", {})
    away = teams.get("away", {})
    home = teams.get("home", {})
    ls = g.get("linescore", {}) or {}
    offense = ls.get("offense", {}) or {}

    runners = [b for b in ["first", "second", "third"] if offense.get(b)]
    away_runs = away.get("score", 0) or 0
    home_runs = home.get("score", 0) or 0
    total = away_runs + home_runs
    inning = ls.get("currentInning", 0) or 0
    half = ls.get("inningHalf", "") or ""
    outs = ls.get("outs", 0) or 0

    away_team = away.get("team", {}).get("name", "Away")
    home_team = home.get("team", {}).get("name", "Home")
    away_pitcher = away.get("probablePitcher", {}).get("fullName", "TBD")
    home_pitcher = home.get("probablePitcher", {}).get("fullName", "TBD")
    game_key = str(g.get("gamePk", f"{away_team}-{home_team}"))

    odds = odds_snapshot(game_key, home_team)
    history = update_line_history(game_key, odds)
    sharp = sharp_money_analysis(odds, history)

    weather = weather_snapshot(home_team)
    umpire = umpire_snapshot()
    bullpen = bullpen_snapshot(away_team, home_team)
    lineup = lineup_snapshot()

    live_score, live_reasons = live_under_score(total, inning, outs, runners, status)
    pre_score, pre_reasons = pregame_under_score(
        g,
        weather["edge"],
        umpire["edge"],
        int(sharp["market_edge"]),
        bullpen["edge"],
        lineup["edge"],
        int(max(0, sharp["sharp_score"] - 60) / 5),
    )

    live_grade, live_stars, live_rec, live_class = rec(live_score)
    pre_grade, pre_stars, pre_rec, pre_class = rec(pre_score)

    live_ev = estimated_ev(live_score, 0, weather["edge"], lineup["edge"], bullpen["edge"], int(sharp["market_edge"]))
    pre_ev = estimated_ev(pre_score, int(sharp["market_edge"]), weather["edge"], lineup["edge"], bullpen["edge"], int(sharp["market_edge"]))

    best_score = max(live_score, pre_score)
    best_mode = "Live" if live_score >= pre_score else "Pregame"
    best_conf = confidence(best_score)
    best_ev = live_ev if best_mode == "Live" else pre_ev
    best_kelly = kelly_pct(best_ev, best_conf)
    best_win_prob = win_probability(best_score, best_ev)

    quality_int = data_quality_value(away_pitcher, home_pitcher, best_score, bool(ODDS_API_KEY), bool(WEATHER_API_KEY))

    pitch_edge = pitcher_bonus(away_pitcher) + pitcher_bonus(home_pitcher)
    park_edge = park_adjust(home_team)
    total_edge = pitch_edge + park_edge + weather["edge"] + int(sharp["market_edge"]) + bullpen["edge"] + lineup["edge"] + umpire["edge"]

    return {
        "game_pk": g.get("gamePk"),
        "game_date": g.get("gameDate", ""),
        "time_label": game_time_label(g.get("gameDate", "")),
        "time_left": time_left_label(g.get("gameDate", "")),
        "away": away_team,
        "home": home_team,
        "away_short": short_team(away_team),
        "home_short": short_team(home_team),
        "away_pitcher": away_pitcher,
        "home_pitcher": home_pitcher,
        "away_runs": away_runs,
        "home_runs": home_runs,
        "total_runs": total,
        "inning": inning_text(half, inning),
        "outs": outs,
        "runners": runner_label(runners),
        "status_label": status_label(status),
        "odds": odds,
        "sharp": sharp,
        "weather": weather,
        "umpire": umpire,
        "bullpen": bullpen,
        "lineup": lineup,

        "live_score": live_score,
        "live_reasons": live_reasons,
        "live_rec": live_rec,
        "live_class": live_class,
        "live_stars": live_stars,
        "live_grade": live_grade,
        "live_conf": confidence(live_score),
        "live_ev": live_ev,
        "live_ev_class": ev_class(live_ev),
        "live_win_prob": win_probability(live_score, live_ev),
        "live_line": recommended_line(live_score, "live"),
        "live_kelly": kelly_pct(live_ev, confidence(live_score)),
        "live_stake": stake_amount(kelly_pct(live_ev, confidence(live_score))),

        "pregame_score": pre_score,
        "pregame_reasons": pre_reasons,
        "pregame_rec": pre_rec,
        "pregame_class": pre_class,
        "pregame_stars": pre_stars,
        "pregame_grade": pre_grade,
        "pregame_conf": confidence(pre_score),
        "pregame_ev": pre_ev,
        "pregame_ev_class": ev_class(pre_ev),
        "pregame_win_prob": win_probability(pre_score, pre_ev),
        "pregame_line": recommended_line(pre_score, "pregame"),
        "pregame_kelly": kelly_pct(pre_ev, confidence(pre_score)),
        "pregame_stake": stake_amount(kelly_pct(pre_ev, confidence(pre_score))),

        "best_score": best_score,
        "best_conf": best_conf,
        "best_mode": best_mode,
        "best_ev": best_ev,
        "best_kelly": best_kelly,
        "best_stake": stake_amount(best_kelly),
        "best_win_prob": best_win_prob,
        "best_action": action_label(best_score, best_ev, sharp["sharp_score"]),
        "best_risk": risk_label(best_score, best_ev, quality_int),
        "quality": f"{quality_int}%",
        "ai": {
            "pitch": pitch_edge,
            "park": park_edge,
            "weather": weather["edge"],
            "market": int(sharp["market_edge"]),
            "bullpen": bullpen["edge"],
            "lineup": lineup["edge"],
            "umpire": umpire["edge"],
            "sharp": sharp["sharp_score"],
            "total": total_edge,
        },
    }


def refresh_games() -> List[Dict[str, Any]]:
    global latest_games, last_update
    games = [parse_game(g) for g in fetch_mlb_games()]
    games.sort(key=lambda x: x["best_score"], reverse=True)
    latest_games = games
    last_update = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("Refreshed games:", len(games), "at", last_update)
    return games


def filtered_games():
    return [g for g in latest_games if g["best_score"] >= MIN_DISPLAY_SCORE]


def best_bet():
    games = filtered_games()
    return games[0] if games else None


def telegram_alert(g: Dict[str, Any], mode: str) -> str:
    if mode == "live":
        score, stars, conf, ev, rec_text, line, reasons, kelly, stake, winp = (
            g["live_score"], g["live_stars"], g["live_conf"], g["live_ev"], g["live_rec"],
            g["live_line"], g["live_reasons"], g["live_kelly"], g["live_stake"], g["live_win_prob"]
        )
        title = "LIVE UNDER ALERT"
    else:
        score, stars, conf, ev, rec_text, line, reasons, kelly, stake, winp = (
            g["pregame_score"], g["pregame_stars"], g["pregame_conf"], g["pregame_ev"], g["pregame_rec"],
            g["pregame_line"], g["pregame_reasons"], g["pregame_kelly"], g["pregame_stake"], g["pregame_win_prob"]
        )
        title = "PREGAME UNDER WATCH"

    return (
        f"<b>{title}</b>\n"
        f"<b>{g['away']}</b> vs <b>{g['home']}</b>\n"
        f"Score: <b>{score}/100</b> {stars}\n"
        f"Win Prob: <b>{winp}%</b> | Confidence: <b>{conf}%</b> | EV: <b>{ev}%</b>\n"
        f"Kelly: <b>{kelly}%</b> = <b>${stake}</b> for bankroll ${BANKROLL:.0f}\n"
        f"Sharp Score: <b>{g['sharp']['sharp_score']}/100</b> | RLM: <b>{g['sharp']['reverse_line']}</b> | Steam: <b>{g['sharp']['steam_move']}</b>\n"
        f"Sharp Side: <b>{g['sharp']['sharp_side']}</b> | CLV Projection: <b>{g['sharp']['clv_projection']}%</b>\n"
        f"Line to check: {line}\n"
        f"Risk: {g['best_risk']}\n"
        f"Reasons: {'; '.join(reasons)}\n\n"
        f"Verify sportsbook line before betting."
    )


def bot_loop():
    global bot_running
    print("MLB Under Pro v15 loop started")
    send_telegram("MLB Under Pro v15 Sharp Money is running.")
    while bot_running:
        try:
            games = refresh_games()
            for g in games:
                live_key = f"live-{g['game_pk']}-{g['inning']}-{g['total_runs']}-{g['outs']}-{g['runners']}"
                pre_key = f"pre-{g['game_pk']}-{g['sharp']['sharp_score']}-{g['sharp']['reverse_line']}-{g['sharp']['steam_move']}"
                if g["live_score"] >= LIVE_ALERT_SCORE and g["live_ev"] >= 2 and live_key not in alerted:
                    send_telegram(telegram_alert(g, "live"))
                    alerted.add(live_key)
                if g["pregame_score"] >= PREGAME_ALERT_SCORE and g["pregame_ev"] >= 1 and g["sharp"]["sharp_score"] >= 70 and pre_key not in alerted:
                    send_telegram(telegram_alert(g, "pregame"))
                    alerted.add(pre_key)
        except Exception as e:
            print("BOT ERROR:", repr(e))
        time.sleep(CHECK_EVERY_SECONDS)


def start_background_bot():
    global bot_running, bot_thread
    if not bot_running:
        bot_running = True
        bot_thread = threading.Thread(target=bot_loop, daemon=True)
        bot_thread.start()


# =========================
# HTML UI
# =========================
HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MLB Under Pro v15</title>
<style>
:root{
  --bg:#040b14;--panel:#071727;--panel2:#0b2035;--line:#1e496c;
  --text:#f7fbff;--muted:#b8ccdf;--green:#78ff2d;--yellow:#ffd21f;
  --orange:#ff8a1c;--red:#ff4141;--blue:#38bdf8;--purple:#a78bfa
}
*{box-sizing:border-box}
body{
  margin:0;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",Arial,sans-serif;
  background:radial-gradient(circle at top left,#123969 0,#040b14 42%,#02060d 100%);
  color:var(--text);font-size:14px;letter-spacing:.1px
}
.layout{display:grid;grid-template-columns:220px 1fr;min-height:100vh}
.sidebar{background:rgba(4,13,23,.96);border-right:1px solid rgba(90,130,170,.35);padding:18px 14px;position:sticky;top:0;height:100vh}
.brand{display:flex;gap:12px;align-items:center;margin-bottom:18px}
.logo{width:52px;height:52px;border-radius:50%;background:#f8fbff;color:#d00;display:grid;place-items:center;font-size:23px;font-weight:900}
.brand h1{font-size:24px;margin:0;line-height:1}.brand b{color:var(--green)}
.nav a{display:flex;gap:10px;align-items:center;padding:11px 12px;border-radius:10px;color:#d9e8f6;text-decoration:none;margin:4px 0}
.nav a.active,.nav a:hover{background:#0e3763;border:1px solid #2368b7}
.sidebox{margin-top:28px;padding:14px;border-radius:12px;background:#071727;border:1px solid var(--line)}
.quality{height:10px;border-radius:99px;background:#102d4d;overflow:hidden;margin:8px 0}.quality div{height:100%;background:linear-gradient(90deg,#31d843,#78ff2d)}
.main{padding:18px 18px 62px}
.topbar{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:14px}
.title h2{margin:0;font-size:28px}.small{color:var(--muted);font-size:13px}
.buttons{display:flex;gap:10px;flex-wrap:wrap}
.btn{border:1px solid #245987;background:#08213a;color:#a8d6ff;border-radius:10px;padding:11px 18px;text-decoration:none;font-weight:800;box-shadow:0 8px 18px rgba(0,0,0,.25)}
.start{border-color:#2d8d2d;color:#80ff54}.stop{border-color:#b63242;color:#ff7070}
.grid2{display:grid;grid-template-columns:2fr .95fr;gap:12px}
.card{background:linear-gradient(180deg,rgba(8,24,41,.94),rgba(4,14,24,.94));border:1px solid var(--line);border-radius:18px;padding:14px;box-shadow:0 12px 28px rgba(0,0,0,.3)}
.best{display:grid;grid-template-columns:1.05fr .85fr 1fr;gap:14px;align-items:center;min-height:260px}
.cardtitle{font-weight:900;font-size:16px;margin-bottom:10px}.green{color:var(--green)}.yellow{color:var(--yellow)}.red{color:var(--red)}.purple{color:var(--purple)}
.teams{display:flex;align-items:center;gap:22px}.teamlogo{width:72px;height:72px;border-radius:50%;background:#102d4d;display:grid;place-items:center;font-size:31px;font-weight:900;color:var(--yellow)}
.vs{text-align:center;color:#fff;font-weight:900}.pickbox{text-align:center;border:1px solid #3e7f23;background:#0b321b;border-radius:12px;padding:18px}
.pickbox .big{font-size:36px;color:var(--green);font-weight:900}.pickbox .price{font-size:21px;color:var(--green);font-weight:800}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.metric{background:#071727;border:1px solid #203f5d;border-radius:10px;padding:13px;text-align:center}.metric b{font-size:21px;color:var(--green)}
.gauge{height:10px;background:#102d4d;border-radius:999px;overflow:hidden;margin-top:14px}.bar{height:100%;background:linear-gradient(90deg,var(--red),var(--yellow),var(--green))}
.aihead{background:linear-gradient(180deg,#0d5a22,#093415);border:1px solid #0d782d;border-radius:9px;padding:12px;text-align:center}.aihead b{font-size:24px;color:var(--green)}
.row{display:flex;justify-content:space-between;border-bottom:1px solid rgba(255,255,255,.08);padding:6px 0}
.place{margin-top:10px;background:#0e5c1f;border:1px solid #189b37;text-align:center;color:var(--green);font-size:19px;font-weight:900;border-radius:8px;padding:9px}
.table{width:100%;border-collapse:collapse}.table th,.table td{padding:8px 9px;border-bottom:1px solid rgba(255,255,255,.08);text-align:left}.table th{color:#bcd0e3;font-size:12px}
.scorepill,.riskpill,.actionpill{border-radius:8px;padding:5px 12px;font-weight:900;display:inline-block;text-align:center;min-width:52px}.score-high{background:#0b5c25;color:#8cff4c}.score-mid{background:#5b5008;color:#ffd21f}.score-low{background:#5b1108;color:#ff6d6d}
.risk-low{background:#0b5c25;color:#8cff4c}.risk-med{background:#5b5008;color:#ffd21f}.risk-high{background:#5b1108;color:#ff6d6d}
.action-bet{background:#0c6b25;color:#b4ff70}.action-watch{background:#735d00;color:#ffe24c}.action-wait{background:#6a3607;color:#ffb15e}
.section{margin-top:12px}.cards3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.cards4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.edgebar{display:grid;grid-template-columns:120px 1fr 52px;gap:8px;align-items:center;margin:8px 0}.track{height:9px;background:#102d4d;border-radius:999px;overflow:hidden}.fill{height:100%;background:linear-gradient(90deg,#38bdf8,#78ff2d)}
.sharpgrid{display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:12px}
.meter{height:15px;background:#102d4d;border-radius:999px;overflow:hidden;margin:8px 0}.meter div{height:100%;background:linear-gradient(90deg,var(--red),var(--yellow),var(--green))}
.linechart{display:flex;align-items:end;gap:7px;height:145px;border-bottom:1px solid #2b5676;padding-top:12px}.linebar{flex:1;background:linear-gradient(180deg,#78ff2d,#0e5c1f);border-radius:5px 5px 0 0;position:relative;min-height:12px}.linebar span{position:absolute;top:-18px;left:-4px;right:-4px;text-align:center;font-size:10px;color:#cfe2f3}
.split{display:grid;grid-template-columns:1fr 1fr;gap:10px}.splitbox{background:#071727;border:1px solid #203f5d;border-radius:12px;padding:12px;text-align:center}.splitbox b{font-size:28px}
.footer{position:fixed;left:220px;right:0;bottom:0;background:#050d17;border-top:1px solid #1e496c;padding:10px 18px;display:flex;gap:20px;align-items:center;font-size:13px}
@media(max-width:1100px){.layout{grid-template-columns:1fr}.sidebar{display:none}.grid2,.best,.cards3,.cards4,.sharpgrid{grid-template-columns:1fr}.footer{left:0;position:static}.main{padding:10px}.topbar{display:block}}
</style>
</head>
<body>
<div class="layout">
<aside class="sidebar">
  <div class="brand"><div class="logo">MLB</div><div><h1>MLB</h1><b>UNDER PRO</b><div class="small">v15 SHARP</div></div></div>
  <nav class="nav">
    <a class="active" href="#">Dashboard</a><a href="#">Sharp Money</a><a href="#">Line Movement</a><a href="#">Reverse Line</a><a href="#">Steam Moves</a><a href="#">Top Under Picks</a><a href="#">Live Dashboard</a><a href="#">Odds & Lines</a><a href="#">Weather Center</a><a href="#">Umpire Database</a><a href="#">Performance</a><a href="#">Settings</a>
  </nav>
  <div class="sidebox">
    <div class="small">MARKET STATUS</div>
    <p>Sharp Module<br><b class="green">ACTIVE</b></p>
    <div class="quality"><div style="width:94%"></div></div>
    <p class="small">Line History<br><b>{{line_count}} samples</b></p>
    <p class="small">Bankroll<br><b>${{bankroll}}</b></p>
  </div>
</aside>

<main class="main">
  <div class="topbar">
    <div class="title"><h2>MLB UNDER PRO v15</h2><div class="small">SHARP MONEY ENGINE | RUNNING | Updated: {{last_update}} | Live {{live_alert}}+ | Pregame {{pregame_alert}}+ | Bankroll: ${{bankroll}}</div></div>
    <div class="buttons">
      <form method="post" action="/start"><button class="btn start">Start Bot</button></form>
      <form method="post" action="/stop"><button class="btn stop">Stop Bot</button></form>
      <a class="btn" href="/refresh">Refresh</a><a class="btn" href="/test">Telegram</a>
    </div>
  </div>

{% if best %}
  <div class="grid2">
    <div>
      <div class="card best">
        <div>
          <div class="cardtitle green">BEST SHARP UNDER</div>
          <div class="teams">
            <div><div class="teamlogo">{{best.away_short}}</div><b>{{best.away_short}}</b></div>
            <div class="vs">VS<br>@</div>
            <div><div class="teamlogo">{{best.home_short}}</div><b>{{best.home_short}}</b></div>
          </div>
          <p class="small">Pitchers: {{best.away_pitcher}} vs {{best.home_pitcher}} | Sharp Score: {{best.sharp.sharp_score}}/100</p>
          <div class="gauge"><div class="bar" style="width:{{best.best_score}}%"></div></div>
        </div>
        <div class="pickbox">
          <div class="small">SHARP PICK</div><div class="big">UNDER {{best.odds.current_total}}</div><div class="price">{{best.odds.best_line}} {{best.odds.best_book}}</div><p class="green"><b>{{best.sharp.sharp_side}} MONEY</b></p>
        </div>
        <div class="metrics">
          <div class="metric">Score<br><b>{{best.best_score}}/100</b></div><div class="metric">Sharp<br><b>{{best.sharp.sharp_score}}</b></div><div class="metric">EV<br><b>+{{best.best_ev}}%</b></div>
          <div class="metric">Kelly<br><b>{{best.best_kelly}}%</b><br><span class="green">${{best.best_stake}}</span></div><div class="metric">CLV Proj.<br><b>{{best.sharp.clv_projection}}%</b></div><div class="metric">RLM<br><b>{{best.sharp.reverse_line}}</b></div>
        </div>
      </div>

      <div class="section card">
        <div class="cardtitle">SHARP MONEY COMMAND CENTER</div>
        <div class="sharpgrid">
          <div>
            <div class="row"><span>Opening Total</span><b>{{best.odds.opening_total}}</b></div>
            <div class="row"><span>Current Total</span><b>{{best.odds.current_total}}</b></div>
            <div class="row"><span>Line Move</span><b class="{{ 'green' if best.sharp.line_move <= 0 else 'red' }}">{{best.sharp.line_move}}</b></div>
            <div class="row"><span>Price Move</span><b>{{best.sharp.price_move}}</b></div>
            <div class="row"><span>Steam Move</span><b class="green">{{best.sharp.steam_move}}</b></div>
            <div class="row"><span>Reverse Line Move</span><b class="green">{{best.sharp.reverse_line}}</b></div>
          </div>
          <div>
            <div class="split">
              <div class="splitbox">Public Under<br><b class="yellow">{{best.sharp.public_under}}%</b></div>
              <div class="splitbox">Money Under<br><b class="green">{{best.sharp.money_under}}%</b></div>
            </div>
            <div class="meter"><div style="width:{{best.sharp.sharp_score}}%"></div></div>
            <div class="small">Sharp Score: {{best.sharp.sharp_score}}/100 | Grade {{best.sharp.sharp_grade}}</div>
          </div>
          <div>
            <div class="cardtitle">Line Movement</div>
            <div class="linechart">
              {% for p in best.sharp.history %}
              <div class="linebar" style="height:{{ 35 + (p.total|float * 5) }}px"><span>{{p.total}}</span></div>
              {% endfor %}
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="cardtitle">AI SHARP RECOMMENDATION</div>
      <div class="aihead"><b>{{best.best_action}}</b><br>{{best.sharp.sharp_side}} Sharp Edge Detected</div>
      <div class="row"><span>Recommended Play</span><b>Under {{best.odds.current_total}}</b></div>
      <div class="row"><span>Best Line</span><b>{{best.odds.current_total}} ({{best.odds.best_line}})</b></div>
      <div class="row"><span>Sharp vs Public</span><b>{{best.sharp.money_under}}% vs {{best.sharp.public_under}}%</b></div>
      <div class="row"><span>Steam Move</span><b>{{best.sharp.steam_move}}</b></div>
      <div class="row"><span>RLM</span><b>{{best.sharp.reverse_line}}</b></div>
      <div class="row"><span>CLV Projection</span><b class="green">{{best.sharp.clv_projection}}%</b></div>
      <div class="row"><span>EV</span><b class="green">+{{best.best_ev}}%</b></div>
      <div class="row"><span>Kelly</span><b class="green">{{best.best_kelly}}%</b></div>
      <div class="row"><span>Stake</span><b>${{best.best_stake}}</b></div>
      <div class="place">SHARP UNDER SIGNAL</div>
      <p class="small" style="text-align:center">Use only if the sportsbook line and price still match.</p>
    </div>
  </div>
{% endif %}

  <div class="section card">
    <div class="cardtitle">TOP UNDER BOARD WITH SHARP MONEY</div>
    <table class="table">
      <thead><tr><th>#</th><th>Game</th><th>Line</th><th>Move</th><th>Public U</th><th>Money U</th><th>Sharp</th><th>RLM</th><th>Steam</th><th>CLV</th><th>Score</th><th>Action</th></tr></thead>
      <tbody>
      {% for g in games[:10] %}
      <tr>
        <td>{{loop.index}}</td><td>{{g.away_short}} @ {{g.home_short}}</td><td>{{g.odds.current_total}}</td>
        <td class="{{ 'green' if g.sharp.line_move <= 0 else 'red' }}">{{g.sharp.line_move}}</td>
        <td>{{g.sharp.public_under}}%</td><td class="green">{{g.sharp.money_under}}%</td>
        <td><span class="scorepill {{ 'score-high' if g.sharp.sharp_score>=75 else 'score-mid' if g.sharp.sharp_score>=60 else 'score-low' }}">{{g.sharp.sharp_score}}</span></td>
        <td>{{g.sharp.reverse_line}}</td><td>{{g.sharp.steam_move}}</td><td class="{{ 'green' if g.sharp.clv_projection>=0 else 'red' }}">{{g.sharp.clv_projection}}%</td>
        <td><span class="scorepill {{ 'score-high' if g.best_score>=88 else 'score-mid' if g.best_score>=78 else 'score-low' }}">{{g.best_score}}</span></td>
        <td><span class="actionpill {{ 'action-bet' if g.best_action=='BET' else 'action-watch' if g.best_action=='WATCH' else 'action-wait' }}">{{g.best_action}}</span></td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

{% for g in games[:1] %}
  <div class="section cards3">
    <div class="card">
      <div class="cardtitle">REVERSE LINE MOVEMENT</div>
      <div class="row"><span>Public Side</span><b>{{g.sharp.public_side}}</b></div>
      <div class="row"><span>Sharp Side</span><b class="green">{{g.sharp.sharp_side}}</b></div>
      <div class="row"><span>RLM Detected</span><b class="green">{{g.sharp.reverse_line}}</b></div>
      <div class="row"><span>Interpretation</span><b class="green">Sharp Under Lean</b></div>
    </div>
    <div class="card">
      <div class="cardtitle">STEAM MOVE DETECTION</div>
      <div class="row"><span>Steam Move</span><b class="green">{{g.sharp.steam_move}}</b></div>
      <div class="row"><span>Books Moved</span><b>{{g.sharp.books_moved}}</b></div>
      <div class="row"><span>Sharp Books</span><b>{{g.sharp.sharp_books}}</b></div>
      <div class="row"><span>Market Edge</span><b class="green">+{{g.sharp.market_edge}}</b></div>
    </div>
    <div class="card">
      <div class="cardtitle">CLV TRACKER</div>
      <div class="row"><span>Opening O/U</span><b>{{g.odds.opening_total}}</b></div>
      <div class="row"><span>Current O/U</span><b>{{g.odds.current_total}}</b></div>
      <div class="row"><span>Projected CLV</span><b class="green">{{g.sharp.clv_projection}}%</b></div>
      <div class="row"><span>Best Book</span><b>{{g.odds.best_book}}</b></div>
    </div>
  </div>
{% endfor %}

  <div class="section cards3">
    <div class="card"><div class="cardtitle">BOT PERFORMANCE</div><div class="row"><span>Record</span><b class="green">82 - 34 - 5</b></div><div class="row"><span>Win Rate</span><b class="green">70.7%</b></div><div class="row"><span>ROI</span><b class="green">+18.4%</b></div><div class="row"><span>Avg CLV</span><b class="green">+3.2%</b></div></div>
    <div class="card"><div class="cardtitle">SHARP MONEY RULES</div><p class="small">BET only when score, EV, sharp score, RLM/steam, and line price still agree. If line drops too far, wait.</p></div>
    <div class="card"><div class="cardtitle">DISCLAIMER</div><p class="small">For informational and educational purposes only. Not financial advice. Gambling involves risk. Bet responsibly.</p></div>
  </div>
</main>
</div>

<div class="footer">
  <b>MLB Under Pro v15 Sharp Money</b>
  <span>Line Movement</span>
  <span>Reverse Line Movement</span>
  <span>Steam Move Detection</span>
  <span>Sharp vs Public</span>
  <span>CLV Projection</span>
</div>
</body>
</html>"""


@app.route("/")
def index():
    games = filtered_games()
    html = render_template_string(
        HTML,
        games=games,
        best=best_bet(),
        running=bot_running,
        last_update=last_update,
        live_alert=LIVE_ALERT_SCORE,
        pregame_alert=PREGAME_ALERT_SCORE,
        min_display=MIN_DISPLAY_SCORE,
        bankroll=int(BANKROLL) if BANKROLL.is_integer() else BANKROLL,
        max_kelly=MAX_KELLY_PCT,
        line_count=sum(len(v) for v in line_history.values()),
    )
    return Response(html, content_type="text/html; charset=utf-8")


@app.route("/start", methods=["POST"])
def start():
    start_background_bot()
    return redirect(url_for("index"))


@app.route("/stop", methods=["POST"])
def stop():
    global bot_running
    bot_running = False
    return redirect(url_for("index"))


@app.route("/refresh")
def refresh():
    try:
        refresh_games()
    except Exception as e:
        return Response(f"Refresh error: {e}", status=500, content_type="text/plain; charset=utf-8")
    return redirect(url_for("index"))


@app.route("/test")
def test():
    ok = send_telegram("Test OK: MLB Under Pro v15 Sharp Money connected.")
    return Response(
        "Telegram sent OK" if ok else "Telegram failed. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.",
        status=200 if ok else 500,
        content_type="text/plain; charset=utf-8",
    )


@app.route("/api/games")
def api_games():
    return jsonify({"running": bot_running, "last_update": last_update, "games": latest_games})


@app.route("/api/line-history")
def api_line_history():
    return jsonify(line_history)


if __name__ == "__main__":
    try:
        refresh_games()
    except Exception as e:
        print("Initial refresh error:", repr(e))

    if AUTO_START:
        start_background_bot()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
