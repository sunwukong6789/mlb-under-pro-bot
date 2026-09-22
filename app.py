import csv, json, math, os, statistics, time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

st.set_page_config(page_title="MLB Edge AI Pro v24", page_icon="⚾", layout="wide")

TZ = ZoneInfo("America/Los_Angeles")
REFRESH = int(os.getenv("REFRESH_SECONDS", "30"))
SCHEDULE = "https://statsapi.mlb.com/api/v1/schedule"
FEED = "https://statsapi.mlb.com/api/v1.1/game/{}/feed/live"
ODDS_URL = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
ODDS_KEY = os.getenv("ODDS_API_KEY", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
LOG = Path(os.getenv("ALERT_LOG_PATH", "mlb_alert_history_v24.csv"))
MEMORY_FILE = Path(os.getenv("LINE_MEMORY_PATH", "/tmp/mlb_v24_line_memory.json"))

TEAM = {
    "Arizona Diamondbacks":"ARI","Athletics":"ATH","Atlanta Braves":"ATL",
    "Baltimore Orioles":"BAL","Boston Red Sox":"BOS","Chicago Cubs":"CHC",
    "Chicago White Sox":"CWS","Cincinnati Reds":"CIN","Cleveland Guardians":"CLE",
    "Colorado Rockies":"COL","Detroit Tigers":"DET","Houston Astros":"HOU",
    "Kansas City Royals":"KC","Los Angeles Angels":"LAA","Los Angeles Dodgers":"LAD",
    "Miami Marlins":"MIA","Milwaukee Brewers":"MIL","Minnesota Twins":"MIN",
    "New York Mets":"NYM","New York Yankees":"NYY","Philadelphia Phillies":"PHI",
    "Pittsburgh Pirates":"PIT","San Diego Padres":"SD","San Francisco Giants":"SF",
    "Seattle Mariners":"SEA","St. Louis Cardinals":"STL","Tampa Bay Rays":"TB",
    "Texas Rangers":"TEX","Toronto Blue Jays":"TOR","Washington Nationals":"WSH"
}

st.markdown("""
<style>
.block-container{padding-top:1.15rem;max-width:1650px}
div[data-testid="stMetric"]{background:rgba(120,120,120,.08);
border:1px solid rgba(150,150,150,.22);border-radius:16px;padding:14px}
h1{letter-spacing:-1px}
.small-note{opacity:.78;font-size:.9rem}
</style>
""", unsafe_allow_html=True)

HTTP = requests.Session()
HTTP.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=.6, status_forcelist=[429,500,502,503,504],
    allowed_methods=["GET","POST"]
)))
HTTP.headers.update({"User-Agent":"MLB-Edge-AI-Pro-v24"})

def get(url, params=None, timeout=12):
    try:
        r = HTTP.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.session_state["api_error"] = str(e)
        return None

def telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        return False, "Telegram variables missing"
    try:
        r = HTTP.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text},
            timeout=10
        )
        r.raise_for_status()
        return True, "Sent"
    except Exception as e:
        return False, str(e)

def american_decimal(o):
    if o is None or o == 0:
        return None
    return 1 + (o / 100 if o > 0 else 100 / abs(o))

def implied(o):
    d = american_decimal(o)
    return (1 / d) if d else None

def no_vig_under(over_odds, under_odds):
    po, pu = implied(over_odds), implied(under_odds)
    if po is None or pu is None or po + pu <= 0:
        return None
    return pu / (po + pu)

def ev_pct(prob, odds):
    d = american_decimal(odds)
    if prob is None or d is None:
        return None
    return (prob * d - 1) * 100

