# ============================================================
# MLB EDGE AI PRO v20.2
# Pregame + Live | Side + Total | OVER / UNDER / TEAM / PASS
# Streamlit / Render
# ============================================================

import os, time, requests
import pandas as pd
import streamlit as st
from datetime import datetime
from zoneinfo import ZoneInfo
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

st.set_page_config(page_title="MLB Edge AI Pro v20.2", page_icon="⚾", layout="wide")
TZ = ZoneInfo("America/Los_Angeles")
MLB_SCHEDULE = "https://statsapi.mlb.com/api/v1/schedule"
MLB_FEED = "https://statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live"
ODDS_BASE = "https://api.the-odds-api.com/v4"
REFRESH_SECONDS = 30

API_KEY = os.getenv("ODDS_API_KEY", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

S = requests.Session()
retry = Retry(total=3, connect=3, read=3, backoff_factor=.6,
              status_forcelist=[429,500,502,503,504], allowed_methods=["GET"])
S.mount("https://", HTTPAdapter(max_retries=retry))
S.headers.update({"User-Agent":"MLB-Edge-AI-Pro-v20.2"})

TEAM_ABBR = {
"Arizona Diamondbacks":"ARI","Athletics":"ATH","Atlanta Braves":"ATL","Baltimore Orioles":"BAL",
"Boston Red Sox":"BOS","Chicago Cubs":"CHC","Chicago White Sox":"CWS","Cincinnati Reds":"CIN",
"Cleveland Guardians":"CLE","Colorado Rockies":"COL","Detroit Tigers":"DET","Houston Astros":"HOU",
"Kansas City Royals":"KC","Los Angeles Angels":"LAA","Los Angeles Dodgers":"LAD","Miami Marlins":"MIA",
"Milwaukee Brewers":"MIL","Minnesota Twins":"MIN","New York Mets":"NYM","New York Yankees":"NYY",
"Philadelphia Phillies":"PHI","Pittsburgh Pirates":"PIT","San Diego Padres":"SD",
"San Francisco Giants":"SF","Seattle Mariners":"SEA","St. Louis Cardinals":"STL",
"Tampa Bay Rays":"TB","Texas Rangers":"TEX","Toronto Blue Jays":"TOR","Washington Nationals":"WSH"
}

def api_get(url, params=None, timeout=12):
    try:
        r=S.get(url,params=params,timeout=timeout); r.raise_for_status(); return r.json()
    except Exception as e:
        st.session_state["last_error"]=str(e); return None

def dec_prob(price):
    if price is None: return None
    return 100/(price+100) if price>0 else (-price)/((-price)+100)

def mlb_schedule(day):
    p={"sportId":1,"date":day.strftime("%Y-%m-%d"),
       "hydrate":"team,probablePitcher,linescore"}
    d=api_get(MLB_SCHEDULE,p)
    if d is None:return None
    out=[]
    for dt in d.get("dates",[]):
        for g in dt.get("games",[]):
            a=g["teams"]["away"]["team"]["name"]; h=g["teams"]["home"]["team"]["name"]
            out.append({
                "gamePk":g["gamePk"],"away":a,"home":h,
                "game":f"{TEAM_ABBR.get(a,a[:3])} @ {TEAM_ABBR.get(h,h[:3])}",
                "status":g.get("status",{}).get("detailedState","Unknown"),
                "away_pitcher":g["teams"]["away"].get("probablePitcher",{}).get("fullName","TBD"),
                "home_pitcher":g["teams"]["home"].get("probablePitcher",{}).get("fullName","TBD")
            })
    return out

@st.cache_data(ttl=60, show_spinner=False)
def odds():
    if not API_KEY:return []
    p={"apiKey":API_KEY,"regions":"us","markets":"h2h,spreads,totals","oddsFormat":"american"}
    return api_get(f"{ODDS_BASE}/sports/baseball_mlb/odds",p) or []

def best_market(event):
    result={"total":None,"over_price":None,"under_price":None,
            "away_ml":None,"home_ml":None,"away_spread":None,"home_spread":None}
    books=event.get("bookmakers",[])
    if not books:return result
    # Prefer first available bookmaker; never invent a market.
    for b in books:
        for m in b.get("markets",[]):
            outs=m.get("outcomes",[])
            if m.get("key")=="totals" and result["total"] is None:
                ov=next((x for x in outs if x.get("name")=="Over"),None)
                un=next((x for x in outs if x.get("name")=="Under"),None)
                if ov and un:
                    result.update(total=ov.get("point"),over_price=ov.get("price"),under_price=un.get("price"))
            elif m.get("key")=="h2h":
                for x in outs:
                    if x.get("name")==event.get("away_team"):result["away_ml"]=x.get("price")
                    if x.get("name")==event.get("home_team"):result["home_ml"]=x.get("price")
            elif m.get("key")=="spreads":
                for x in outs:
                    if x.get("name")==event.get("away_team"):result["away_spread"]=(x.get("point"),x.get("price"))
                    if x.get("name")==event.get("home_team"):result["home_spread"]=(x.get("point"),x.get("price"))
        if result["total"] is not None and result["away_ml"] is not None:return result
    return result

def match_odds(g, events):
    for e in events:
        if e.get("home_team")==g["home"] and e.get("away_team")==g["away"]:
            return best_market(e)
    return best_market({})

def live_state(pk):
    d=api_get(MLB_FEED.format(gamePk=pk),timeout=10)
    if not d:return None
    ls=d.get("liveData",{}).get("linescore",{}); tm=ls.get("teams",{})
    ar=tm.get("away",{}).get("runs",0) or 0; hr=tm.get("home",{}).get("runs",0) or 0
    off=ls.get("offense",{}); bases=[]
    if off.get("first"):bases.append("1B")
    if off.get("second"):bases.append("2B")
    if off.get("third"):bases.append("3B")
    status=d.get("gameData",{}).get("status",{}).get("detailedState","Unknown")
    coded=str(d.get("gameData",{}).get("status",{}).get("codedGameState","")).upper()
    return {"live":status.lower() in {"in progress","review","manager challenge","delayed","game delayed"} or coded in {"I","M","N"},
            "status":status,"away_runs":ar,"home_runs":hr,"runs":ar+hr,
            "inning":ls.get("currentInning",0) or 0,"half":ls.get("inningHalf",""),
            "outs":ls.get("outs",0) or 0,"bases":", ".join(bases) if bases else "Empty"}

def total_model(total, live=None):
    if total is None:return 50,50,"PASS"
    # Neutral baseline: market total itself is not treated as an edge.
    under=50.0
    if live and live["live"] and live["inning"]>0:
        halves=max(1,(live["inning"]-1)*2+(1 if str(live["half"]).lower().startswith("bottom") else 0))
        pace=live["runs"]/(halves/2)
        projected=live["runs"]+pace*max(0,9-live["inning"])
        under += (total-projected)*5
        if live["bases"]!="Empty":under-=6
        if live["inning"]>=5 and live["runs"]<=3:under+=8
    under=max(5,min(95,round(under)))
    over=100-under
    pick="UNDER" if under>=62 else "OVER" if over>=62 else "PASS"
    return under,over,pick

def side_model(g,m,live=None):
    ap=dec_prob(m.get("away_ml")); hp=dec_prob(m.get("home_ml"))
    if ap is None or hp is None:return 50,50,"PASS"
    # Remove bookmaker vig before comparing teams.
    z=ap+hp
    away=100*ap/z; home=100*hp/z
    if live and live["live"]:
        diff=live["away_runs"]-live["home_runs"]
        away += diff*4 if live["inning"]>=4 else diff*2
        home=100-away
    away=max(5,min(95,round(away))); home=100-away
    edge=max(away,home)
    pick=(TEAM_ABBR.get(g["away"],"AWAY") if away>=58 else
          TEAM_ABBR.get(g["home"],"HOME") if home>=58 else "PASS")
    return away,home,pick

def strength(score):
    return "🔥 STRONG" if score>=72 else "✅ PLAY" if score>=62 else "👀 LEAN" if score>=57 else "PASS"

st.title("⚾ MLB Edge AI Pro v20.2")
st.caption("Pregame + Live • Team + Total • UNDER / OVER / SIDE / PASS • real markets when available")

with st.sidebar:
    day=st.date_input("Game date",datetime.now(TZ).date())
    bankroll=st.number_input("Bankroll",10.0,value=1000.0,step=10.0)
    auto=st.toggle("Auto refresh 30s",True)
    st.write("Odds API:", "🟢 Connected" if API_KEY else "🟠 Missing key")
    st.write("Telegram:", "🟢 Ready" if TG_TOKEN and TG_CHAT else "⚪ Optional")

games=mlb_schedule(day)
key="sched_"+day.isoformat()
if games is not None:st.session_state[key]=games
elif key in st.session_state:
    games=st.session_state[key]; st.warning("MLB API tạm lỗi — đang dùng schedule gần nhất.")
else:
    st.error("Không lấy được MLB schedule."); st.stop()
if not games:st.info("Không có MLB game trong ngày này."); st.stop()

events=odds()
pregame=[]; live_rows=[]
for g in games:
    m=match_odds(g,events); lv=live_state(g["gamePk"])
    u,o,tp=total_model(m.get("total"),lv if lv and lv["live"] else None)
    aw,hm,sp=side_model(g,m,lv if lv and lv["live"] else None)
    total_conf=max(u,o); side_conf=max(aw,hm)
    total_label=(f"{tp} {m['total']}" if tp!="PASS" and m.get("total") else "PASS")
    side_label=sp
    best=(total_label,total_conf) if total_conf>=side_conf else (side_label,side_conf)
    row={"Game":g["game"],"Pitchers":f"{g['away_pitcher']} / {g['home_pitcher']}",
         "Total":m.get("total") or "N/A","Under":u,"Over":o,"Total Pick":total_label,
         "Away":aw,"Home":hm,"Team Pick":side_label,
         "Best Bet":best[0],"Confidence":best[1],"Grade":strength(best[1])}
    if lv and lv["live"]:
        row.update({"Score":f"{lv['away_runs']}-{lv['home_runs']}",
                    "Inning":f"{lv['half']} {lv['inning']} • {lv['outs']} out","Bases":lv["bases"]})
        live_rows.append(row)
    else:pregame.append(row)

tab1,tab2,tab3=st.tabs(["🏆 TOP PICKS","🧠 PREGAME","🔴 LIVE"])
allrows=pregame+live_rows
with tab1:
    top=pd.DataFrame(allrows)
    if not top.empty:
        top=top[top["Confidence"]>=57].sort_values("Confidence",ascending=False).head(5)
        st.dataframe(top[["Game","Best Bet","Confidence","Grade","Total","Total Pick","Team Pick"]],
                     use_container_width=True,hide_index=True)
    if top.empty:st.info("Không có edge đủ đẹp — PASS là lựa chọn tốt nhất.")
with tab2:
    d=pd.DataFrame(pregame)
    if d.empty:st.info("Không còn game pregame.")
    else:st.dataframe(d,use_container_width=True,hide_index=True,column_config={
        "Under":st.column_config.ProgressColumn("Under",0,100),
        "Over":st.column_config.ProgressColumn("Over",0,100),
        "Confidence":st.column_config.ProgressColumn("Confidence",0,100)})
with tab3:
    d=pd.DataFrame(live_rows)
    if d.empty:st.info("Chưa có game LIVE.")
    else:st.dataframe(d,use_container_width=True,hide_index=True,column_config={
        "Under":st.column_config.ProgressColumn("Under",0,100),
        "Over":st.column_config.ProgressColumn("Over",0,100),
        "Confidence":st.column_config.ProgressColumn("Confidence",0,100)})

st.caption("Model scores are decision-support estimates, not guaranteed win probabilities. Missing market data is shown as N/A/PASS.")
st.caption("Updated "+datetime.now(TZ).strftime("%Y-%m-%d %I:%M:%S %p"))
if auto:
    time.sleep(REFRESH_SECONDS)
    st.rerun()
