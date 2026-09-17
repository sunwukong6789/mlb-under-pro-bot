import csv, math, os, statistics, time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

st.set_page_config(page_title="MLB Edge AI Pro v22", page_icon="⚾", layout="wide")
TZ=ZoneInfo("America/Los_Angeles"); REFRESH=int(os.getenv("REFRESH_SECONDS","30"))
SCHEDULE="https://statsapi.mlb.com/api/v1/schedule"; FEED="https://statsapi.mlb.com/api/v1.1/game/{}/feed/live"
ODDS_URL="https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
ODDS_KEY=os.getenv("ODDS_API_KEY",""); TG_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN",""); TG_CHAT=os.getenv("TELEGRAM_CHAT_ID","")
LOG=Path(os.getenv("ALERT_LOG_PATH","mlb_alert_history.csv"))
TEAM={"Arizona Diamondbacks":"ARI","Athletics":"ATH","Atlanta Braves":"ATL","Baltimore Orioles":"BAL","Boston Red Sox":"BOS","Chicago Cubs":"CHC","Chicago White Sox":"CWS","Cincinnati Reds":"CIN","Cleveland Guardians":"CLE","Colorado Rockies":"COL","Detroit Tigers":"DET","Houston Astros":"HOU","Kansas City Royals":"KC","Los Angeles Angels":"LAA","Los Angeles Dodgers":"LAD","Miami Marlins":"MIA","Milwaukee Brewers":"MIL","Minnesota Twins":"MIN","New York Mets":"NYM","New York Yankees":"NYY","Philadelphia Phillies":"PHI","Pittsburgh Pirates":"PIT","San Diego Padres":"SD","San Francisco Giants":"SF","Seattle Mariners":"SEA","St. Louis Cardinals":"STL","Tampa Bay Rays":"TB","Texas Rangers":"TEX","Toronto Blue Jays":"TOR","Washington Nationals":"WSH"}
st.markdown("""<style>.block-container{padding-top:1.2rem;max-width:1550px}div[data-testid="stMetric"]{background:rgba(120,120,120,.09);border:1px solid rgba(150,150,150,.22);border-radius:16px;padding:14px}h1{letter-spacing:-1px}</style>""",unsafe_allow_html=True)
HTTP=requests.Session(); HTTP.mount("https://",HTTPAdapter(max_retries=Retry(total=3,backoff_factor=.6,status_forcelist=[429,500,502,503,504],allowed_methods=["GET","POST"])))
HTTP.headers.update({"User-Agent":"MLB-Edge-AI-Pro-v22"})

def get(url,params=None,timeout=12):
    try:
        r=HTTP.get(url,params=params,timeout=timeout); r.raise_for_status(); return r.json()
    except Exception as e: st.session_state["api_error"]=str(e); return None

def telegram(text):
    if not TG_TOKEN or not TG_CHAT:return False,"Telegram variables missing"
    try:
        r=HTTP.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",json={"chat_id":TG_CHAT,"text":text},timeout=10);r.raise_for_status();return True,"Sent"
    except Exception as e:return False,str(e)

def games_for(day):
    data=get(SCHEDULE,{"sportId":1,"date":day.isoformat(),"hydrate":"team,probablePitcher,linescore"})
    if data is None:return None
    out=[]
    for block in data.get("dates",[]):
      for g in block.get("games",[]):
        a=g["teams"]["away"];h=g["teams"]["home"];an=a["team"]["name"];hn=h["team"]["name"]
        out.append({"pk":g["gamePk"],"away":an,"home":hn,"game":f"{TEAM.get(an,an[:3])} @ {TEAM.get(hn,hn[:3])}","status":g.get("status",{}).get("detailedState","Unknown"),"ap":a.get("probablePitcher",{}).get("fullName","TBD"),"hp":h.get("probablePitcher",{}).get("fullName","TBD")})
    return out

@st.cache_data(ttl=45,show_spinner=False)
def odds():
    if not ODDS_KEY:return []
    return get(ODDS_URL,{"apiKey":ODDS_KEY,"regions":"us","markets":"totals","oddsFormat":"american","dateFormat":"iso"}) or []

def dt(v):
    try:return datetime.fromisoformat(v.replace("Z","+00:00"))
    except (AttributeError,ValueError):return None

