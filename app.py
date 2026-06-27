# ============================================================
# MLB UNDER PRO BOT v20 ULTIMATE
# Live Under / Pregame Edge Dashboard
# Deploy: Render.com
# Run: streamlit run app.py --server.port $PORT --server.address 0.0.0.0
# ============================================================

import time
import math
import requests
import pandas as pd
import streamlit as st
from datetime import datetime, date
from zoneinfo import ZoneInfo

st.set_page_config(
    page_title="MLB Under Pro v20 Ultimate",
    page_icon="⚾",
    layout="wide",
)

# -----------------------------
# CONFIG
# -----------------------------
TZ = ZoneInfo("America/Los_Angeles")
REFRESH_SECONDS = 30
MLB_SCHEDULE_API = "https://statsapi.mlb.com/api/v1/schedule"
MLB_GAME_API = "https://statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live"

TEAM_SHORT = {
    "Seattle Mariners": "MAR", "Cleveland Guardians": "GUA",
    "Houston Astros": "AST", "Detroit Tigers": "TIG",
    "New York Yankees": "NYY", "Boston Red Sox": "SOX",
    "Texas Rangers": "RAN", "Toronto Blue Jays": "JAY",
    "Cincinnati Reds": "RED", "Pittsburgh Pirates": "PIR",
    "Philadelphia Phillies": "PHI", "New York Mets": "NYM",
    "Miami Marlins": "MIA", "St. Louis Cardinals": "STL",
    "Arizona Diamondbacks": "ARI", "Tampa Bay Rays": "RAY",
    "Colorado Rockies": "COL", "Minnesota Twins": "MIN",
    "Kansas City Royals": "KC", "Chicago White Sox": "SOX",
    "Washington Nationals": "NAT", "Baltimore Orioles": "ORI",
    "Chicago Cubs": "CHC", "Milwaukee Brewers": "MIL",
    "Atlanta Braves": "ATL", "San Francisco Giants": "SFG",
    "Los Angeles Dodgers": "LAD", "San Diego Padres": "SD",
    "Athletics": "ATH", "Los Angeles Angels": "LAA",
}

DEFAULT_TOTALS = {
    "MAR @ GUA": 7.5,
    "AST @ TIG": 9.0,
    "NYY @ SOX": 8.5,
    "RAN @ JAY": 8.5,
    "RED @ PIR": 7.5,
    "PHI @ NYM": 8.5,
    "ARI @ RAY": 8.5,
    "KC @ SOX": 8.5,
    "NAT @ ORI": 9.0,
    "CHC @ MIL": 7.0,
    "COL @ MIN": 9.0,
    "MIA @ STL": 8.5,
}

PITCHER_PARK_UNDER_BIAS = {
    "MAR @ GUA": 8,
    "CHC @ MIL": 7,
    "PHI @ NYM": 5,
    "AST @ TIG": 4,
    "ARI @ RAY": 4,
    "NAT @ ORI": 2,
    "KC @ SOX": 2,
    "RED @ PIR": 2,
    "COL @ MIN": -3,
    "NYY @ SOX": -5,
    "RAN @ JAY": -4,
}

# -----------------------------
# HELPERS
# -----------------------------
def team_abbr(name: str) -> str:
    return TEAM_SHORT.get(name, name[:3].upper())

def safe_get(url, params=None, timeout=10):
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

def get_schedule(day: date):
    params = {
        "sportId": 1,
        "date": day.strftime("%Y-%m-%d"),
        "hydrate": "team,linescore,probablePitcher",
    }
    data = safe_get(MLB_SCHEDULE_API, params=params)
    games = []
    if not data:
        return games
    for d in data.get("dates", []):
        for g in d.get("games", []):
            away = g["teams"]["away"]["team"]["name"]
            home = g["teams"]["home"]["team"]["name"]
            away_ab = team_abbr(away)
            home_ab = team_abbr(home)
            key = f"{away_ab} @ {home_ab}"
            status = g.get("status", {}).get("detailedState", "Unknown")
            game_time = g.get("gameDate")
            games.append({
                "gamePk": g.get("gamePk"),
                "game": key,
                "away": away,
                "home": home,
                "status": status,
                "game_time": game_time,
                "pregame_total": DEFAULT_TOTALS.get(key, 8.5),
            })
    return games

