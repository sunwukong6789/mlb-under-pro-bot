# -*- coding: utf-8 -*-
import os, time, threading, datetime as dt
from typing import Dict, Any, List, Set, Tuple
import requests
from flask import Flask, jsonify, render_template_string, redirect, url_for, Response

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CHECK_EVERY_SECONDS = int(os.getenv("CHECK_EVERY_SECONDS", "300"))
LIVE_ALERT_SCORE = int(os.getenv("LIVE_ALERT_SCORE", "86"))
PREGAME_ALERT_SCORE = int(os.getenv("PREGAME_ALERT_SCORE", "80"))
PREGAME_WINDOW_HOURS = int(os.getenv("PREGAME_WINDOW_HOURS", "24"))
AUTO_START = os.getenv("AUTO_START", "1") == "1"

MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

latest_games: List[Dict[str, Any]] = []
last_update = "Chưa cập nhật"
alerted: Set[str] = set()
bot_running = False
bot_thread = None

PITCHER_EDGE = {
    "Paul Skenes": 12, "Tarik Skubal": 11, "Zack Wheeler": 11, "Logan Gilbert": 9,
    "George Kirby": 9, "Chris Sale": 9, "Spencer Strider": 9, "Corbin Burnes": 10,
    "Kevin Gausman": 7, "Nathan Eovaldi": 6, "Framber Valdez": 8, "Cristopher Sanchez": 8,
    "Garrett Crochet": 9, "Cole Ragans": 8, "Max Fried": 8, "Sonny Gray": 7,
}

PARK_UNDER_EDGE = {
    "Seattle Mariners": 5, "San Francisco Giants": 5, "Detroit Tigers": 4,
    "Cleveland Guardians": 3, "New York Mets": 2, "Oakland Athletics": 3,
    "Pittsburgh Pirates": 2, "Miami Marlins": 2,
}

