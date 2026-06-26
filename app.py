import os, time, threading, datetime as dt
from typing import Dict, Any, List, Optional, Set, Tuple
import requests
from flask import Flask, jsonify, render_template_string, request

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
CHECK_EVERY_SECONDS = int(os.getenv("CHECK_EVERY_SECONDS", "60"))
ALERT_SCORE = int(os.getenv("ALERT_SCORE", "80"))

MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"

app = Flask(__name__)
bot_running = False
bot_thread = None
alerted: Set[str] = set()
latest_games: List[Dict[str, Any]] = []
last_update = "Never"

def today():
    return dt.datetime.now().strftime("%Y-%m-%d")

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Missing Telegram token/chat id")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }, timeout=15)

def fetch_games():
    r = requests.get(MLB_SCHEDULE_URL, params={
        "sportId": 1,
        "date": today(),
        "hydrate": "linescore,team"
    }, timeout=20)
    r.raise_for_status()
    data = r.json()
    games = []
    for d in data.get("dates", []):
        games += d.get("games", [])
    return games

def risk_penalty(runners, outs):
    s = set(runners)
    if not s: return 0
    if len(s) == 3: return 25 if outs < 2 else 15
    if "third" in s and "second" in s: return 22 if outs < 2 else 12
    if "third" in s: return 18 if outs < 2 else 9
    if "second" in s: return 12 if outs < 2 else 5
    if "first" in s: return 6 if outs < 2 else 2
    return 0

def score_under(total_runs, inning, outs, runners):
    score = 50
    reasons = []
    if inning >= 8:
        score += 22; reasons.append("late inning")
    elif inning == 7:
        score += 18; reasons.append("inning 7")
    elif inning == 6:
        score += 12; reasons.append("inning 6")
    else:
        score -= 15; reasons.append("còn sớm")

    if total_runs <= 3:
        score += 18; reasons.append("điểm rất thấp")
    elif total_runs == 4:
        score += 14; reasons.append("điểm thấp")
    elif total_runs == 5:
        score += 9; reasons.append("điểm ổn")
    elif total_runs == 6:
        score += 2; reasons.append("điểm trung bình")
    else:
        score -= 12; reasons.append("điểm cao")

    if outs == 2:
        score += 5; reasons.append("2 outs")
    elif outs == 0:
        score -= 4; reasons.append("0 out")

    p = risk_penalty(runners, outs)
    if p:
        score -= p; reasons.append(f"base nguy hiểm -{p}")
    else:
        reasons.append("base sạch")

    return max(0, min(100, score)), reasons

def parse_game(g):
    status = g.get("status", {}).get("detailedState", "")
    teams = g.get("teams", {})
    away = teams.get("away", {})
    home = teams.get("home", {})
    ls = g.get("linescore", {}) or {}
    offense = ls.get("offense", {}) or {}
    runners = [b for b in ["first", "second", "third"] if offense.get(b)]
    away_runs = away.get("score", 0) or 0
    home_runs = home.get("score", 0) or 0
    inning = ls.get("currentInning", 0) or 0
    outs = ls.get("outs", 0) or 0
    total = away_runs + home_runs
    score, reasons = score_under(total, inning, outs, runners)
    return {
        "game_pk": g.get("gamePk"),
        "away": away.get("team", {}).get("name", "Away"),
        "home": home.get("team", {}).get("name", "Home"),
        "away_runs": away_runs,
        "home_runs": home_runs,
        "total_runs": total,
        "inning": inning,
        "half": ls.get("inningHalf", ""),
        "outs": outs,
        "runners": ",".join(runners) if runners else "none",
        "status": status,
        "score": score,
        "reasons": ", ".join(reasons)
    }

def refresh_once():
    global latest_games, last_update
    games = [parse_game(g) for g in fetch_games()]
    games.sort(key=lambda x: x["score"], reverse=True)
    latest_games = games
    last_update = dt.datetime.now().strftime("%H:%M:%S")
    return games

