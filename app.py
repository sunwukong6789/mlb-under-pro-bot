import csv, json, math, os, statistics, time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

st.set_page_config(page_title="MLB Edge AI Pro v24.1", page_icon="⚾", layout="wide")

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
                "away_id": a["team"]["id"], "home_id": h["team"]["id"],
                "ap_id": a.get("probablePitcher", {}).get("id"),
                "hp_id": h.get("probablePitcher", {}).get("id"),
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
    empty = {"total":None,"under":None,"over":None,"under_book":"N/A","over_book":"N/A",
             "book":"N/A","books":0,"age":None,"spread":None,"pairs":[],
             "market_fair_under":None,"market_fair_over":None}
    if not ev: return empty
    q=[]
    for b in ev.get("bookmakers",[]):
        for mk in b.get("markets",[]):
            if mk.get("key")!="totals": continue
            o=next((x for x in mk.get("outcomes",[]) if x.get("name")=="Over"),None)
            u=next((x for x in mk.get("outcomes",[]) if x.get("name")=="Under"),None)
            if o and u and o.get("point")==u.get("point"):
                q.append({"line":float(o["point"]),"over":o.get("price"),"under":u.get("price"),
                          "book":b.get("title","Unknown"),"updated":mk.get("last_update") or b.get("last_update")})
    if not q: return empty
    lines=[x["line"] for x in q]; med=float(statistics.median(lines))
    same=[x for x in q if x["line"]==med] or [min(q,key=lambda x:abs(x["line"]-med))]
    med=same[0]["line"] if not any(x["line"]==med for x in q) else med
    us=[x for x in same if x["under"] is not None]; os_=[x for x in same if x["over"] is not None]
    if not us or not os_: return empty
    bu=max(us,key=lambda x:x["under"]); bo=max(os_,key=lambda x:x["over"])
    fps=[no_vig_under(x["over"],x["under"]) for x in same]
    fps=[x for x in fps if x is not None]; fu=statistics.median(fps) if fps else None
    updates=[dt(x["updated"]) for x in q]; updates=[x for x in updates if x]
    age=round((datetime.now(timezone.utc)-max(updates)).total_seconds()/60,1) if updates else None
    return {"total":med,"under":bu["under"],"over":bo["over"],"under_book":bu["book"],"over_book":bo["book"],
            "book":bu["book"],"books":len(q),"age":age,"spread":round(max(lines)-min(lines),1),"pairs":same,
            "market_fair_under":fu,"market_fair_over":1-fu if fu is not None else None}

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

PARK = {"COL":1.16,"BOS":1.07,"CIN":1.06,"NYY":1.05,"PHI":1.04,"ARI":1.04,
        "TEX":1.03,"LAD":1.02,"CHC":1.02,"BAL":1.01,"ATL":1.01,"KC":1.00,
        "LAA":1.00,"ATH":1.00,"HOU":0.99,"MIN":0.99,"TOR":0.99,"WSH":0.99,
        "CWS":0.99,"CLE":0.98,"MIL":0.98,"STL":0.98,"SD":0.96,"SEA":0.94,
        "SF":0.94,"TB":0.96,"MIA":0.96,"PIT":0.97,"NYM":0.98,"DET":0.98}

@st.cache_data(ttl=600, show_spinner=False)
def recent_team_form(day_iso):
    """Independent baseball input: runs scored/allowed from completed MLB games in prior 14 days."""
    from datetime import date, timedelta
    day=date.fromisoformat(day_iso); start=day-timedelta(days=14); end=day-timedelta(days=1)
    d=get(SCHEDULE,{"sportId":1,"startDate":start.isoformat(),"endDate":end.isoformat()}) or {}
    z={}
    for block in d.get("dates",[]):
        for g in block.get("games",[]):
            if g.get("status",{}).get("abstractGameState")!="Final": continue
            a=g["teams"]["away"]; h=g["teams"]["home"]
            if a.get("score") is None or h.get("score") is None: continue
            aid=a["team"]["id"]; hid=h["team"]["id"]; ar=float(a["score"]); hr=float(h["score"])
            z.setdefault(aid,{"rs":[],"ra":[]}); z.setdefault(hid,{"rs":[],"ra":[]})
            z[aid]["rs"].append(ar); z[aid]["ra"].append(hr); z[hid]["rs"].append(hr); z[hid]["ra"].append(ar)
    out={}
    for tid,v in z.items():
        out[tid]={"rpg":sum(v["rs"])/len(v["rs"]),"rapg":sum(v["ra"])/len(v["ra"]),"n":len(v["rs"])}
    return out

