import os
import time
import threading
import datetime as dt
from typing import Dict, Any, List, Set, Tuple

import requests
from flask import Flask, jsonify, render_template_string, redirect, url_for

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CHECK_EVERY_SECONDS = int(os.getenv("CHECK_EVERY_SECONDS", "300"))
LIVE_ALERT_SCORE = int(os.getenv("LIVE_ALERT_SCORE", os.getenv("ALERT_SCORE", "85")))
PREGAME_ALERT_SCORE = int(os.getenv("PREGAME_ALERT_SCORE", "72"))
PREGAME_WINDOW_HOURS = int(os.getenv("PREGAME_WINDOW_HOURS", "6"))
AUTO_START = os.getenv("AUTO_START", "1") == "1"

MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"

app = Flask(__name__)

latest_games: List[Dict[str, Any]] = []
last_update = "Never"
alerted: Set[str] = set()
bot_running = False
bot_thread = None


def today() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d")


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_game_time(game_date: str):
    if not game_date:
        return None
    try:
        return dt.datetime.fromisoformat(game_date.replace("Z", "+00:00"))
    except Exception:
        return None


def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
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
        params={
            "sportId": 1,
            "date": today(),
            "hydrate": "linescore,team,probablePitcher",
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    games = []
    for d in data.get("dates", []):
        games.extend(d.get("games", []))
    return games


def base_penalty(runners: List[str], outs: int) -> int:
    s = set(runners)
    if not s:
        return 0
    if len(s) == 3:
        return 28 if outs < 2 else 16
    if "second" in s and "third" in s:
        return 24 if outs < 2 else 13
    if "third" in s:
        return 20 if outs < 2 else 10
    if "second" in s:
        return 12 if outs < 2 else 5
    if "first" in s:
        return 7 if outs < 2 else 3
    return 0


def live_under_score(total_runs: int, inning: int, outs: int, runners: List[str], status: str) -> Tuple[int, List[str]]:
    status_l = (status or "").lower()
    if "in progress" not in status_l:
        return 0, ["not live"]

    score = 50
    reasons = []

    if inning >= 8:
        score += 24
        reasons.append("late inning 8+")
    elif inning == 7:
        score += 19
        reasons.append("inning 7")
    elif inning == 6:
        score += 13
        reasons.append("inning 6")
    elif inning == 5:
        score += 5
        reasons.append("inning 5")
    else:
        score -= 18
        reasons.append("con som")

    if total_runs <= 3:
        score += 20
        reasons.append("diem rat thap")
    elif total_runs == 4:
        score += 16
        reasons.append("diem thap")
    elif total_runs == 5:
        score += 10
        reasons.append("diem on")
    elif total_runs == 6:
        score += 2
        reasons.append("diem trung binh")
    else:
        score -= 14
        reasons.append("diem cao")

    if outs == 2:
        score += 6
        reasons.append("2 outs")
    elif outs == 0:
        score -= 5
        reasons.append("0 out")

    p = base_penalty(runners, outs)
    if p:
        score -= p
        reasons.append(f"base nguy hiem -{p}")
    else:
        reasons.append("base sach")

    return max(0, min(100, score)), reasons


def pregame_under_score(g: Dict[str, Any]) -> Tuple[int, List[str]]:
    status = g.get("status", {}).get("detailedState", "")
    status_l = (status or "").lower()
    if "scheduled" not in status_l and "pre-game" not in status_l and "warmup" not in status_l:
        return 0, ["not pregame"]

    game_dt = parse_game_time(g.get("gameDate", ""))
    if not game_dt:
        return 50, ["pregame, chua doc duoc gio"]

    hours_to_start = (game_dt - now_utc()).total_seconds() / 3600
    if hours_to_start < -0.25:
        return 0, ["da qua gio"]
    if hours_to_start > PREGAME_WINDOW_HOURS:
        return 45, [f"con xa gio: {hours_to_start:.1f}h"]

    teams = g.get("teams", {})
    away_pitcher = teams.get("away", {}).get("probablePitcher", {}).get("fullName")
    home_pitcher = teams.get("home", {}).get("probablePitcher", {}).get("fullName")

    score = 58
    reasons = [f"truoc gio {hours_to_start:.1f}h"]

    if away_pitcher and home_pitcher:
        score += 12
        reasons.append("co probable pitchers")
    else:
        score -= 8
        reasons.append("thieu probable pitcher")

    # Conservative pregame rule without odds/weather API:
    # alert only medium score; user should check line manually.
    score += 5
    reasons.append("pregame candidate - can check total line")

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

    live_score, live_reasons = live_under_score(total, inning, outs, runners, status)
    pre_score, pre_reasons = pregame_under_score(g)

    return {
        "game_pk": g.get("gamePk"),
        "away": away.get("team", {}).get("name", "Away"),
        "home": home.get("team", {}).get("name", "Home"),
        "away_pitcher": away.get("probablePitcher", {}).get("fullName", "TBD"),
        "home_pitcher": home.get("probablePitcher", {}).get("fullName", "TBD"),
        "away_runs": away_runs,
        "home_runs": home_runs,
        "total_runs": total,
        "inning": inning,
        "half": half,
        "outs": outs,
        "runners": ", ".join(runners) if runners else "none",
        "status": status,
        "live_score": live_score,
        "live_reasons": ", ".join(live_reasons),
        "pregame_score": pre_score,
        "pregame_reasons": ", ".join(pre_reasons),
        "best_score": max(live_score, pre_score),
    }


def refresh_games() -> List[Dict[str, Any]]:
    global latest_games, last_update
    games = [parse_game(g) for g in fetch_mlb_games()]
    games.sort(key=lambda x: x["best_score"], reverse=True)
    latest_games = games
    last_update = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("Refreshed games:", len(games), "at", last_update)
    return games


def live_alert_message(g: Dict[str, Any]) -> str:
    return (
        f"ð¥ <b>LIVE UNDER ALERT</b>\n"
        f"<b>{g['away']}</b> {g['away_runs']} - {g['home_runs']} <b>{g['home']}</b>\n"
        f"{g['half']} {g['inning']} | Outs: {g['outs']} | Runners: {g['runners']}\n"
        f"Total runs: {g['total_runs']}\n"
        f"Live Under Score: <b>{g['live_score']}/100</b>\n"
        f"Ly do: {g['live_reasons']}\n\n"
        f"â ï¸ Mo sportsbook check live total/odds truoc khi vao."
    )


def pregame_alert_message(g: Dict[str, Any]) -> str:
    return (
        f"â¾ <b>PREGAME UNDER WATCH</b>\n"
        f"<b>{g['away']}</b> vs <b>{g['home']}</b>\n"
        f"Pitchers: {g['away_pitcher']} vs {g['home_pitcher']}\n"
        f"Pregame Under Score: <b>{g['pregame_score']}/100</b>\n"
        f"Ly do: {g['pregame_reasons']}\n\n"
        f"â ï¸ Pregame la watchlist. Check total line, weather, lineup truoc khi bet."
    )


def bot_loop():
    global bot_running
    print("MLB Under Pro v2 loop started")
    send_telegram("â MLB Under Pro v2 da chay. Bot se canh Pregame + Live Under.")

    while bot_running:
        try:
            games = refresh_games()
            for g in games:
                live_key = f"live-{g['game_pk']}-{g['inning']}-{g['half']}-{g['total_runs']}-{g['outs']}-{g['runners']}"
                pre_key = f"pre-{g['game_pk']}"

                if g["live_score"] >= LIVE_ALERT_SCORE and live_key not in alerted:
                    send_telegram(live_alert_message(g))
                    alerted.add(live_key)

                if g["pregame_score"] >= PREGAME_ALERT_SCORE and pre_key not in alerted:
                    send_telegram(pregame_alert_message(g))
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


HTML = """<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MLB Under Pro v2</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,Arial;margin:0;background:#0f172a;color:white}
.header{padding:18px;background:#111827;position:sticky;top:0;z-index:10}
h1{margin:0;font-size:25px}
.small{color:#cbd5e1;font-size:14px}
.card{margin:12px;padding:15px;border-radius:16px;background:#1e293b}
.teams{font-size:19px;font-weight:800}
.score{font-size:24px;font-weight:900;margin-top:8px}
.good{color:#22c55e}.mid{color:#facc15}.bad{color:#fb7185}
.btn{display:inline-block;margin-top:10px;background:#38bdf8;color:#001;padding:10px 14px;border-radius:12px;text-decoration:none;font-weight:800;border:0}
.stop{background:#fb7185;color:#111827}
.start{background:#22c55e;color:#111827}
.badge{display:inline-block;padding:3px 8px;border-radius:10px;background:#334155;margin-top:4px}
</style>
</head>
<body>
<div class="header">
<h1>â¾ MLB Under Pro v2</h1>
<div class="small">Bot: {{ "RUNNING ð¢" if running else "STOPPED ð´" }} | Last update: {{last_update}}</div>
<div class="small">Live alert: {{live_alert}} | Pregame alert: {{pregame_alert}}</div>
<form method="post" action="/start" style="display:inline"><button class="btn start">â¶ Start</button></form>
<form method="post" action="/stop" style="display:inline"><button class="btn stop">â  Stop</button></form>
<a class="btn" href="/refresh">â» Refresh</a>
<a class="btn" href="/test">Test Telegram</a>
</div>
{% for g in games %}
<div class="card">
 <div class="teams">{{g.away}} {{g.away_runs}} - {{g.home_runs}} {{g.home}}</div>
 <div class="small">{{g.status}} | {{g.half}} {{g.inning}} | Outs: {{g.outs}} | Runners: {{g.runners}}</div>
 <div class="small">Pitchers: {{g.away_pitcher}} vs {{g.home_pitcher}}</div>
 <div class="score {{'good' if g.live_score>=85 else ('mid' if g.live_score>=70 else 'bad')}}">Live Under: {{g.live_score}}/100</div>
 <div class="small">Ly do live: {{g.live_reasons}}</div>
 <div class="score {{'good' if g.pregame_score>=72 else ('mid' if g.pregame_score>=60 else 'bad')}}">Pregame Under: {{g.pregame_score}}/100</div>
 <div class="small">Ly do pregame: {{g.pregame_reasons}}</div>
</div>
{% endfor %}
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(
        HTML,
        games=latest_games,
        running=bot_running,
        last_update=last_update,
        live_alert=LIVE_ALERT_SCORE,
        pregame_alert=PREGAME_ALERT_SCORE,
    )


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
        return f"Refresh error: {e}", 500
    return redirect(url_for("index"))


@app.route("/test")
def test():
    ok = send_telegram("â Test thanh cong: MLB Under Pro v2 da ket noi Telegram.")
    if ok:
        return "Telegram sent â"
    return "Telegram failed â. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.", 500


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
    