def market(game,events):
    ev=next((x for x in events if x.get("home_team")==game["home"] and x.get("away_team")==game["away"]),None)
    empty={"total":None,"under":None,"book":"N/A","books":0,"age":None,"spread":None}
    if not ev:return empty
    q=[]
    for b in ev.get("bookmakers",[]):
      for m in b.get("markets",[]):
       if m.get("key")=="totals":
        o=next((x for x in m.get("outcomes",[]) if x.get("name")=="Over"),None);u=next((x for x in m.get("outcomes",[]) if x.get("name")=="Under"),None)
        if o and u and o.get("point")==u.get("point"):q.append({"line":float(o["point"]),"price":u.get("price"),"book":b.get("title","Unknown"),"updated":m.get("last_update") or b.get("last_update")})
    if not q:return empty
    lines=[x["line"] for x in q];med=float(statistics.median(lines));same=[x for x in q if x["line"]==med]
    if not same:same=[min(q,key=lambda x:abs(x["line"]-med))];med=same[0]["line"]
    best=max(same,key=lambda x:x["price"] if x["price"] is not None else -9999); updates=[dt(x["updated"]) for x in q];updates=[x for x in updates if x]
    age=round((datetime.now(timezone.utc)-max(updates)).total_seconds()/60,1) if updates else None
    return {"total":med,"under":best["price"],"book":best["book"],"books":len(q),"age":age,"spread":round(max(lines)-min(lines),1)}

def live(pk):
    d=get(FEED.format(pk),timeout=10)
    if not d:return None
    ls=d.get("liveData",{}).get("linescore",{});t=ls.get("teams",{});off=ls.get("offense",{});s=d.get("gameData",{}).get("status",{});status=s.get("detailedState","Unknown")
    bases=[n for k,n in (("first","1B"),("second","2B"),("third","3B")) if off.get(k)];ar=t.get("away",{}).get("runs",0) or 0;hr=t.get("home",{}).get("runs",0) or 0
    return {"is_live":status.lower() in {"in progress","review","manager challenge","delayed","game delayed"} or str(s.get("codedGameState","")).upper() in {"I","M","N"},"status":status,"ar":ar,"hr":hr,"runs":ar+hr,"inn":ls.get("currentInning",0) or 0,"half":ls.get("inningHalf",""),"outs":ls.get("outs",0) or 0,"bases":", ".join(bases) if bases else "Empty"}

def elapsed(s):
    if not s or s["inn"]<=0:return 0
    return min(9,(s["inn"]-1)+(.5 if str(s["half"]).lower().startswith("bottom") else 0)+min(2,s["outs"])/6)

def remember(g,m,s):
    key=str(g["pk"]);mem=st.session_state.setdefault("line_memory",{});r=mem.get(key)
    if m["total"] is None:return r
    if r is None and (not s or not s["is_live"]):r={"opening":m["total"],"best":m["total"],"last":m["total"]};mem[key]=r
    elif r:r["best"]=max(r["best"],m["total"]);r["last"]=m["total"]
    return r

