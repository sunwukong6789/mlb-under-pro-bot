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
last_update = "ChÆ°a cáº­p nháº­t"
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

def status_vi(status: str) -> str:
    s = (status or "").lower()
    if "in progress" in s: return "Äang live"
    if "scheduled" in s: return "ChÆ°a báº¯t Äáº§u"
    if "pre-game" in s or "warmup" in s: return "Sáº¯p báº¯t Äáº§u"
    if "final" in s: return "ÄÃ£ káº¿t thÃºc"
    return status or "KhÃ´ng rÃµ"

def inning_text(half: str, inning: int) -> str:
    if not inning: return "Pregame"
    if (half or "").lower().startswith("top"): return f"Top {inning}"
    if (half or "").lower().startswith("bottom"): return f"Bot {inning}"
    return f"Inning {inning}"

def runner_vi(runners: List[str]) -> str:
    if not runners: return "Bases trá»ng"
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
    if score >= 94: return "A+", "â­â­â­â­â­", "ð¥ BEST BET", "elite"
    if score >= 88: return "A", "â­â­â­â­", "â Ráº¤T Äáº¸P", "strong"
    if score >= 80: return "B+", "â­â­â­", "ð WATCHLIST", "watch"
    if score >= 70: return "B", "â­â­", "â³ CHá» THÃM", "wait"
    return "C", "â­", "â Bá» QUA", "avoid"

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
        if score >= 94: return "Under live náº¿u line cÃ²n 7.5+"
        if score >= 88: return "Under live náº¿u line cÃ²n 8 / 7.5"
        if score >= 80: return "Chá» theo dÃµi, chÆ°a vá»i vÃ o"
        return "KhÃ´ng vÃ o live lÃºc nÃ y"
    else:
        if score >= 94: return "Pregame Under 8.5+ ráº¥t ÄÃ¡ng kiá»m tra"
        if score >= 88: return "Pregame Under 8 / 8.5 náº¿u odds á»n"
        if score >= 80: return "Watchlist, chá» line tá»t hÆ¡n"
        return "KhÃ´ng Æ°u tiÃªn pregame"

def odds_snapshot() -> Dict[str, Any]:
    if not ODDS_API_KEY:
        return {
            "opening_total": "ChÆ°a gáº¯n Odds API",
            "current_total": "ChÆ°a gáº¯n Odds API",
            "movement": "ChÆ°a cÃ³ line movement",
            "sharp": "Chá» dá»¯ liá»u odds tháº­t",
            "edge": 0,
        }
    return {
        "opening_total": "ÄÃ£ cÃ³ ODDS_API_KEY",
        "current_total": "Cáº§n map provider",
        "movement": "Äang chá» tÃ­ch há»£p endpoint",
        "sharp": "ChÆ°a Äá»§ dá»¯ liá»u",
        "edge": 0,
    }

def weather_snapshot(home_team: str) -> Dict[str, Any]:
    edge = 0
    if home_team in PARK_UNDER_EDGE:
        edge += 1
    return {
        "wind": "ChÆ°a gáº¯n Weather API",
        "temp": "ChÆ°a cÃ³",
        "roof": "ChÆ°a cÃ³",
        "impact": f"Park/weather edge táº¡m tÃ­nh: +{edge}" if edge else "ChÆ°a cÃ³ weather edge",
        "edge": edge,
    }

def umpire_snapshot() -> Dict[str, Any]:
    return {"name": "ChÆ°a cÃ³ umpire", "under_pct": "ChÆ°a cÃ³", "edge": 0}

def bullpen_snapshot(away: str, home: str) -> Dict[str, Any]:
    # Placeholder until team pitching endpoint is added.
    edge = 0
    if away in PARK_UNDER_EDGE or home in PARK_UNDER_EDGE:
        edge += 1
    return {
        "away": "ChÆ°a gáº¯n bullpen API",
        "home": "ChÆ°a gáº¯n bullpen API",
        "fatigue": "ChÆ°a cÃ³ bullpen fatigue",
        "edge": edge,
    }

def lineup_snapshot() -> Dict[str, Any]:
    return {
        "status": "ChÆ°a cÃ³ lineup chÃ­nh thá»©c",
        "missing_bats": "Chá» lineup",
        "edge": 0,
    }