def games_for(day):
    data = get(SCHEDULE, {
        "sportId": 1, "date": day.isoformat(),
        "hydrate": "team,probablePitcher,linescore"
    })
    if data is None:
        return None
    out = []
    for block in data.get("dates", []):
        for g in block.get("games", []):
            a = g["teams"]["away"]; h = g["teams"]["home"]
            an = a["team"]["name"]; hn = h["team"]["name"]
            out.append({
                "pk": g["gamePk"], "away": an, "home": hn,
                "game": f"{TEAM.get(an, an[:3])} @ {TEAM.get(hn, hn[:3])}",
                "status": g.get("status", {}).get("detailedState", "Unknown"),
                "ap": a.get("probablePitcher", {}).get("fullName", "TBD"),
                "hp": h.get("probablePitcher", {}).get("fullName", "TBD")
            })
    return out

@st.cache_data(ttl=30, show_spinner=False)
def odds():
    if not ODDS_KEY:
        return []
    return get(ODDS_URL, {
        "apiKey": ODDS_KEY, "regions": "us", "markets": "totals",
        "oddsFormat": "american", "dateFormat": "iso"
    }) or []

def dt(v):
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None

def market(game, events):
    ev = next((x for x in events
               if x.get("home_team") == game["home"]
               and x.get("away_team") == game["away"]), None)

    empty = {
        "total":None,"under":None,"over":None,"book":"N/A","books":0,
        "age":None,"spread":None,"pairs":[],"fair_under":None,
        "ev":None,"median_under":None
    }
    if not ev:
        return empty

    q = []
    for b in ev.get("bookmakers", []):
        for mk in b.get("markets", []):
            if mk.get("key") != "totals":
                continue
            o = next((x for x in mk.get("outcomes", []) if x.get("name") == "Over"), None)
            u = next((x for x in mk.get("outcomes", []) if x.get("name") == "Under"), None)
            if o and u and o.get("point") == u.get("point"):
                q.append({
                    "line": float(o["point"]),
                    "over": o.get("price"),
                    "under": u.get("price"),
                    "book": b.get("title", "Unknown"),
                    "updated": mk.get("last_update") or b.get("last_update")
                })

    if not q:
        return empty

    lines = [x["line"] for x in q]
    med = float(statistics.median(lines))
    same = [x for x in q if x["line"] == med]
    if not same:
        same = [min(q, key=lambda x: abs(x["line"] - med))]
        med = same[0]["line"]

    valid_under = [x for x in same if x["under"] is not None]
    if not valid_under:
        return empty

    best = max(valid_under, key=lambda x: x["under"])
    fair_probs = [no_vig_under(x["over"], x["under"]) for x in same]
    fair_probs = [x for x in fair_probs if x is not None]
    fair_under = statistics.median(fair_probs) if fair_probs else None
    edge_ev = ev_pct(fair_under, best["under"])

    updates = [dt(x["updated"]) for x in q]
    updates = [x for x in updates if x]
    age = round((datetime.now(timezone.utc) - max(updates)).total_seconds()/60, 1) if updates else None

    return {
        "total": med,
        "under": best["under"],
        "over": best["over"],
        "book": best["book"],
        "books": len(q),
        "age": age,
        "spread": round(max(lines)-min(lines), 1),
        "pairs": same,
        "fair_under": fair_under,
        "ev": edge_ev,
        "median_under": statistics.median([x["under"] for x in same if x["under"] is not None])
    }

def live(pk):
    d = get(FEED.format(pk), timeout=10)
    if not d:
        return None
    ls = d.get("liveData", {}).get("linescore", {})
    t = ls.get("teams", {})
    off = ls.get("offense", {})
    s = d.get("gameData", {}).get("status", {})
    status = s.get("detailedState", "Unknown")
    bases = [n for k,n in (("first","1B"),("second","2B"),("third","3B")) if off.get(k)]
    ar = t.get("away", {}).get("runs", 0) or 0
    hr = t.get("home", {}).get("runs", 0) or 0
    return {
        "is_live": status.lower() in {
            "in progress","review","manager challenge","delayed","game delayed"
        } or str(s.get("codedGameState","")).upper() in {"I","M","N"},
        "status":status, "ar":ar, "hr":hr, "runs":ar+hr,
        "inn":ls.get("currentInning",0) or 0,
        "half":ls.get("inningHalf",""),
        "outs":ls.get("outs",0) or 0,
        "bases":", ".join(bases) if bases else "Empty"
    }

