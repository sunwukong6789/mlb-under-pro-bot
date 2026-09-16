# ============================================================
# MLB EDGE AI PRO v20.3 PRO MAX — UNDER PRIORITY
# Pregame + Live | Side + Total | Telegram Alerts | Streamlit
# ============================================================
import os, time, requests
import pandas as pd
import streamlit as st
from datetime import datetime
from zoneinfo import ZoneInfo
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

st.set_page_config(page_title="MLB Edge AI Pro v20.3", page_icon="⚾", layout="wide")

TZ=ZoneInfo("America/Los_Angeles"); REFRESH=30
MLB_SCHEDULE="https://statsapi.mlb.com/api/v1/schedule"
MLB_FEED="https://statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live"
ODDS_URL="https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
ODDS_KEY=os.getenv("ODDS_API_KEY","")
TG_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
TG_CHAT=os.getenv("TELEGRAM_CHAT_ID","")

S=requests.Session()
retry=Retry(total=3,connect=3,read=3,backoff_factor=.6,
            status_forcelist=[429,500,502,503,504],allowed_methods=["GET"])
S.mount("https://",HTTPAdapter(max_retries=retry))
S.headers.update({"User-Agent":"MLB-Edge-AI-Pro-v20.3"})

TEAM={"Arizona Diamondbacks":"ARI","Athletics":"ATH","Atlanta Braves":"ATL","Baltimore Orioles":"BAL",
"Boston Red Sox":"BOS","Chicago Cubs":"CHC","Chicago White Sox":"CWS","Cincinnati Reds":"CIN",
"Cleveland Guardians":"CLE","Colorado Rockies":"COL","Detroit Tigers":"DET","Houston Astros":"HOU",
"Kansas City Royals":"KC","Los Angeles Angels":"LAA","Los Angeles Dodgers":"LAD","Miami Marlins":"MIA",
"Milwaukee Brewers":"MIL","Minnesota Twins":"MIN","New York Mets":"NYM","New York Yankees":"NYY",
"Philadelphia Phillies":"PHI","Pittsburgh Pirates":"PIT","San Diego Padres":"SD","San Francisco Giants":"SF",
"Seattle Mariners":"SEA","St. Louis Cardinals":"STL","Tampa Bay Rays":"TB","Texas Rangers":"TEX",
"Toronto Blue Jays":"TOR","Washington Nationals":"WSH"}

st.markdown("""<style>
.block-container{padding-top:1.4rem;max-width:1500px}
div[data-testid="stMetric"]{background:rgba(120,120,120,.09);border:1px solid rgba(150,150,150,.2);
border-radius:16px;padding:14px}
h1{letter-spacing:-1px}
</style>""",unsafe_allow_html=True)

def get(url,params=None,timeout=12):
    try:
        r=S.get(url,params=params,timeout=timeout); r.raise_for_status(); return r.json()
    except Exception as e:
        st.session_state["api_error"]=str(e); return None

def telegram(text):
    if not TG_TOKEN or not TG_CHAT:return False,"Telegram variables missing"
    try:
        r=S.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                 json={"chat_id":TG_CHAT,"text":text},timeout=10)
        r.raise_for_status(); return True,"Sent"
    except Exception as e:return False,str(e)

def schedule(day):
    d=get(MLB_SCHEDULE,{"sportId":1,"date":day.strftime("%Y-%m-%d"),
        "hydrate":"team,probablePitcher,linescore"})
    if d is None:return None
    out=[]
    for z in d.get("dates",[]):
        for g in z.get("games",[]):
            a=g["teams"]["away"]["team"]["name"]; h=g["teams"]["home"]["team"]["name"]
            out.append({"pk":g["gamePk"],"away":a,"home":h,
             "game":f"{TEAM.get(a,a[:3])} @ {TEAM.get(h,h[:3])}",
             "status":g.get("status",{}).get("detailedState","Unknown"),
             "ap":g["teams"]["away"].get("probablePitcher",{}).get("fullName","TBD"),
             "hp":g["teams"]["home"].get("probablePitcher",{}).get("fullName","TBD")})
    return out

@st.cache_data(ttl=60,show_spinner=False)
def odds_data():
    if not ODDS_KEY:return []
    return get(ODDS_URL,{"apiKey":ODDS_KEY,"regions":"us",
        "markets":"h2h,spreads,totals","oddsFormat":"american"}) or []

