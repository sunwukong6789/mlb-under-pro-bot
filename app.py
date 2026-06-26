# -*- coding: utf-8 -*-
import os, time, threading, datetime as dt, math
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

MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

latest_games: List[Dict[str, Any]] = []
last_update = "Chưa cập nhật"
alerted: Set[str] = set()
bot_running = False
bot_thread = None

# Manual lightweight model until Odds/Weather/Umpire APIs are connected.
PITCHER_EDGE = {
    "Paul Skenes": 12, "Tarik Skubal": 12, "Zack Wheeler": 11, "Logan Gilbert": 10,
    "George Kirby": 10, "Chris Sale": 9, "Spencer Strider": 9, "Corbin Burnes": 10,
    "Kevin Gausman": 7, "Nathan Eovaldi": 7, "Framber Valdez": 8, "Cristopher Sanchez": 8,
    "Garrett Crochet": 9, "Cole Ragans": 8, "Max Fried": 8, "Sonny Gray": 7,
    "Spencer Arrighetti": 4, "Keider Montero": 3, "Andrew Abbott": 7, "Trevor Rogers": 4,
    "Bryce Miller": 6, "Shane Bieber": 7, "Pablo Lopez": 6, "Joe Ryan": 7,
}

PARK_UNDER_EDGE = {
    "Seattle Mariners": 5, "San Francisco Giants": 5, "Detroit Tigers": 4,
    "Cleveland Guardians": 3, "New York Mets": 2, "Oakland Athletics": 3,
    "Pittsburgh Pirates": 2, "Miami Marlins": 2, "Toronto Blue Jays": 1,
    "Baltimore Orioles": 1,
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

def rec(score: int) -> Tuple[str, str, str]:
    if score >= 94: return "⭐⭐⭐⭐⭐", "🔥 BEST BET", "elite"
    if score >= 88: return "⭐⭐⭐⭐", "✅ RẤT ĐẸP", "strong"
    if score >= 80: return "⭐⭐⭐", "👀 WATCHLIST", "watch"
    if score >= 70: return "⭐⭐", "⏳ CHỜ THÊM", "wait"
    return "⭐", "❌ BỎ QUA", "avoid"

def confidence(score: int) -> int:
    if score <= 0: return 0
    return max(35, min(98, int(score * 0.9 + 10)))

def estimated_ev(score: int) -> float:
    # Placeholder EV estimate until real odds are attached.
    if score < 70:
        return -4.0
    return round((score - 78) * 0.65, 1)

def ev_class(ev: float) -> str:
    if ev >= 8: return "elite"
    if ev >= 4: return "strong"
    if ev >= 0: return "watch"
    return "avoid"

def recommended_line(score: int, mode: str) -> str:
    if mode == "live":
        if score >= 94: return "Under live nếu line còn 7.5+"
        if score >= 88: return "Under live nếu line còn 8 / 7.5"
        if score >= 80: return "Chỉ theo dõi, chưa vội vào"
        return "Không vào live lúc này"
    else:
        if score >= 94: return "Pregame Under 8.5+ rất đáng kiểm tra"
        if score >= 88: return "Pregame Under 8 / 8.5 nếu odds ổn"
        if score >= 80: return "Watchlist, chờ line tốt hơn"
        return "Không ưu tiên pregame"

def data_quality(away_pitcher: str, home_pitcher: str, score: int) -> str:
    q = 50
    if away_pitcher != "TBD" and home_pitcher != "TBD":
        q += 25
    if score >= 80:
        q += 10
    return f"{min(q, 95)}%"

def live_under_score(total_runs: int, inning: int, outs: int, runners: List[str], status: str) -> Tuple[int, List[str]]:
    if "in progress" not in (status or "").lower():
        return 0, ["Trận chưa live"]
    score = 50
    reasons = []
    if inning >= 8:
        score += 30; reasons.append("Cuối trận, thời gian ghi điểm còn ít")
    elif inning == 7:
        score += 25; reasons.append("Inning 7, vùng live Under đẹp")
    elif inning == 6:
        score += 18; reasons.append("Inning 6, bắt đầu vào vùng live Under")
    elif inning == 5:
        score += 9; reasons.append("Inning 5, theo dõi")
    else:
        score -= 20; reasons.append("Còn sớm, rủi ro cao")

    if total_runs <= 3:
        score += 25; reasons.append("Tổng điểm rất thấp")
    elif total_runs == 4:
        score += 19; reasons.append("Tổng điểm thấp")
    elif total_runs == 5:
        score += 12; reasons.append("Tổng điểm tạm ổn")
    elif total_runs == 6:
        score += 3; reasons.append("Tổng điểm trung bình")
    else:
        score -= 18; reasons.append("Tổng điểm đã cao")

    if outs == 2:
        score += 9; reasons.append("2 outs, giảm rủi ro ghi điểm")
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

    away_pitcher = away.get("probablePitcher", {}).get("fullName", "TBD")
    home_pitcher = home.get("probablePitcher", {}).get("fullName", "TBD")

    live_score, live_reasons = live_under_score(total, inning, outs, runners, status)
    pre_score, pre_reasons = pregame_under_score(g)
    live_stars, live_rec, live_class = rec(live_score)
    pre_stars, pre_rec, pre_class = rec(pre_score)
    best_score = max(live_score, pre_score)
    best_mode = "Live" if live_score >= pre_score else "Pregame"

    return {
        "game_pk": g.get("gamePk"),
        "away": away.get("team", {}).get("name", "Away"),
        "home": home.get("team", {}).get("name", "Home"),
        "away_pitcher": away_pitcher, "home_pitcher": home_pitcher,
        "away_runs": away_runs, "home_runs": home_runs, "total_runs": total,
        "inning": inning_text(half, inning), "outs": outs, "runners": runner_vi(runners),
        "status_vi": status_vi(status),
        "live_score": live_score, "live_reasons": live_reasons, "live_rec": live_rec, "live_class": live_class, "live_stars": live_stars,
        "live_conf": confidence(live_score), "live_ev": estimated_ev(live_score), "live_ev_class": ev_class(estimated_ev(live_score)),
        "live_line": recommended_line(live_score, "live"),
        "pregame_score": pre_score, "pregame_reasons": pre_reasons, "pregame_rec": pre_rec, "pregame_class": pre_class, "pregame_stars": pre_stars,
        "pregame_conf": confidence(pre_score), "pregame_ev": estimated_ev(pre_score), "pregame_ev_class": ev_class(estimated_ev(pre_score)),
        "pregame_line": recommended_line(pre_score, "pregame"),
        "best_score": best_score, "best_conf": confidence(best_score), "best_mode": best_mode,
        "quality": data_quality(away_pitcher, home_pitcher, best_score),
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

def live_alert_message(g: Dict[str, Any]) -> str:
    return (
        f"🚨 <b>LIVE UNDER ALERT</b>\n"
        f"<b>{g['away']}</b> {g['away_runs']} - {g['home_runs']} <b>{g['home']}</b>\n"
        f"Inning: {g['inning']} | Outs: {g['outs']} | Runners: {g['runners']}\n"
        f"Live Under Score: <b>{g['live_score']}/100</b> {g['live_stars']}\n"
        f"Confidence: <b>{g['live_conf']}%</b> | EV est: <b>{g['live_ev']}%</b>\n"
        f"Khuyến nghị: <b>{g['live_rec']}</b>\n"
        f"Line nên kiểm tra: {g['live_line']}\n"
        f"Lý do: {'; '.join(g['live_reasons'])}\n\n"
        f"⚠️ Kiểm tra live total/odds trước khi vào."
    )

def pregame_alert_message(g: Dict[str, Any]) -> str:
    return (
        f"⚾ <b>PREGAME UNDER WATCH</b>\n"
        f"<b>{g['away']}</b> vs <b>{g['home']}</b>\n"
        f"Pitchers: {g['away_pitcher']} vs {g['home_pitcher']}\n"
        f"Pregame Under Score: <b>{g['pregame_score']}/100</b> {g['pregame_stars']}\n"
        f"Confidence: <b>{g['pregame_conf']}%</b> | EV est: <b>{g['pregame_ev']}%</b>\n"
        f"Khuyến nghị: <b>{g['pregame_rec']}</b>\n"
        f"Line nên kiểm tra: {g['pregame_line']}\n"
        f"Lý do: {'; '.join(g['pregame_reasons'])}\n\n"
        f"⚠️ Đây là watchlist. Kiểm tra total line, lineup, weather trước khi bet."
    )

def bot_loop():
    global bot_running
    print("MLB Under Pro v7 loop started")
    send_telegram("✅ MLB Under Pro v7 đã chạy. Có Best Bet, EV estimate, Confidence và lọc tín hiệu.")
    while bot_running:
        try:
            games = refresh_games()
            for g in games:
                live_key = f"live-{g['game_pk']}-{g['inning']}-{g['total_runs']}-{g['outs']}-{g['runners']}"
                pre_key = f"pre-{g['game_pk']}"
                if g["live_score"] >= LIVE_ALERT_SCORE and g["live_ev"] >= 2 and live_key not in alerted:
                    send_telegram(live_alert_message(g)); alerted.add(live_key)
                if g["pregame_score"] >= PREGAME_ALERT_SCORE and g["pregame_ev"] >= 1 and pre_key not in alerted:
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
<title>MLB Under Pro v7</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,Arial;margin:0;background:#06111f;color:white}
.header{padding:18px;background:#08101e;position:sticky;top:0;z-index:10;border-bottom:1px solid #1f3b57}
h1{margin:0;font-size:25px}.small{color:#d4e4f5;font-size:14px}
.card,.topbox,.footer,.best{margin:12px;padding:15px;border-radius:16px;background:#0d2138;border:1px solid #21496f}
.best{background:#102814;border-color:#22c55e}
.teams{font-size:19px;font-weight:800}.grid{display:grid;grid-template-columns:1fr;gap:10px}
.score{font-size:24px;font-weight:900;margin-top:8px}
.elite{color:#22c55e}.strong{color:#4ade80}.watch{color:#facc15}.wait{color:#fb923c}.avoid{color:#fb7185}
.btn{display:inline-block;margin-top:10px;margin-right:6px;background:#38bdf8;color:#001;padding:10px 14px;border-radius:12px;text-decoration:none;font-weight:800;border:0}
.stop{background:#fb7185;color:#111827}.start{background:#22c55e;color:#111827}
.reason{margin-top:5px;color:#e5f2ff}.box{padding:10px;border-radius:12px;background:#091a2d;border:1px solid #1f3b57}
.badge{display:inline-block;margin-top:5px;margin-right:5px;padding:4px 8px;border-radius:999px;background:#102d4d}
@media(min-width:800px){.grid{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<div class="header">
<h1>⚾ MLB Under Pro v7</h1>
<div class="small">Bot: {{ "ĐANG CHẠY 🟢" if running else "ĐANG DỪNG 🔴" }} | Cập nhật: {{last_update}}</div>
<div class="small">Live Alert: {{live_alert}}+ | Pregame Alert: {{pregame_alert}}+ | Ẩn game dưới {{min_display}}</div>
<form method="post" action="/start" style="display:inline"><button class="btn start">▶ Start Bot</button></form>
<form method="post" action="/stop" style="display:inline"><button class="btn stop">■ Stop Bot</button></form>
<a class="btn" href="/refresh">↻ Refresh</a>
<a class="btn" href="/test">Test Telegram</a>
</div>

{% if best %}
<div class="best">
<b>🔥 BEST BET OF THE DAY</b><br>
<div class="teams">{{best.away}} vs {{best.home}}</div>
Best Mode: <b>{{best.best_mode}}</b> | Score: <b>{{best.best_score}}/100</b> | Confidence: <b>{{best.best_conf}}%</b><br>
Pitchers: {{best.away_pitcher}} vs {{best.home_pitcher}}<br>
Data quality: {{best.quality}}
</div>
{% endif %}

<div class="topbox">
<b>🏆 Top kèo Under đẹp nhất</b><br>
{% for g in games[:5] %}
{{loop.index}}. {{g.away}} vs {{g.home}} — {{g.best_mode}} — <b>{{g.best_score}}/100</b> — Confidence: <b>{{g.best_conf}}%</b><br>
{% endfor %}
</div>

{% for g in games %}
<div class="card">
 <div class="teams">{{g.away}} {{g.away_runs}} - {{g.home_runs}} {{g.home}}</div>
 <div class="small">{{g.status_vi}} | {{g.inning}} | Outs: {{g.outs}} | {{g.runners}}</div>
 <div class="small">Pitchers: {{g.away_pitcher}} vs {{g.home_pitcher}} | Data quality: {{g.quality}}</div>
 <div class="small">Tổng điểm hiện tại: {{g.total_runs}}</div>
 <div class="grid">
   <div class="box">
     <div class="score {{g.live_class}}">Live Under: {{g.live_score}}/100 {{g.live_stars}}</div>
     <div class="{{g.live_class}}"><b>{{g.live_rec}}</b></div>
     <div><span class="badge">Confidence: {{g.live_conf}}%</span><span class="badge {{g.live_ev_class}}">EV est: {{g.live_ev}}%</span></div>
     <div class="reason">Line nên kiểm tra: {{g.live_line}}</div>
     <div class="reason">Lý do live: {{ "; ".join(g.live_reasons) }}</div>
   </div>
   <div class="box">
     <div class="score {{g.pregame_class}}">Pregame Under: {{g.pregame_score}}/100 {{g.pregame_stars}}</div>
     <div class="{{g.pregame_class}}"><b>{{g.pregame_rec}}</b></div>
     <div><span class="badge">Confidence: {{g.pregame_conf}}%</span><span class="badge {{g.pregame_ev_class}}">EV est: {{g.pregame_ev}}%</span></div>
     <div class="reason">Line nên kiểm tra: {{g.pregame_line}}</div>
     <div class="reason">Lý do pregame: {{ "; ".join(g.pregame_reasons) }}</div>
   </div>
 </div>
</div>
{% endfor %}

<div class="footer">
<b>V7 có gì mới:</b><br>
• Best Bet of the Day<br>
• EV estimate để lọc tín hiệu yếu<br>
• Data quality %<br>
• Line nên kiểm tra<br>
• Telegram chỉ gửi khi Score + EV đủ điều kiện<br><br>
Lưu ý: EV và line hiện là ước tính vì chưa gắn Odds API thật. Luôn kiểm tra sportsbook trước khi bet.
</div>
</body>
</html>"""

@app.route("/")
def index():
    games = filtered_games()
    html = render_template_string(
        HTML, games=games, best=best_bet(), running=bot_running, last_update=last_update,
        live_alert=LIVE_ALERT_SCORE, pregame_alert=PREGAME_ALERT_SCORE, min_display=MIN_DISPLAY_SCORE
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
    ok = send_telegram("✅ Test thành công: MLB Under Pro v7 đã kết nối Telegram.")
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