def elapsed(s):
    if not s or s["inn"] <= 0:
        return 0
    return min(9, (s["inn"]-1)
               + (.5 if str(s["half"]).lower().startswith("bottom") else 0)
               + min(2, s["outs"])/6)

def load_memory():
    mem = st.session_state.setdefault("line_memory", {})
    if not mem and MEMORY_FILE.exists():
        try:
            mem.update(json.loads(MEMORY_FILE.read_text()))
        except Exception:
            pass
    return mem

def save_memory(mem):
    try:
        MEMORY_FILE.write_text(json.dumps(mem), encoding="utf-8")
    except OSError:
        pass

def remember(g, m, s):
    key = str(g["pk"])
    mem = load_memory()
    r = mem.get(key)
    if m["total"] is None:
        return r
    now = datetime.now(TZ).isoformat()
    is_live = bool(s and s["is_live"])
    if r is None and not is_live:
        r = {
            "opening":m["total"], "best":m["total"], "last":m["total"],
            "seen":1, "first_seen":now
        }
        mem[key] = r
    elif r:
        r["best"] = max(float(r.get("best", m["total"])), m["total"])
        r["last"] = m["total"]
        r["seen"] = int(r.get("seen",1)) + 1
    save_memory(mem)
    return r

def common_gates(m, max_price=-115):
    bad = []
    if not ODDS_KEY:
        bad.append("Thiếu ODDS_API_KEY")
    if m["total"] is None:
        bad.append("Không có sportsbook total thật")
    if m["books"] < 4:
        bad.append("Cần ít nhất 4 sportsbook")
    if m["age"] is None or m["age"] > 8:
        bad.append("Odds cũ hoặc thiếu timestamp")
    if m["spread"] is not None and m["spread"] > 1:
        bad.append(f"Books lệch {m['spread']:.1f} run")
    if m["under"] is None:
        bad.append("Không có giá Under")
    elif m["under"] < max_price:
        bad.append(f"Giá Under quá đắt ({m['under']})")
    return bad

def analyze_pregame(m, mem, threshold, min_ev, min_fair_prob, max_price):
    z = {
        "pick":"PASS","mode":"PREGAME","quality":0,"projection":None,
        "edge":None,"min_line":None,"move":"N/A","fair_prob":None,
        "ev":None,"reason":"Chưa đủ dữ liệu"
    }
    bad = common_gates(m, max_price)
    if mem is None:
        bad.append("Chưa có baseline pregame")
        z["reason"] = "; ".join(bad)
        return z

    opening = float(mem["opening"])
    current = float(m["total"])
    move = current - opening
    fair = m.get("fair_under")
    ev = m.get("ev")
    med_price = m.get("median_under")

    if int(mem.get("seen",1)) < 2:
        bad.append("Cần ít nhất 2 refresh để xác nhận line")
    if fair is None:
        bad.append("Không tính được no-vig probability")
    elif fair < min_fair_prob:
        bad.append(f"Fair Under {fair*100:.1f}% < {min_fair_prob*100:.1f}%")
    if ev is None:
        bad.append("Không tính được EV")
    elif ev < min_ev:
        bad.append(f"EV {ev:.1f}% < {min_ev:.1f}%")
    if med_price is None or med_price > -102:
        bad.append(f"Consensus Under chưa đủ mạnh ({med_price})")
    if move > 0.5:
        bad.append(f"Line đi ngược Under {move:+.1f}")

    # Score is signal quality, not win probability.
    quality = 50
    quality += min(m["books"], 10) * 2
    if fair is not None:
        quality += max(0, min(12, (fair - .50) * 200))
    if ev is not None:
        quality += max(0, min(12, ev * 1.5))
    if move < 0:
        quality += min(8, abs(move) * 8)
    elif move > .5:
        quality -= 10
    if m["under"] is not None and m["under"] <= -110:
        quality -= min(6, abs(m["under"] + 110) * .5)
    quality = int(max(0, min(94, round(quality))))

    if quality < threshold:
        bad.append(f"Quality {quality} < {threshold}")

    z.update(
        pick="UNDER" if not bad else "PASS",
        quality=quality,
        move=f"{move:+.1f}",
        min_line=current,
        fair_prob=round(fair*100,1) if fair is not None else None,
        ev=round(ev,1) if ev is not None else None,
        reason=("No-vig consensus + EV + line movement đều đạt chuẩn"
                if not bad else "; ".join(bad))
    )
    return z