def read_live_game(game_pk):
    data = safe_get(MLB_GAME_API.format(gamePk=game_pk), timeout=8)
    if not data:
        return None

    live = data.get("liveData", {})
    linescore = live.get("linescore", {})
    plays = live.get("plays", {})
    current = plays.get("currentPlay", {})
    about = current.get("about", {})

    teams = linescore.get("teams", {})
    away_runs = teams.get("away", {}).get("runs", 0) or 0
    home_runs = teams.get("home", {}).get("runs", 0) or 0

    inning = linescore.get("currentInning", 0) or 0
    half = linescore.get("inningHalf", "")
    outs = linescore.get("outs", 0) or 0

    offense = linescore.get("offense", {})
    runners = []
    if offense.get("first"): runners.append("1B")
    if offense.get("second"): runners.append("2B")
    if offense.get("third"): runners.append("3B")
    runners_txt = ", ".join(runners) if runners else "Bases empty"

    status = data.get("gameData", {}).get("status", {}).get("detailedState", "Unknown")

    return {
        "status": status,
        "away_runs": away_runs,
        "home_runs": home_runs,
        "total_runs": away_runs + home_runs,
        "inning": inning,
        "half": half,
        "outs": outs,
        "runners": runners_txt,
        "is_live": status.lower() in ["in progress", "manager challenge", "review", "delayed"],
    }

def inning_progress(inning, half, outs):
    if inning <= 0:
        return 0.0
    base = (inning - 1) * 2
    if str(half).lower().startswith("bottom"):
        base += 1
    return min(18, base + outs / 3) / 18

def projected_final(total_runs, inning, half, outs):
    prog = max(0.15, inning_progress(inning, half, outs))
    raw = total_runs / prog
    return max(total_runs, round(raw, 1))

def estimate_live_total(pregame_total, total_runs, inning, half, outs):
    prog = inning_progress(inning, half, outs)
    remaining = max(0, 1 - prog)
    expected_remaining = pregame_total * remaining
    live_total = total_runs + expected_remaining
    return round(max(total_runs + 0.5, live_total * 0.98), 1)

def score_under_edge(game, pre_total, live_total, total_runs, inning, half, outs, runners):
    edge = 50

    # Pregame profile
    edge += PITCHER_PARK_UNDER_BIAS.get(game, 0)

    # Pace
    proj = projected_final(total_runs, inning, half, outs)
    diff = live_total - proj
    edge += diff * 8

    # Late inning low score = good for Under
    if inning >= 4 and total_runs <= 2:
        edge += 12
    if inning >= 5 and total_runs <= 3:
        edge += 10

    # Danger spots
    if runners != "Bases empty":
        edge -= 7
    if "2B" in runners or "3B" in runners:
        edge -= 7
    if total_runs >= live_total - 1:
        edge -= 20
    if inning <= 2:
        edge -= 5

    edge = max(0, min(100, round(edge)))
    over = max(0, min(100, 100 - edge + 5))
    return edge, over, proj

def decision_from_scores(under, over, inning, runners):
    if under >= 78 and runners == "Bases empty":
        return "🟢 LIVE UNDER"
    if under >= 70:
        return "🟡 LEAN UNDER"
    if over >= 65:
        return "🔴 AVOID / OVER RISK"
    return "⚪ PASS"

def kelly_from_edge(under_score):
    edge = max(0, under_score - 60)
    return round(min(3.0, edge / 10), 1)

def clv_score(pre_total, live_total, decision):
    if "UNDER" in decision:
        return round(max(0, live_total - pre_total) * 3.5, 1)
    return 0.0

# -----------------------------
# UI
# -----------------------------
st.title("⚾ MLB Under Pro Bot v20 Ultimate")
st.caption("Live Under Engine • Auto LIVE detection • 30s refresh • MLB Stats API")

