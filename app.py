import os
import time
import threading
import datetime as dt
from typing import Dict, Any, List, Set

import requests
from flask import Flask, jsonify, render_template_string, request, redirect, url_for

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
CHECK_EVERY_SECONDS = int(os.getenv("CHECK_EVERY_SECONDS", "300"))
ALERT_SCORE = int(os.getenv("ALERT_SCORE", "80"))
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


def under_score(total_runs: int, inning: int, outs: int, runners: List[str], status: str):
    score = 50
    reasons = []

    live = "in progress" in (status or "").lower()
    scheduled = "scheduled" in (status or "").lower() or "pre-game" in (status or "").lower()

    if live:
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
            reasons.append("cÃ²n sá»m")
    elif scheduled:
        score = 49
        reasons.append("chÆ°a live")
    else:
        return 0, ["not live"]

    if total_runs <= 3:
        score += 20
        reasons.append("Äiá»m ráº¥t tháº¥p")
    elif total_runs == 4:
        score += 16
        reasons.append("Äiá»m tháº¥p")
    elif total_runs == 5:
        score += 10
        reasons.append("Äiá»m á»n")
    elif total_runs == 6:
        score += 2
        reasons.append("Äiá»m trung bÃ¬nh")
    else:
        score -= 14
        reasons.append("Äiá»m cao")

    if outs == 2:
        score += 6
        reasons.append("2 outs")
    elif outs == 0:
        score -= 5
        reasons.append("0 out")

    p = base_penalty(runners, outs)
    if p:
        score -= p
        reasons.append(f"base nguy hiá»m -{p}")
    else:
        reasons.append("base sáº¡ch")

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

    score, reasons = under_score(total, inning, outs, runners, status)

    return {
        "game_pk": g.get("gamePk"),
        "away": away.get("team", {}).get("name", "Away"),
        "home": home.get("team", {}).get("name", "Home"),
        "away_runs": away_runs,
        "home_runs": home_runs,
        "total_runs": total,
        "inning": inning,
        "half": half,
        "outs": outs,
        "runners": ", ".join(runners) if runners else "none",
        "status": status,
        "score": score,
        "reasons": ", ".join(reasons),
    }


def refresh_games() -> List[Dict[str, Any]]:
    global latest_games, last_update

    games = [parse_game(g) for g in fetch_mlb_games()]
    games.sort(key=lambda x: x["score"], reverse=True)
    latest_games = games
    last_update = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("Refreshed games:", len(games), "at", last_update)
    return games


def alert_message(g: Dict[str, Any]) -> str:
    return (
        f"ð¥ <b>MLB Under Alert</b>\n"
        f"<b>{g['away']}</b> {g['away_runs']} - {g['home_runs']} <b>{g['home']}</b>\n"
        f"{g['half']} {g['inning']} | Outs: {g['outs']} | Runners: {g['runners']}\n"
        f"Status: {g['status']}\n"
        f"Total runs: {g['total_runs']}\n"
        f"Under Score: <b>{g['score']}/100</b>\n"
        f"Reason: {g['reasons']}\n\n"
        f"â ï¸ Má» sportsbook check live total/odds trÆ°á»c khi vÃ o."
    )


def bot_loop():
    global bot_running

    print("MLB Under bot loop started")
    send_telegram("â MLB Under Bot ÄÃ£ cháº¡y. Bot sáº½ tá»± canh kÃ¨o Under live.")

    while bot_running:
        try:
            games = refresh_games()
            for g in games:
                is_live = "in progress" in (g["status"] or "").lower()
                key = f"{g['game_pk']}-{g['inning']}-{g['half']}-{g['total_runs']}-{g['outs']}-{g['runners']}"

                if is_live and g["score"] >= ALERT_SCORE and key not in alerted:
                    send_telegram(alert_message(g))
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


HTML = """<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MLB Under Bot</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,Arial;margin:0;background:#0f172a;color:white}
.header{padding:18px;background:#111827;position:sticky;top:0;z-index:10}
h1{margin:0;font-size:25px}
.small{color:#cbd5e1;font-size:14px}
.card{margin:12px;padding:15px;border-radius:16px;background:#1e293b}
.teams{font-size:19px;font-weight:800}
.score{font-size:26px;font-weight:900;margin-top:8px}
.good{color:#22c55e}.mid{color:#facc15}.bad{color:#fb7185}
.btn{display:inline-block;margin-top:10px;background:#38bdf8;color:#001;padding:10px 14px;border-radius:12px;text-decoration:none;font-weight:800;border:0}
.stop{background:#fb7185;color:#111827}
.start{background:#22c55e;color:#111827}
</style>
</head>
<body>
<div class="header">
<h1>â¾ MLB Under Bot</h1>
<div class="small">Bot: {{ "RUNNING ð¢" if running else "STOPPED ð´" }} | Last update: {{last_update}} | Alert score: {{alert_score}}</div>
<form method="post" action="/start" style="display:inline"><button class="btn start">â¶ Start</button></form>
<form method="post" action="/stop" style="display:inline"><button class="btn stop">â  Stop</button></form>
<a class="btn" href="/refresh">â» Refresh</a>
<a class="btn" href="/test">Test Telegram</a>
</div>
{% for g in games %}
<div class="card">
 <div class="teams">{{g.away}} {{g.away_runs}} - {{g.home_runs}} {{g.home}}</div>
 <div class="small">{{g.status}} | {{g.half}} {{g.inning}} | Outs: {{g.outs}} | Runners: {{g.runners}}</div>
 <div class="small">Total runs: {{g.total_runs}}</div>
 <div class="score {{'good' if g.score>=80 else ('mid' if g.score>=65 else 'bad')}}">Under Score: {{g.score}}/100</div>
 <div class="small">LÃ½ do: {{g.reasons}}</div>
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
        alert_score=ALERT_SCORE,
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
    ok = send_telegram("â Test thÃ nh cÃ´ng: MLB Under Bot ÄÃ£ káº¿t ná»i Telegram.")
    if ok:
        return "Telegram sent â"
    return "Telegram failed â. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Render Environment.", 500


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
