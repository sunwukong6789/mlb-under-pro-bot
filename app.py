# MLB UNDER PRO BOT v20.1 STABLE
import time, requests, pandas as pd, streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime
from zoneinfo import ZoneInfo

st.set_page_config(page_title="MLB Under Pro v20.1 Stable", page_icon="⚾", layout="wide")
TZ=ZoneInfo("America/Los_Angeles"); REFRESH_SECONDS=30
SCHEDULE="https://statsapi.mlb.com/api/v1/schedule"
FEED="https://statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live"

SESSION=requests.Session()
SESSION.headers.update({"User-Agent":"MLB-Under-Pro-v20.1/1.0"})
retry=Retry(total=3,connect=3,read=3,backoff_factor=.6,status_forcelist=[429,500,502,503,504],allowed_methods=["GET"])
SESSION.mount("https://",HTTPAdapter(max_retries=retry))

TEAM={"Seattle Mariners":"MAR","Cleveland Guardians":"GUA","Houston Astros":"AST","Detroit Tigers":"TIG",
"New York Yankees":"NYY","Boston Red Sox":"BOS","Texas Rangers":"TEX","Toronto Blue Jays":"TOR",
"Cincinnati Reds":"CIN","Pittsburgh Pirates":"PIT","Philadelphia Phillies":"PHI","New York Mets":"NYM",
"Miami Marlins":"MIA","St. Louis Cardinals":"STL","Arizona Diamondbacks":"ARI","Tampa Bay Rays":"TB",
"Colorado Rockies":"COL","Minnesota Twins":"MIN","Kansas City Royals":"KC","Chicago White Sox":"CWS",
"Washington Nationals":"WSH","Baltimore Orioles":"BAL","Chicago Cubs":"CHC","Milwaukee Brewers":"MIL",
"Atlanta Braves":"ATL","San Francisco Giants":"SF","Los Angeles Dodgers":"LAD","San Diego Padres":"SD",
"Athletics":"ATH","Los Angeles Angels":"LAA"}
BIAS={"MAR @ GUA":8,"CHC @ MIL":7,"PHI @ NYM":5,"AST @ TIG":4,"ARI @ TB":4}

def get(url,params=None,timeout=12):
    try:
        r=SESSION.get(url,params=params,timeout=timeout); r.raise_for_status(); return r.json()
    except Exception as e:
        st.session_state["api_error"]=str(e); return None

def abbr(n): return TEAM.get(n,n[:3].upper())

def schedule(day):
    d=get(SCHEDULE,{"sportId":1,"date":day.strftime("%Y-%m-%d"),"hydrate":"team,linescore,probablePitcher"})
    if d is None:return None
    out=[]
    for x in d.get("dates",[]):
        for g in x.get("games",[]):
            a=g["teams"]["away"]["team"]["name"]; h=g["teams"]["home"]["team"]["name"]
            out.append({"pk":g["gamePk"],"game":f"{abbr(a)} @ {abbr(h)}",
                        "status":g.get("status",{}).get("detailedState","Unknown")})
    return out

def live(pk):
    d=get(FEED.format(gamePk=pk),timeout=10)
    if not d:return None
    ls=d.get("liveData",{}).get("linescore",{}); teams=ls.get("teams",{})
    ar=teams.get("away",{}).get("runs",0) or 0; hr=teams.get("home",{}).get("runs",0) or 0
    status=d.get("gameData",{}).get("status",{}).get("detailedState","Unknown")
    coded=str(d.get("gameData",{}).get("status",{}).get("codedGameState","")).upper()
    states={"in progress","manager challenge","review","delayed","warmup","game delayed","suspended"}
    off=ls.get("offense",{}); rr=[]
    if off.get("first"):rr.append("1B")
    if off.get("second"):rr.append("2B")
    if off.get("third"):rr.append("3B")
    return {"status":status,"is_live":status.lower() in states or coded in {"I","M","N"},
            "score":f"{ar}-{hr}","runs":ar+hr,"inning":ls.get("currentInning",0) or 0,
            "half":ls.get("inningHalf",""),"outs":ls.get("outs",0) or 0,
            "runners":", ".join(rr) if rr else "Bases empty"}

def under_score(game,x):
    s=50+BIAS.get(game,0)
    if x and x["is_live"]:
        if x["inning"]>=4 and x["runs"]<=2:s+=12
        if x["inning"]>=5 and x["runs"]<=3:s+=10
        if x["runners"]!="Bases empty":s-=7
        if "2B" in x["runners"] or "3B" in x["runners"]:s-=7
        if x["inning"]<=2:s-=5
    return max(0,min(100,round(s)))

st.title("⚾ MLB Under Pro Bot v20.1 Stable")
st.caption("MLB Stats live feed • retry protection • cached schedule • 30-second refresh")
with st.sidebar:
    day=st.date_input("Game date",datetime.now(TZ).date())
    auto=st.toggle("Auto refresh 30s",True)
    bankroll=st.number_input("Bankroll",10.0,value=1000.0,step=10.0)

games=schedule(day); key="schedule_"+day.isoformat()
if games is not None: st.session_state[key]=games
elif key in st.session_state:
    games=st.session_state[key]; st.warning("MLB API tạm lỗi — đang giữ lịch lần tải thành công gần nhất.")
else:
    st.error("Không lấy được MLB schedule."); st.caption(st.session_state.get("api_error","")); st.stop()
if not games: st.info("Không có trận MLB trong ngày đã chọn."); st.stop()

rows=[]
for g in games:
    x=live(g["pk"]); u=under_score(g["game"],x); o=100-u
    islive=bool(x and x["is_live"])
    decision=("🟢 LIVE UNDER" if islive and u>=78 and x["runners"]=="Bases empty"
              else "🟡 LEAN UNDER" if islive and u>=70
              else "🔴 AVOID / OVER RISK" if o>=65 else "⚪ PASS")
    rows.append({"Game":g["game"],"Status":"LIVE" if islive else (x["status"] if x else g["status"]),
                 "Score":x["score"] if x else "0-0",
                 "Inning":f'{x["half"]} {x["inning"]} ({x["outs"]} out)' if islive else "Pregame",
                 "Runners":x["runners"] if x else "Bases empty","Under":u,"Over":o,"Decision":decision})
df=pd.DataFrame(rows)
st.subheader("🔥 LIVE EDGE DASHBOARD")
st.dataframe(df,use_container_width=True,hide_index=True,column_config={
"Under":st.column_config.ProgressColumn("Under",min_value=0,max_value=100),
"Over":st.column_config.ProgressColumn("Over",min_value=0,max_value=100)})
st.caption("Last updated: "+datetime.now(TZ).strftime("%Y-%m-%d %I:%M:%S %p"))
if auto:
    time.sleep(REFRESH_SECONDS); st.rerun()