def analyze_live(m, s, mem, min_edge, threshold, min_ev, max_price):
    z = {
        "pick":"PASS","mode":"LIVE","quality":0,"projection":None,
        "edge":None,"min_line":None,"move":"N/A","fair_prob":None,
        "ev":None,"reason":"Chưa đủ dữ liệu"
    }
    bad = common_gates(m, max_price)
    if not s or not s["is_live"]:
        bad.append("Chưa LIVE")
    if mem is None:
        bad.append("Không có baseline pregame")
    if bad:
        z["reason"] = "; ".join(bad)
        return z

    e = elapsed(s)
    if e < 3.5:
        z["reason"] = "Quá sớm: chờ ít nhất 3.5 innings"
        return z
    if e >= 7.5:
        z["reason"] = "Quá muộn: variance cuối game cao"
        return z

    opening = float(mem["opening"])
    current = float(m["total"])
    move = current - opening

    # Conservative live projection: pregame scoring prior blended with observed pace.
    prior_rate = opening / 9
    observed_rate = s["runs"] / max(e, .5)
    w = min(.40, max(.20, e/20))
    projection = s["runs"] + (prior_rate*(1-w) + observed_rate*w) * (9-e)
    edge = current - projection
    minimum = math.ceil((projection + min_edge) * 2) / 2

    fair = m.get("fair_under")
    market_ev = m.get("ev")

    if s["bases"] != "Empty":
        bad.append(f"Có runner: {s['bases']}")
    if edge < min_edge:
        bad.append(f"Projection edge {edge:.1f} < {min_edge:.1f}")
    if current < minimum:
        bad.append(f"Current {current} < minimum {minimum}")
    if market_ev is None or market_ev < min_ev:
        bad.append(f"Market EV {market_ev if market_ev is not None else 'N/A'} < {min_ev:.1f}%")

    quality = 52 + edge*9 + e*1.4 + min(m["books"],8)*1.5
    if market_ev is not None:
        quality += max(0, min(8, market_ev))
    if move < 0:
        quality -= min(6, abs(move)*4)
    quality = int(max(0, min(94, round(quality))))

    if quality < threshold:
        bad.append(f"Live Quality {quality} < {threshold}")

    z.update(
        pick="UNDER" if not bad else "PASS",
        quality=quality,
        projection=round(projection,1),
        edge=round(edge,1),
        min_line=minimum,
        move=f"{move:+.1f}",
        fair_prob=round(fair*100,1) if fair is not None else None,
        ev=round(market_ev,1) if market_ev is not None else None,
        reason=("Projection + no-vig EV + current line đều còn value"
                if not bad else "; ".join(bad))
    )
    return z

def log_alert(r):
    fields = [
        "Timestamp","Date","Game PK","Game","Mode","Pick","Line","Odds",
        "Quality","Fair Under %","EV %","Projection","Edge","Minimum Line",
        "Opening","Best Seen","Score","Inning","Book","Result","Profit Units"
    ]
    try:
        new = not LOG.exists()
        with LOG.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            if new:
                w.writeheader()
            w.writerow({x:r.get(x,"") for x in fields})
    except OSError:
        pass