@st.cache_data(ttl=1800, show_spinner=False)
def pitcher_era(pid, season):
    if not pid: return None
    d=get(f"https://statsapi.mlb.com/api/v1/people/{pid}/stats",{"stats":"season","group":"pitching","season":season}) or {}
    try:
        splits=d["stats"][0]["splits"]
        return float(splits[0]["stat"]["era"]) if splits else None
    except (KeyError,IndexError,TypeError,ValueError): return None

def normal_cdf(x, mu, sd=3.05):
    return .5*(1+math.erf((x-mu)/(sd*math.sqrt(2))))

def model_probs(proj, line):
    # Treat integer totals with a push band; half totals have no practical push.
    if abs(line-round(line)) < .01:
        p_under=normal_cdf(line-.5,proj); p_over=1-normal_cdf(line+.5,proj)
        p_push=max(0,1-p_under-p_over)
    else:
        p_under=normal_cdf(line,proj); p_over=1-p_under; p_push=0
    return p_under,p_over,p_push

def bet_ev(prob_win, prob_push, odds):
    d=american_decimal(odds)
    if d is None: return None
    p_loss=max(0,1-prob_win-prob_push)
    return (prob_win*(d-1)-p_loss)*100

def independent_projection(g, form, season):
    a=form.get(g["away_id"]); h=form.get(g["home_id"])
    if not a or not h or min(a["n"],h["n"])<5: return None,None
    # Blend each offense with opponent recent run prevention; 4.45 is a neutral MLB run/team anchor.
    away_runs=.55*a["rpg"]+.30*h["rapg"]+.15*4.45
    home_runs=.55*h["rpg"]+.30*a["rapg"]+.15*4.45
    ap=pitcher_era(g.get("ap_id"),season); hp=pitcher_era(g.get("hp_id"),season)
    # Starter adjustment is deliberately capped so ERA never dominates the model.
    if hp is not None: away_runs += max(-.55,min(.55,(hp-4.20)*.18))
    if ap is not None: home_runs += max(-.55,min(.55,(ap-4.20)*.18))
    pf=PARK.get(TEAM.get(g["home"],""),1.0)
    proj=max(4.5,min(13.5,(away_runs+home_runs)*pf))
    meta={"away_rpg":a["rpg"],"home_rpg":h["rpg"],"away_rapg":a["rapg"],"home_rapg":h["rapg"],
          "ap_era":ap,"hp_era":hp,"park":pf,"games":min(a["n"],h["n"])}
    return proj,meta

def common_gates(m):
    bad=[]
    if not ODDS_KEY: bad.append("Thiếu ODDS_API_KEY")
    if m["total"] is None: bad.append("Không có sportsbook total thật")
    if m["books"]<4: bad.append("Cần ít nhất 4 sportsbook")
    if m["age"] is None or m["age"]>8: bad.append("Odds cũ hoặc thiếu timestamp")
    if m["spread"] is not None and m["spread"]>1: bad.append(f"Books lệch {m['spread']:.1f} run")
    return bad