def analyze(m,s,mem,min_edge,threshold):
    z={"pick":"PASS","quality":0,"projection":None,"edge":None,"min_line":None,"move":"N/A","reason":"Chưa đủ dữ liệu"};bad=[]
    if not ODDS_KEY:bad.append("Thiếu ODDS_API_KEY")
    if m["total"] is None:bad.append("Không có sportsbook total thật")
    if m["books"]<2:bad.append("Cần ít nhất 2 sportsbook")
    if m["age"] is None or m["age"]>8:bad.append("Odds cũ hoặc thiếu timestamp")
    if not s or not s["is_live"]:bad.append("Pregame chỉ WATCH; không auto-alert")
    if mem is None:bad.append("Không có baseline pregame; app khởi động giữa trận")
    if bad:z["reason"]="; ".join(bad);return z
    e=elapsed(s)
    if e<3.5:z["reason"]="Quá sớm: chờ ít nhất 3.5 innings";return z
    if e>=7.5:z["reason"]="Quá muộn: variance cuối game cao";return z
    opening=float(mem["opening"]);current=float(m["total"]);move=current-opening
    # Correct units: opening/9 is combined runs per GAME inning, not per half-inning.
    prior=opening/9;observed=s["runs"]/max(e,.5);w=min(.45,max(.20,e/18));projection=s["runs"]+(prior*(1-w)+observed*w)*(9-e);edge=current-projection;minimum=math.ceil((projection+min_edge)*2)/2
    if s["bases"]!="Empty":bad.append(f"Có runner: {s['bases']}")
    if m["under"] is None or m["under"]<-120:bad.append(f"Giá Under không đạt ({m['under']})")
    if edge<min_edge:bad.append(f"Edge {edge:.1f} < {min_edge:.1f}")
    if current<mem["best"]-.25:bad.append(f"Line đã xấu đi: từng có {mem['best']}, hiện {current}")
    if m["spread"] is not None and m["spread"]>1:bad.append(f"Books lệch {m['spread']:.1f} run")
    quality=int(max(0,min(92,round(58+edge*9+e*1.5+min(m["books"],6)*1.5-max(0,-move)*8))))
    if quality<threshold:bad.append(f"Quality {quality} < {threshold}")
    z.update(pick="PASS" if bad else "UNDER",quality=quality,projection=round(projection,1),edge=round(edge,1),min_line=minimum,move=f"{move:+.1f}",reason="; ".join(bad) if bad else "Consensus line, baseline và game state cùng đạt chuẩn")
    return z

def log_alert(r):
    fields=["Timestamp","Date","Game PK","Game","Pick","Line","Odds","Quality","Projection","Edge","Minimum Line","Opening","Best Seen","Score","Inning","Book","Result","Profit Units"]
    try:
      new=not LOG.exists()
      with LOG.open("a",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=fields)
        if new:w.writeheader()
        w.writerow({x:r.get(x,"") for x in fields})
    except OSError:pass

st.title("⚾ MLB Edge AI Pro v22 — Verified Line Mode")
st.caption("REAL ODDS ONLY • CONSENSUS BOOKS • PREGAME BASELINE → BEST → CURRENT • STRICT PASS GATES")
st.warning("Quality Score là độ mạnh tín hiệu, KHÔNG phải xác suất thắng. Không có baseline/odds thật = PASS.")
with st.sidebar:
 st.header("⚙️ Control Center");day=st.date_input("Game date",datetime.now(TZ).date());auto=st.toggle(f"Auto refresh {REFRESH}s",True);threshold=st.slider("Telegram quality threshold",85,92,88);min_edge=st.slider("Minimum model edge (runs)",1.5,3.0,2.0,.5);st.caption("Stake tối đa 0.25u; không chase loss.");st.divider();st.write("Odds API","🟢 Connected" if ODDS_KEY else "🔴 Missing");st.write("Telegram","🟢 Ready" if TG_TOKEN and TG_CHAT else "🔴 Missing")
 if st.button("📨 Test Telegram",use_container_width=True):
  ok,msg=telegram("⚾ MLB Edge AI Pro v22\n✅ Telegram connected. Test only — NO BET.");(st.success if ok else st.error)(msg)