with st.sidebar:
    st.header("Settings")
    selected_date = st.date_input("Game date", value=datetime.now(TZ).date())
    auto_refresh = st.toggle("Auto refresh 30s", value=True)
    bankroll = st.number_input("Bankroll / Stake base", min_value=10.0, value=1000.0, step=10.0)
    st.info("Nếu Live U/O vẫn = 0 ở bản cũ, v20 này đã dùng MLB Stats live feed để tự đổi trạng thái khi game vào.")

if auto_refresh:
    time.sleep(0.1)

games = get_schedule(selected_date)

if not games:
    st.warning("Không lấy được lịch MLB hôm nay. Kiểm tra internet/Render logs.")
    st.stop()

rows = []
for g in games:
    live = read_live_game(g["gamePk"])
    pre_total = float(g["pregame_total"])

    if live and live["is_live"]:
        live_total = estimate_live_total(
            pre_total, live["total_runs"], live["inning"], live["half"], live["outs"]
        )
        under, over, proj = score_under_edge(
            g["game"], pre_total, live_total, live["total_runs"],
            live["inning"], live["half"], live["outs"], live["runners"]
        )
        decision = decision_from_scores(under, over, live["inning"], live["runners"])
        inning_txt = f"{live['half']} {live['inning']} ({live['outs']} out)"
        score = f"{live['away_runs']}-{live['home_runs']}"
        kelly = kelly_from_edge(under)
        stake = round(bankroll * (kelly / 100), 2)
        clv = clv_score(pre_total, live_total, decision)
        status = "LIVE"
    else:
        under = 50 + PITCHER_PARK_UNDER_BIAS.get(g["game"], 0) + max(0, 9 - pre_total) * 4
        under = max(0, min(100, round(under)))
        over = 100 - under
        decision = "PREGAME UNDER" if under >= 62 else "PASS"
        live_total = 0
        proj = 0
        inning_txt = "Pregame"
        score = "0-0"
        kelly = kelly_from_edge(under)
        stake = round(bankroll * (kelly / 100), 2)
        clv = 0
        status = g["status"]

    rows.append({
        "#": len(rows) + 1,
        "Game": g["game"],
        "Status": status,
        "Score": score,
        "Inning": inning_txt,
        "Runners": live["runners"] if live else "Bases empty",
        "Pre O/U": pre_total,
        "Live O/U": live_total,
        "Projected": proj,
        "Under": under,
        "Over": over,
        "Decision": decision,
        "Kelly %": kelly,
        "Stake": f"${stake}",
        "CLV": f"{clv}%",
    })

df = pd.DataFrame(rows)

st.subheader("🔥 LIVE EDGE DASHBOARD")
st.dataframe(
    df,
    use_container_width=True,
    hide_index=True,
    column_config={
        "Under": st.column_config.ProgressColumn("Under", min_value=0, max_value=100),
        "Over": st.column_config.ProgressColumn("Over", min_value=0, max_value=100),
    }
)

live_under = df[df["Decision"].str.contains("LIVE UNDER|LEAN UNDER", regex=True)]
st.subheader("✅ Best Live Under Signals")
if live_under.empty:
    st.write("Chưa có kèo LIVE UNDER đủ đẹp. Chờ line tốt hơn hoặc hết inning.")
else:
    st.dataframe(live_under[["Game", "Score", "Inning", "Runners", "Live O/U", "Projected", "Under", "Decision", "Kelly %", "Stake"]], use_container_width=True, hide_index=True)

st.subheader("🧠 Pregame Edge Board")
pre = df.sort_values("Under", ascending=False)[["Game", "Pre O/U", "Under", "Over", "Decision", "Kelly %", "Stake"]]
st.dataframe(pre, use_container_width=True, hide_index=True)

st.caption(f"Last updated: {datetime.now(TZ).strftime('%Y-%m-%d %I:%M:%S %p')}")

if auto_refresh:
    st.rerun()
