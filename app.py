# -*- coding: utf-8 -*-
import os, time, threading, datetime as dt
from typing import Dict, Any, List, Set, Tuple
import requests
from flask import Flask, jsonify, render_template_string, redirect, url_for, Response

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

def today() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d")

def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def parse_game_time(game_date: str):
    try:
        return dt.datetime.fromisoformat(game_date.replace("Z", "+00:00"))
    except Exception:
        return None

def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
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
    if "in progress" in s: return "LIVE"
    if "scheduled" in s: return "PREGAME"
    if "pre-game" in s or "warmup" in s: return "STARTING"
    if "final" in s: return "FINAL"
    return status or "UNKNOWN"

def inning_text(half: str, inning: int) -> str:
    if not inning: return "Pregame"
    if (half or "").lower().startswith("top"): return f"Top {inning}"
    if (half or "").lower().startswith("bottom"): return f"Bot {inning}"
    return f"Inning {inning}"

def runner_label(runners: List[str]) -> str:
    if not runners: return "Bases empty"
    mapping = {"first": "1B", "second": "2B", "third": "3B"}
    return ", ".join(mapping.get(r, r) for r in runners)

def base_penalty(runners: List[str], outs: int) -> int:
    s = set(runners)
    if not s: return 0
    if len(s) == 3: return 34 if outs < 2 else 19
    if "second" in s and "third" in s: return 28 if outs < 2 else 15
    if "third" in s: return 23 if outs < 2 else 12
    if "second" in s: return 14 if outs < 2 else 6
    if "first" in s: return 8 if outs < 2 else 3
    return 0

def pitcher_bonus(name: str) -> int:
    return PITCHER_EDGE.get(name or "", 0)

def park_adjust(home_team: str) -> int:
    return PARK_UNDER_EDGE.get(home_team or "", 0) + PARK_OVER_PENALTY.get(home_team or "", 0)

def rec(score: int) -> Tuple[str, str, str, str]:
    if score >= 94: return "A+", "5 STAR", "BET NOW", "elite"
    if score >= 88: return "A", "4 STAR", "STRONG", "strong"
    if score >= 80: return "B+", "3 STAR", "WATCH", "watch"
    if score >= 70: return "B", "2 STAR", "WAIT", "wait"
    return "C", "1 STAR", "PASS", "avoid"

def confidence(score: int) -> int:
    if score <= 0: return 0
    return max(35, min(98, int(score * 0.9 + 10)))

def estimated_ev(score: int, odds_edge: int = 0, weather_edge: int = 0, lineup_edge: int = 0, bullpen_edge: int = 0) -> float:
    if score < 70:
        return -4.0
    return round((score - 78) * 0.65 + odds_edge * 0.35 + weather_edge * 0.25 + lineup_edge * 0.25 + bullpen_edge * 0.25, 1)

def ev_class(ev: float) -> str:
    if ev >= 8: return "elite"
    if ev >= 4: return "strong"
    if ev >= 0: return "watch"
    return "avoid"

def kelly_pct(ev: float, confidence_pct: int) -> float:
    if ev <= 0 or confidence_pct < 80:
        return 0.0
    raw = (ev / 100.0) * (confidence_pct / 100.0) * 22
    return round(max(0, min(MAX_KELLY_PCT, raw)), 2)

def stake_amount(kelly: float) -> float:
    return round(BANKROLL * kelly / 100.0, 2)

def recommended_line(score: int, mode: str) -> str:
    if mode == "live":
        if score >= 94: return "Under live if line is 7.5+"
        if score >= 88: return "Under live 8 / 7.5 only"
        if score >= 80: return "Watch only, wait for better line"
        return "No live bet"
    else:
        if score >= 94: return "Under 8.5+ preferred"
        if score >= 88: return "Under 8 / 8.5 if price is fair"
        if score >= 80: return "Watchlist, wait for better price"
        return "No pregame bet"

def odds_snapshot() -> Dict[str, Any]:
    if not ODDS_API_KEY:
        return {
            "opening_total": "Waiting for live odds",
            "current_total": "Waiting for live odds",
            "movement": "Line movement pending",
            "sharp": "Sharp money pending",
            "edge": 0,
        }
    return {
        "opening_total": "ODDS_API_KEY detected",
        "current_total": "Provider mapping needed",
        "movement": "Endpoint integration pending",
        "sharp": "Not enough data",
        "edge": 0,
    }

