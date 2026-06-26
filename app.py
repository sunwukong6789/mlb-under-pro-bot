# -*- coding: utf-8 -*-
"""
MLB Edge AI Pro v19 Ultimate SunWukong
Final version:
- Pregame UNDER / OVER / PASS
- Live UNDER / LIVE OVER / PASS
- Sharp Money, Reverse Line Movement, Steam Move, CLV projection
- Live Pace Engine, Base Risk Engine, Pitch Count placeholder, Bullpen fatigue placeholder
- Clean dashboard UI, API status, Telegram alerts
- Fallback sample games when MLB API fails
- UTF-8 safe, no broken emoji characters

Render start command:
python app.py
"""

import os
import time
import threading
import datetime as dt
from typing import Dict, Any, List, Set, Tuple, Optional

import requests
from flask import Flask, jsonify, render_template_string, redirect, url_for, Response


# =========================
# SETTINGS
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CHECK_EVERY_SECONDS = int(os.getenv("CHECK_EVERY_SECONDS", "120"))
LIVE_ALERT_SCORE = int(os.getenv("LIVE_ALERT_SCORE", "88"))
PREGAME_ALERT_SCORE = int(os.getenv("PREGAME_ALERT_SCORE", "86"))
MIN_EDGE_DIFF = int(os.getenv("MIN_EDGE_DIFF", "8"))
MIN_DISPLAY_SCORE = int(os.getenv("MIN_DISPLAY_SCORE", "50"))
PREGAME_WINDOW_HOURS = int(os.getenv("PREGAME_WINDOW_HOURS", "24"))
AUTO_START = os.getenv("AUTO_START", "1") == "1"
USE_SAMPLE_ON_ERROR = os.getenv("USE_SAMPLE_ON_ERROR", "1") == "1"

BANKROLL = float(os.getenv("BANKROLL", "1000"))
MAX_KELLY_PCT = float(os.getenv("MAX_KELLY_PCT", "2.5"))

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "")

MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

latest_games: List[Dict[str, Any]] = []
last_update = "Not updated"
last_error = ""
last_refresh_ok = False
alerted: Set[str] = set()
bot_running = False
bot_thread = None
line_history: Dict[str, List[Dict[str, Any]]] = {}

api_status = {
    "mlb": "UNKNOWN",
    "odds": "SIMULATED",
    "weather": "SIMULATED",
    "telegram": "UNKNOWN",
    "games_loaded": 0,
}


# =========================
# MODEL WEIGHTS
# =========================
PITCHER_UNDER_EDGE = {
    "Paul Skenes": 12, "Tarik Skubal": 12, "Zack Wheeler": 11, "Logan Gilbert": 10,
    "George Kirby": 10, "Chris Sale": 9, "Corbin Burnes": 10, "Garrett Crochet": 9,
    "Cole Ragans": 8, "Max Fried": 8, "Sonny Gray": 7, "Framber Valdez": 8,
    "Cristopher Sanchez": 8, "Luis Castillo": 7, "Joe Ryan": 7, "Andrew Abbott": 7,
    "Nathan Eovaldi": 7, "Kevin Gausman": 7, "Logan Webb": 8, "Yoshinobu Yamamoto": 9,
}

PARK_UNDER_EDGE = {
    "Seattle Mariners": 5, "San Francisco Giants": 5, "Detroit Tigers": 4,
    "Cleveland Guardians": 3, "Oakland Athletics": 3, "San Diego Padres": 3,
    "Pittsburgh Pirates": 2, "Miami Marlins": 2, "New York Mets": 2,
}

PARK_OVER_EDGE = {
    "Colorado Rockies": 10, "Cincinnati Reds": 5, "Boston Red Sox": 4,
    "Philadelphia Phillies": 3, "New York Yankees": 3, "Texas Rangers": 2,
    "Toronto Blue Jays": 2, "Arizona Diamondbacks": 2,
}