st.title("⚾ MLB Edge AI Pro v24 — Value First Mode")
st.caption("REAL ODDS • NO-VIG FAIR PROBABILITY • EV FILTER • LINE VALUE • STRICT TELEGRAM")
st.warning(
    "v24: Quality Score KHÔNG phải xác suất thắng. "
    "VERIFIED chỉ xuất hiện khi dữ liệu, giá cược và EV đều đạt chuẩn."
)

with st.sidebar:
    st.header("⚙️ V24 Control Center")
    day = st.date_input("Game date", datetime.now(TZ).date())
    auto = st.toggle(f"Auto refresh {REFRESH}s", True)
    pregame_threshold = st.slider("Pregame Quality threshold", 75, 94, 84)
    live_threshold = st.slider("Live Quality threshold", 78, 94, 86)
    min_ev = st.slider("Minimum EV %", 0.0, 8.0, 2.0, .5)
    min_fair_prob = st.slider("Minimum fair Under probability", .50, .60, .525, .005)
    min_edge = st.slider("Minimum live projection edge", .5, 2.5, 1.5, .5)
    max_price = st.slider("Worst acceptable Under odds", -130, -105, -115, 5)
    st.caption("Ví dụ -115: bot PASS mọi Under -120, -125... dù tín hiệu mạnh.")
    st.caption("Ít picks hơn, ưu tiên giá + EV. Không ép phải có bet.")
    st.divider()
    st.write("Odds API", "🟢 Connected" if ODDS_KEY else "🔴 Missing")
    st.write("Telegram", "🟢 Ready" if TG_TOKEN and TG_CHAT else "🔴 Missing")

games = games_for(day)
events = odds()

if games is None:
    st.error("Không tải được MLB schedule.")
    st.stop()

rows = []
alert_candidates = []
live_count = 0

for g in games:
    s = live(g["pk"])
    if s and s["is_live"]:
        live_count += 1
    m = market(g, events)
    mem = remember(g, m, s)

    if s and s["is_live"]:
        a = analyze_live(m, s, mem, min_edge, live_threshold, min_ev, max_price)
    else:
        a = analyze_pregame(
            m, mem, pregame_threshold, min_ev, min_fair_prob, max_price
        )

    verified = a["pick"] == "UNDER"
    row = {
        "Game": g["game"],
        "Mode": a["mode"],
        "Best Bet": f"UNDER {m['total']}" if verified else "PASS",
        "Best Under Odds": m["under"] if m["under"] is not None else "N/A",
        "Book": m["book"],
        "Fair Under %": a["fair_prob"] if a["fair_prob"] is not None else "N/A",
        "EV %": a["ev"] if a["ev"] is not None else "N/A",
        "Quality": a["quality"],
        "Projection": a["projection"] if a["projection"] is not None else "—",
        "Edge": a["edge"] if a["edge"] is not None else "—",
        "Minimum Acceptable Line": a["min_line"] if a["min_line"] is not None else "—",
        "Move": a["move"],
        "Books": m["books"],
        "Reason": a["reason"],
        "Pitchers": f"{g['ap']} vs {g['hp']}"
    }
    rows.append(row)

    if verified:
        alert_candidates.append((g,m,s,mem,a))

verified_count = len(alert_candidates)
pass_count = max(0, len(games)-verified_count)

c1,c2,c3,c4 = st.columns(4)
c1.metric("Games", len(games))
c2.metric("Verified Plays", verified_count)
c3.metric("PASS", pass_count)
c4.metric("Live", live_count)

df = pd.DataFrame(rows)
tabs = st.tabs(["🏆 VERIFIED PICKS","🔴 ALL GAMES","📈 LINE / VALUE","🧾 ALERT HISTORY"])

with tabs[0]:
    v = df[df["Best Bet"] != "PASS"].copy() if not df.empty else df
    if v.empty:
        st.success("Không có kèo nào đủ chuẩn V24 lúc này — PASS toàn bộ slate.")
    else:
        st.dataframe(
            v[["Game","Mode","Best Bet","Best Under Odds","Book","Fair Under %",
               "EV %","Quality","Projection","Edge","Minimum Acceptable Line","Move"]],
            use_container_width=True, hide_index=True
        )
        st.caption("VERIFIED không đồng nghĩa chắc thắng; nó chỉ có nghĩa kèo đã vượt toàn bộ filter V24.")