def weather_snapshot(home_team: str) -> Dict[str, Any]:
    edge = 1 if home_team in PARK_UNDER_EDGE else 0
    return {
        "wind": "Weather API pending",
        "temp": "N/A",
        "roof": "N/A",
        "impact": f"Park factor edge +{edge}" if edge else "No weather edge yet",
        "edge": edge,
    }

def umpire_snapshot() -> Dict[str, Any]:
    return {"name": "Umpire pending", "under_pct": "N/A", "edge": 0}

def bullpen_snapshot(away: str, home: str) -> Dict[str, Any]:
    edge = 1 if away in PARK_UNDER_EDGE or home in PARK_UNDER_EDGE else 0
    return {
        "away": "Pending",
        "home": "Pending",
        "fatigue": "Bullpen fatigue pending",
        "edge": edge,
    }

def lineup_snapshot() -> Dict[str, Any]:
    return {"status": "Official lineup pending", "missing_bats": "Lineup data pending", "edge": 0}

def data_quality(away_pitcher: str, home_pitcher: str, score: int, has_odds: bool, has_weather: bool) -> str:
    q = 45
    if away_pitcher != "TBD" and home_pitcher != "TBD": q += 25
    if score >= 80: q += 10
    if has_odds: q += 12
    if has_weather: q += 8
    return f"{min(q, 98)}%"

def win_probability(score: int, ev: float) -> int:
    if score <= 0:
        return 0
    return max(40, min(96, int(45 + score * 0.45 + max(ev, 0) * 0.7)))