games=games_for(day);key="schedule_"+day.isoformat()
if games is not None:st.session_state[key]=games
elif key in st.session_state:games=st.session_state[key];st.warning("MLB API tạm lỗi — dùng schedule gần nhất.")
else:st.error("Không lấy được MLB schedule.");st.stop()
if not games:st.info("Không có MLB game ngày này.");st.stop()
events=odds();rows=[]
for g in games:
 m=market(g,events);s=live(g["pk"]);mem=remember(g,m,s);a=analyze(m,s,mem,min_edge,threshold);is_live=bool(s and s["is_live"]);score=f"{s['ar']}-{s['hr']}" if s else "0-0";inning=f"{s['half']} {s['inn']} • {s['outs']} out • {s['bases']}" if is_live else "Pregame";opening=mem["opening"] if mem else "N/A";best=mem["best"] if mem else "N/A";current=m["total"] if m["total"] is not None else "N/A"
 rows.append({"Game":g["game"],"Status":"LIVE" if is_live else g["status"],"Score":score,"Inning":inning,"Pitchers":f"{g['ap']} / {g['hp']}","Pregame Baseline":opening,"Best Seen":best,"Current":current,"Move":a["move"],"Books":m["books"],"Best Under Odds":m["under"] or "N/A","Book":m["book"],"Odds Age Min":m["age"] if m["age"] is not None else "N/A","Projection":a["projection"] if a["projection"] is not None else "N/A","Edge":a["edge"] if a["edge"] is not None else "N/A","Minimum Acceptable Line":a["min_line"] if a["min_line"] is not None else "N/A","Pick":a["pick"],"Best Bet":f"UNDER {current}" if a["pick"]=="UNDER" else "PASS","Quality":a["quality"],"Why / PASS":a["reason"]})
 if a["pick"]=="UNDER" and a["quality"]>=threshold and TG_TOKEN and TG_CHAT:
  ak=f"{day}:{g['pk']}:UNDER";sent=st.session_state.setdefault("sent_alerts",set())
  if ak not in sent:
   text=f"⚾ VERIFIED ALERT v22\n🔴 {g['game']}\n✅ UNDER {current} ({m['under']})\nMinimum acceptable: {a['min_line']}\nPregame baseline: {opening} | Best seen: {best}\nProjection: {a['projection']} | Edge: {a['edge']}\nQuality: {a['quality']}/100 (NOT win probability)\nScore: {score} | {inning}\nConsensus: {m['books']} books | Best price: {m['book']}\nStake cap: 0.25u. Do not chase."
   ok,_=telegram(text)
   if ok:sent.add(ak);log_alert({"Timestamp":datetime.now(TZ).isoformat(),"Date":day.isoformat(),"Game PK":g["pk"],"Game":g["game"],"Pick":"UNDER","Line":current,"Odds":m["under"],"Quality":a["quality"],"Projection":a["projection"],"Edge":a["edge"],"Minimum Line":a["min_line"],"Opening":opening,"Best Seen":best,"Score":score,"Inning":inning,"Book":m["book"],"Result":"PENDING"})

df=pd.DataFrame(rows);plays=df[df["Pick"]=="UNDER"].sort_values("Quality",ascending=False);live_df=df[df["Status"]=="LIVE"]
c1,c2,c3,c4=st.columns(4);c1.metric("Games",len(df));c2.metric("Verified Plays",len(plays));c3.metric("PASS",int((df["Pick"]=="PASS").sum()));c4.metric("Live",len(live_df))
t1,t2,t3,t4=st.tabs(["🏆 VERIFIED PICKS","🔴 ALL LIVE","📈 LINE TRACKER","🧾 ALERT HISTORY"])
with t1:
 if plays.empty:st.info("Không có kèo đạt toàn bộ điều kiện — PASS là kết quả hợp lệ.")
 else:st.dataframe(plays[["Game","Best Bet","Best Under Odds","Minimum Acceptable Line","Quality","Projection","Edge","Pregame Baseline","Best Seen","Current","Score","Inning"]],use_container_width=True,hide_index=True,column_config={"Quality":st.column_config.ProgressColumn("Quality",min_value=0,max_value=100)})
with t2:
 if live_df.empty:st.info("Chưa có game LIVE.")
 else:st.dataframe(live_df,use_container_width=True,hide_index=True,column_config={"Quality":st.column_config.ProgressColumn("Quality",min_value=0,max_value=100)})
with t3:st.dataframe(df[["Game","Status","Pregame Baseline","Best Seen","Current","Move","Books","Odds Age Min","Minimum Acceptable Line","Pick","Why / PASS"]],use_container_width=True,hide_index=True)
with t4:
 if LOG.exists():
  history=pd.read_csv(LOG);st.dataframe(history.sort_values("Timestamp",ascending=False),use_container_width=True,hide_index=True);st.download_button("⬇️ Download alert history CSV",LOG.read_bytes(),LOG.name,"text/csv")
 else:st.info("Chưa có Telegram alert nào được ghi lại.")
st.caption("v22 PASS nếu odds cũ, dưới 2 books, thiếu baseline pregame, line đã xấu đi, có runner, giá dưới -120, hoặc edge/quality không đủ.")
st.caption("Pregame baseline là line đầu tiên app quan sát trước giờ đấu, không tự nhận là market opening. Restart giữa trận sẽ cố ý PASS.")
st.caption("Updated "+datetime.now(TZ).strftime("%Y-%m-%d %I:%M:%S %p %Z"))
if auto:time.sleep(REFRESH);st.rerun()