def bot_loop():
    global bot_running
    while bot_running:
        try:
            games = refresh_once()
            for g in games:
                live = "in progress" in g["status"].lower()
                key = f'{g["game_pk"]}-{g["inning"]}-{g["half"]}-{g["total_runs"]}-{g["outs"]}-{g["runners"]}'
                if live and g["score"] >= ALERT_SCORE and key not in alerted:
                    msg = (
                        f"🔥 <b>MLB Under Alert</b>\n"
                        f"{g['away']} {g['away_runs']} - {g['home_runs']} {g['home']}\n"
                        f"{g['half']} {g['inning']} | Outs: {g['outs']} | Runners: {g['runners']}\n"
                        f"Under Score: <b>{g['score']}/100</b>\n"
                        f"Reason: {g['reasons']}\n\n"
                        f"⚠️ Check live total/odds trước khi vào."
                    )
                    send_telegram(msg)
                    alerted.add(key)
        except Exception as e:
            print("BOT ERROR:", e)
        time.sleep(CHECK_EVERY_SECONDS)

HTML = """
<!doctype html><html><head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MLB Under Bot</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,Arial;margin:0;background:#101322;color:white}
.header{padding:20px;background:#171b34;position:sticky;top:0}
h1{margin:0;font-size:26px}.status{margin-top:8px;color:#9ee493}
.btn{border:0;border-radius:14px;padding:13px 18px;margin:8px 6px 0 0;font-size:16px;font-weight:700}
.start{background:#00d084}.stop{background:#ff4d4d;color:white}.refresh{background:#38bdf8}
.card{margin:14px;padding:16px;border-radius:18px;background:#1e2444;box-shadow:0 2px 10px #0005}
.score{font-size:28px;font-weight:900}.good{color:#00d084}.mid{color:#ffd166}.bad{color:#ff6b6b}
.small{color:#cbd5e1;font-size:14px}.teams{font-size:20px;font-weight:800}.meta{margin-top:8px}
</style></head>
<body>
<div class="header">
<h1>⚾ MLB Under Bot</h1>
<div class="status">Bot: {{ "RUNNING 🟢" if running else "STOPPED 🔴" }} | Last update: {{ last_update }}</div>
<form method="post" action="/start" style="display:inline"><button class="btn start">▶ Start</button></form>
<form method="post" action="/stop" style="display:inline"><button class="btn stop">■ Stop</button></form>
<form method="post" action="/refresh" style="display:inline"><button class="btn refresh">↻ Refresh</button></form>
</div>
{% for g in games %}
<div class="card">
  <div class="teams">{{ g.away }} {{ g.away_runs }} - {{ g.home_runs }} {{ g.home }}</div>
  <div class="meta">{{ g.half }} {{ g.inning }} | Outs: {{ g.outs }} | Runners: {{ g.runners }}</div>
  <div class="meta small">Status: {{ g.status }} | Total runs: {{ g.total_runs }}</div>
  <div class="score {{ 'good' if g.score>=80 else ('mid' if g.score>=65 else 'bad') }}">Under Score: {{ g.score }}/100</div>
  <div class="small">Lý do: {{ g.reasons }}</div>
</div>
{% endfor %}
</body></html>
"""

@app.route("/")
def index():
    return render_template_string(HTML, games=latest_games, running=bot_running, last_update=last_update)

@app.route("/start", methods=["POST"])
def start():
    global bot_running, bot_thread
    if not bot_running:
        bot_running = True
        bot_thread = threading.Thread(target=bot_loop, daemon=True)
        bot_thread.start()
    return index()

@app.route("/stop", methods=["POST"])
def stop():
    global bot_running
    bot_running = False
    return index()

@app.route("/refresh", methods=["POST"])
def refresh():
    try: refresh_once()
    except Exception as e: print(e)
    return index()

@app.route("/api/games")
def api_games():
    return jsonify({"running": bot_running, "last_update": last_update, "games": latest_games})

if __name__ == "__main__":
    refresh_once()
    if os.getenv("AUTO_START", "1") == "1":
        bot_running = True
        bot_thread = threading.Thread(target=bot_loop, daemon=True)
        bot_thread.start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
