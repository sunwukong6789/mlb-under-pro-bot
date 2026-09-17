# ============================================================
# MLB EDGE AI PRO v21.1 — STRICT QUALITY ALERT
# Pregame + Live | Under Priority | Telegram | Audit Log
# ============================================================
import csv
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

st.set_page_config(page_title="MLB Edge AI Pro v21.1", page_icon="⚾", layout="wide")

TZ = ZoneInfo("America/Los_Angeles")
REFRESH = 30
MLB_SCHEDULE = "https://statsapi.mlb.com/api/v1/schedule"
MLB_FEED = "https://statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live"
ODDS_URL = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
ODDS_KEY = os.getenv("ODDS_API_KEY", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
LOG_PATH = Path(os.getenv("ALERT_LOG_PATH", "mlb_alert_history.csv"))

TEAM = {
    "Arizona Diamondbacks": "ARI", "Athletics": "ATH", "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL", "Boston Red Sox": "BOS", "Chicago Cubs": "CHC",
    "Chicago White Sox": "CWS", "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE",
    "Colorado Rockies": "COL", "Detroit Tigers": "DET", "Houston Astros": "HOU",
    "Kansas City Royals": "KC", "Los Angeles Angels": "LAA", "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA", "Milwaukee Brewers": "MIL", "Minnesota Twins": "MIN",
    "New York Mets": "NYM", "New York Yankees": "NYY", "Philadelphia Phillies": "PHI",
    "Pittsburgh Pirates": "PIT", "San Diego Padres": "SD", "San Francisco Giants": "SF",
    "Seattle Mariners": "SEA", "St. Louis Cardinals": "STL", "Tampa Bay Rays": "TB",
    "Texas Rangers": "TEX", "Toronto Blue Jays": "TOR", "Washington Nationals": "WSH",
}

st.markdown("""<style>
.block-container{padding-top:1.4rem;max-width:1500px}
div[data-testid="stMetric"]{background:rgba(120,120,120,.09);border:1px solid rgba(150,150,150,.2);border-radius:16px;padding:14px}
h1{letter-spacing:-1px}
</style>""", unsafe_allow_html=True)

S = requests.Session()
retry = Retry(total=3, connect=3, read=3, backoff_factor=.6,
              status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
S.mount("https://", HTTPAdapter(max_retries=retry))
S.headers.update({"User-Agent": "MLB-Edge-AI-Pro-v21.1"})


def get(url, params=None, timeout=12):
    try:
        r = S.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        st.session_state["api_error"] = str(exc)
        return None


def telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        return False, "Telegram variables missing"
    try:
        r = S.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                   json={"chat_id": TG_CHAT, "text": text}, timeout=10)
        r.raise_for_status()
        return True, "Sent"
    except Exception as exc:
        return False, str(exc)


def schedule(day):
    data = get(MLB_SCHEDULE, {"sportId": 1, "date": day.strftime("%Y-%m-%d"),
                              "hydrate": "team,probablePitcher,linescore"})
    if data is None:
        return None
    out = []
    for date_block in data.get("dates", []):
        for game in date_block.get("games", []):
            away = game["teams"]["away"]["team"]["name"]
            home = game["teams"]["home"]["team"]["name"]
            out.append({
                "pk": game["gamePk"], "away": away, "home": home,
                "game": f"{TEAM.get(away, away[:3])} @ {TEAM.get(home, home[:3])}",
                "status": game.get("status", {}).get("detailedState", "Unknown"),
                "ap": game["teams"]["away"].get("probablePitcher", {}).get("fullName", "TBD"),
                "hp": game["teams"]["home"].get("probablePitcher", {}).get("fullName", "TBD"),
            })
    return out


@st.cache_data(ttl=60, show_spinner=False)
def odds_data():
    if not ODDS_KEY:
        return []
    return get(ODDS_URL, {"apiKey": ODDS_KEY, "regions": "us", "markets": "h2h,totals",
                          "oddsFormat": "american"}) or []


def market(game, events):
    event = next((x for x in events if x.get("home_team") == game["home"]
                  and x.get("away_team") == game["away"]), None)
    ret = {"total": None, "over": None, "under": None, "aml": None, "hml": None, "book": "N/A"}
    if not event:
        return ret
    for book in event.get("bookmakers", []):
        for market_item in book.get("markets", []):
            outcomes = market_item.get("outcomes", [])
            if market_item.get("key") == "totals" and ret["total"] is None:
                over = next((x for x in outcomes if x.get("name") == "Over"), None)
                under = next((x for x in outcomes if x.get("name") == "Under"), None)
                if over and under and over.get("point") == under.get("point"):
                    ret.update(total=over.get("point"), over=over.get("price"),
                               under=under.get("price"), book=book.get("title", ""))
            elif market_item.get("key") == "h2h":
                for outcome in outcomes:
                    if outcome.get("name") == game["away"]:
                        ret["aml"] = outcome.get("price")
                    elif outcome.get("name") == game["home"]:
                        ret["hml"] = outcome.get("price")
        if ret["total"] is not None:
            break
    return ret


def live(game_pk):
    data = get(MLB_FEED.format(gamePk=game_pk), timeout=10)
    if not data:
        return None
    linescore = data.get("liveData", {}).get("linescore", {})
    teams = linescore.get("teams", {})
    away_runs = teams.get("away", {}).get("runs", 0) or 0
    home_runs = teams.get("home", {}).get("runs", 0) or 0
    offense = linescore.get("offense", {})
    bases = [name for key, name in (("first", "1B"), ("second", "2B"), ("third", "3B")) if offense.get(key)]
    status_data = data.get("gameData", {}).get("status", {})
    status = status_data.get("detailedState", "Unknown")
    coded = str(status_data.get("codedGameState", "")).upper()
    return {
        "is_live": status.lower() in {"in progress", "review", "manager challenge", "delayed", "game delayed"}
                   or coded in {"I", "M", "N"},
        "status": status, "ar": away_runs, "hr": home_runs, "runs": away_runs + home_runs,
        "inn": linescore.get("currentInning", 0) or 0, "half": linescore.get("inningHalf", ""),
        "outs": linescore.get("outs", 0) or 0, "bases": ", ".join(bases) if bases else "Empty",
    }


def american_implied(price):
    if price is None:
        return None
    return 100 / (price + 100) if price > 0 else (-price) / ((-price) + 100)


def completed_innings(state):
    half = str(state["half"]).lower()
    return max(.5, (state["inn"] - 1) + (0.5 if half.startswith("bottom") else 0.0))


def quality_signal(market_data, state):
    """Return honest signal strength, not a claimed win probability."""
    result = {"pick": "PASS", "quality": 0, "projection": None, "edge": None,
              "reason": "Chưa đủ dữ liệu live", "under_score": 50, "over_score": 50}
    if not state or not state["is_live"] or market_data["total"] is None or state["inn"] <= 0:
        return result

    completed = completed_innings(state)
    if completed < 4.5:
        result["reason"] = "Quá sớm: STRICT MODE chờ ít nhất 4.5 innings"
        return result
    if completed >= 8.0:
        result["reason"] = "Quá muộn: biến động cuối game cao"
        return result

    # Stabilize observed scoring with a neutral 0.50 runs/half-inning prior.
    raw_rate = state["runs"] / completed
    sample_weight = min(.78, max(.30, completed / 8.0))
    rate = raw_rate * sample_weight + .50 * (1 - sample_weight)
    remaining = max(0.0, 9.0 - completed)
    projection = state["runs"] + rate * remaining
    edge = float(market_data["total"]) - projection

    traffic_penalty = 0
    if state["bases"] != "Empty":
        traffic_penalty += 7
    if "2B" in state["bases"] or "3B" in state["bases"]:
        traffic_penalty += 7
    if state["outs"] == 0 and state["bases"] != "Empty":
        traffic_penalty += 4

    # Quality is evidence strength. It is deliberately capped below 100.
    quality = 45 + max(0, edge) * 13 + min(completed, 7) * 2 - traffic_penalty
    price = market_data["under"]
    if price is None:
        quality -= 8
    elif price < -120:
        quality -= 10  # expensive price; poor value even when the total looks low-risk
    elif price >= -115:
        quality += 3
    quality = int(max(0, min(94, round(quality))))

    reasons = []
    if edge < 2.0:
        reasons.append(f"Edge chỉ {edge:.1f} run (<2.0)")
    if state["bases"] != "Empty":
        reasons.append(f"Có runner: {state['bases']}")
    if price is not None and price < -120:
        reasons.append(f"Giá Under quá đắt ({price})")

    clean_state = state["bases"] == "Empty"
    price_ok = price is not None and price >= -120
    if edge >= 2.0 and clean_state and price_ok and quality >= 85:
        result["pick"] = "UNDER"
        result["reason"] = f"Projection thấp hơn line {edge:.1f} run; game state đạt chuẩn"
    else:
        result["reason"] = "; ".join(reasons) or "Chưa đủ đồng thuận để vào kèo"

    result.update(quality=quality, projection=round(projection, 1), edge=round(edge, 1),
                  under_score=quality if edge > 0 else max(0, 50 + int(edge * 10)),
                  over_score=max(0, min(94, 100 - quality)))
    return result


def grade(quality, pick):
    if pick == "PASS":
        return "⚪ PASS"
    if quality >= 85:
        return "🔥 A+ ALERT"
    if quality >= 80:
        return "✅ A ALERT"
    return "👀 WATCH"


def append_alert(row):
    fields = ["Timestamp", "Date", "Game PK", "Game", "Pick", "Line", "Odds", "Quality Score",
              "Projection", "Edge", "Score", "Inning", "Book", "Result", "Profit Units"]
    try:
        is_new = not LOG_PATH.exists()
        with LOG_PATH.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            if is_new:
                writer.writeheader()
            writer.writerow({key: row.get(key, "") for key in fields})
    except OSError:
        pass


st.title("⚾ MLB Edge AI Pro v21.1 — Strict Quality Alert")
st.caption("STRICT MODE • Live inning 5–8 • Quality Score ≠ xác suất thắng • Telegram ≥85")
st.warning("Bot chỉ là công cụ lọc kèo, không bảo đảm thắng. Không tăng tiền để gỡ; nên paper-track trước.")

with st.sidebar:
    st.header("⚙️ Control Center")
    day = st.date_input("Game date", datetime.now(TZ).date())
    auto = st.toggle("Auto refresh 30s", True)
    alert_threshold = st.slider("Telegram Quality threshold", 85, 92, 88)
    bankroll = st.number_input("Bankroll", 10.0, value=1000.0, step=10.0)
    st.caption("Khuyến nghị: tối đa 0.25u mỗi alert; không chase loss.")
    st.divider()
    st.write("Odds API", "🟢 Connected" if ODDS_KEY else "🔴 Missing")
    st.write("Telegram", "🟢 Ready" if TG_TOKEN and TG_CHAT else "🔴 Missing")
    if st.button("📨 Test Telegram", use_container_width=True):
        ok, msg = telegram("⚾ MLB Edge AI Pro v21.1 STRICT\n✅ Telegram connected successfully.")
        (st.success if ok else st.error)(msg)

games = schedule(day)
cache_key = "schedule_" + day.isoformat()
if games is not None:
    st.session_state[cache_key] = games
elif cache_key in st.session_state:
    games = st.session_state[cache_key]
    st.warning("MLB API tạm lỗi — dùng schedule gần nhất.")
else:
    st.error("Không lấy được MLB schedule.")
    st.stop()
if not games:
    st.info("Không có MLB game ngày này.")
    st.stop()

events = odds_data()
rows = []
for game in games:
    market_data = market(game, events)
    state = live(game["pk"])
    signal = quality_signal(market_data, state)
    status = "LIVE" if state and state["is_live"] else game["status"]
    score = f"{state['ar']}-{state['hr']}" if state else "0-0"
    inning = f"{state['half']} {state['inn']} • {state['outs']} out • {state['bases']}" if state and state["is_live"] else "Pregame"
    best = f"UNDER {market_data['total']}" if signal["pick"] == "UNDER" else "PASS"
    row = {
        "Game": game["game"], "Status": status, "Score": score, "Inning": inning,
        "Pitchers": f"{game['ap']} / {game['hp']}", "Book": market_data["book"],
        "Total": market_data["total"] or "N/A", "Under Odds": market_data["under"] or "N/A",
        "Projection": signal["projection"] if signal["projection"] is not None else "N/A",
        "Edge": signal["edge"] if signal["edge"] is not None else "N/A",
        "Pick": signal["pick"], "Best Bet": best, "Quality Score": signal["quality"],
        "Grade": grade(signal["quality"], signal["pick"]), "Why / PASS reason": signal["reason"],
    }
    rows.append(row)

    if signal["pick"] != "PASS" and signal["quality"] >= max(85, alert_threshold) and TG_TOKEN and TG_CHAT:
        # STRICT: one Telegram alert per game/day, even if the line changes later.
        alert_key = f"{day}:{game['pk']}:UNDER"
        sent = st.session_state.setdefault("sent_alerts", set())
        if LOG_PATH.exists():
            try:
                old = pd.read_csv(LOG_PATH)
                already_logged = ((old["Date"].astype(str) == day.isoformat()) &
                                  (old["Game PK"].astype(str) == str(game["pk"]))).any()
                if already_logged:
                    sent.add(alert_key)
            except (OSError, KeyError, pd.errors.EmptyDataError, pd.errors.ParserError):
                pass
        if alert_key not in sent:
            msg = (f"⚾ MLB STRICT QUALITY ALERT v21.1\n🔴 LIVE — {game['game']}\n"
                   f"✅ {best} ({market_data['under']})\nQuality Score: {signal['quality']}/100 (không phải win %)\n"
                   f"Projection: {signal['projection']} | Edge: {signal['edge']}\n"
                   f"Score: {score} | {inning}\nBook: {market_data['book']}\n"
                   f"Lý do: {signal['reason']}\nStake: tối đa 0.25u; không chase.")
            ok, _ = telegram(msg)
            if ok:
                sent.add(alert_key)
                append_alert({
                    "Timestamp": datetime.now(TZ).isoformat(), "Date": day.isoformat(),
                    "Game PK": game["pk"], "Game": game["game"], "Pick": "UNDER",
                    "Line": market_data["total"], "Odds": market_data["under"],
                    "Quality Score": signal["quality"], "Projection": signal["projection"],
                    "Edge": signal["edge"], "Score": score, "Inning": inning,
                    "Book": market_data["book"], "Result": "PENDING", "Profit Units": "",
                })

df = pd.DataFrame(rows)
plays = df[df["Pick"] != "PASS"].sort_values("Quality Score", ascending=False)
live_df = df[df["Status"] == "LIVE"]

c1, c2, c3, c4 = st.columns(4)
c1.metric("Games", len(df))
c2.metric("Quality Plays", len(plays))
c3.metric("PASS", int((df["Pick"] == "PASS").sum()))
c4.metric("Live", len(live_df))

t1, t2, t3 = st.tabs(["🏆 QUALITY PICKS", "🔴 ALL LIVE", "🧾 ALERT HISTORY"])
with t1:
    if plays.empty:
        st.info("Không có kèo đạt chuẩn — PASS là kết quả hợp lệ.")
    else:
        st.dataframe(plays[["Game", "Best Bet", "Under Odds", "Quality Score", "Grade", "Projection",
                            "Edge", "Score", "Inning", "Why / PASS reason"]],
                     use_container_width=True, hide_index=True,
                     column_config={"Quality Score": st.column_config.ProgressColumn(
                         "Quality Score", min_value=0, max_value=100)})
with t2:
    if live_df.empty:
        st.info("Chưa có game LIVE.")
    else:
        st.dataframe(live_df, use_container_width=True, hide_index=True,
                     column_config={"Quality Score": st.column_config.ProgressColumn(
                         "Quality Score", min_value=0, max_value=100)})
with t3:
    if LOG_PATH.exists():
        history = pd.read_csv(LOG_PATH)
        st.dataframe(history.sort_values("Timestamp", ascending=False), use_container_width=True, hide_index=True)
        st.download_button("⬇️ Download alert history CSV", LOG_PATH.read_bytes(), LOG_PATH.name, "text/csv")
    else:
        st.info("Chưa có Telegram alert nào được ghi lại.")

st.caption("v21.1 STRICT: Không alert trước 4.5 innings, khi có runner, edge <2.0, odds xấu hơn -120, hoặc score <85.")
st.caption("Pregame và side đều PASS khi chưa có dữ liệu độc lập. Một game chỉ được gửi tối đa một alert.")
st.caption("Updated " + datetime.now(TZ).strftime("%Y-%m-%d %I:%M:%S %p"))
if auto:
    time.sleep(REFRESH)
    st.rerun()