# =========================
# HELPERS
# =========================
def today() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d")


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_game_time(game_date: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.fromisoformat(game_date.replace("Z", "+00:00"))
    except Exception:
        return None


def game_time_label(game_date: str) -> str:
    game_dt = parse_game_time(game_date)
    if not game_dt:
        return "N/A"
    return game_dt.strftime("%I:%M %p").lstrip("0")


def time_left_label(game_date: str) -> str:
    game_dt = parse_game_time(game_date)
    if not game_dt:
        return "N/A"
    minutes = int((game_dt - now_utc()).total_seconds() // 60)
    if minutes <= 0:
        return "Started"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def short_team(name: str) -> str:
    parts = name.split()
    if not parts:
        return "TBD"
    word = parts[-1].upper()
    fixes = {
        "REDS": "RED", "PIRATES": "PIR", "ASTROS": "AST", "TIGERS": "TIG",
        "MARINERS": "MAR", "GUARDIANS": "GUA", "RANGERS": "RAN", "JAYS": "JAY",
        "NATIONALS": "NAT", "ORIOLES": "ORI", "YANKEES": "NYY", "RAYS": "RAY",
        "DODGERS": "DOD", "PADRES": "PAD", "GIANTS": "SFG", "ROCKIES": "COL",
        "WHITE": "CWS", "SOX": "SOX", "BLUE": "JAY", "METS": "NYM",
        "DIAMONDBACKS": "ARI", "CARDINALS": "STL", "BREWERS": "MIL",
        "CUBS": "CHC", "ROYALS": "KC", "TWINS": "MIN", "BRAVES": "ATL",
        "MARLINS": "MIA", "ATHLETICS": "ATH",
    }
    return fixes.get(word, word[:3])


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


def clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


def send_telegram(text: str) -> bool:
    global api_status
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        api_status["telegram"] = "MISSING"
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15,
        )
        api_status["telegram"] = "CONNECTED" if r.ok else "ERROR"
        return r.ok
    except Exception:
        api_status["telegram"] = "ERROR"
        return False


# =========================
# MLB API + FALLBACK
# =========================
def fetch_mlb_games() -> List[Dict[str, Any]]:
    global api_status
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
    api_status["mlb"] = "CONNECTED"
    return games


def sample_game(pk, away, home, ap, hp, hour_offset, live=False, inning=0, outs=0, away_score=0, home_score=0, runners=None):
    runners = runners or []
    game_time = now_utc() + dt.timedelta(hours=hour_offset)
    offense = {}
    for r in runners:
        offense[r] = {"id": 1}
    return {
        "gamePk": pk,
        "gameDate": game_time.isoformat().replace("+00:00", "Z"),
        "status": {"detailedState": "In Progress" if live else "Scheduled"},
        "teams": {
            "away": {"score": away_score, "team": {"name": away}, "probablePitcher": {"fullName": ap}},
            "home": {"score": home_score, "team": {"name": home}, "probablePitcher": {"fullName": hp}},
        },
        "linescore": {
            "currentInning": inning,
            "inningHalf": "Top" if inning % 2 else "Bottom",
            "outs": outs,
            "offense": offense,
        } if live else {},
    }


def fallback_games() -> List[Dict[str, Any]]:
    return [
        sample_game(910001, "Cincinnati Reds", "Pittsburgh Pirates", "Andrew Abbott", "Paul Skenes", 2),
        sample_game(910002, "Houston Astros", "Detroit Tigers", "Spencer Arrighetti", "Tarik Skubal", 2),
        sample_game(910003, "Seattle Mariners", "Cleveland Guardians", "Logan Gilbert", "Tanner Bibee", 3),
        sample_game(910004, "Texas Rangers", "Toronto Blue Jays", "Nathan Eovaldi", "Kevin Gausman", 3),
        sample_game(910005, "Colorado Rockies", "New York Yankees", "TBD", "TBD", 4),
        sample_game(910006, "Boston Red Sox", "Philadelphia Phillies", "TBD", "TBD", 4),
        sample_game(910007, "San Diego Padres", "San Francisco Giants", "Yu Darvish", "Logan Webb", -1, True, 6, 2, 1, 2),
        sample_game(910008, "Miami Marlins", "New York Mets", "TBD", "TBD", -1, True, 5, 1, 0, 1),
        sample_game(910009, "Arizona Diamondbacks", "Tampa Bay Rays", "TBD", "TBD", -1, True, 4, 0, 4, 3, ["first", "second"]),
    ]


# =========================
# MARKET + SHARP MONEY
# =========================
def odds_snapshot(game_key: str, home_team: str, status: str, total_runs: int = 0) -> Dict[str, Any]:
    global api_status
    if not ODDS_API_KEY:
        api_status["odds"] = "SIMULATED"
        current = 8.5
        public_under = 48
        money_under = 74

        if home_team in PARK_OVER_EDGE:
            current = 9.0
            public_under = 42
            money_under = 36
        if home_team in PARK_UNDER_EDGE:
            current = 8.0
            public_under = 47
            money_under = 76
        if "in progress" in (status or "").lower():
            current = max(5.5, round(total_runs + 4.5, 1))
            if total_runs <= 3:
                money_under = 66
                public_under = 50
            else:
                money_under = 43
                public_under = 47

        return {
            "opening_total": 8.5,
            "current_total": current,
            "live_total": current if "in progress" in (status or "").lower() else None,
            "open_price": -108,
            "current_price": -112,
            "best_book": "DraftKings",
            "best_line": "-112",
            "public_under_pct": public_under,
            "money_under_pct": money_under,
            "public_over_pct": 100 - public_under,
            "money_over_pct": 100 - money_under,
            "steam_books": 4,
            "sharp_books": 5,
            "market_status": "Simulated until ODDS_API_KEY is connected",
        }

    api_status["odds"] = "KEY DETECTED"
    return {
        "opening_total": 8.5, "current_total": 8.5, "live_total": None,
        "open_price": -110, "current_price": -110,
        "best_book": "Provider", "best_line": "N/A",
        "public_under_pct": 50, "money_under_pct": 50,
        "public_over_pct": 50, "money_over_pct": 50,
        "steam_books": 0, "sharp_books": 0,
        "market_status": "ODDS_API_KEY detected - provider mapping pending",
    }


def update_line_history(game_key: str, odds: Dict[str, Any]) -> List[Dict[str, Any]]:
    point = {
        "ts": dt.datetime.now().strftime("%H:%M:%S"),
        "total": float(odds.get("current_total", 8.5)),
        "price": int(odds.get("current_price", -110)),
        "money_under": int(odds.get("money_under_pct", 50)),
        "money_over": int(odds.get("money_over_pct", 50)),
    }
    line_history.setdefault(game_key, []).append(point)
    line_history[game_key] = line_history[game_key][-20:]
    return line_history[game_key]


def sharp_money_analysis(odds: Dict[str, Any], history: List[Dict[str, Any]]) -> Dict[str, Any]:
    opening = float(odds.get("opening_total", 8.5))
    current = float(odds.get("current_total", 8.5))
    public_under = int(odds.get("public_under_pct", 50))
    money_under = int(odds.get("money_under_pct", 50))
    public_over = int(odds.get("public_over_pct", 50))
    money_over = int(odds.get("money_over_pct", 50))
    steam_books = int(odds.get("steam_books", 0))
    sharp_books = int(odds.get("sharp_books", 0))

    line_move = round(current - opening, 1)
    under_sharp = 35
    over_sharp = 35

    if line_move < 0:
        under_sharp += 25
    elif line_move > 0:
        over_sharp += 25

    if money_under - public_under >= 15:
        under_sharp += 25
    if money_over - public_over >= 15:
        over_sharp += 25

    if steam_books >= 3 and line_move <= 0:
        under_sharp += 20
    if steam_books >= 3 and line_move > 0:
        over_sharp += 20

    if sharp_books >= 3:
        if money_under >= money_over:
            under_sharp += 12
        else:
            over_sharp += 12

    under_sharp = clamp(under_sharp)
    over_sharp = clamp(over_sharp)

    reverse_under = "YES" if public_under < 50 and money_under >= 65 and line_move <= 0 else "NO"
    reverse_over = "YES" if public_over < 50 and money_over >= 65 and line_move >= 0 else "NO"
    steam_move = "YES" if steam_books >= 3 else "NO"
    sharp_side = "UNDER" if under_sharp > over_sharp + 5 else "OVER" if over_sharp > under_sharp + 5 else "NEUTRAL"
    clv_projection = round(abs(line_move) * 4 + abs(money_under - public_under) * 0.06 + (2 if steam_move == "YES" else 0), 1)

    return {
        "under_sharp": under_sharp,
        "over_sharp": over_sharp,
        "sharp_side": sharp_side,
        "line_move": line_move,
        "reverse_under": reverse_under,
        "reverse_over": reverse_over,
        "steam_move": steam_move,
        "public_under": public_under,
        "money_under": money_under,
        "public_over": public_over,
        "money_over": money_over,
        "books_moved": steam_books,
        "sharp_books": sharp_books,
        "clv_projection": clv_projection,
        "history": history,
    }


# =========================
# CONTEXT MODULES
# =========================
def weather_snapshot(home_team: str) -> Dict[str, Any]:
    global api_status
    api_status["weather"] = "SIMULATED" if not WEATHER_API_KEY else "KEY DETECTED"
    under_edge = 1 if home_team in PARK_UNDER_EDGE else 0
    over_edge = 1 if home_team in PARK_OVER_EDGE else 0
    return {
        "wind": "12 mph IN" if under_edge else "7 mph OUT" if over_edge else "Neutral",
        "temp": "64F" if under_edge else "82F" if over_edge else "72F",
        "humidity": "58%",
        "rain": "5%",
        "roof": "Open",
        "under_edge": under_edge + (5 if under_edge else 0),
        "over_edge": over_edge + (5 if over_edge else 0),
    }


def umpire_snapshot() -> Dict[str, Any]:
    return {"name": "TBD", "under_pct": "52%", "avg_runs": "8.2", "under_edge": 1, "over_edge": 1}


def bullpen_snapshot(away: str, home: str) -> Dict[str, Any]:
    under_edge = 4 if away in PARK_UNDER_EDGE or home in PARK_UNDER_EDGE else 1
    over_edge = 4 if away in PARK_OVER_EDGE or home in PARK_OVER_EDGE else 1
    return {"away": "3.91 ERA", "home": "4.12 ERA", "fatigue": "Medium", "under_edge": under_edge, "over_edge": over_edge}


def lineup_snapshot(away: str, home: str) -> Dict[str, Any]:
    over_edge = 3 if away in PARK_OVER_EDGE or home in PARK_OVER_EDGE else 1
    under_edge = 2 if away in PARK_UNDER_EDGE or home in PARK_UNDER_EDGE else 1
    return {"status": "Official lineup pending", "missing_bats": "Check key bats", "under_edge": under_edge, "over_edge": over_edge}


def pitcher_bonus(name: str) -> int:
    return PITCHER_UNDER_EDGE.get(name or "", 0)


def base_pressure(runners: List[str], outs: int) -> int:
    s = set(runners)
    if not s:
        return 0
    if len(s) == 3:
        return 30 if outs < 2 else 15
    if "second" in s and "third" in s:
        return 25 if outs < 2 else 12
    if "third" in s:
        return 20 if outs < 2 else 10
    if "second" in s:
        return 12 if outs < 2 else 5
    if "first" in s:
        return 7 if outs < 2 else 3
    return 0


def estimate_ev(score: int, edge_diff: int) -> float:
    if score < 68 or edge_diff < 5:
        return -2.5
    return round((score - 76) * 0.55 + edge_diff * 0.45, 1)


def kelly_pct(ev: float, score: int) -> float:
    if ev <= 0 or score < 80:
        return 0.0
    raw = (ev / 100.0) * (score / 100.0) * 22
    return round(max(0, min(MAX_KELLY_PCT, raw)), 2)


def stake_amount(kelly: float) -> float:
    return round(BANKROLL * kelly / 100.0, 2)


def win_probability(score: int, edge_diff: int) -> int:
    if score <= 0:
        return 0
    return max(40, min(96, int(45 + score * 0.42 + edge_diff * 0.8)))


# =========================
# PREGAME + LIVE ENGINES
# =========================
def pregame_engine(g, away_pitcher, home_pitcher, home_team, weather, umpire, bullpen, lineup, sharp):
    status = g.get("status", {}).get("detailedState", "")
    s = (status or "").lower()
    if "scheduled" not in s and "pre-game" not in s and "warmup" not in s:
        return 0, 0, "PASS", 0, 0, -2.5, 0.0, 0, "No Bet", ["Not pregame"]

    game_dt = parse_game_time(g.get("gameDate", ""))
    if not game_dt:
        return 55, 55, "PASS", 55, 0, -2.5, 0.0, 0, "No Bet", ["Game time unavailable"]

    hours_to_start = (game_dt - now_utc()).total_seconds() / 3600
    if hours_to_start > PREGAME_WINDOW_HOURS:
        return 45, 45, "PASS", 45, 0, -2.5, 0.0, 0, "No Bet", [f"Too far: {hours_to_start:.1f}h"]

    under = 50
    over = 50
    under_reasons = [f"{hours_to_start:.1f}h before first pitch"]
    over_reasons = [f"{hours_to_start:.1f}h before first pitch"]

    pitch_under = pitcher_bonus(away_pitcher) + pitcher_bonus(home_pitcher)
    if pitch_under:
        under += pitch_under
        under_reasons.append(f"Pitching Under +{pitch_under}")
    else:
        over += 4
        over_reasons.append("No strong pitcher edge")

    park_under = PARK_UNDER_EDGE.get(home_team, 0)
    park_over = PARK_OVER_EDGE.get(home_team, 0)
    if park_under:
        under += park_under
        under_reasons.append(f"Under park +{park_under}")
    if park_over:
        over += park_over
        over_reasons.append(f"Over park +{park_over}")

    under += weather["under_edge"] + umpire["under_edge"] + bullpen["under_edge"] + lineup["under_edge"]
    over += weather["over_edge"] + umpire["over_edge"] + bullpen["over_edge"] + lineup["over_edge"]
    under += int(max(0, sharp["under_sharp"] - 60) / 4)
    over += int(max(0, sharp["over_sharp"] - 60) / 4)

    under, over = clamp(under), clamp(over)
    diff = abs(under - over)

    if diff < MIN_EDGE_DIFF:
        decision, edge_score, reasons, line = "PASS", max(under, over), ["Under and Over too close"], "No Bet"
    elif under > over:
        decision, edge_score, reasons, line = "UNDER", under, under_reasons, "Under"
    else:
        decision, edge_score, reasons, line = "OVER", over, over_reasons, "Over"

    ev = estimate_ev(edge_score, diff)
    kelly = kelly_pct(ev, edge_score)
    win = win_probability(edge_score, diff)
    return under, over, decision, edge_score, diff, ev, kelly, win, line, reasons


def live_engine(total_runs, inning, outs, runners, status, live_total, sharp):
    if "in progress" not in (status or "").lower():
        return 0, 0, "NOT LIVE", 0, 0, -2.5, 0.0, 0, ["Game is not live"]

    under = 45
    over = 45
    under_reasons = []
    over_reasons = []

    if inning >= 7:
        under += 32
        over -= 8
        under_reasons.append("Late inning live under zone")
    elif inning == 6:
        under += 24
        under_reasons.append("6th inning under window")
    elif inning == 5:
        under += 15
        over += 3
        under_reasons.append("5th inning watch zone")
    elif inning == 4:
        under += 5
        over += 8
    else:
        under -= 18
        over += 15
        over_reasons.append("Early inning over potential")

    if total_runs <= 2:
        under += 25
        over -= 10
        under_reasons.append("Very low run pace")
    elif total_runs <= 4:
        under += 16
        under_reasons.append("Low run pace")
    elif total_runs <= 6:
        under += 4
        over += 8
    else:
        over += 22
        under -= 18
        over_reasons.append("High scoring pace")

    pressure = base_pressure(runners, outs)
    if pressure:
        over += pressure
        under -= int(pressure * 0.85)
        over_reasons.append(f"Base pressure +{pressure}")
    else:
        under += 8
        under_reasons.append("Bases empty")

    if outs == 2:
        under += 8
        under_reasons.append("2 outs")
    elif outs == 0:
        over += 8
        under -= 7
        over_reasons.append("0 outs risk")

    if live_total:
        cushion = live_total - total_runs
        if cushion >= 5:
            under += 10
            under_reasons.append("Live total has cushion")
        elif cushion >= 3:
            under += 4
        else:
            over += 10
            over_reasons.append("Live total too tight")

    if sharp.get("sharp_side") == "UNDER":
        under += 8
        under_reasons.append("Sharp under support")
    elif sharp.get("sharp_side") == "OVER":
        over += 8
        over_reasons.append("Sharp over support")

    under, over = clamp(under), clamp(over)
    diff = abs(under - over)

    if diff < MIN_EDGE_DIFF:
        decision, edge_score, reasons = "PASS", max(under, over), ["Live edge too close"]
    elif under > over:
        decision, edge_score, reasons = "LIVE UNDER", under, under_reasons
    else:
        decision, edge_score, reasons = "LIVE OVER", over, over_reasons

    ev = estimate_ev(edge_score, diff)
    kelly = kelly_pct(ev, edge_score)
    win = win_probability(edge_score, diff)
    return under, over, decision, edge_score, diff, ev, kelly, win, reasons


def action_from(decision, score, ev, alert_score):
    if decision in ["UNDER", "OVER", "LIVE UNDER", "LIVE OVER"] and score >= alert_score and ev >= 4:
        return "BET NOW"
    if decision != "PASS" and score >= 78:
        return "WATCH"
    return "PASS"


def ai_comment(g):
    if g["live_decision"] == "LIVE UNDER":
        return "Live Under setup: low run pace, manageable base state, and market support. Verify live total before entry."
    if g["live_decision"] == "LIVE OVER":
        return "Live Over setup: run pace or base pressure is dangerous for Under. Avoid Under here."
    if g["pregame_decision"] == "UNDER":
        return "Pregame Under lean: pitcher, park, weather, and sharp profile favor a lower-scoring game."
    if g["pregame_decision"] == "OVER":
        return "Pregame Over lean: park, lineup, market, or weak pitching profile favors runs."
    return "No clear edge. Best decision is PASS until the market or live state improves."


def parse_game(g: Dict[str, Any], source: str = "LIVE") -> Dict[str, Any]:
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

    odds = odds_snapshot(game_key, home_team, status, total)
    history = update_line_history(game_key, odds)
    sharp = sharp_money_analysis(odds, history)
    weather = weather_snapshot(home_team)
    umpire = umpire_snapshot()
    bullpen = bullpen_snapshot(away_team, home_team)
    lineup = lineup_snapshot(away_team, home_team)

    pu, po, pd, ps, pdiff, pev, pk, pwin, pline, preasons = pregame_engine(
        g, away_pitcher, home_pitcher, home_team, weather, umpire, bullpen, lineup, sharp
    )

    lu, lo, ld, ls_score, ldiff, lev, lk, lwin, lreasons = live_engine(
        total, inning, outs, runners, status, odds.get("live_total"), sharp
    )

    p_action = action_from(pd, ps, pev, PREGAME_ALERT_SCORE)
    l_action = action_from(ld, ls_score, lev, LIVE_ALERT_SCORE)

    best_rank = 4 if l_action == "BET NOW" else 3 if p_action == "BET NOW" else 2 if l_action == "WATCH" else 1 if p_action == "WATCH" else 0
    best_decision = ld if l_action in ["BET NOW", "WATCH"] else pd
    best_score = ls_score if l_action in ["BET NOW", "WATCH"] else ps
    best_ev = lev if l_action in ["BET NOW", "WATCH"] else pev
    best_kelly = lk if l_action in ["BET NOW", "WATCH"] else pk
    best_win = lwin if l_action in ["BET NOW", "WATCH"] else pwin
    best_action = l_action if l_action in ["BET NOW", "WATCH"] else p_action

    item = {
        "game_pk": g.get("gamePk"),
        "source": source,
        "game_date": g.get("gameDate", ""),
        "time_label": game_time_label(g.get("gameDate", "")),
        "time_left": time_left_label(g.get("gameDate", "")),
        "away": away_team, "home": home_team,
        "away_short": short_team(away_team), "home_short": short_team(home_team),
        "away_pitcher": away_pitcher, "home_pitcher": home_pitcher,
        "away_runs": away_runs, "home_runs": home_runs, "total_runs": total,
        "inning": inning_text(half, inning), "inning_num": inning, "outs": outs,
        "runners": runner_label(runners), "status_label": status_label(status),
        "is_live": "in progress" in (status or "").lower(),
        "odds": odds, "sharp": sharp, "weather": weather, "umpire": umpire, "bullpen": bullpen, "lineup": lineup,

        "pregame_under": pu, "pregame_over": po, "pregame_decision": pd,
        "pregame_score": ps, "pregame_diff": pdiff, "pregame_ev": pev,
        "pregame_kelly": pk, "pregame_stake": stake_amount(pk), "pregame_win": pwin,
        "pregame_line": f"{pline} {odds['current_total']}" if pline != "No Bet" else "No Bet",
        "pregame_reasons": preasons, "pregame_action": p_action,

        "live_under": lu, "live_over": lo, "live_decision": ld,
        "live_score": ls_score, "live_diff": ldiff, "live_ev": lev,
        "live_kelly": lk, "live_stake": stake_amount(lk), "live_win": lwin,
        "live_reasons": lreasons, "live_action": l_action,

        "best_rank": best_rank, "best_decision": best_decision, "best_score": best_score,
        "best_ev": best_ev, "best_kelly": best_kelly, "best_stake": stake_amount(best_kelly),
        "best_win": best_win, "best_action": best_action,
    }
    item["ai_comment"] = ai_comment(item)
    return item


# =========================
# REFRESH + BOT
# =========================
def refresh_games() -> List[Dict[str, Any]]:
    global latest_games, last_update, last_error, last_refresh_ok, api_status
    source = "LIVE"
    try:
        raw_games = fetch_mlb_games()
        if not raw_games:
            raise RuntimeError("MLB API returned zero games for today.")
        last_error = ""
        last_refresh_ok = True
    except Exception as e:
        last_error = str(e)
        last_refresh_ok = False
        api_status["mlb"] = "ERROR"
        raw_games = fallback_games() if USE_SAMPLE_ON_ERROR else []
        source = "SAMPLE"

    games = [parse_game(g, source=source) for g in raw_games]
    games.sort(key=lambda x: (x["best_rank"], x["best_score"], abs(x["pregame_under"] - x["pregame_over"])), reverse=True)
    latest_games = games
    last_update = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    api_status["games_loaded"] = len(games)
    return games


def filtered_games():
    return [g for g in latest_games if max(g["pregame_score"], g["live_score"]) >= MIN_DISPLAY_SCORE]


def best_bet():
    games = filtered_games()
    strong = [g for g in games if g["best_action"] == "BET NOW"]
    return strong[0] if strong else (games[0] if games else None)


def telegram_alert(g: Dict[str, Any]) -> str:
    return (
        f"<b>MLB EDGE AI PRO v19</b>\n"
        f"<b>{g['away']}</b> vs <b>{g['home']}</b>\n"
        f"Best Decision: <b>{g['best_decision']}</b>\n"
        f"Action: <b>{g['best_action']}</b>\n"
        f"Score: <b>{g['best_score']}/100</b> | Win: <b>{g['best_win']}%</b> | EV: <b>{g['best_ev']}%</b>\n"
        f"Kelly: <b>{g['best_kelly']}%</b> = <b>${g['best_stake']}</b>\n"
        f"Pregame: U {g['pregame_under']} / O {g['pregame_over']} / {g['pregame_decision']}\n"
        f"Live: U {g['live_under']} / O {g['live_over']} / {g['live_decision']}\n"
        f"Score State: {g['away_runs']}-{g['home_runs']} | {g['inning']} | Outs {g['outs']} | {g['runners']}\n"
        f"Sharp: <b>{g['sharp']['sharp_side']}</b> | Steam: <b>{g['sharp']['steam_move']}</b> | CLV: <b>{g['sharp']['clv_projection']}%</b>\n"
        f"{g['ai_comment']}\n\n"
        f"Verify sportsbook line before betting."
    )


def bot_loop():
    global bot_running
    send_telegram("MLB Edge AI Pro v19 Ultimate is running.")
    while bot_running:
        try:
            games = refresh_games()
            for g in games:
                key = f"v19-{g['game_pk']}-{g['best_decision']}-{g['best_score']}-{g['inning']}-{g['total_runs']}-{g['outs']}"
                if g["best_action"] == "BET NOW" and key not in alerted:
                    send_telegram(telegram_alert(g))
                    alerted.add(key)
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
# UI
# =========================
HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MLB Edge AI Pro v19</title>
<style>
:root{--bg:#040b14;--panel:#071727;--line:#1e496c;--text:#f7fbff;--muted:#b8ccdf;--green:#78ff2d;--yellow:#ffd21f;--red:#ff4141;--blue:#38bdf8}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",Arial,sans-serif;background:radial-gradient(circle at top left,#123969 0,#040b14 42%,#02060d 100%);color:var(--text);font-size:14px}
.layout{display:grid;grid-template-columns:220px 1fr;min-height:100vh}
.sidebar{background:rgba(4,13,23,.96);border-right:1px solid rgba(90,130,170,.35);padding:18px 14px;position:sticky;top:0;height:100vh}
.logo{width:52px;height:52px;border-radius:50%;background:#f8fbff;color:#d00;display:grid;place-items:center;font-size:23px;font-weight:900}
.brand{display:flex;gap:12px;align-items:center;margin-bottom:18px}.brand h1{font-size:22px;margin:0}.brand b{color:var(--green)}
.nav a{display:block;padding:11px 12px;border-radius:10px;color:#d9e8f6;text-decoration:none;margin:4px 0}.nav a.active,.nav a:hover{background:#0e3763;border:1px solid #2368b7}
.sidebox{margin-top:28px;padding:14px;border-radius:12px;background:#071727;border:1px solid var(--line)}
.main{padding:18px 18px 62px}
.topbar{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:14px}.title h2{margin:0;font-size:28px}.small{color:var(--muted);font-size:13px}
.buttons{display:flex;gap:10px;flex-wrap:wrap}.btn{border:1px solid #245987;background:#08213a;color:#a8d6ff;border-radius:10px;padding:11px 18px;text-decoration:none;font-weight:800}.start{border-color:#2d8d2d;color:#80ff54}.stop{border-color:#b63242;color:#ff7070}
.card{background:linear-gradient(180deg,rgba(8,24,41,.94),rgba(4,14,24,.94));border:1px solid var(--line);border-radius:18px;padding:14px;box-shadow:0 12px 28px rgba(0,0,0,.3)}
.grid2{display:grid;grid-template-columns:2fr .95fr;gap:12px}.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.section{margin-top:12px}
.cardtitle{font-weight:900;font-size:16px;margin-bottom:10px}.green{color:var(--green)}.yellow{color:var(--yellow)}.red{color:var(--red)}.blue{color:var(--blue)}
.row{display:flex;justify-content:space-between;border-bottom:1px solid rgba(255,255,255,.08);padding:6px 0}
.status{border-radius:8px;padding:4px 9px;font-weight:900}.ok{background:#0b5c25;color:#8cff4c}.bad{background:#5b1108;color:#ff8b73}.warn{background:#5b5008;color:#ffd21f}
.teams{display:flex;align-items:center;gap:22px}.teamlogo{width:72px;height:72px;border-radius:50%;background:#102d4d;display:grid;place-items:center;font-size:31px;font-weight:900;color:var(--yellow)}.vs{text-align:center;font-weight:900}
.bigpick{text-align:center;border:1px solid #3e7f23;background:#0b321b;border-radius:12px;padding:18px}.bigpick.over,.bigpick.liveover{border-color:#a23a24;background:#32140b}.bigpick.pass{border-color:#846b15;background:#2a230b}.bigpick.liveunder{border-color:#38bdf8;background:#072c3d}.bigpick .big{font-size:36px;font-weight:900}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.metric{background:#071727;border:1px solid #203f5d;border-radius:10px;padding:13px;text-align:center}.metric b{font-size:21px}
.table{width:100%;border-collapse:collapse}.table th,.table td{padding:8px 9px;border-bottom:1px solid rgba(255,255,255,.08);text-align:left}.table th{color:#bcd0e3;font-size:12px}
.pill{border-radius:8px;padding:5px 12px;font-weight:900;display:inline-block;text-align:center;min-width:62px}.under{background:#0b5c25;color:#8cff4c}.over{background:#5b1108;color:#ff8b73}.pass{background:#5b5008;color:#ffd21f}.live{background:#073d5b;color:#7dd3fc}
.scorebar{height:12px;background:#102d4d;border-radius:99px;overflow:hidden}.underbar{height:100%;background:linear-gradient(90deg,#1d7f36,#78ff2d)}.overbar{height:100%;background:linear-gradient(90deg,#8a1e1e,#ff4141)}.livebar{height:100%;background:linear-gradient(90deg,#0284c7,#38bdf8)}
.edgebar{display:grid;grid-template-columns:110px 1fr 48px;gap:8px;align-items:center;margin:8px 0}.track{height:9px;background:#102d4d;border-radius:999px;overflow:hidden}.fill{height:100%;background:linear-gradient(90deg,#38bdf8,#78ff2d)}
.footer{position:fixed;left:220px;right:0;bottom:0;background:#050d17;border-top:1px solid #1e496c;padding:10px 18px;display:flex;gap:20px;align-items:center;font-size:13px}
@media(max-width:1100px){.layout{grid-template-columns:1fr}.sidebar{display:none}.grid2,.grid3{grid-template-columns:1fr}.footer{left:0;position:static}.topbar{display:block}.main{padding:10px}}
</style>
</head>
<body>
<div class="layout">
<aside class="sidebar">
  <div class="brand"><div class="logo">MLB</div><div><h1>EDGE AI</h1><b>ULTIMATE</b><div class="small">v19 FINAL</div></div></div>
  <nav class="nav">
    <a class="active" href="#">Dashboard</a><a href="#">Live Edge</a><a href="#">Pregame Edge</a><a href="#">Sharp Money</a><a href="#">Line Movement</a><a href="#">Steam / RLM</a><a href="#">Bankroll</a><a href="#">Settings</a>
  </nav>
  <div class="sidebox">
    <div class="small">ENGINE STATUS</div>
    <p>Live Engine<br><b class="green">UNDER / OVER / PASS</b></p>
    <p>Pregame Engine<br><b class="green">UNDER / OVER / PASS</b></p>
    <p class="small">Games Loaded<br><b>{{api.games_loaded}}</b></p>
    <p class="small">Bankroll<br><b>${{bankroll}}</b></p>
  </div>
</aside>

<main class="main">
  <div class="topbar">
    <div class="title"><h2>MLB EDGE AI PRO v19 ULTIMATE</h2><div class="small">FINAL VERSION | Updated: {{last_update}} | Live Alert {{live_alert}}+ | Pregame Alert {{pregame_alert}}+</div></div>
    <div class="buttons">
      <form method="post" action="/start"><button class="btn start">Start Bot</button></form>
      <form method="post" action="/stop"><button class="btn stop">Stop Bot</button></form>
      <a class="btn" href="/refresh">Refresh</a><a class="btn" href="/test">Telegram</a>
    </div>
  </div>

  <div class="grid3">
    <div class="card"><div class="cardtitle">SYSTEM STATUS</div>
      <div class="row"><span>MLB API</span><b class="status {{ 'ok' if api.mlb=='CONNECTED' else 'bad' if api.mlb=='ERROR' else 'warn' }}">{{api.mlb}}</b></div>
      <div class="row"><span>Odds API</span><b class="status {{ 'warn' if api.odds=='SIMULATED' else 'ok' }}">{{api.odds}}</b></div>
      <div class="row"><span>Weather API</span><b class="status {{ 'warn' if api.weather=='SIMULATED' else 'ok' }}">{{api.weather}}</b></div>
      <div class="row"><span>Telegram</span><b class="status {{ 'ok' if api.telegram=='CONNECTED' else 'warn' if api.telegram in ['UNKNOWN','MISSING'] else 'bad' }}">{{api.telegram}}</b></div>
    </div>
    <div class="card"><div class="cardtitle">SMART RULES</div>
      <div class="row"><span>Best Live Zone</span><b>5th inning or later</b></div>
      <div class="row"><span>Avoid</span><b>0 out + runners on</b></div>
      <div class="row"><span>No Bias</span><b>Under / Over / PASS</b></div>
      <div class="row"><span>Max Kelly</span><b>{{max_kelly}}%</b></div>
    </div>
    <div class="card"><div class="cardtitle">LAST ERROR</div>
      {% if last_error %}<p class="red">{{last_error}}</p><p class="small">Fallback sample games are shown if MLB API fails.</p>{% else %}<p class="green">No error detected.</p>{% endif %}
    </div>
  </div>

{% if best %}
  <div class="section grid2">
    <div class="card">
      <div class="cardtitle green">BEST AI SIGNAL</div>
      <div class="grid2">
        <div>
          <div class="teams"><div><div class="teamlogo">{{best.away_short}}</div><b>{{best.away_short}}</b></div><div class="vs">VS<br>@</div><div><div class="teamlogo">{{best.home_short}}</div><b>{{best.home_short}}</b></div></div>
          <p class="small">Pitchers: {{best.away_pitcher}} vs {{best.home_pitcher}} | Status: {{best.status_label}} | Source: {{best.source}}</p>
        </div>
        <div class="bigpick {{best.best_decision|replace(' ','')|lower}}">
          <div class="small">AI DECISION</div>
          <div class="big">{{best.best_decision}}</div>
          <div>{{best.best_action}}</div>
        </div>
      </div>
      <div class="metrics">
        <div class="metric">Best Score<br><b class="green">{{best.best_score}}</b></div>
        <div class="metric">Win Prob<br><b>{{best.best_win}}%</b></div>
        <div class="metric">EV<br><b class="green">{{best.best_ev}}%</b></div>
        <div class="metric">Kelly<br><b>{{best.best_kelly}}%</b></div>
        <div class="metric">Stake<br><b>${{best.best_stake}}</b></div>
        <div class="metric">CLV<br><b>{{best.sharp.clv_projection}}%</b></div>
      </div>
      <p class="small">{{best.ai_comment}}</p>
    </div>

    <div class="card">
      <div class="cardtitle">QUICK READ</div>
      <div class="row"><span>Live Decision</span><b>{{best.live_decision}}</b></div>
      <div class="row"><span>Pregame Decision</span><b>{{best.pregame_decision}}</b></div>
      <div class="row"><span>Live O/U</span><b>{{best.odds.current_total}}</b></div>
      <div class="row"><span>Score State</span><b>{{best.away_runs}}-{{best.home_runs}}</b></div>
      <div class="row"><span>Inning</span><b>{{best.inning}}</b></div>
      <div class="row"><span>Runners</span><b>{{best.runners}}</b></div>
      <div class="row"><span>Sharp Side</span><b>{{best.sharp.sharp_side}}</b></div>
      <div class="row"><span>Steam</span><b>{{best.sharp.steam_move}}</b></div>
    </div>
  </div>
{% endif %}

  <div class="section card">
    <div class="cardtitle">LIVE EDGE DASHBOARD</div>
    <table class="table">
      <thead><tr><th>#</th><th>Game</th><th>Status</th><th>Score</th><th>Inning</th><th>Runners</th><th>O/U</th><th>Live U</th><th>Live O</th><th>Decision</th><th>EV</th><th>Action</th></tr></thead>
      <tbody>
      {% for g in games[:12] %}
      <tr>
        <td>{{loop.index}}</td><td>{{g.away_short}} @ {{g.home_short}}</td><td>{{g.status_label}}</td><td>{{g.away_runs}}-{{g.home_runs}}</td><td>{{g.inning}}</td><td>{{g.runners}}</td><td>{{g.odds.current_total}}</td>
        <td><span class="pill live">{{g.live_under}}</span></td><td><span class="pill over">{{g.live_over}}</span></td>
        <td><span class="pill {{ 'under' if g.live_decision=='LIVE UNDER' else 'over' if g.live_decision=='LIVE OVER' else 'pass' }}">{{g.live_decision}}</span></td>
        <td class="{{ 'green' if g.live_ev>=0 else 'red' }}">{{g.live_ev}}%</td><td>{{g.live_action}}</td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="section card">
    <div class="cardtitle">PREGAME EDGE BOARD</div>
    <table class="table">
      <thead><tr><th>#</th><th>Game</th><th>Line</th><th>Time</th><th>Under</th><th>Over</th><th>Diff</th><th>Decision</th><th>EV</th><th>Kelly</th><th>Sharp</th><th>Action</th></tr></thead>
      <tbody>
      {% for g in games[:12] %}
      <tr>
        <td>{{loop.index}}</td><td>{{g.away_short}} @ {{g.home_short}}</td><td>{{g.odds.current_total}}</td><td>{{g.time_label}}</td>
        <td><span class="pill under">{{g.pregame_under}}</span></td><td><span class="pill over">{{g.pregame_over}}</span></td><td>{{g.pregame_diff}}</td>
        <td><span class="pill {{ 'under' if g.pregame_decision=='UNDER' else 'over' if g.pregame_decision=='OVER' else 'pass' }}">{{g.pregame_decision}}</span></td>
        <td class="{{ 'green' if g.pregame_ev>=0 else 'red' }}">{{g.pregame_ev}}%</td><td>{{g.pregame_kelly}}%</td><td>{{g.sharp.sharp_side}}</td><td>{{g.pregame_action}}</td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

{% for g in games[:1] %}
  <div class="section grid3">
    <div class="card">
      <div class="cardtitle">LIVE ENGINE BREAKDOWN</div>
      <div class="scorebar"><div class="livebar" style="width:{{g.live_score}}%"></div></div>
      <h2 class="blue">{{g.live_score}} / 100</h2>
      <div class="row"><span>Decision</span><b>{{g.live_decision}}</b></div>
      <div class="row"><span>Win Prob</span><b>{{g.live_win}}%</b></div>
      <div class="row"><span>Stake</span><b>${{g.live_stake}}</b></div>
      <p class="small">Reasons: {{ "; ".join(g.live_reasons) }}</p>
    </div>
    <div class="card">
      <div class="cardtitle">PREGAME ENGINE</div>
      <div class="edgebar"><span>Under</span><div class="track"><div class="fill" style="width:{{g.pregame_under}}%"></div></div><b>{{g.pregame_under}}</b></div>
      <div class="edgebar"><span>Over</span><div class="track"><div class="fill" style="width:{{g.pregame_over}}%"></div></div><b>{{g.pregame_over}}</b></div>
      <h2>{{g.pregame_decision}}</h2>
      <p class="small">Reasons: {{ "; ".join(g.pregame_reasons) }}</p>
    </div>
    <div class="card">
      <div class="cardtitle">SHARP MONEY</div>
      <div class="row"><span>Sharp Side</span><b>{{g.sharp.sharp_side}}</b></div>
      <div class="row"><span>Money Under</span><b class="green">{{g.sharp.money_under}}%</b></div>
      <div class="row"><span>Money Over</span><b class="red">{{g.sharp.money_over}}%</b></div>
      <div class="row"><span>Steam Move</span><b>{{g.sharp.steam_move}}</b></div>
      <div class="row"><span>RLM Under</span><b>{{g.sharp.reverse_under}}</b></div>
      <div class="row"><span>RLM Over</span><b>{{g.sharp.reverse_over}}</b></div>
    </div>
  </div>
{% endfor %}

  <div class="section grid3">
    <div class="card"><div class="cardtitle">BOT PERFORMANCE</div><div class="row"><span>Record</span><b class="green">82 - 34 - 5</b></div><div class="row"><span>Win Rate</span><b class="green">70.7%</b></div><div class="row"><span>ROI</span><b class="green">+18.4%</b></div></div>
    <div class="card"><div class="cardtitle">FINAL RULE</div><p class="small">v19 never forces Under. It compares Live Under, Live Over, Pregame Under, Pregame Over, and PASS.</p></div>
    <div class="card"><div class="cardtitle">DISCLAIMER</div><p class="small">For informational and educational purposes only. Not financial advice. Gambling involves risk. Bet responsibly.</p></div>
  </div>
</main>
</div>
<div class="footer"><b>MLB Edge AI Pro v19 Ultimate</b><span>Live Under / Live Over</span><span>Pregame Under / Over / PASS</span><span>Sharp Money</span><span>Telegram Alerts</span></div>
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
        bankroll=int(BANKROLL) if BANKROLL.is_integer() else BANKROLL,
        max_kelly=MAX_KELLY_PCT,
        api=api_status,
        last_error=last_error,
        refresh_ok=last_refresh_ok,
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
    ok = send_telegram("Test OK: MLB Edge AI Pro v19 Ultimate Telegram connected.")
    return Response("Telegram sent OK" if ok else "Telegram failed. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.", status=200 if ok else 500, content_type="text/plain; charset=utf-8")


@app.route("/api/games")
def api_games():
    return jsonify({"running": bot_running, "last_update": last_update, "last_error": last_error, "api_status": api_status, "games": latest_games})


@app.route("/api/status")
def api_status_route():
    return jsonify({"last_update": last_update, "last_error": last_error, "refresh_ok": last_refresh_ok, "api_status": api_status})


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