def data_quality(away_pitcher: str, home_pitcher: str, score: int, has_odds: bool, has_weather: bool) -> str:
    q = 45
    if away_pitcher != "TBD" and home_pitcher != "TBD": q += 25
    if score >= 80: q += 10
    if has_odds: q += 12
    if has_weather: q += 8
    return f"{min(q, 98)}%"

def live_under_score(total_runs: int, inning: int, outs: int, runners: List[str], status: str) -> Tuple[int, List[str]]:
    if "in progress" not in (status or "").lower():
        return 0, ["Tráº­n chÆ°a live"]
    score = 50
    reasons = []
    if inning >= 8:
        score += 30; reasons.append("Cuá»i tráº­n, thá»i gian ghi Äiá»m cÃ²n Ã­t")
    elif inning == 7:
        score += 25; reasons.append("Inning 7, vÃ¹ng live Under Äáº¹p")
    elif inning == 6:
        score += 18; reasons.append("Inning 6, báº¯t Äáº§u vÃ o vÃ¹ng live Under")
    elif inning == 5:
        score += 9; reasons.append("Inning 5, theo dÃµi")
    else:
        score -= 20; reasons.append("CÃ²n sá»m, rá»§i ro cao")

    if total_runs <= 3:
        score += 25; reasons.append("Tá»ng Äiá»m ráº¥t tháº¥p")
    elif total_runs == 4:
        score += 19; reasons.append("Tá»ng Äiá»m tháº¥p")
    elif total_runs == 5:
        score += 12; reasons.append("Tá»ng Äiá»m táº¡m á»n")
    elif total_runs == 6:
        score += 3; reasons.append("Tá»ng Äiá»m trung bÃ¬nh")
    else:
        score -= 18; reasons.append("Tá»ng Äiá»m ÄÃ£ cao")

    if outs == 2:
        score += 9; reasons.append("2 outs, giáº£m rá»§i ro ghi Äiá»m")
    elif outs == 0:
        score -= 8; reasons.append("0 out, rá»§i ro cao")

    p = base_penalty(runners, outs)
    if p:
        score -= p; reasons.append(f"CÃ³ runner trÃªn base, trá»« {p} Äiá»m")
    else:
        reasons.append("Bases trá»ng")
    return max(0, min(100, score)), reasons