def live_under_score(total_runs: int, inning: int, outs: int, runners: List[str], status: str) -> Tuple[int, List[str]]:
    if "in progress" not in (status or "").lower():
        return 0, ["Game is not live"]
    score = 50
    reasons = []
    if inning >= 8:
        score += 30; reasons.append("Late inning")
    elif inning == 7:
        score += 25; reasons.append("Strong live Under zone")
    elif inning == 6:
        score += 18; reasons.append("Live Under zone starting")
    elif inning == 5:
        score += 9; reasons.append("Watch zone")
    else:
        score -= 20; reasons.append("Too early")

    if total_runs <= 3:
        score += 25; reasons.append("Very low total")
    elif total_runs == 4:
        score += 19; reasons.append("Low total")
    elif total_runs == 5:
        score += 12; reasons.append("Acceptable total")
    elif total_runs == 6:
        score += 3; reasons.append("Average total")
    else:
        score -= 18; reasons.append("Total already high")

    if outs == 2:
        score += 9; reasons.append("2 outs")
    elif outs == 0:
        score -= 8; reasons.append("0 outs risk")

    p = base_penalty(runners, outs)
    if p:
        score -= p; reasons.append(f"Base risk -{p}")
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
    if hours_to_start < -0.25: return 0, ["Game already started"]
    if hours_to_start > PREGAME_WINDOW_HOURS: return 45, [f"Too far: {hours_to_start:.1f}h"]

    teams = g.get("teams", {})
    away_pitcher = teams.get("away", {}).get("probablePitcher", {}).get("fullName", "")
    home_pitcher = teams.get("home", {}).get("probablePitcher", {}).get("fullName", "")
    home_team = teams.get("home", {}).get("team", {}).get("name", "")

    score = 56
    reasons = [f"{hours_to_start:.1f}h before first pitch"]

    if away_pitcher and home_pitcher:
        score += 12; reasons.append("Pitchers confirmed")
    else:
        score -= 10; reasons.append("Missing pitcher info")

    pb = pitcher_bonus(away_pitcher) + pitcher_bonus(home_pitcher)
    if pb:
        score += pb; reasons.append(f"Pitching +{pb}")
    else:
        reasons.append("No pitcher boost")

    park = park_adjust(home_team)
    if park > 0:
        score += park; reasons.append(f"Park +{park}")
    elif park < 0:
        score += park; reasons.append(f"Park penalty {park}")

    for label, edge in [("Weather", weather_edge), ("Umpire", umpire_edge), ("Market", odds_edge), ("Bullpen", bullpen_edge), ("Lineup", lineup_edge)]:
        if edge:
            score += edge; reasons.append(f"{label} +{edge}")

    if 0 <= hours_to_start <= 4:
        score += 6; reasons.append("Near first pitch")
    else:
        score += 2; reasons.append("Early watchlist")

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

    pitch_edge = pitcher_bonus(away_pitcher) + pitcher_bonus(home_pitcher)
    park_edge = park_adjust(home_team)
    total_edge = pitch_edge + park_edge + weather["edge"] + odds["edge"] + bullpen["edge"] + lineup["edge"] + umpire["edge"]

    return {
        "game_pk": g.get("gamePk"),
        "away": away_team, "home": home_team,
        "away_short": away_team.split()[-1][:3].upper(), "home_short": home_team.split()[-1][:3].upper(),
        "away_pitcher": away_pitcher, "home_pitcher": home_pitcher,
        "away_runs": away_runs, "home_runs": home_runs, "total_runs": total,
        "inning": inning_text(half, inning), "outs": outs, "runners": runner_label(runners),
        "status_label": status_label(status),
        "odds": odds, "weather": weather, "umpire": umpire, "bullpen": bullpen, "lineup": lineup,
        "live_score": live_score, "live_reasons": live_reasons, "live_rec": live_rec, "live_class": live_class, "live_stars": live_stars, "live_grade": live_grade,
        "live_conf": confidence(live_score), "live_ev": live_ev, "live_ev_class": ev_class(live_ev), "live_win_prob": win_probability(live_score, live_ev),
        "live_line": recommended_line(live_score, "live"), "live_kelly": kelly_pct(live_ev, confidence(live_score)), "live_stake": stake_amount(kelly_pct(live_ev, confidence(live_score))),
        "pregame_score": pre_score, "pregame_reasons": pre_reasons, "pregame_rec": pre_rec, "pregame_class": pre_class, "pregame_stars": pre_stars, "pregame_grade": pre_grade,
        "pregame_conf": confidence(pre_score), "pregame_ev": pre_ev, "pregame_ev_class": ev_class(pre_ev), "pregame_win_prob": win_probability(pre_score, pre_ev),
        "pregame_line": recommended_line(pre_score, "pregame"), "pregame_kelly": kelly_pct(pre_ev, confidence(pre_score)), "pregame_stake": stake_amount(kelly_pct(pre_ev, confidence(pre_score))),
        "best_score": best_score, "best_conf": best_conf, "best_mode": best_mode, "best_ev": best_ev, "best_kelly": best_kelly, "best_stake": stake_amount(best_kelly), "best_win_prob": best_win_prob,
        "quality": data_quality(away_pitcher, home_pitcher, best_score, bool(ODDS_API_KEY), bool(WEATHER_API_KEY)),
        "ai": {"pitch": pitch_edge, "park": park_edge, "weather": weather["edge"], "market": odds["edge"], "bullpen": bullpen["edge"], "lineup": lineup["edge"], "umpire": umpire["edge"], "total": total_edge},
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
        score, stars, conf, ev, rec_text, line, reasons, kelly, stake, winp = g["live_score"], g["live_stars"], g["live_conf"], g["live_ev"], g["live_rec"], g["live_line"], g["live_reasons"], g["live_kelly"], g["live_stake"], g["live_win_prob"]
        title = "LIVE UNDER ALERT"
    else:
        score, stars, conf, ev, rec_text, line, reasons, kelly, stake, winp = g["pregame_score"], g["pregame_stars"], g["pregame_conf"], g["pregame_ev"], g["pregame_rec"], g["pregame_line"], g["pregame_reasons"], g["pregame_kelly"], g["pregame_stake"], g["pregame_win_prob"]
        title = "PREGAME UNDER WATCH"

    return (
        f"<b>{title}</b>\n"
        f"<b>{g['away']}</b> vs <b>{g['home']}</b>\n"
        f"Score: <b>{score}/100</b> {stars}\n"
        f"Win Prob: <b>{winp}%</b> | Confidence: <b>{conf}%</b> | EV: <b>{ev}%</b>\n"
        f"Kelly: <b>{kelly}%</b> = <b>${stake}</b> for bankroll ${BANKROLL:.0f}\n"
        f"Recommendation: <b>{rec_text}</b>\n"
        f"Line to check: {line}\n"
        f"Reasons: {'; '.join(reasons)}\n\n"
        f"Verify sportsbook line before betting."
    )

def bot_loop():
    global bot_running
    print("MLB Under Pro v11 loop started")
    send_telegram("MLB Under Pro v11 is running.")
    while bot_running:
        try:
            games = refresh_games()
            for g in games:
                live_key = f"live-{g['game_pk']}-{g['inning']}-{g['total_runs']}-{g['outs']}-{g['runners']}"
                pre_key = f"pre-{g['game_pk']}"
                if g["live_score"] >= LIVE_ALERT_SCORE and g["live_ev"] >= 2 and live_key not in alerted:
                    send_telegram(telegram_alert(g, "live")); alerted.add(live_key)
                if g["pregame_score"] >= PREGAME_ALERT_SCORE and g["pregame_ev"] >= 1 and pre_key not in alerted:
                    send_telegram(telegram_alert(g, "pregame")); alerted.add(pre_key)
        except Exception as e:
            print("BOT ERROR:", repr(e))
        time.sleep(CHECK_EVERY_SECONDS)

def start_background_bot():
    global bot_running, bot_thread
    if not bot_running:
        bot_running = True
        bot_thread = threading.Thread(target=bot_loop, daemon=True)
        bot_thread.start()

HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MLB Under Pro v11</title>
<style>
:root{
  --bg:#06111f;--panel:#0b1e34;--panel2:#08182b;--line:#234967;--text:#f7fbff;--muted:#bcd0e3;
  --green:#22c55e;--yellow:#facc15;--orange:#fb923c;--red:#fb7185;--blue:#38bdf8;--purple:#a78bfa;
}
*{box-sizing:border-box}
body{font-family:Arial,Helvetica,sans-serif;margin:0;background:radial-gradient(circle at top,#11335a 0,#06111f 45%);color:var(--text);font-size:15px;line-height:1.35}
.shell{max-width:1240px;margin:0 auto;padding:14px}
.header{padding:16px 18px;margin-bottom:12px;background:rgba(8,16,30,.92);position:sticky;top:0;z-index:10;border:1px solid var(--line);border-radius:18px;backdrop-filter:blur(8px)}
h1{margin:0;font-size:30px;letter-spacing:.2px}.small{color:var(--muted);font-size:14px}
.btn{display:inline-block;margin-top:10px;margin-right:6px;background:var(--blue);color:#001;padding:10px 14px;border-radius:12px;text-decoration:none;font-weight:800;border:0}
.stop{background:var(--red);color:#111827}.start{background:var(--green);color:#111827}
.hero{display:grid;grid-template-columns:1fr;gap:12px}
.card,.best,.panel{padding:16px;border-radius:18px;background:linear-gradient(180deg,rgba(16,48,82,.96),rgba(8,24,43,.96));border:1px solid var(--line);box-shadow:0 8px 22px rgba(0,0,0,.22)}
.best{background:linear-gradient(135deg,#113b1e,#0b2a3e);border-color:var(--green)}
.title{font-weight:900;font-size:18px;margin-bottom:6px}.teams{font-size:24px;font-weight:900;margin:5px 0}
.badges{display:flex;flex-wrap:wrap;gap:7px;margin:8px 0}.badge{display:inline-block;padding:6px 10px;border-radius:999px;background:#10345c;color:#eaf6ff;font-weight:700;font-size:13px}
.gauge{width:100%;height:14px;background:#102d4d;border-radius:999px;overflow:hidden;margin:10px 0}.bar{height:100%;background:linear-gradient(90deg,#fb7185,#facc15,#22c55e)}
.grid{display:grid;grid-template-columns:1fr;gap:12px}.mini{display:grid;grid-template-columns:1fr;gap:10px;margin:12px 0}
.box{padding:12px;border-radius:14px;background:rgba(7,20,35,.8);border:1px solid rgba(88,137,177,.45)}
.elite{color:var(--green)}.strong{color:#4ade80}.watch{color:var(--yellow)}.wait{color:var(--orange)}.avoid{color:var(--red)}
.score{font-size:24px;font-weight:900}.muted{color:var(--muted)}.reason{margin-top:6px;color:#e5f2ff}
.table{width:100%;border-collapse:collapse;margin-top:8px}.table th,.table td{padding:9px;border-bottom:1px solid rgba(255,255,255,.08);text-align:left}.table th{color:#b9d7ef;font-size:13px}
.action{font-weight:900;border-radius:999px;padding:5px 9px;display:inline-block}.action.bet{background:#123f23;color:#63f59a}.action.watch{background:#3b3410;color:#ffe171}.action.wait{background:#3d2515;color:#ffb16b}
.airow{display:grid;grid-template-columns:90px 1fr 44px;align-items:center;gap:8px;margin:8px 0}.aibar{height:9px;background:#102d4d;border-radius:999px;overflow:hidden}.aifill{height:100%;background:linear-gradient(90deg,var(--blue),var(--green))}
.footer{margin-top:12px;padding:15px;border-radius:16px;background:#08182b;border:1px solid var(--line);color:var(--muted)}
@media(min-width:900px){.hero{grid-template-columns:1.15fr .85fr}.grid{grid-template-columns:1fr 1fr}.mini{grid-template-columns:repeat(3,1fr)}}
</style>
</head>
<body><div class="shell">
<div class="header">
<h1>MLB Under Pro v11</h1>
<div class="small">Bot: {{ "RUNNING" if running else "STOPPED" }} | Updated: {{last_update}} | Live {{live_alert}}+ | Pregame {{pregame_alert}}+ | Hide below {{min_display}} | Bankroll ${{bankroll}}</div>
<form method="post" action="/start" style="display:inline"><button class="btn start">Start Bot</button></form>
<form method="post" action="/stop" style="display:inline"><button class="btn stop">Stop Bot</button></form>
<a class="btn" href="/refresh">Refresh</a>
<a class="btn" href="/test">Test Telegram</a>
</div>

<div class="hero">
{% if best %}
<div class="best">
<div class="title">BEST BET OF THE DAY</div>
<div class="teams">{{best.away}} vs {{best.home}}</div>
<div class="badges">
<span class="badge">Mode {{best.best_mode}}</span><span class="badge">Score {{best.best_score}}/100</span><span class="badge">Win {{best.best_win_prob}}%</span><span class="badge">EV {{best.best_ev}}%</span><span class="badge">Kelly {{best.best_kelly}}% = ${{best.best_stake}}</span>
</div>
<div class="gauge"><div class="bar" style="width:{{best.best_score}}%"></div></div>
<div class="muted">Pitchers: {{best.away_pitcher}} vs {{best.home_pitcher}} | Data quality: {{best.quality}}</div>
</div>
<div class="panel">
<div class="title">AI Recommendation</div>
<div class="score {{ 'elite' if best.best_score >= 94 else 'strong' if best.best_score >= 88 else 'watch' }}">BET FOCUS: {{best.best_mode}} UNDER</div>
<div class="badges">
<span class="badge">Recommended: {{best.pregame_line if best.best_mode == 'Pregame' else best.live_line}}</span>
<span class="badge">Win Prob {{best.best_win_prob}}%</span>
<span class="badge">EV {{best.best_ev}}%</span>
</div>
<div class="muted">Use only if sportsbook line and price still match the recommended range.</div>
</div>
{% endif %}
</div>

<div class="panel">
<div class="title">Top Under Board</div>
<table class="table">
<thead><tr><th>#</th><th>Game</th><th>Mode</th><th>Score</th><th>Win</th><th>EV</th><th>Kelly</th><th>Action</th></tr></thead>
<tbody>
{% for g in games[:8] %}
<tr>
<td>{{loop.index}}</td><td>{{g.away_short}} @ {{g.home_short}}</td><td>{{g.best_mode}}</td><td>{{g.best_score}}</td><td>{{g.best_win_prob}}%</td><td>{{g.best_ev}}%</td><td>{{g.best_kelly}}%</td>
<td><span class="action {{ 'bet' if g.best_score >= 88 else 'watch' if g.best_score >= 80 else 'wait' }}">{{ 'BET' if g.best_score >= 88 else 'WATCH' if g.best_score >= 80 else 'WAIT' }}</span></td>
</tr>
{% endfor %}
</tbody></table>
</div>

{% for g in games %}
<div class="card">
 <div class="teams">{{g.away}} {{g.away_runs}} - {{g.home_runs}} {{g.home}}</div>
 <div class="small">{{g.status_label}} | {{g.inning}} | Outs: {{g.outs}} | {{g.runners}} | Current total: {{g.total_runs}}</div>
 <div class="small">Pitchers: {{g.away_pitcher}} vs {{g.home_pitcher}} | Data quality: {{g.quality}}</div>

 <div class="mini">
   <div class="box"><b>Odds</b><br>Opening: {{g.odds.opening_total}}<br>Current: {{g.odds.current_total}}<br>{{g.odds.movement}}<br>{{g.odds.sharp}}</div>
   <div class="box"><b>Weather</b><br>Wind: {{g.weather.wind}}<br>Temp: {{g.weather.temp}}<br>Roof: {{g.weather.roof}}<br>{{g.weather.impact}}</div>
   <div class="box"><b>Umpire</b><br>{{g.umpire.name}}<br>Under %: {{g.umpire.under_pct}}</div>
 </div>

 <div class="grid">
   <div class="box">
     <div class="score {{g.live_class}}">Live Under {{g.live_score}}/100 | {{g.live_grade}}</div>
     <div><b>{{g.live_rec}}</b> | {{g.live_stars}}</div>
     <div class="gauge"><div class="bar" style="width:{{g.live_score}}%"></div></div>
     <div class="badges"><span class="badge">Win {{g.live_win_prob}}%</span><span class="badge">EV {{g.live_ev}}%</span><span class="badge">Kelly {{g.live_kelly}}% = ${{g.live_stake}}</span></div>
     <div class="reason">Line: {{g.live_line}}</div>
     <div class="reason">Reasons: {{ "; ".join(g.live_reasons) }}</div>
   </div>
   <div class="box">
     <div class="score {{g.pregame_class}}">Pregame Under {{g.pregame_score}}/100 | {{g.pregame_grade}}</div>
     <div><b>{{g.pregame_rec}}</b> | {{g.pregame_stars}}</div>
     <div class="gauge"><div class="bar" style="width:{{g.pregame_score}}%"></div></div>
     <div class="badges"><span class="badge">Win {{g.pregame_win_prob}}%</span><span class="badge">EV {{g.pregame_ev}}%</span><span class="badge">Kelly {{g.pregame_kelly}}% = ${{g.pregame_stake}}</span></div>
     <div class="reason">Line: {{g.pregame_line}}</div>
     <div class="reason">Reasons: {{ "; ".join(g.pregame_reasons) }}</div>
   </div>
 </div>

 <div class="mini">
   <div class="box"><b>Bullpen</b><br>Away: {{g.bullpen.away}}<br>Home: {{g.bullpen.home}}<br>{{g.bullpen.fatigue}}</div>
   <div class="box"><b>Lineup</b><br>{{g.lineup.status}}<br>{{g.lineup.missing_bats}}</div>
   <div class="box"><b>AI Edge Breakdown</b>
     <div class="airow"><span>Pitch</span><div class="aibar"><div class="aifill" style="width:{{ [g.ai.pitch*4,100]|min }}%"></div></div><b>+{{g.ai.pitch}}</b></div>
     <div class="airow"><span>Park</span><div class="aibar"><div class="aifill" style="width:{{ [g.ai.park*8,100]|min }}%"></div></div><b>{{g.ai.park}}</b></div>
     <div class="airow"><span>Market</span><div class="aibar"><div class="aifill" style="width:{{ [g.ai.market*8,100]|min }}%"></div></div><b>+{{g.ai.market}}</b></div>
     <div class="airow"><span>Total</span><div class="aibar"><div class="aifill" style="width:{{ [g.ai.total*3,100]|min }}%"></div></div><b>{{g.ai.total}}</b></div>
   </div>
 </div>
</div>
{% endfor %}

<div class="footer">
v11 uses the cleaner interface 2 style: large Best Bet, AI Recommendation, table-style Top Board, compact cards, better spacing, and clearer action labels. API sections are ready for real Odds, Weather, Umpire, Bullpen, and Lineup data.
</div>
</div></body></html>"""

@app.route("/")
def index():
    games = filtered_games()
    html = render_template_string(
        HTML, games=games, best=best_bet(), running=bot_running, last_update=last_update,
        live_alert=LIVE_ALERT_SCORE, pregame_alert=PREGAME_ALERT_SCORE, min_display=MIN_DISPLAY_SCORE,
        bankroll=int(BANKROLL) if BANKROLL.is_integer() else BANKROLL, max_kelly=MAX_KELLY_PCT
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
    try: refresh_games()
    except Exception as e: return Response(f"Refresh error: {e}", status=500, content_type="text/plain; charset=utf-8")
    return redirect(url_for("index"))

@app.route("/test")
def test():
    ok = send_telegram("Test OK: MLB Under Pro v11 Telegram connected.")
    return Response("Telegram sent OK" if ok else "Telegram failed. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.", status=200 if ok else 500, content_type="text/plain; charset=utf-8")

@app.route("/api/games")
def api_games():
    return jsonify({"running": bot_running, "last_update": last_update, "games": latest_games})

if __name__ == "__main__":
    try: refresh_games()
    except Exception as e: print("Initial refresh error:", repr(e))
    if AUTO_START: start_background_bot()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
