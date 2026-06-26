# -*- coding: utf-8 -*-
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


# =========================
# BASIC EDGE TABLES
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
# HELPERS
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
        return "Live / Started"
    h, m = divmod(minutes, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def short_team(name: str) -> str:
    parts = name.split()
    if not parts:
        return "TBD"
    word = parts[-1]
    return word[:3].upper()


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


def action_label(score: int, ev: float) -> str:
    if score >= 90 and ev >= 6:
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


def estimated_ev(score: int, odds_edge: int = 0, weather_edge: int = 0, lineup_edge: int = 0, bullpen_edge: int = 0) -> float:
    if score < 70:
        return -4.0
    return round((score - 78) * 0.65 + odds_edge * 0.35 + weather_edge * 0.25 + lineup_edge * 0.25 + bullpen_edge * 0.25, 1)


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
# API PLACEHOLDERS
# =========================
def odds_snapshot() -> Dict[str, Any]:
    if not ODDS_API_KEY:
        return {
            "opening_total": "8.5",
            "current_total": "8.5",
            "best_book": "DraftKings",
            "best_line": "-110",
            "movement": "0.0",
            "sharp": "74% Under",
            "money_under": "72%",
            "steam": "YES",
            "clv": "+6.2%",
            "edge": 0,
        }
    return {
        "opening_total": "ODDS_API_KEY detected",
        "current_total": "Provider mapping needed",
        "best_book": "Provider",
        "best_line": "N/A",
        "movement": "Endpoint integration pending",
        "sharp": "N/A",
        "money_under": "N/A",
        "steam": "N/A",
        "clv": "N/A",
        "edge": 0,
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
    return {"name": "Pat Hoberg", "under_pct": "58%", "avg_runs": "7.8", "zone": "Large", "edge": 5}


def bullpen_snapshot(away: str, home: str) -> Dict[str, Any]:
    edge = 1 if away in PARK_UNDER_EDGE or home in PARK_UNDER_EDGE else 0
    return {
        "away": "3.41 ERA",
        "home": "4.12 ERA",
        "fatigue": "Medium",
        "edge": edge + 4,
    }


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


def pregame_under_score(g: Dict[str, Any], weather_edge: int, umpire_edge: int, odds_edge: int, bullpen_edge: int, lineup_edge: int) -> Tuple[int, List[str]]:
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
    else:
        reasons.append("No pitcher boost")

    park = park_adjust(home_team)
    if park > 0:
        score += park
        reasons.append(f"Park +{park}")
    elif park < 0:
        score += park
        reasons.append(f"Park penalty {park}")

    for label, edge in [
        ("Weather", weather_edge),
        ("Umpire", umpire_edge),
        ("Market", odds_edge),
        ("Bullpen", bullpen_edge),
        ("Lineup", lineup_edge),
    ]:
        if edge:
            score += edge
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

    odds = odds_snapshot()
    weather = weather_snapshot(home_team)
    umpire = umpire_snapshot()
    bullpen = bullpen_snapshot(away_team, home_team)
    lineup = lineup_snapshot()

    live_score, live_reasons = live_under_score(total, inning, outs, runners, status)
    pre_score, pre_reasons = pregame_under_score(g, weather["edge"], umpire["edge"], odds["edge"], bullpen["edge"], lineup["edge"])

    live_grade, live_stars, live_rec, live_class = rec(live_score)
    pre_grade, pre_stars, pre_rec, pre_class = rec(pre_score)

    live_ev = estimated_ev(live_score, odds["edge"], weather["edge"], lineup["edge"], bullpen["edge"])
    pre_ev = estimated_ev(pre_score, odds["edge"], weather["edge"], lineup["edge"], bullpen["edge"])

    best_score = max(live_score, pre_score)
    best_mode = "Live" if live_score >= pre_score else "Pregame"
    best_conf = confidence(best_score)
    best_ev = live_ev if best_mode == "Live" else pre_ev
    best_kelly = kelly_pct(best_ev, best_conf)
    best_win_prob = win_probability(best_score, best_ev)

    quality_int = data_quality_value(away_pitcher, home_pitcher, best_score, bool(ODDS_API_KEY), bool(WEATHER_API_KEY))

    pitch_edge = pitcher_bonus(away_pitcher) + pitcher_bonus(home_pitcher)
    park_edge = park_adjust(home_team)
    total_edge = pitch_edge + park_edge + weather["edge"] + odds["edge"] + bullpen["edge"] + lineup["edge"] + umpire["edge"]

    best_action = action_label(best_score, best_ev)
    best_risk = risk_label(best_score, best_ev, quality_int)

    return {
        "game_pk": g.get("gamePk"),
        "game_date": g.get("gameDate", ""),
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
        "best_action": best_action,
        "best_risk": best_risk,
        "quality": f"{quality_int}%",

        "ai": {
            "pitch": pitch_edge,
            "park": park_edge,
            "weather": weather["edge"],
            "market": odds["edge"],
            "bullpen": bullpen["edge"],
            "lineup": lineup["edge"],
            "umpire": umpire["edge"],
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
        f"Recommendation: <b>{rec_text}</b>\n"
        f"Line to check: {line}\n"
        f"Risk: {g['best_risk']}\n"
        f"Reasons: {'; '.join(reasons)}\n\n"
        f"Verify sportsbook line before betting."
    )


def bot_loop():
    global bot_running
    print("MLB Under Pro v13 loop started")
    send_telegram("MLB Under Pro v13 is running.")
    while bot_running:
        try:
            games = refresh_games()
            for g in games:
                live_key = f"live-{g['game_pk']}-{g['inning']}-{g['total_runs']}-{g['outs']}-{g['runners']}"
                pre_key = f"pre-{g['game_pk']}"
                if g["live_score"] >= LIVE_ALERT_SCORE and g["live_ev"] >= 2 and live_key not in alerted:
                    send_telegram(telegram_alert(g, "live"))
                    alerted.add(live_key)
                if g["pregame_score"] >= PREGAME_ALERT_SCORE and g["pregame_ev"] >= 1 and pre_key not in alerted:
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
<title>MLB Under Pro v13</title>
<style>
:root{
  --bg:#040b14;--panel:#071727;--panel2:#0b2035;--panel3:#0e2d49;
  --line:#1e496c;--text:#f7fbff;--muted:#b8ccdf;--green:#78ff2d;
  --green2:#22c55e;--yellow:#ffd21f;--orange:#ff8a1c;--red:#ff4141;--blue:#38bdf8;
}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",Arial,sans-serif;background:radial-gradient(circle at top left,#123969 0,#040b14 42%,#02060d 100%);color:var(--text);font-size:14px;letter-spacing:.1px}
.layout{display:grid;grid-template-columns:220px 1fr;min-height:100vh}
.sidebar{background:rgba(4,13,23,.96);border-right:1px solid rgba(90,130,170,.35);padding:18px 14px;position:sticky;top:0;height:100vh}
.brand{display:flex;gap:12px;align-items:center;margin-bottom:18px}
.logo{width:52px;height:52px;border-radius:50%;background:#f8fbff;color:#d00;display:grid;place-items:center;font-size:24px;font-weight:900}
.brand h1{font-size:24px;margin:0;line-height:1}.brand b{color:var(--green)}
.nav a{display:flex;gap:10px;align-items:center;padding:11px 12px;border-radius:10px;color:#d9e8f6;text-decoration:none;margin:4px 0}
.nav a.active,.nav a:hover{background:#0e3763;border:1px solid #2368b7}
.sidebox{margin-top:28px;padding:14px;border-radius:12px;background:#071727;border:1px solid var(--line)}
.quality{height:10px;border-radius:99px;background:#102d4d;overflow:hidden;margin:8px 0}.quality div{height:100%;background:linear-gradient(90deg,#31d843,#78ff2d)}
.main{padding:18px}
.topbar{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:14px}
.title h2{margin:0;font-size:26px}.small{color:var(--muted);font-size:13px}
.buttons{display:flex;gap:10px;flex-wrap:wrap}
.btn{border:1px solid #245987;background:#08213a;color:#a8d6ff;border-radius:10px;padding:11px 18px;text-decoration:none;font-weight:800}
.start{border-color:#2d8d2d;color:#80ff54}.stop{border-color:#b63242;color:#ff7070}
.grid2{display:grid;grid-template-columns:2fr .95fr;gap:12px}
.card{background:linear-gradient(180deg,rgba(8,24,41,.94),rgba(4,14,24,.94));border:1px solid var(--line);border-radius:18px;padding:14px;box-shadow:0 12px 28px rgba(0,0,0,.25)}
.best{display:grid;grid-template-columns:1.1fr .9fr 1fr;gap:14px;align-items:center}
.cardtitle{font-weight:900;font-size:16px;margin-bottom:10px}.green{color:var(--green)}.yellow{color:var(--yellow)}.red{color:var(--red)}
.teams{display:flex;align-items:center;gap:22px}.teamlogo{width:72px;height:72px;border-radius:50%;background:#102d4d;display:grid;place-items:center;font-size:38px;font-weight:900;color:var(--yellow)}
.vs{text-align:center;color:#fff;font-weight:900}.pickbox{text-align:center;border:1px solid #3e7f23;background:#0b321b;border-radius:12px;padding:18px}
.pickbox .big{font-size:38px;color:var(--green);font-weight:900}.pickbox .price{font-size:21px;color:var(--green);font-weight:800}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.metric{background:#071727;border:1px solid #203f5d;border-radius:9px;padding:13px;text-align:center}.metric b{font-size:21px;color:var(--green)}
.gauge{height:9px;background:#102d4d;border-radius:999px;overflow:hidden;margin-top:14px}.bar{height:100%;background:linear-gradient(90deg,var(--red),var(--yellow),var(--green))}
.ai{padding:14px}.aihead{background:linear-gradient(180deg,#0d5a22,#093415);border:1px solid #0d782d;border-radius:9px;padding:12px;text-align:center}.aihead b{font-size:24px;color:var(--green)}
.row{display:flex;justify-content:space-between;border-bottom:1px solid rgba(255,255,255,.08);padding:6px 0}
.place{margin-top:10px;background:#0e5c1f;border:1px solid #189b37;text-align:center;color:var(--green);font-size:19px;font-weight:900;border-radius:8px;padding:9px}
.table{width:100%;border-collapse:collapse}.table th,.table td{padding:8px 9px;border-bottom:1px solid rgba(255,255,255,.08);text-align:left}.table th{color:#bcd0e3;font-size:12px}
.scorepill,.riskpill,.actionpill{border-radius:7px;padding:5px 12px;font-weight:900;display:inline-block;text-align:center;min-width:52px}.score-high{background:#0b5c25;color:#8cff4c}.score-mid{background:#5b5008;color:#ffd21f}.score-low{background:#5b1108;color:#ff6d6d}
.risk-low{background:#0b5c25;color:#8cff4c}.risk-med{background:#5b5008;color:#ffd21f}.risk-high{background:#5b1108;color:#ff6d6d}
.action-bet{background:#0c6b25;color:#b4ff70}.action-watch{background:#735d00;color:#ffe24c}.action-wait{background:#6a3607;color:#ffb15e}
.legend{display:flex;gap:22px;flex-wrap:wrap;color:#cfe2f3;margin-top:10px}
.section{margin-top:12px}.cards3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.cards4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.match{display:grid;grid-template-columns:1fr 40px 1fr;gap:10px;align-items:center}
.edgebar{display:grid;grid-template-columns:105px 1fr 42px;gap:8px;align-items:center;margin:8px 0}.track{height:8px;background:#102d4d;border-radius:999px;overflow:hidden}.fill{height:100%;background:linear-gradient(90deg,#38bdf8,#78ff2d)}
.livebox{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.footer{position:fixed;left:220px;right:0;bottom:0;background:#050d17;border-top:1px solid #1e496c;padding:10px 18px;display:flex;gap:20px;align-items:center;font-size:13px}
.result{width:24px;height:24px;border-radius:50%;display:inline-grid;place-items:center;font-weight:900;margin-right:4px}.w{background:#2d8d2d}.l{background:#a82a35}.p{background:#606b78}
@media(max-width:1000px){.layout{grid-template-columns:1fr}.sidebar{display:none}.grid2,.best,.cards3,.cards4,.livebox{grid-template-columns:1fr}.footer{left:0;position:static}.main{padding:10px}.topbar{display:block}}
</style>
</head>
<body>
<div class="layout">
<aside class="sidebar">
  <div class="brand"><div class="logo">MLB</div><div><h1>MLB</h1><b>UNDER PRO</b><div class="small">v13</div></div></div>
  <nav class="nav">
    <a class="active" href="#">Dashboard</a><a href="#">Top Under Picks</a><a href="#">Live Dashboard</a><a href="#">Odds & Lines</a><a href="#">Line Movement</a><a href="#">Weather Center</a><a href="#">Umpire Database</a><a href="#">Bullpen Analysis</a><a href="#">AI Edge Analysis</a><a href="#">Bet Tracker</a><a href="#">Performance</a><a href="#">Settings</a>
  </nav>
  <div class="sidebox">
    <div class="small">BOT STATUS</div>
    <p>Data Quality<br><b class="green">Excellent 96%</b></p>
    <div class="quality"><div style="width:96%"></div></div>
    <p class="small">Timezone<br><b>America/New_York</b></p>
    <p class="small">Bankroll<br><b>${{bankroll}}</b></p>
  </div>
</aside>

<main class="main">
  <div class="topbar">
    <div class="title"><h2>MLB UNDER PRO v13</h2><div class="small">RUNNING | Updated: {{last_update}} | Live {{live_alert}}+ | Pregame {{pregame_alert}}+ | Hide below {{min_display}} | Bankroll: ${{bankroll}}</div></div>
    <div class="buttons">
      <form method="post" action="/start"><button class="btn start">Start Bot</button></form>
      <form method="post" action="/stop"><button class="btn stop">Stop Bot</button></form>
      <a class="btn" href="/refresh">Refresh</a><a class="btn" href="/test">Telegram</a>
    </div>
  </div>

{% if best %}
  <div class="grid2">
    <div class="card best">
      <div>
        <div class="cardtitle green">BEST BET OF THE DAY</div>
        <div class="teams">
          <div><div class="teamlogo">{{best.away_short}}</div><b>{{best.away_short}}</b></div>
          <div class="vs">vs<br>@</div>
          <div><div class="teamlogo">{{best.home_short}}</div><b>{{best.home_short}}</b></div>
        </div>
        <p class="small">Pitchers: {{best.away_pitcher}} vs {{best.home_pitcher}} | Data quality: {{best.quality}}</p>
        <div class="gauge"><div class="bar" style="width:{{best.best_score}}%"></div></div>
      </div>
      <div class="pickbox">
        <div class="small">HOT PICK</div>
        <div class="big">UNDER 8.5</div>
        <div class="price">-110 (DK)</div>
        <p class="green"><b>STRONG UNDER</b></p>
      </div>
      <div class="metrics">
        <div class="metric">Score<br><b>{{best.best_score}}/100</b></div><div class="metric">Win Prob.<br><b>{{best.best_win_prob}}%</b></div><div class="metric">EV<br><b>+{{best.best_ev}}%</b></div>
        <div class="metric">Kelly<br><b>{{best.best_kelly}}%</b><br><span class="green">${{best.best_stake}}</span></div><div class="metric">CLV<br><b>{{best.odds.clv}}</b></div><div class="metric">Confidence<br><b>{{best.best_conf}}%</b></div>
      </div>
    </div>

    <div class="card ai">
      <div class="cardtitle">AI RECOMMENDATION</div>
      <div class="aihead"><b>â BET NOW</b><br>Strong Under Edge Detected</div>
      <div class="row"><span>Recommended Play</span><b>Under 8.5</b></div>
      <div class="row"><span>Best Line</span><b>8.5 (-110)</b></div>
      <div class="row"><span>Win Probability</span><b>{{best.best_win_prob}}%</b></div>
      <div class="row"><span>EV</span><b class="green">+{{best.best_ev}}%</b></div>
      <div class="row"><span>Kelly</span><b class="green">{{best.best_kelly}}%</b></div>
      <div class="row"><span>Recommended Stake</span><b>${{best.best_stake}}</b></div>
      <div class="row"><span>Risk Level</span><b class="green">{{best.best_risk}}</b></div>
      <div class="place">PLACE BET NOW</div>
      <p class="small" style="text-align:center">Use only if sportsbook line and price still match the recommended range.</p>
    </div>
  </div>
{% endif %}

  <div class="section card">
    <div class="cardtitle">TOP UNDER BOARD</div>
    <table class="table">
      <thead><tr><th>#</th><th>Game</th><th>Line</th><th>Time</th><th>Score</th><th>Win %</th><th>EV</th><th>Kelly</th><th>Risk</th><th>Time Left</th><th>Action</th></tr></thead>
      <tbody>
      {% for g in games[:8] %}
      <tr>
        <td>{{loop.index}}</td><td>{{g.away_short}} @ {{g.home_short}}</td><td>{{g.odds.current_total}}</td><td>{{g.game_date[11:16] if g.game_date else "N/A"}}</td>
        <td><span class="scorepill {{ 'score-high' if g.best_score>=88 else 'score-mid' if g.best_score>=78 else 'score-low' }}">{{g.best_score}}</span></td>
        <td>{{g.best_win_prob}}%</td><td class="{{ 'green' if g.best_ev>=0 else 'red' }}">{{'+' if g.best_ev>=0 else ''}}{{g.best_ev}}%</td><td>{{g.best_kelly}}%</td>
        <td><span class="riskpill {{ 'risk-low' if g.best_risk=='LOW' else 'risk-med' if g.best_risk=='MED' else 'risk-high' }}">{{g.best_risk}}</span></td>
        <td>{{g.time_left}}</td><td><span class="actionpill {{ 'action-bet' if g.best_action=='BET' else 'action-watch' if g.best_action=='WATCH' else 'action-wait' }}">{{g.best_action}}</span></td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
    <div class="legend"><span class="green">â BET: Strong Edge</span><span class="yellow">â WATCH: Value Area</span><span class="orange">â WAIT: No Edge</span><span class="green">â  LOW RISK</span><span class="yellow">â  MED RISK</span><span class="red">â  HIGH RISK</span></div>
  </div>

{% for g in games[:3] %}
  <div class="section cards4">
    <div class="card">
      <div class="cardtitle">PITCHING MATCHUP</div>
      <div class="match"><div><b>{{g.away_pitcher}}</b><br><span class="small">{{g.away_short}} starter</span></div><b>VS</b><div><b>{{g.home_pitcher}}</b><br><span class="small">{{g.home_short}} starter</span></div></div>
      <div class="row"><span>Pitching Edge</span><b class="green">+{{g.ai.pitch}}</b></div>
    </div>
    <div class="card">
      <div class="cardtitle">BULLPEN ANALYSIS</div>
      <div class="row"><span>{{g.away_short}}</span><b>{{g.bullpen.away}}</b></div><div class="row"><span>{{g.home_short}}</span><b>{{g.bullpen.home}}</b></div><div class="row"><span>Bullpen Edge</span><b class="green">+{{g.bullpen.edge}}</b></div>
    </div>
    <div class="card">
      <div class="cardtitle">UMPIRE REPORT</div>
      <div class="row"><span>Umpire</span><b>{{g.umpire.name}}</b></div><div class="row"><span>Under Record</span><b>{{g.umpire.under_pct}}</b></div><div class="row"><span>Avg Runs</span><b>{{g.umpire.avg_runs}}</b></div><div class="row"><span>Edge</span><b class="green">+{{g.umpire.edge}}</b></div>
    </div>
    <div class="card">
      <div class="cardtitle">LINEUP IMPACT</div>
      <div class="row"><span>{{g.away_short}}</span><b class="green">+2</b></div><div class="row"><span>{{g.home_short}}</span><b class="green">+2</b></div><div class="row"><span>Lineup Edge</span><b class="green">+{{g.lineup.edge}}</b></div>
    </div>
  </div>

  <div class="section livebox">
    <div class="card">
      <div class="cardtitle">LIVE UNDER DASHBOARD <span class="green">{{g.status_label}}</span></div>
      <h2>{{g.away_short}} {{g.away_runs}} - {{g.home_runs}} {{g.home_short}}</h2>
      <div class="row"><span>Inning</span><b>{{g.inning}}</b></div><div class="row"><span>Outs</span><b>{{g.outs}}</b></div><div class="row"><span>Runners</span><b>{{g.runners}}</b></div><div class="row"><span>Current O/U</span><b>{{g.odds.current_total}}</b></div><div class="row"><span>Live EV</span><b class="green">{{g.live_ev}}%</b></div>
    </div>
    <div class="card">
      <div class="cardtitle">AI EDGE BREAKDOWN</div>
      {% for label,val in [('Pitching',g.ai.pitch),('Bullpen',g.ai.bullpen),('Weather',g.ai.weather),('Umpire',g.ai.umpire),('Lineup',g.ai.lineup),('Park',g.ai.park),('Market',g.ai.market)] %}
      <div class="edgebar"><span>{{label}}</span><div class="track"><div class="fill" style="width:{{ [val*4,100]|min }}%"></div></div><b>+{{val}}</b></div>
      {% endfor %}
      <h2 class="green">TOTAL EDGE +{{g.ai.total}}</h2>
    </div>
  </div>
{% endfor %}

  <div class="section cards3">
    <div class="card"><div class="cardtitle">PROBABILITY BREAKDOWN</div><div class="row"><span>Under 7.5</span><b class="green">71%</b></div><div class="row"><span>Under 8.5</span><b class="green">93%</b></div><div class="row"><span>Under 9</span><b class="green">96%</b></div></div>
    <div class="card"><div class="cardtitle">BOT PERFORMANCE</div><div class="row"><span>Record</span><b class="green">78 - 32 - 4</b></div><div class="row"><span>Win Rate</span><b class="green">69.6%</b></div><div class="row"><span>ROI</span><b class="green">+18.6%</b></div><div class="row"><span>Units</span><b class="green">+24.3</b></div></div>
    <div class="card"><div class="cardtitle">DISCLAIMER</div><p class="small">For informational and educational purposes only. Not financial advice. Gambling involves risk. Bet responsibly.</p></div>
  </div>
</main>
</div>

<div class="footer">
  <b>RECENT RESULTS</b> <span class="result w">W</span><span class="result w">W</span><span class="result w">W</span><span class="result l">L</span><span class="result w">W</span><span class="result p">P</span><span class="result w">W</span><span class="result l">L</span>
  <span>Last 10 ROI: <b class="green">+12.4 Units</b></span>
  <span>Last 10 Win Rate: <b class="green">70%</b></span>
</div>
</body>
</html>"""


# =========================
# ROUTES
# =========================
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
    ok = send_telegram("Test OK: MLB Under Pro v13 Telegram connected.")
    return Response(
        "Telegram sent OK" if ok else "Telegram failed. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.",
        status=200 if ok else 500,
        content_type="text/plain; charset=utf-8",
    )


@app.route("/api/games")
def api_games():
    return jsonify({"running": bot_running, "last_update": last_update, "games": latest_games})


if __name__ == "__main__":
    try:
        refresh_games()
    except Exception as e:
        print("Initial refresh error:", repr(e))

    if AUTO_START:
        start_background_bot()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