def market(g,events):
    e=next((x for x in events if x.get("home_team")==g["home"] and x.get("away_team")==g["away"]),None)
    ret={"total":None,"over":None,"under":None,"aml":None,"hml":None,"book":"N/A"}
    if not e:return ret
    for b in e.get("bookmakers",[]):
        for m in b.get("markets",[]):
            o=m.get("outcomes",[])
            if m.get("key")=="totals" and ret["total"] is None:
                ov=next((x for x in o if x.get("name")=="Over"),None)
                un=next((x for x in o if x.get("name")=="Under"),None)
                if ov and un:
                    ret.update(total=ov.get("point"),over=ov.get("price"),under=un.get("price"),book=b.get("title",""))
            if m.get("key")=="h2h":
                for x in o:
                    if x.get("name")==g["away"]:ret["aml"]=x.get("price")
                    if x.get("name")==g["home"]:ret["hml"]=x.get("price")
        if ret["total"] is not None and ret["aml"] is not None:break
    return ret

def live(pk):
    d=get(MLB_FEED.format(gamePk=pk),timeout=10)
    if not d:return None
    ls=d.get("liveData",{}).get("linescore",{}); t=ls.get("teams",{})
    ar=t.get("away",{}).get("runs",0) or 0; hr=t.get("home",{}).get("runs",0) or 0
    off=ls.get("offense",{}); b=[]
    if off.get("first"):b.append("1B")
    if off.get("second"):b.append("2B")
    if off.get("third"):b.append("3B")
    status=d.get("gameData",{}).get("status",{}).get("detailedState","Unknown")
    coded=str(d.get("gameData",{}).get("status",{}).get("codedGameState","")).upper()
    return {"is_live":status.lower() in {"in progress","review","manager challenge","delayed","game delayed"} or coded in {"I","M","N"},
      "status":status,"ar":ar,"hr":hr,"runs":ar+hr,"inn":ls.get("currentInning",0) or 0,
      "half":ls.get("inningHalf",""),"outs":ls.get("outs",0) or 0,"bases":", ".join(b) if b else "Empty"}

def implied(x):
    if x is None:return None
    return 100/(x+100) if x>0 else (-x)/((-x)+100)

def scores(g,m,l):
    # Conservative neutral starting point. Under receives only a small PRIORITY tie-break,
    # not a fabricated statistical advantage.
    u=52.0; o=48.0
    if m["total"] is None:u=o=50.0
    if l and l["is_live"] and m["total"] is not None and l["inn"]>0:
        completed=max(.5,(l["inn"]-1)+(0.5 if str(l["half"]).lower().startswith("bottom") else 0))
        pace=l["runs"]/completed
        proj=l["runs"]+pace*max(0,9-l["inn"])
        u += (m["total"]-proj)*5
        if l["inn"]>=5 and l["runs"]<=3:u+=9
        if l["bases"]!="Empty":u-=7
        if "2B" in l["bases"] or "3B" in l["bases"]:u-=5
        o=100-u
    u=max(5,min(95,round(u))); o=100-u

    ap,hp=implied(m["aml"]),implied(m["hml"])
    if ap is None or hp is None:aw=hm=50
    else:
        z=ap+hp; aw=100*ap/z; hm=100*hp/z
        if l and l["is_live"]:
            delta=l["ar"]-l["hr"]; mult=4 if l["inn"]>=5 else 2
            aw+=delta*mult; hm=100-aw
        aw=max(5,min(95,round(aw))); hm=100-aw
    return u,o,aw,hm

def grade(x):
    return "🔥 STRONG" if x>=72 else "✅ PLAY" if x>=63 else "👀 LEAN" if x>=58 else "⚪ PASS"

st.title("⚾ MLB Edge AI Pro v20.3 PRO MAX")
st.caption("UNDER PRIORITY • Pregame + Live • Team + Total • Telegram Strong Alerts")

with st.sidebar:
    st.header("⚙️ Control Center")
    day=st.date_input("Game date",datetime.now(TZ).date())
    auto=st.toggle("Auto refresh 30s",True)
    strong=st.slider("Strong alert threshold",68,90,72)
    bankroll=st.number_input("Bankroll",10.0,value=1000.0,step=10.0)
    st.divider()
    st.write("Odds API", "🟢 Connected" if ODDS_KEY else "🔴 Missing")
    st.write("Telegram", "🟢 Ready" if TG_TOKEN and TG_CHAT else "🔴 Missing")
    if st.button("📨 Test Telegram",use_container_width=True):
        ok,msg=telegram("⚾ MLB Edge AI Pro v20.3\n✅ Telegram connected successfully.")
        (st.success if ok else st.error)(msg)

games=schedule(day); ck="schedule_"+day.isoformat()
if games is not None:st.session_state[ck]=games
elif ck in st.session_state:
    games=st.session_state[ck]; st.warning("MLB API tạm lỗi — dùng schedule gần nhất.")
else:
    st.error("Không lấy được MLB schedule."); st.stop()
if not games:st.info("Không có MLB game ngày này."); st.stop()

