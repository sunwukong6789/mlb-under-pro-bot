import csv, json, math, os, statistics, time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

st.set_page_config(page_title="MLB Edge AI Pro v23", page_icon="⚾", layout="wide")
TZ = ZoneInfo("America/Los_Angeles")
REFRESH = int(os.getenv("REFRESH_SECONDS", "30"))
SCHEDULE = "https://statsapi.mlb.com/api/v1/schedule"
FEED = "https://statsapi.mlb.com/api/v1.1/game/{}/feed/live"
ODDS_URL = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
ODDS_KEY = os.getenv("ODDS_API_KEY", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
LOG = Path(os.getenv("ALERT_LOG_PATH", "mlb_alert_history.csv"))
MEMORY_FILE = Path(os.getenv("LINE_MEMORY_PATH", "/tmp/mlb_v23_line_memory.json"))

TEAM = {"Arizona Diamondbacks":"ARI","Athletics":"ATH","Atlanta Braves":"ATL","Baltimore Orioles":"BAL","Boston Red Sox":"BOS","Chicago Cubs":"CHC","Chicago White Sox":"CWS","Cincinnati Reds":"CIN","Cleveland Guardians":"CLE","Colorado Rockies":"COL","Detroit Tigers":"DET","Houston Astros":"HOU","Kansas City Royals":"KC","Los Angeles Angels":"LAA","Los Angeles Dodgers":"LAD","Miami Marlins":"MIA","Milwaukee Brewers":"MIL","Minnesota Twins":"MIN","New York Mets":"NYM","New York Yankees":"NYY","Philadelphia Phillies":"PHI","Pittsburgh Pirates":"PIT","San Diego Padres":"SD","San Francisco Giants":"SF","Seattle Mariners":"SEA","St. Louis Cardinals":"STL","Tampa Bay Rays":"TB","Texas Rangers":"TEX","Toronto Blue Jays":"TOR","Washington Nationals":"WSH"}

st.markdown("""<style>.block-container{padding-top:1.2rem;max-width:1600px}div[data-testid="stMetric"]{background:rgba(120,120,120,.09);border:1px solid rgba(150,150,150,.22);border-radius:16px;padding:14px}h1{letter-spacing:-1px}</style>""", unsafe_allow_html=True)

HTTP = requests.Session()
HTTP.mount("https://", HTTPAdapter(max_retries=Retry(total=3, backoff_factor=.6, status_forcelist=[429,500,502,503,504], allowed_methods=["GET","POST"])))
HTTP.headers.update({"User-Agent":"MLB-Edge-AI-Pro-v23"})

def get(url, params=None, timeout=12):
    try:
        r = HTTP.get(url, params=params, timeout=timeout); r.raise_for_status(); return r.json()
    except Exception as e:
        st.session_state["api_error"] = str(e); return None

def telegram(text):
    if not TG_TOKEN or not TG_CHAT: return False, "Telegram variables missing"
    try:
        r = HTTP.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id":TG_CHAT,"text":text}, timeout=10)
        r.raise_for_status(); return True, "Sent"
    except Exception as e: return False, str(e)

def games_for(day):
    data = get(SCHEDULE, {"sportId":1,"date":day.isoformat(),"hydrate":"team,probablePitcher,linescore"})
    if data is None: return None
    out=[]
    for block in data.get("dates",[]):
        for g in block.get("games",[]):
            a=g["teams"]["away"]; h=g["teams"]["home"]; an=a["team"]["name"]; hn=h["team"]["name"]
            out.append({"pk":g["gamePk"],"away":an,"home":hn,"game":f"{TEAM.get(an,an[:3])} @ {TEAM.get(hn,hn[:3])}","status":g.get("status",{}).get("detailedState","Unknown"),"ap":a.get("probablePitcher",{}).get("fullName","TBD"),"hp":h.get("probablePitcher",{}).get("fullName","TBD")})
    return out

@st.cache_data(ttl=30, show_spinner=False)
def odds():
    if not ODDS_KEY: return []
    return get(ODDS_URL, {"apiKey":ODDS_KEY,"regions":"us","markets":"totals","oddsFormat":"american","dateFormat":"iso"}) or []

def dt(v):
    try: return datetime.fromisoformat(v.replace("Z","+00:00"))
    except (AttributeError,ValueError): return None

def market(game, events):
    ev=next((x for x in events if x.get("home_team")==game["home"] and x.get("away_team")==game["away"]),None)
    empty={"total":None,"under":None,"book":"N/A","books":0,"age":None,"spread":None,"under_prices":[]}
    if not ev: return empty
    q=[]
    for b in ev.get("bookmakers",[]):
        for m in b.get("markets",[]):
            if m.get("key")!="totals": continue
            o=next((x for x in m.get("outcomes",[]) if x.get("name")=="Over"),None)
            u=next((x for x in m.get("outcomes",[]) if x.get("name")=="Under"),None)
            if o and u and o.get("point")==u.get("point"):
                q.append({"line":float(o["point"]),"price":u.get("price"),"book":b.get("title","Unknown"),"updated":m.get("last_update") or b.get("last_update")})
    if not q: return empty
    lines=[x["line"] for x in q]; med=float(statistics.median(lines)); same=[x for x in q if x["line"]==med]
    if not same: same=[min(q,key=lambda x:abs(x["line"]-med))]; med=same[0]["line"]
    best=max(same,key=lambda x:x["price"] if x["price"] is not None else -9999)
    updates=[dt(x["updated"]) for x in q]; updates=[x for x in updates if x]
    age=round((datetime.now(timezone.utc)-max(updates)).total_seconds()/60,1) if updates else None
    return {"total":med,"under":best["price"],"book":best["book"],"books":len(q),"age":age,"spread":round(max(lines)-min(lines),1),"under_prices":[x["price"] for x in same if x["price"] is not None]}

def live(pk):
    d=get(FEED.format(pk),timeout=10)
    if not d: return None
    ls=d.get("liveData",{}).get("linescore",{}); t=ls.get("teams",{}); off=ls.get("offense",{}); s=d.get("gameData",{}).get("status",{}); status=s.get("detailedState","Unknown")
    bases=[n for k,n in (("first","1B"),("second","2B"),("third","3B")) if off.get(k)]
    ar=t.get("away",{}).get("runs",0) or 0; hr=t.get("home",{}).get("runs",0) or 0
    return {"is_live":status.lower() in {"in progress","review","manager challenge","delayed","game delayed"} or str(s.get("codedGameState","")).upper() in {"I","M","N"},"status":status,"ar":ar,"hr":hr,"runs":ar+hr,"inn":ls.get("currentInning",0) or 0,"half":ls.get("inningHalf",""),"outs":ls.get("outs",0) or 0,"bases":", ".join(bases) if bases else "Empty"}

def elapsed(s):
    if not s or s["inn"]<=0: return 0
    return min(9,(s["inn"]-1)+(.5 if str(s["half"]).lower().startswith("bottom") else 0)+min(2,s["outs"])/6)

def load_memory():
    mem=st.session_state.setdefault("line_memory",{})
    if not mem and MEMORY_FILE.exists():
        try: mem.update(json.loads(MEMORY_FILE.read_text()))
        except Exception: pass
    return mem

def save_memory(mem):
    try: MEMORY_FILE.write_text(json.dumps(mem),encoding="utf-8")
    except OSError: pass

def remember(g,m,s):
    key=str(g["pk"]); mem=load_memory(); r=mem.get(key)
    if m["total"] is None: return r
    now=datetime.now(TZ).isoformat(); is_live=bool(s and s["is_live"])
    if r is None and not is_live:
        r={"opening":m["total"],"best":m["total"],"last":m["total"],"seen":1,"first_seen":now}; mem[key]=r
    elif r:
        r["best"]=max(float(r.get("best",m["total"])),m["total"]); r["last"]=m["total"]; r["seen"]=int(r.get("seen",1))+1
    save_memory(mem); return r

def common_gates(m):
    bad=[]
    if not ODDS_KEY: bad.append("Thiếu ODDS_API_KEY")
    if m["total"] is None: bad.append("Không có sportsbook total thật")
    if m["books"]<3: bad.append("Cần ít nhất 3 sportsbook")
    if m["age"] is None or m["age"]>8: bad.append("Odds cũ hoặc thiếu timestamp")
    if m["spread"] is not None and m["spread"]>1: bad.append(f"Books lệch {m['spread']:.1f} run")
    if m["under"] is None or m["under"]<-120: bad.append(f"Giá Under không đạt ({m['under']})")
    return bad

def analyze_pregame(m,mem,pregame_threshold):
    z={"pick":"PASS","mode":"PREGAME","quality":0,"projection":None,"edge":None,"min_line":None,"move":"N/A","reason":"Chưa đủ dữ liệu"}
    bad=common_gates(m)
    if mem is None:
        bad.append("Chưa có baseline pregame")
        z["reason"]="; ".join(bad); return z
    opening=float(mem["opening"]); current=float(m["total"]); move=current-opening
    prices=m.get("under_prices",[]); median_price=statistics.median(prices) if prices else None
    # Pregame is market-confirmation only: never fabricate a run projection from the total itself.
    quality=60 + min(m["books"],8)*2
    if median_price is not None:
        if median_price<=-105: quality+=8
        if median_price<=-115: quality+=5
    if move<0: quality+=min(8,abs(move)*8)  # market moved toward Under
    elif move>0.5: quality-=8
    if int(mem.get("seen",1))<2: bad.append("Cần ít nhất 2 lần refresh để xác nhận line")
    if median_price is None or median_price>-105: bad.append(f"Under consensus chưa đủ mạnh ({median_price})")
    if move>0.5: bad.append(f"Line đi ngược Under: {move:+.1f}")
    quality=int(max(0,min(90,round(quality))))
    if quality<pregame_threshold: bad.append(f"Pregame Quality {quality} < {pregame_threshold}")
    z.update(pick="UNDER" if not bad else "PASS",quality=quality,move=f"{move:+.1f}",min_line=current,reason="Pregame consensus xác nhận Under; không dùng projection giả" if not bad else "; ".join(bad))
    return z

def analyze_live(m,s,mem,min_edge,live_threshold):
    z={"pick":"PASS","mode":"LIVE","quality":0,"projection":None,"edge":None,"min_line":None,"move":"N/A","reason":"Chưa đủ dữ liệu"}
    bad=common_gates(m)
    if not s or not s["is_live"]: bad.append("Chưa LIVE")
    if mem is None: bad.append("Không có baseline pregame; không đo line movement")
    if bad: z["reason"]="; ".join(bad); return z
    e=elapsed(s)
    if e<3.5: z["reason"]="Quá sớm: chờ ít nhất 3.5 innings"; return z
    if e>=7.5: z["reason"]="Quá muộn: variance cuối game cao"; return z
    opening=float(mem["opening"]); current=float(m["total"]); move=current-opening
    prior=opening/9; observed=s["runs"]/max(e,.5); w=min(.45,max(.20,e/18))
    projection=s["runs"]+(prior*(1-w)+observed*w)*(9-e); edge=current-projection
    minimum=math.ceil((projection+min_edge)*2)/2
    if s["bases"]!="Empty": bad.append(f"Có runner: {s['bases']}")
    if edge<min_edge: bad.append(f"Edge {edge:.1f} < {min_edge:.1f}")
    # Do NOT reject a current line just because a higher line existed earlier. Only require current >= minimum.
    if current<minimum: bad.append(f"Current {current} < minimum acceptable {minimum}")
    quality=int(max(0,min(94,round(56+edge*10+e*1.5+min(m["books"],6)*1.5-max(0,-move)*4))))
    if quality<live_threshold: bad.append(f"Live Quality {quality} < {live_threshold}")
    z.update(pick="UNDER" if not bad else "PASS",quality=quality,projection=round(projection,1),edge=round(edge,1),min_line=minimum,move=f"{move:+.1f}",reason="Current line vẫn còn value; projection + game state + consensus cùng đạt chuẩn" if not bad else "; ".join(bad))
    return z

def log_alert(r):
    fields=["Timestamp","Date","Game PK","Game","Mode","Pick","Line","Odds","Quality","Projection","Edge","Minimum Line","Opening","Best Seen","Score","Inning","Book","Result","Profit Units"]
    try:
        new=not LOG.exists()
        with LOG.open("a",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=fields)
            if new: w.writeheader()
            w.writerow({x:r.get(x,"") for x in fields})
    except OSError: pass

st.title("⚾ MLB Edge AI Pro v23 — Smart Signal Mode")
st.caption("REAL ODDS • PREGAME CONSENSUS SIGNALS • LIVE VALUE CHECK • NO CHASING WORSE LINES")
st.warning("Quality Score là độ mạnh tín hiệu, KHÔNG phải xác suất thắng. Không đủ dữ liệu = PASS, không gửi Telegram.")
with st.sidebar:
    st.header("⚙️ Control Center")
    day=st.date_input("Game date",datetime.now(TZ).date()); auto=st.toggle(f"Auto refresh {REFRESH}s",True)
    live_threshold=st.slider("Live Telegram threshold",82,92,86)
    pregame_threshold=st.slider("Pregame Telegram threshold",82,92,86)
    min_edge=st.slider("Minimum live edge (runs)",1.0,2.5,1.5,.5)
    st.caption("Pregame cần ≥3 books + ≥2 refresh + Under consensus. Live cần projection edge + current line ≥ minimum acceptable.")
    st.caption("Stake tối đa 0.25u; không chase loss.")
    st.divider(); st.write("Odds API","🟢 Connected" if ODDS_KEY else "🔴 Missing"); st.write("Telegram","🟢 Ready" if TG_TOKEN and TG_CHAT else "🔴 Missing")
    if st.button("📨 Test Telegram",use_container_width=True):
        ok,msg=telegram("⚾ MLB Edge AI Pro v23\n✅ Telegram connected. Test only — NO BET."); (st.success if ok else st.error)(msg)

games=games_for(day); key="schedule_"+day.isoformat()
if games is not None: st.session_state[key]=games
elif key in st.session_state: games=st.session_state[key]; st.warning("MLB API tạm lỗi — dùng schedule gần nhất.")
else: st.error("Không lấy được MLB schedule."); st.stop()
if not games: st.info("Không có MLB game ngày này."); st.stop()

events=odds(); rows=[]
for g in games:
    m=market(g,events); s=live(g["pk"]); mem=remember(g,m,s); is_live=bool(s and s["is_live"])
    a=analyze_live(m,s,mem,min_edge,live_threshold) if is_live else analyze_pregame(m,mem,pregame_threshold)
    score=f"{s['ar']}-{s['hr']}" if s else "0-0"; inning=f"{s['half']} {s['inn']} • {s['outs']} out • {s['bases']}" if is_live else "Pregame"
    opening=mem["opening"] if mem else "N/A"; best=mem["best"] if mem else "N/A"; current=m["total"] if m["total"] is not None else "N/A"
    rows.append({"Game":g["game"],"Mode":a["mode"],"Status":"LIVE" if is_live else g["status"],"Score":score,"Inning":inning,"Pitchers":f"{g['ap']} / {g['hp']}","Pregame Baseline":opening,"Best Seen":best,"Current":current,"Move":a["move"],"Books":m["books"],"Best Under Odds":m["under"] if m["under"] is not None else "N/A","Book":m["book"],"Odds Age Min":m["age"] if m["age"] is not None else "N/A","Projection":a["projection"] if a["projection"] is not None else "N/A","Edge":a["edge"] if a["edge"] is not None else "N/A","Minimum Acceptable Line":a["min_line"] if a["min_line"] is not None else "N/A","Pick":a["pick"],"Best Bet":f"UNDER {current}" if a["pick"]=="UNDER" else "PASS","Quality":a["quality"],"Why / PASS":a["reason"]})
    threshold=live_threshold if is_live else pregame_threshold
    if a["pick"]=="UNDER" and a["quality"]>=threshold and TG_TOKEN and TG_CHAT:
        # Separate one pregame and one live alert max per game. Live alert only if line is still >= minimum acceptable.
        ak=f"{day}:{g['pk']}:{a['mode']}:UNDER"; sent=st.session_state.setdefault("sent_alerts",set())
        if ak not in sent:
            mode_icon="🔴 LIVE" if is_live else "🟢 PREGAME"
            text=(f"⚾ VERIFIED ALERT v23\n{mode_icon} — {g['game']}\n✅ UNDER {current} ({m['under']})\n"
                  f"Quality: {a['quality']}/100 (NOT win probability)\n"
                  f"Pregame baseline: {opening} | Best seen: {best} | Current: {current}\n"
                  f"Projection: {a['projection'] if a['projection'] is not None else 'N/A'} | Edge: {a['edge'] if a['edge'] is not None else 'N/A'} | Minimum: {a['min_line']}\n"
                  f"Score: {score} | {inning}\nConsensus: {m['books']} books | Best price: {m['book']}\n"
                  f"Reason: {a['reason']}\nStake cap: 0.25u. Do not chase a worse line.")
            ok,_=telegram(text)
            if ok:
                sent.add(ak); log_alert({"Timestamp":datetime.now(TZ).isoformat(),"Date":day.isoformat(),"Game PK":g["pk"],"Game":g["game"],"Mode":a["mode"],"Pick":"UNDER","Line":current,"Odds":m["under"],"Quality":a["quality"],"Projection":a["projection"],"Edge":a["edge"],"Minimum Line":a["min_line"],"Opening":opening,"Best Seen":best,"Score":score,"Inning":inning,"Book":m["book"],"Result":"PENDING"})

df=pd.DataFrame(rows); plays=df[df["Pick"]=="UNDER"].sort_values("Quality",ascending=False); live_df=df[df["Status"]=="LIVE"]
c1,c2,c3,c4=st.columns(4); c1.metric("Games",len(df)); c2.metric("Verified Plays",len(plays)); c3.metric("PASS",int((df["Pick"]=="PASS").sum())); c4.metric("Live",len(live_df))
t1,t2,t3,t4=st.tabs(["🏆 VERIFIED PICKS","🔴 ALL LIVE","📈 LINE TRACKER","🧾 ALERT HISTORY"])
with t1:
    if plays.empty: st.info("Chưa có kèo đạt toàn bộ điều kiện — bot sẽ im lặng thay vì ép signal.")
    else: st.dataframe(plays[["Game","Mode","Best Bet","Best Under Odds","Minimum Acceptable Line","Quality","Projection","Edge","Pregame Baseline","Best Seen","Current","Score","Inning","Why / PASS"]],use_container_width=True,hide_index=True,column_config={"Quality":st.column_config.ProgressColumn("Quality",min_value=0,max_value=100)})
with t2:
    if live_df.empty: st.info("Chưa có game LIVE.")
    else: st.dataframe(live_df,use_container_width=True,hide_index=True,column_config={"Quality":st.column_config.ProgressColumn("Quality",min_value=0,max_value=100)})
with t3:
    st.dataframe(df[["Game","Mode","Status","Pregame Baseline","Best Seen","Current","Move","Books","Odds Age Min","Minimum Acceptable Line","Quality","Pick","Why / PASS"]],use_container_width=True,hide_index=True)
with t4:
    if LOG.exists():
        try:
            history=pd.read_csv(LOG); st.dataframe(history.sort_values("Timestamp",ascending=False),use_container_width=True,hide_index=True); st.download_button("⬇️ Download alert history CSV",LOG.read_bytes(),LOG.name,"text/csv")
        except Exception: st.info("Alert history cũ không cùng schema v23; file mới sẽ được ghi khi có alert.")
    else: st.info("Chưa có Telegram alert nào được ghi lại.")

st.caption("v23: Pregame có thể VERIFIED khi consensus Under đủ mạnh; Live mặc định edge ≥1.5 run và chỉ alert khi CURRENT vẫn ≥ Minimum Acceptable Line.")
st.caption("Không còn chặn live chỉ vì từng thấy line cao hơn. Không baseline khi mở app giữa trận = PASS để tránh đoán line giả.")
st.caption("Updated "+datetime.now(TZ).strftime("%Y-%m-%d %I:%M:%S %p %Z"))
if auto:
    time.sleep(REFRESH); st.rerun()