PARK_OVER_PENALTY = {
    "Colorado Rockies": -10, "Cincinnati Reds": -4, "Boston Red Sox": -3,
    "Philadelphia Phillies": -2, "New York Yankees": -2,
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
    if "in progress" in s: return "Đang live"
    if "scheduled" in s: return "Chưa bắt đầu"
    if "pre-game" in s or "warmup" in s: return "Sắp bắt đầu"
    if "final" in s: return "Đã kết thúc"
    return status or "Không rõ"

def inning_text(half: str, inning: int) -> str:
    if not inning: return "Pregame"
    if (half or "").lower().startswith("top"): return f"Top {inning}"
    if (half or "").lower().startswith("bottom"): return f"Bot {inning}"
    return f"Inning {inning}"

def runner_vi(runners: List[str]) -> str:
    if not runners: return "Bases trống"
    mapping = {"first": "1B", "second": "2B", "third": "3B"}
    return ", ".join(mapping.get(r, r) for r in runners)

def base_penalty(runners: List[str], outs: int) -> int:
    s = set(runners)
    if not s: return 0
    if len(s) == 3: return 32 if outs < 2 else 18
    if "second" in s and "third" in s: return 26 if outs < 2 else 14
    if "third" in s: return 22 if outs < 2 else 11
    if "second" in s: return 13 if outs < 2 else 6
    if "first" in s: return 8 if outs < 2 else 3
    return 0

def pitcher_bonus(name: str) -> int:
    return PITCHER_EDGE.get(name or "", 0)

def park_adjust(home_team: str) -> int:
    return PARK_UNDER_EDGE.get(home_team or "", 0) + PARK_OVER_PENALTY.get(home_team or "", 0)

def rec(score: int) -> Tuple[str, str, str]:
    if score >= 92: return "⭐⭐⭐⭐⭐", "🔥 BET UNDER NGAY", "elite"
    if score >= 86: return "⭐⭐⭐⭐", "✅ RẤT ĐẸP", "strong"
    if score >= 78: return "⭐⭐⭐", "👀 WATCHLIST", "watch"
    if score >= 68: return "⭐⭐", "⏳ CHỜ THÊM", "wait"
    return "⭐", "❌ BỎ QUA", "avoid"

def live_under_score(total_runs: int, inning: int, outs: int, runners: List[str], status: str) -> Tuple[int, List[str]]:
    if "in progress" not in (status or "").lower():
        return 0, ["Trận chưa live"]
    score = 50
    reasons = []
    if inning >= 8:
        score += 28; reasons.append("Cuối trận, thời gian ghi điểm còn ít")
    elif inning == 7:
        score += 23; reasons.append("Inning 7, vùng live Under đẹp")
    elif inning == 6:
        score += 17; reasons.append("Inning 6, bắt đầu vào vùng live Under")
    elif inning == 5:
        score += 8; reasons.append("Inning 5, theo dõi")
    else:
        score -= 20; reasons.append("Còn sớm, rủi ro cao")

    if total_runs <= 3:
        score += 24; reasons.append("Tổng điểm rất thấp")
    elif total_runs == 4:
        score += 18; reasons.append("Tổng điểm thấp")
    elif total_runs == 5:
        score += 11; reasons.append("Tổng điểm tạm ổn")
    elif total_runs == 6:
        score += 3; reasons.append("Tổng điểm trung bình")
    else:
        score -= 17; reasons.append("Tổng điểm đã cao")

    if outs == 2:
        score += 8; reasons.append("2 outs, giảm rủi ro ghi điểm")
    elif outs == 0:
        score -= 8; reasons.append("0 out, rủi ro cao")

    p = base_penalty(runners, outs)
    if p:
        score -= p; reasons.append(f"Có runner trên base, trừ {p} điểm")
    else:
        reasons.append("Bases trống")
    return max(0, min(100, score)), reasons

def pregame_under_score(g: Dict[str, Any]) -> Tuple[int, List[str]]:
    status = g.get("status", {}).get("detailedState", "")
    s = (status or "").lower()
    if "scheduled" not in s and "pre-game" not in s and "warmup" not in s:
        return 0, ["Không phải pregame"]

    game_dt = parse_game_time(g.get("gameDate", ""))
    if not game_dt:
        return 55, ["Chưa đọc được giờ thi đấu"]

    hours_to_start = (game_dt - now_utc()).total_seconds() / 3600
    if hours_to_start < -0.25: return 0, ["Trận đã bắt đầu hoặc đã qua giờ"]
    if hours_to_start > PREGAME_WINDOW_HOURS: return 45, [f"Còn xa giờ thi đấu: {hours_to_start:.1f} giờ"]

    teams = g.get("teams", {})
    away_pitcher = teams.get("away", {}).get("probablePitcher", {}).get("fullName", "")
    home_pitcher = teams.get("home", {}).get("probablePitcher", {}).get("fullName", "")
    home_team = teams.get("home", {}).get("team", {}).get("name", "")

    score = 56
    reasons = [f"Còn {hours_to_start:.1f} giờ trước khi bóng chạy"]

    if away_pitcher and home_pitcher:
        score += 12; reasons.append("Đã có probable pitchers")
    else:
        score -= 10; reasons.append("Thiếu thông tin pitcher")

    pb = pitcher_bonus(away_pitcher) + pitcher_bonus(home_pitcher)
    if pb:
        score += pb; reasons.append(f"Pitcher edge +{pb}")
    else:
        reasons.append("Pitcher chưa đủ dữ liệu nâng điểm")

    park = park_adjust(home_team)
    if park > 0:
        score += park; reasons.append(f"Sân có xu hướng Under +{park}")
    elif park < 0:
        score += park; reasons.append(f"Sân dễ Over {park}")

    if 0 <= hours_to_start <= 4:
        score += 6; reasons.append("Gần giờ thi đấu, nên kiểm tra line")
    else:
        score += 2; reasons.append("Watchlist trước giờ")

    reasons.append("Cần kiểm tra thêm total line, lineup, weather")
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
    live_stars, live_rec, live_class = rec(live_score)
    pre_stars, pre_rec, pre_class = rec(pre_score)

    return {
        "game_pk": g.get("gamePk"),
        "away": away.get("team", {}).get("name", "Away"),
        "home": home.get("team", {}).get("name", "Home"),
        "away_pitcher": away.get("probablePitcher", {}).get("fullName", "TBD"),
        "home_pitcher": home.get("probablePitcher", {}).get("fullName", "TBD"),
        "away_runs": away_runs, "home_runs": home_runs, "total_runs": total,
        "inning": inning_text(half, inning), "outs": outs, "runners": runner_vi(runners),
        "status_vi": status_vi(status),
        "live_score": live_score, "live_reasons": live_reasons, "live_rec": live_rec, "live_class": live_class, "live_stars": live_stars,
        "pregame_score": pre_score, "pregame_reasons": pre_reasons, "pregame_rec": pre_rec, "pregame_class": pre_class, "pregame_stars": pre_stars,
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
        f"🚨 <b>LIVE UNDER ALERT</b>\n"
        f"<b>{g['away']}</b> {g['away_runs']} - {g['home_runs']} <b>{g['home']}</b>\n"
        f"Inning: {g['inning']} | Outs: {g['outs']} | Runners: {g['runners']}\n"
        f"Tổng điểm hiện tại: {g['total_runs']}\n"
        f"Live Under Score: <b>{g['live_score']}/100</b> {g['live_stars']}\n"
        f"Khuyến nghị: <b>{g['live_rec']}</b>\n"
        f"Lý do: {'; '.join(g['live_reasons'])}\n\n"
        f"⚠️ Kiểm tra live total/odds trước khi vào."
    )

def pregame_alert_message(g: Dict[str, Any]) -> str:
    return (
        f"⚾ <b>PREGAME UNDER WATCH</b>\n"
        f"<b>{g['away']}</b> vs <b>{g['home']}</b>\n"
        f"Pitchers: {g['away_pitcher']} vs {g['home_pitcher']}\n"
        f"Pregame Under Score: <b>{g['pregame_score']}/100</b> {g['pregame_stars']}\n"
        f"Khuyến nghị: <b>{g['pregame_rec']}</b>\n"
        f"Lý do: {'; '.join(g['pregame_reasons'])}\n\n"
        f"⚠️ Đây là watchlist. Kiểm tra total line, lineup, weather trước khi bet."
    )

def bot_loop():
    global bot_running
    print("MLB Under Pro v5 loop started")
    send_telegram("✅ MLB Under Pro v5 đã chạy. Bot sẽ lọc Top Under, Pregame và Live.")
    while bot_running:
        try:
            games = refresh_games()
            for g in games:
                live_key = f"live-{g['game_pk']}-{g['inning']}-{g['total_runs']}-{g['outs']}-{g['runners']}"
                pre_key = f"pre-{g['game_pk']}"
                if g["live_score"] >= LIVE_ALERT_SCORE and live_key not in alerted:
                    send_telegram(live_alert_message(g)); alerted.add(live_key)
                if g["pregame_score"] >= PREGAME_ALERT_SCORE and pre_key not in alerted:
                    send_telegram(pregame_alert_message(g)); alerted.add(pre_key)
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
<title>MLB Under Pro v5</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,Arial;margin:0;background:#06111f;color:white}
.header{padding:18px;background:#08101e;position:sticky;top:0;z-index:10;border-bottom:1px solid #1f3b57}
h1{margin:0;font-size:25px}.small{color:#d4e4f5;font-size:14px}
.card,.topbox,.footer{margin:12px;padding:15px;border-radius:16px;background:#0d2138;border:1px solid #21496f}
.teams{font-size:19px;font-weight:800}.grid{display:grid;grid-template-columns:1fr;gap:10px}
.score{font-size:24px;font-weight:900;margin-top:8px}
.elite{color:#22c55e}.strong{color:#4ade80}.watch{color:#facc15}.wait{color:#fb923c}.avoid{color:#fb7185}
.btn{display:inline-block;margin-top:10px;margin-right:6px;background:#38bdf8;color:#001;padding:10px 14px;border-radius:12px;text-decoration:none;font-weight:800;border:0}
.stop{background:#fb7185;color:#111827}.start{background:#22c55e;color:#111827}
.reason{margin-top:5px;color:#e5f2ff}.box{padding:10px;border-radius:12px;background:#091a2d;border:1px solid #1f3b57}
@media(min-width:800px){.grid{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<div class="header">
<h1>⚾ MLB Under Pro v5</h1>
<div class="small">Bot: {{ "ĐANG CHẠY 🟢" if running else "ĐANG DỪNG 🔴" }} | Cập nhật: {{last_update}}</div>
<div class="small">Live Alert Score: {{live_alert}}+ | Pregame Alert Score: {{pregame_alert}}+</div>
<form method="post" action="/start" style="display:inline"><button class="btn start">▶ Start Bot</button></form>
<form method="post" action="/stop" style="display:inline"><button class="btn stop">■ Stop Bot</button></form>
<a class="btn" href="/refresh">↻ Refresh</a>
<a class="btn" href="/test">Test Telegram</a>
</div>

<div class="topbox">
<b>🏆 Top kèo Under đẹp nhất</b><br>
{% for g in games[:5] %}
{{loop.index}}. {{g.away}} vs {{g.home}} — Best Score: <b>{{g.best_score}}/100</b><br>
{% endfor %}
</div>

{% for g in games %}
<div class="card">
 <div class="teams">{{g.away}} {{g.away_runs}} - {{g.home_runs}} {{g.home}}</div>
 <div class="small">{{g.status_vi}} | {{g.inning}} | Outs: {{g.outs}} | {{g.runners}}</div>
 <div class="small">Pitchers: {{g.away_pitcher}} vs {{g.home_pitcher}}</div>
 <div class="small">Tổng điểm hiện tại: {{g.total_runs}}</div>
 <div class="grid">
   <div class="box">
     <div class="score {{g.live_class}}">Live Under: {{g.live_score}}/100 {{g.live_stars}}</div>
     <div class="{{g.live_class}}"><b>{{g.live_rec}}</b></div>
     <div class="reason">Lý do live: {{ "; ".join(g.live_reasons) }}</div>
   </div>
   <div class="box">
     <div class="score {{g.pregame_class}}">Pregame Under: {{g.pregame_score}}/100 {{g.pregame_stars}}</div>
     <div class="{{g.pregame_class}}"><b>{{g.pregame_rec}}</b></div>
     <div class="reason">Lý do pregame: {{ "; ".join(g.pregame_reasons) }}</div>
   </div>
 </div>
</div>
{% endfor %}

<div class="footer">
<b>Bảng sao:</b><br>
⭐⭐⭐⭐⭐ 92–100: 🔥 BET UNDER NGAY<br>
⭐⭐⭐⭐ 86–91: ✅ RẤT ĐẸP<br>
⭐⭐⭐ 78–85: 👀 WATCHLIST<br>
⭐⭐ 68–77: ⏳ CHỜ THÊM<br>
⭐ 0–67: ❌ BỎ QUA<br><br>
Lưu ý: bản này chưa có odds API trả phí, nên vẫn cần kiểm tra O/U line, lineup và weather trước khi bet.
</div>
</body>
</html>"""

@app.route("/")
def index():
    html = render_template_string(
        HTML, games=latest_games, running=bot_running, last_update=last_update,
        live_alert=LIVE_ALERT_SCORE, pregame_alert=PREGAME_ALERT_SCORE
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
    ok = send_telegram("✅ Test thành công: MLB Under Pro v5 đã kết nối Telegram.")
    return Response("Telegram sent ✅" if ok else "Telegram failed ❌. Kiểm tra TELEGRAM_BOT_TOKEN và TELEGRAM_CHAT_ID.", status=200 if ok else 500, content_type="text/plain; charset=utf-8")

@app.route("/api/games")
def api_games():
    return jsonify({"running": bot_running, "last_update": last_update, "games": latest_games})

if __name__ == "__main__":
    try: refresh_games()
    except Exception as e: print("Initial refresh error:", repr(e))
    if AUTO_START: start_background_bot()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