events=odds_data(); rows=[]
for g in games:
    m=market(g,events); l=live(g["pk"]); u,o,aw,hm=scores(g,m,l)
    total_pick="UNDER" if u>=63 else "OVER" if o>=66 else "PASS"
    side_pick=TEAM.get(g["away"],"AWAY") if aw>=60 else TEAM.get(g["home"],"HOME") if hm>=60 else "PASS"
    total_conf=max(u,o); side_conf=max(aw,hm)

    # Under priority: when Under is playable, it wins close comparisons.
    if total_pick=="UNDER" and u>=side_conf-3:
        best=f"UNDER {m['total']}" if m["total"] is not None else "PASS"; conf=u
    elif side_pick!="PASS" and side_conf>total_conf:
        best=side_pick+" ML"; conf=side_conf
    elif total_pick!="PASS" and m["total"] is not None:
        best=f"{total_pick} {m['total']}"; conf=total_conf
    else:best="PASS"; conf=max(total_conf,side_conf)

    status="LIVE" if l and l["is_live"] else g["status"]
    score=f"{l['ar']}-{l['hr']}" if l else "0-0"
    inning=f"{l['half']} {l['inn']} • {l['outs']} out" if l and l["is_live"] else "Pregame"
    row={"Game":g["game"],"Status":status,"Score":score,"Inning":inning,
      "Pitchers":f"{g['ap']} / {g['hp']}","Book":m["book"],"Total":m["total"] or "N/A",
      "Under":u,"Over":o,"Total Pick":total_pick,"Away":aw,"Home":hm,
      "Team Pick":side_pick,"Best Bet":best,"Confidence":conf,"Grade":grade(conf)}
    rows.append(row)

    # Telegram: strong picks only; dedupe per day/game/bet.
    if best!="PASS" and conf>=strong and TG_TOKEN and TG_CHAT:
        alert_key=f"{day}:{g['pk']}:{best}:{status}"
        sent=st.session_state.setdefault("sent_alerts",set())
        if alert_key not in sent:
            kind="🔴 LIVE" if status=="LIVE" else "🧠 PREGAME"
            msg=(f"⚾ MLB EDGE AI PRO v20.3\n{kind}\n{g['game']}\n"
                 f"🔥 BEST BET: {best}\nConfidence: {conf}/100\n"
                 f"Score: {score} | {inning}\nPitchers: {g['ap']} / {g['hp']}")
            ok,_=telegram(msg)
            if ok:sent.add(alert_key)

df=pd.DataFrame(rows)
top=df[df["Best Bet"]!="PASS"].sort_values("Confidence",ascending=False)
under=df[(df["Total Pick"]=="UNDER")].sort_values("Under",ascending=False)
pregame=df[df["Status"]!="LIVE"]; live_df=df[df["Status"]=="LIVE"]

c1,c2,c3,c4=st.columns(4)
c1.metric("Games",len(df)); c2.metric("Playable",len(top))
c3.metric("Under Signals",len(under)); c4.metric("Live",len(live_df))

t1,t2,t3,t4=st.tabs(["🏆 TOP PICKS","💚 TOP UNDER","🧠 PREGAME","🔴 LIVE"])
with t1:
    st.subheader("🏆 Best Bets Today")
    if top.empty:st.info("Không có edge đủ đẹp — PASS.")
    else:st.dataframe(top.head(5)[["Game","Best Bet","Confidence","Grade","Total","Team Pick","Status"]],
                      use_container_width=True,hide_index=True)
with t2:
    st.subheader("💚 Under Priority Board")
    if under.empty:st.info("Hôm nay chưa có Under đạt chuẩn.")
    else:st.dataframe(under[["Game","Total","Under","Over","Grade","Pitchers","Status"]],
                      use_container_width=True,hide_index=True,
                      column_config={"Under":st.column_config.ProgressColumn("Under", min_value=0, max_value=100),
                                     "Over":st.column_config.ProgressColumn("Over", min_value=0, max_value=100)})
with t3:
    if pregame.empty:st.info("Không còn game Pregame.")
    else:st.dataframe(pregame,use_container_width=True,hide_index=True,
        column_config={"Under":st.column_config.ProgressColumn("Under", min_value=0, max_value=100),
                       "Over":st.column_config.ProgressColumn("Over", min_value=0, max_value=100),
                       "Confidence":st.column_config.ProgressColumn("Confidence", min_value=0, max_value=100)})
with t4:
    if live_df.empty:st.info("Chưa có game LIVE.")
    else:st.dataframe(live_df,use_container_width=True,hide_index=True,
        column_config={"Under":st.column_config.ProgressColumn("Under", min_value=0, max_value=100),
                       "Over":st.column_config.ProgressColumn("Over", min_value=0, max_value=100),
                       "Confidence":st.column_config.ProgressColumn("Confidence", min_value=0, max_value=100)})

st.caption("Confidence = model signal score, not a guaranteed win probability. Missing market data => N/A/PASS.")
st.caption("Updated "+datetime.now(TZ).strftime("%Y-%m-%d %I:%M:%S %p"))
if auto:
    time.sleep(REFRESH); st.rerun()