def analyze_pregame(g,m,mem,form,threshold,min_model_ev,min_model_edge,max_price):
    z={"pick":"PASS","mode":"PREGAME","quality":0,"projection":None,"edge":None,"min_line":None,
       "move":"N/A","fair_prob":None,"ev":None,"reason":"Chưa đủ dữ liệu","odds":None,"book":"N/A"}
    bad=common_gates(m)
    if mem is None: bad.append("Chưa có baseline pregame")
    proj,meta=independent_projection(g,form,day.year)
    if proj is None: bad.append("Thiếu recent-form data độc lập")
    if bad: z["reason"]="; ".join(bad); return z
    current=float(m["total"]); opening=float(mem["opening"]); move=current-opening
    raw_edge=current-proj
    side="UNDER" if raw_edge>=min_model_edge else "OVER" if raw_edge<=-min_model_edge else "PASS"
    if side=="PASS":
        z.update(projection=round(proj,2),edge=round(abs(raw_edge),2),move=f"{move:+.1f}",reason=f"Model edge {abs(raw_edge):.2f} < {min_model_edge:.2f}")
        return z
    odds_=m["under"] if side=="UNDER" else m["over"]; book=m["under_book"] if side=="UNDER" else m["over_book"]
    if odds_ is None: bad.append(f"Không có giá {side}")
    elif odds_ < max_price: bad.append(f"Giá {side} quá đắt ({odds_})")
    pu,po,pp=model_probs(proj,current); pw=pu if side=="UNDER" else po
    mev=bet_ev(pw,pp,odds_)
    if mev is None or mev<min_model_ev: bad.append(f"Model EV {mev if mev is not None else 'N/A'} < {min_model_ev:.1f}%")
    if int(mem.get("seen",1))<2: bad.append("Cần ít nhất 2 refresh để xác nhận line")
    # Reject meaningful movement against our chosen side.
    if side=="UNDER" and move>0.5: bad.append(f"Line đi ngược UNDER {move:+.1f}")
    if side=="OVER" and move<-0.5: bad.append(f"Line đi ngược OVER {move:+.1f}")
    edge=abs(raw_edge)
    quality=58+edge*10+min(m["books"],8)*1.3+max(0,min(10,(mev or 0)*1.2))
    quality=int(max(0,min(94,round(quality))))
    if quality<threshold: bad.append(f"Quality {quality} < {threshold}")
    z.update(pick=side if not bad else "PASS",quality=quality,projection=round(proj,2),edge=round(edge,2),
             min_line=current,move=f"{move:+.1f}",fair_prob=round(pw*100,1),ev=round(mev,1) if mev is not None else None,
             odds=odds_,book=book,reason=(f"Independent model: recent offense/run prevention + starters + park; market only validates price" if not bad else "; ".join(bad)))
    return z

def analyze_live(m, s, mem, min_edge, threshold, min_ev, max_price):
    z = {
        "pick":"PASS","mode":"LIVE","quality":0,"projection":None,
        "edge":None,"min_line":None,"move":"N/A","fair_prob":None,
        "ev":None,"reason":"Chưa đủ dữ liệu"
    }
    bad = common_gates(m)
    if m.get("under") is None: bad.append("Không có giá Under")
    elif m["under"] < max_price: bad.append(f"Giá Under quá đắt ({m['under']})")
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

    fair = m.get("market_fair_under")
    market_ev = ev_pct(fair, m.get("under")) if fair is not None else None

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
        odds=m.get("under"),
        book=m.get("under_book",m.get("book","N/A")),
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

def settle_history():
    if not LOG.exists(): return
    try:
        h=pd.read_csv(LOG)
    except Exception:
        return
    changed=False
    for i,r in h.iterrows():
        if str(r.get("Result","")).strip() not in {"","nan","None"}: continue
        try: pk=int(r["Game PK"]); line=float(r["Line"]); odds_=float(r["Odds"]); pick=str(r["Pick"]).upper()
        except (ValueError,TypeError,KeyError): continue
        s0=live(pk)
        if not s0 or s0.get("is_live") or str(s0.get("status","")).lower() not in {"final","game over","completed early"}: continue
        total=float(s0["runs"]); result="PUSH" if abs(total-line)<1e-9 else ("WIN" if (pick=="UNDER" and total<line) or (pick=="OVER" and total>line) else "LOSS")
        dec=american_decimal(odds_) or 1
        profit=0.0 if result=="PUSH" else (dec-1 if result=="WIN" else -1.0)
        h.at[i,"Result"]=result; h.at[i,"Profit Units"]=round(profit,3); changed=True
    if changed:
        try: h.to_csv(LOG,index=False)
        except OSError: pass

st.title("⚾ MLB Edge AI Pro v24.1 — Independent Projection")
st.caption("INDEPENDENT RUN PROJECTION • UNDER / OVER / PASS • MARKET PRICE VALIDATION • STRICT TELEGRAM")
st.warning(
    "v24.1: Sportsbook KHÔNG quyết định hướng pick. Model baseball tạo projected total trước; odds chỉ kiểm tra value."
)