with tabs[1]:
    st.dataframe(df, use_container_width=True, hide_index=True)

with tabs[2]:
    if df.empty:
        st.info("Chưa có dữ liệu.")
    else:
        st.dataframe(
            df[["Game","Best Under Odds","Fair Under %","EV %","Quality",
                "Move","Books","Reason"]],
            use_container_width=True, hide_index=True
        )
    st.info(
        "V24 không cộng điểm chỉ vì odds âm. Odds càng đắt càng khó VERIFIED. "
        "Fair Under % được tính từ no-vig consensus của sportsbook tại cùng total."
    )

with tabs[3]:
    if LOG.exists():
        try:
            history = pd.read_csv(LOG)
            st.dataframe(
                history.sort_values("Timestamp", ascending=False),
                use_container_width=True, hide_index=True
            )
            st.download_button(
                "⬇️ Download alert history CSV",
                LOG.read_bytes(), LOG.name, "text/csv"
            )
        except Exception:
            st.info("Chưa đọc được alert history.")
    else:
        st.info("Chưa có Telegram alert nào từ V24.")

# Send only once per game/mode/line using session state.
sent = st.session_state.setdefault("sent_alerts_v24", set())
for g,m,s,mem,a in alert_candidates:
    key = f"{g['pk']}|{a['mode']}|{m['total']}|{m['under']}"
    if key in sent:
        continue

    score = f"{s['ar']}-{s['hr']}" if s else ""
    inning = f"{s['half']} {s['inn']}" if s and s["is_live"] else "Pregame"
    text = (
        f"⚾ MLB EDGE AI PRO V24\n"
        f"✅ VERIFIED {a['mode']}\n"
        f"{g['game']}\n"
        f"🎯 UNDER {m['total']} @ {m['under']} ({m['book']})\n"
        f"📊 Fair Under: {a['fair_prob']}%\n"
        f"💰 EV: {a['ev']}%\n"
        f"⭐ Quality: {a['quality']}/100\n"
        f"📉 Move: {a['move']}\n"
    )
    if a["mode"] == "LIVE":
        text += (
            f"🧮 Projection: {a['projection']} | Edge: {a['edge']}\n"
            f"🛡 Minimum line: {a['min_line']}\n"
            f"⚾ Score/Inning: {score} | {inning}\n"
        )
    text += "⚠️ Signal quality ≠ guaranteed win. Không chase nếu line xấu hơn."

    ok, _ = telegram(text)
    if ok:
        sent.add(key)
        record = {
            "Timestamp":datetime.now(TZ).isoformat(),
            "Date":day.isoformat(),"Game PK":g["pk"],"Game":g["game"],
            "Mode":a["mode"],"Pick":"UNDER","Line":m["total"],"Odds":m["under"],
            "Quality":a["quality"],"Fair Under %":a["fair_prob"],"EV %":a["ev"],
            "Projection":a["projection"],"Edge":a["edge"],
            "Minimum Line":a["min_line"],
            "Opening":mem.get("opening") if mem else "",
            "Best Seen":mem.get("best") if mem else "",
            "Score":score,"Inning":inning,"Book":m["book"],
            "Result":"","Profit Units":""
        }
        log_alert(record)

st.caption(
    "v24 changes: no-vig fair probability + EV gate + price ceiling + unique Quality scores. "
    "Pregame VERIFIED no longer shows fake Projection/Edge; live requires projection edge."
)
st.caption("Updated " + datetime.now(TZ).strftime("%Y-%m-%d %I:%M:%S %p %Z"))

if "api_error" in st.session_state:
    with st.expander("API diagnostics"):
        st.code(st.session_state["api_error"])

if auto:
    time.sleep(REFRESH)
    st.rerun()