def pregame_under_score(g: Dict[str, Any], weather_edge: int, umpire_edge: int, odds_edge: int, bullpen_edge: int, lineup_edge: int) -> Tuple[int, List[str]]:
    status = g.get("status", {}).get("detailedState", "")
    s = (status or "").lower()
    if "scheduled" not in s and "pre-game" not in s and "warmup" not in s:
        return 0, ["KhÃ´ng pháº£i pregame"]

    game_dt = parse_game_time(g.get("gameDate", ""))
    if not game_dt:
        return 55, ["ChÆ°a Äá»c ÄÆ°á»£c giá» thi Äáº¥u"]

    hours_to_start = (game_dt - now_utc()).total_seconds() / 3600
    if hours_to_start < -0.25: return 0, ["Tráº­n ÄÃ£ báº¯t Äáº§u hoáº·c ÄÃ£ qua giá»"]
    if hours_to_start > PREGAME_WINDOW_HOURS: return 45, [f"CÃ²n xa giá» thi Äáº¥u: {hours_to_start:.1f} giá»"]

    teams = g.get("teams", {})
    away_pitcher = teams.get("away", {}).get("probablePitcher", {}).get("fullName", "")
    home_pitcher = teams.get("home", {}).get("probablePitcher", {}).get("fullName", "")
    home_team = teams.get("home", {}).get("team", {}).get("name", "")

    score = 56
    reasons = [f"CÃ²n {hours_to_start:.1f} giá» trÆ°á»c khi bÃ³ng cháº¡y"]

    if away_pitcher and home_pitcher:
        score += 12; reasons.append("ÄÃ£ cÃ³ probable pitchers")
    else:
        score -= 10; reasons.append("Thiáº¿u thÃ´ng tin pitcher")

    pb = pitcher_bonus(away_pitcher) + pitcher_bonus(home_pitcher)
    if pb:
        score += pb; reasons.append(f"Pitcher edge +{pb}")
    else:
        reasons.append("Pitcher chÆ°a Äá»§ dá»¯ liá»u nÃ¢ng Äiá»m")

    park = park_adjust(home_team)
    if park > 0:
        score += park; reasons.append(f"SÃ¢n cÃ³ xu hÆ°á»ng Under +{park}")
    elif park < 0:
        score += park; reasons.append(f"SÃ¢n dá» Over {park}")

    for label, edge in [
        ("Weather edge", weather_edge),
        ("Umpire edge", umpire_edge),
        ("Odds/line edge", odds_edge),
        ("Bullpen edge", bullpen_edge),
        ("Lineup edge", lineup_edge),
    ]:
        if edge:
            score += edge; reasons.append(f"{label} +{edge}")

    if 0 <= hours_to_start <= 4:
        score += 6; reasons.append("Gáº§n giá» thi Äáº¥u, nÃªn kiá»m tra line")
    else:
        score += 2; reasons.append("Watchlist trÆ°á»c giá»")

    reasons.append("Cáº§n kiá»m tra thÃªm total line, lineup, weather tháº­t")
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

    return {
        "game_pk": g.get("gamePk"),
        "away": away_team, "home": home_team,
        "away_pitcher": away_pitcher, "home_pitcher": home_pitcher,
        "away_runs": away_runs, "home_runs": home_runs, "total_runs": total,
        "inning": inning_text(half, inning), "outs": outs, "runners": runner_vi(runners),
        "status_vi": status_vi(status),
        "odds": odds, "weather": weather, "umpire": umpire, "bullpen": bullpen, "lineup": lineup,
        "live_score": live_score, "live_reasons": live_reasons, "live_rec": live_rec, "live_class": live_class, "live_stars": live_stars, "live_grade": live_grade,
        "live_conf": confidence(live_score), "live_ev": live_ev, "live_ev_class": ev_class(live_ev),
        "live_line": recommended_line(live_score, "live"), "live_kelly": kelly_pct(live_ev, confidence(live_score)), "live_stake": stake_amount(kelly_pct(live_ev, confidence(live_score))),
        "pregame_score": pre_score, "pregame_reasons": pre_reasons, "pregame_rec": pre_rec, "pregame_class": pre_class, "pregame_stars": pre_stars, "pregame_grade": pre_grade,
        "pregame_conf": confidence(pre_score), "pregame_ev": pre_ev, "pregame_ev_class": ev_class(pre_ev),
        "pregame_line": recommended_line(pre_score, "pregame"), "pregame_kelly": kelly_pct(pre_ev, confidence(pre_score)), "pregame_stake": stake_amount(kelly_pct(pre_ev, confidence(pre_score))),
        "best_score": best_score, "best_conf": best_conf, "best_mode": best_mode, "best_ev": best_ev, "best_kelly": best_kelly, "best_stake": stake_amount(best_kelly),
        "quality": data_quality(away_pitcher, home_pitcher, best_score, bool(ODDS_API_KEY), bool(WEATHER_API_KEY)),
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
        score, stars, conf, ev, rec_text, line, reasons, kelly, stake = g["live_score"], g["live_stars"], g["live_conf"], g["live_ev"], g["live_rec"], g["live_line"], g["live_reasons"], g["live_kelly"], g["live_stake"]
        title = "ð¨ LIVE UNDER ALERT"
    else:
        score, stars, conf, ev, rec_text, line, reasons, kelly, stake = g["pregame_score"], g["pregame_stars"], g["pregame_conf"], g["pregame_ev"], g["pregame_rec"], g["pregame_line"], g["pregame_reasons"], g["pregame_kelly"], g["pregame_stake"]
        title = "â¾ PREGAME UNDER WATCH"

    return (
        f"{title}\n"
        f"<b>{g['away']}</b> vs <b>{g['home']}</b>\n"
        f"Score: <b>{score}/100</b> {stars}\n"
        f"Confidence: <b>{conf}%</b> | EV est: <b>{ev}%</b>\n"
        f"Kelly gá»£i Ã½: <b>{kelly}%</b> â <b>${stake}</b> náº¿u bankroll ${BANKROLL:.0f}\n"
        f"Khuyáº¿n nghá»: <b>{rec_text}</b>\n"
        f"Line nÃªn kiá»m tra: {line}\n"
        f"Pitchers: {g['away_pitcher']} vs {g['home_pitcher']}\n"
        f"Odds: {g['odds']['movement']}\n"
        f"Weather: {g['weather']['impact']}\n"
        f"Umpire: {g['umpire']['name']}\n"
        f"Bullpen: {g['bullpen']['fatigue']}\n"
        f"Lineup: {g['lineup']['status']}\n"
        f"LÃ½ do: {'; '.join(reasons)}\n\n"
        f"â ï¸ LuÃ´n kiá»m tra sportsbook trÆ°á»c khi bet."
    )

def bot_loop():
    global bot_running
    print("MLB Under Pro v9 loop started")
    send_telegram("â MLB Under Pro v9 ÄÃ£ cháº¡y. CÃ³ Kelly stake, EV, Bullpen/Lineup slots vÃ  Best Bet.")
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
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MLB Under Pro v9</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,Arial;margin:0;background:#06111f;color:white}
.header{padding:18px;background:#08101e;position:sticky;top:0;z-index:10;border-bottom:1px solid #1f3b57}
h1{margin:0;font-size:25px}.small{color:#d4e4f5;font-size:14px}
.card,.topbox,.footer,.best{margin:12px;padding:15px;border-radius:16px;background:#0d2138;border:1px solid #21496f}
.best{background:#102814;border-color:#22c55e}
.teams{font-size:19px;font-weight:800}.grid{display:grid;grid-template-columns:1fr;gap:10px}.mini{display:grid;grid-template-columns:1fr;gap:8px}
.score{font-size:24px;font-weight:900;margin-top:8px}
.elite{color:#22c55e}.strong{color:#4ade80}.watch{color:#facc15}.wait{color:#fb923c}.avoid{color:#fb7185}
.btn{display:inline-block;margin-top:10px;margin-right:6px;background:#38bdf8;color:#001;padding:10px 14px;border-radius:12px;text-decoration:none;font-weight:800;border:0}
.stop{background:#fb7185;color:#111827}.start{background:#22c55e;color:#111827}
.reason{margin-top:5px;color:#e5f2ff}.box{padding:10px;border-radius:12px;background:#091a2d;border:1px solid #1f3b57}
.badge{display:inline-block;margin-top:5px;margin-right:5px;padding:4px 8px;border-radius:999px;background:#102d4d}
@media(min-width:800px){.grid{grid-template-columns:1fr 1fr}.mini{grid-template-columns:1fr 1fr 1fr}}
</style>
</head>
<body>
<div class="header">
<h1>â¾ MLB Under Pro v9</h1>
<div class="small">Bot: {{ "ÄANG CHáº Y ð¢" if running else "ÄANG Dá»ªNG ð´" }} | Cáº­p nháº­t: {{last_update}}</div>
<div class="small">Live Alert: {{live_alert}}+ | Pregame Alert: {{pregame_alert}}+ | áº¨n game dÆ°á»i {{min_display}} | Bankroll: ${{bankroll}}</div>
<form method="post" action="/start" style="display:inline"><button class="btn start">â¶ Start Bot</button></form>
<form method="post" action="/stop" style="display:inline"><button class="btn stop">â  Stop Bot</button></form>
<a class="btn" href="/refresh">â» Refresh</a>
<a class="btn" href="/test">Test Telegram</a>
</div>

{% if best %}
<div class="best">
<b>ð¥ BEST BET OF THE DAY</b><br>
<div class="teams">{{best.away}} vs {{best.home}}</div>
Best Mode: <b>{{best.best_mode}}</b> | Score: <b>{{best.best_score}}/100</b> | Confidence: <b>{{best.best_conf}}%</b> | EV: <b>{{best.best_ev}}%</b><br>
Kelly gá»£i Ã½: <b>{{best.best_kelly}}%</b> â <b>${{best.best_stake}}</b><br>
Pitchers: {{best.away_pitcher}} vs {{best.home_pitcher}}<br>
Data quality: {{best.quality}}
</div>
{% endif %}

<div class="topbox">
<b>ð Top kÃ¨o Under Äáº¹p nháº¥t</b><br>
{% for g in games[:5] %}
{{loop.index}}. {{g.away}} vs {{g.home}} â {{g.best_mode}} â <b>{{g.best_score}}/100</b> â Conf <b>{{g.best_conf}}%</b> â EV <b>{{g.best_ev}}%</b> â Kelly <b>{{g.best_kelly}}%</b><br>
{% endfor %}
</div>

{% for g in games %}
<div class="card">
 <div class="teams">{{g.away}} {{g.away_runs}} - {{g.home_runs}} {{g.home}}</div>
 <div class="small">{{g.status_vi}} | {{g.inning}} | Outs: {{g.outs}} | {{g.runners}}</div>
 <div class="small">Pitchers: {{g.away_pitcher}} vs {{g.home_pitcher}} | Data quality: {{g.quality}}</div>
 <div class="small">Tá»ng Äiá»m hiá»n táº¡i: {{g.total_runs}}</div>

 <div class="mini">
   <div class="box"><b>ð Odds</b><br>Opening: {{g.odds.opening_total}}<br>Current: {{g.odds.current_total}}<br>{{g.odds.movement}}<br>{{g.odds.sharp}}</div>
   <div class="box"><b>ð¦ Weather</b><br>Wind: {{g.weather.wind}}<br>Temp: {{g.weather.temp}}<br>Roof: {{g.weather.roof}}<br>{{g.weather.impact}}</div>
   <div class="box"><b>ð¨ââï¸ Umpire</b><br>{{g.umpire.name}}<br>Under %: {{g.umpire.under_pct}}</div>
 </div>
 <div class="mini">
   <div class="box"><b>ðª Bullpen</b><br>Away: {{g.bullpen.away}}<br>Home: {{g.bullpen.home}}<br>{{g.bullpen.fatigue}}</div>
   <div class="box"><b>ð¥ Lineup</b><br>{{g.lineup.status}}<br>{{g.lineup.missing_bats}}</div>
   <div class="box"><b>ðµ Bankroll</b><br>Bankroll: ${{bankroll}}<br>Max Kelly: {{max_kelly}}%</div>
 </div>

 <div class="grid">
   <div class="box">
     <div class="score {{g.live_class}}">Live Under: {{g.live_score}}/100 {{g.live_stars}} {{g.live_grade}}</div>
     <div class="{{g.live_class}}"><b>{{g.live_rec}}</b></div>
     <div><span class="badge">Confidence: {{g.live_conf}}%</span><span class="badge {{g.live_ev_class}}">EV est: {{g.live_ev}}%</span><span class="badge">Kelly: {{g.live_kelly}}% â ${{g.live_stake}}</span></div>
     <div class="reason">Line nÃªn kiá»m tra: {{g.live_line}}</div>
     <div class="reason">LÃ½ do live: {{ "; ".join(g.live_reasons) }}</div>
   </div>
   <div class="box">
     <div class="score {{g.pregame_class}}">Pregame Under: {{g.pregame_score}}/100 {{g.pregame_stars}} {{g.pregame_grade}}</div>
     <div class="{{g.pregame_class}}"><b>{{g.pregame_rec}}</b></div>
     <div><span class="badge">Confidence: {{g.pregame_conf}}%</span><span class="badge {{g.pregame_ev_class}}">EV est: {{g.pregame_ev}}%</span><span class="badge">Kelly: {{g.pregame_kelly}}% â ${{g.pregame_stake}}</span></div>
     <div class="reason">Line nÃªn kiá»m tra: {{g.pregame_line}}</div>
     <div class="reason">LÃ½ do pregame: {{ "; ".join(g.pregame_reasons) }}</div>
   </div>
 </div>
</div>
{% endfor %}

<div class="footer">
<b>V9 cÃ³ gÃ¬ má»i:</b><br>
â¢ Kelly stake % theo bankroll<br>
â¢ Bullpen + Lineup slots<br>
â¢ Grade A+, A, B+, B<br>
â¢ Telegram cÃ³ Kelly, EV, odds/weather/umpire/bullpen/lineup<br><br>
LÆ°u Ã½: Odds, Weather, Umpire, Bullpen, Lineup hiá»n lÃ  slot chá» API tháº­t. EV/Kelly lÃ  Æ°á»c tÃ­nh tham kháº£o, khÃ´ng pháº£i lá»i khuyÃªn tÃ i chÃ­nh.
</div>
</body>
</html>"""

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
    ok = send_telegram("â Test thÃ nh cÃ´ng: MLB Under Pro v9 ÄÃ£ káº¿t ná»i Telegram.")
    return Response("Telegram sent â" if ok else "Telegram failed â. Kiá»m tra TELEGRAM_BOT_TOKEN vÃ  TELEGRAM_CHAT_ID.", status=200 if ok else 500, content_type="text/plain; charset=utf-8")

@app.route("/api/games")
def api_games():
    return jsonify({"running": bot_running, "last_update": last_update, "games": latest_games})

if __name__ == "__main__":
    try: refresh_games()
    except Exception as e: print("Initial refresh error:", repr(e))
    if AUTO_START: start_background_bot()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