with st.sidebar:
    st.header("⚙️ V24.1 Control Center")
    day = st.date_input("Game date", datetime.now(TZ).date())
    auto = st.toggle(f"Auto refresh {REFRESH}s", True)
    pregame_threshold = st.slider("Pregame Quality threshold", 75, 94, 84)
    live_threshold = st.slider("Live Quality threshold", 78, 94, 86)
    min_ev = st.slider("Minimum model EV %", 0.0, 10.0, 2.0, .5)
    pregame_edge = st.slider("Minimum pregame model edge", .5, 2.0, .75, .25)
    min_edge = st.slider("Minimum live projection edge", .5, 2.5, 1.5, .5)
    max_price = st.slider("Worst acceptable Under odds", -130, -105, -115, 5)
    st.caption("Ví dụ -115: bot PASS giá -120/-125 cho cả UNDER lẫn OVER.")
    st.caption("Ít picks hơn, ưu tiên giá + EV. Không ép phải có bet.")
    st.divider()
    st.write("Odds API", "🟢 Connected" if ODDS_KEY else "🔴 Missing")
    st.write("Telegram", "🟢 Ready" if TG_TOKEN and TG_CHAT else "🔴 Missing")

games = games_for(day)
events = odds()

if games is None:
    st.error("Không tải được MLB schedule.")
    st.stop()

form = recent_team_form(day.isoformat())

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
        a = analyze_pregame(g, m, mem, form, pregame_threshold, min_ev, pregame_edge, max_price)

    verified = a["pick"] in {"UNDER","OVER"}
    row = {
        "Game": g["game"],
        "Mode": a["mode"],
        "Best Bet": f"{a['pick']} {m['total']}" if verified else "PASS",
        "Best Odds": a.get("odds") if verified else "N/A",
        "Book": a.get("book", m["book"]),
        "Model Win %": a["fair_prob"] if a["fair_prob"] is not None else "N/A",
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

settle_history()

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
            v[["Game","Mode","Best Bet","Best Odds","Book","Model Win %",
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
            df[["Game","Best Odds","Model Win %","EV %","Quality",
                "Move","Books","Reason"]],
            use_container_width=True, hide_index=True
        )
    st.info(
        "V24.1 tạo projection từ dữ liệu baseball trước. Sportsbook consensus không được dùng để chọn UNDER/OVER; odds chỉ dùng cho price/EV gate."
    )

with tabs[3]:
    if LOG.exists():
        try:
            history = pd.read_csv(LOG)
            settled = history[history["Result"].isin(["WIN","LOSS","PUSH"])] if "Result" in history else pd.DataFrame()
            if not settled.empty:
                risked = int((settled["Result"] != "PUSH").sum())
                profit = pd.to_numeric(settled["Profit Units"], errors="coerce").fillna(0).sum()
                roi = (profit/risked*100) if risked else 0
                x1,x2,x3=st.columns(3); x1.metric("Settled",len(settled)); x2.metric("Profit",f"{profit:+.2f}u"); x3.metric("ROI",f"{roi:+.1f}%")
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
sent = st.session_state.setdefault("sent_alerts_v241", set())
for g,m,s,mem,a in alert_candidates:
    key = f"{g['pk']}|{a['mode']}|{m['total']}|{m['under']}"
    if key in sent:
        continue

    score = f"{s['ar']}-{s['hr']}" if s else ""
    inning = f"{s['half']} {s['inn']}" if s and s["is_live"] else "Pregame"
    text = (
        f"⚾ MLB EDGE AI PRO V24.1\n"
        f"✅ VERIFIED {a['mode']}\n"
        f"{g['game']}\n"
        f"🎯 {a['pick']} {m['total']} @ {a.get('odds', m['under'])} ({a.get('book', m['book'])})\n"
        f"📊 Model Win: {a['fair_prob']}%\n"
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
            "Mode":a["mode"],"Pick":a["pick"],"Line":m["total"],"Odds":a.get("odds",m["under"]),
            "Quality":a["quality"],"Fair Under %":a["fair_prob"],"EV %":a["ev"],
            "Projection":a["projection"],"Edge":a["edge"],
            "Minimum Line":a["min_line"],
            "Opening":mem.get("opening") if mem else "",
            "Best Seen":mem.get("best") if mem else "",
            "Score":score,"Inning":inning,"Book":a.get("book",m["book"]),
            "Result":"","Profit Units":""
        }
        log_alert(record)

st.caption(
    "v24.1: pregame direction comes from independent baseball projection; sportsbook is price validation only. Live remains conservative and UNDER-only."
)
st.caption("Updated " + datetime.now(TZ).strftime("%Y-%m-%d %I:%M:%S %p %Z"))

if "api_error" in st.session_state:
    with st.expander("API diagnostics"):
        st.code(st.session_state["api_error"])

if auto:
    time.sleep(REFRESH)
    st.rerun()
