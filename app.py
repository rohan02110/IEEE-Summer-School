"""QR-code event attendance, backed by Supabase (Postgres) instead of SQLite.

Run schema.sql in your Supabase project's SQL editor FIRST, then:
  pip install -r requirements.txt
  cp .env.example .env   # fill in SUPABASE_URL, SUPABASE_KEY (service_role), etc.
  set -a; source .env; set +a
  python load_participants.py     # once participants.csv has data
  uvicorn app:app --host 0.0.0.0 --port 8000

Run:  uvicorn app:app --host 0.0.0.0 --port 8000
"""
import csv
import html
import io
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime
from hmac import compare_digest
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware
from supabase import Client, create_client

# ---------------------------------------------------------------- config
def _load_env():
    for fpath in (".env", ".env.example"):
        if os.path.exists(fpath):
            with open(fpath, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip("'\"")
                        if k not in os.environ:
                            os.environ[k] = v

_load_env()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").replace("/rest/v1/", "").replace("/rest/v1", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")  # service_role key -- server-side only, never expose to a browser
SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
VOLUNTEER_PIN = os.getenv("VOLUNTEER_PIN", "1234")
ADMIN_PIN = os.getenv("ADMIN_PIN", "999999")
EVENT_DATES = [d.strip() for d in os.getenv("EVENT_DATES", "").split(",") if d.strip()]
DAY_OVERRIDE = os.getenv("DAY_OVERRIDE")  # testing only
TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Kolkata"))
CODE_RE = re.compile(r"^P\d{3}$")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in the environment or .env file.")

sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def now_iso() -> str:
    return datetime.now(TZ).isoformat()


def today_day():
    day_override = os.getenv("DAY_OVERRIDE", DAY_OVERRIDE)
    if day_override:
        return int(day_override)
    event_dates = [d.strip() for d in os.getenv("EVENT_DATES", "").split(",") if d.strip()] or EVENT_DATES
    today = datetime.now(TZ).date().isoformat()
    return event_dates.index(today) + 1 if today in event_dates else None


def display_name(p: dict) -> str:
    return p.get("name") or p["placeholder"]


def is_unique_violation(exc: Exception) -> bool:
    """Supabase/Postgrest raises on the (placeholder, day) primary key conflict.
    Match on the Postgres unique-violation code (23505) or the message text,
    since exact exception shape varies across supabase-py versions."""
    msg = str(getattr(exc, "message", "") or exc)
    code = getattr(exc, "code", "") or ""
    return "23505" in str(code) or "duplicate key" in msg.lower()


# ---------------------------------------------------------------- core logic (sync -- run via threadpool)
def mark_sync(code: str, volunteer: str, session: int = 1) -> dict:
    day = today_day()
    ts = now_iso()
    session = 2 if session == 2 else 1

    res = sb.table("participants").select("*").eq("placeholder", code).execute()
    if not res.data:
        sb.table("scan_log").insert({"ts": ts, "code": code, "result": "unknown", "volunteer": volunteer}).execute()
        return {"status": "unknown"}

    p = res.data[0]
    base = {"name": display_name(p), "roll_no": p.get("roll_no") or "", "day": day, "session": session}
    if day is None:
        return {"status": "no_event_day", **base}

    try:
        sb.table("attendance").insert(
            {"placeholder": code, "day": day, "session": session, "marked_at": ts, "marked_by": volunteer}
        ).execute()
        status, at = "marked", ts
    except Exception as ex:
        if not is_unique_violation(ex):
            raise
        existing = (sb.table("attendance").select("marked_at")
                    .eq("placeholder", code).eq("day", day).eq("session", session).execute())
        status, at = "duplicate", (existing.data[0]["marked_at"] if existing.data else ts)

    sb.table("scan_log").insert({"ts": ts, "code": code, "result": status, "volunteer": volunteer}).execute()
    return {"status": status, "at": at, **base}


def stats_sync():
    total = len(sb.table("participants").select("placeholder").execute().data)
    att = sb.table("attendance").select("day,session").execute().data
    per_day_session = Counter((r["day"], r.get("session", 1)) for r in att)
    unknown = len(sb.table("scan_log").select("id").eq("result", "unknown").execute().data)
    # Embedded select pulls the participant's name/roll_no in the same query via the FK.
    recent = (sb.table("attendance")
              .select("day,session,marked_at,marked_by,placeholder,participants(name,roll_no)")
              .order("marked_at", desc=True).limit(40).execute().data)
    return total, per_day_session, unknown, recent


def export_rows_sync(n_days: int):
    participants = sb.table("participants").select("*").order("placeholder").execute().data
    att = sb.table("attendance").select("placeholder,day,session,marked_at").execute().data
    marks = defaultdict(dict)
    for r in att:
        marks[r["placeholder"]][(r["day"], r.get("session", 1))] = r["marked_at"]
    rows = []
    for p in participants:
        m = marks[p["placeholder"]]
        rows.append([p["placeholder"], p.get("roll_no") or "", p.get("name") or ""] +
                    [m.get((d, s), "") for d in range(1, n_days + 1) for s in (1, 2)] + [len(m)])
    return rows


def manual_mark_sync(code: str, day: int, session: int, action: str, volunteer: str):
    session = 2 if session == 2 else 1
    res = sb.table("participants").select("placeholder,name").eq("placeholder", code).execute()
    if not res.data:
        return None
    p = res.data[0]
    if action == "unmark":
        sb.table("attendance").delete().eq("placeholder", code).eq("day", day).eq("session", session).execute()
        return f"Removed Day {day} - Lecture {session} mark for {display_name(p)}"
    try:
        sb.table("attendance").insert(
            {"placeholder": code, "day": day, "session": session, "marked_at": now_iso(), "marked_by": volunteer}
        ).execute()
    except Exception as ex:
        if not is_unique_violation(ex):
            raise  # already marked -- fine, treat as a no-op
    return f"Marked {display_name(p)} present for Day {day} - Lecture {session}"


# ---------------------------------------------------------------- auth
FAILS = defaultdict(list)


def rate_limited(ip: str) -> bool:
    t = time.time()
    FAILS[ip] = [x for x in FAILS[ip] if t - x < 300]
    return len(FAILS[ip]) >= 8


def safe_next(n: str) -> str:
    return n if n.startswith("/") and not n.startswith("//") else "/scan"


def vol(request: Request):
    return request.session.get("vol")


def is_admin(request: Request):
    return bool(request.session.get("admin"))


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=60 * 60 * 16,
                   same_site="lax", https_only=os.getenv("HTTPS_ONLY_COOKIES", "0") == "1")

CSS = """
*{box-sizing:border-box}body{margin:0;font-family:system-ui,sans-serif;background:#0f172a;color:#f8fafc;
min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:20px;text-align:center}
.card{width:100%;max-width:440px}h1{font-size:2.2rem;margin:.2em 0}h2{margin:.2em 0}p{font-size:1.1rem;opacity:.9}
input,select,button{font-size:1.1rem;padding:14px;border-radius:10px;border:0;width:100%;margin:6px 0}
button{background:#2563eb;color:#fff;font-weight:600}a{color:#93c5fd}
.big{font-size:4rem;margin:0}.ok{background:#15803d}.warn{background:#b45309}.bad{background:#b91c1c}
.grey{background:#334155}table{width:100%;border-collapse:collapse;text-align:left;font-size:.95rem}
td,th{padding:6px;border-bottom:1px solid #334155}.admin{align-items:stretch;justify-content:flex-start;text-align:left}
.admin .card{max-width:900px;margin:auto}
"""


def page(body: str, cls: str = "", title: str = "Attendance") -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' "
        f"content='width=device-width,initial-scale=1'><title>{title}</title><style>{CSS}</style></head>"
        f"<body class='{cls}'><div class='card'>{body}</div></body></html>")


def e(x) -> str:
    return html.escape(str(x))


def result_page(r: dict) -> HTMLResponse:
    s = r["status"]
    day = r.get("day")
    session = r.get("session", 1)
    if s == "marked":
        return page(f"<p class='big'>✓</p><h1>{e(r['name'])}</h1><p>{e(r['roll_no'])}</p>"
                    f"<h2>Marked present — Day {day}, Lecture {session}</h2><p>{e(r['at'][11:19])}</p>"
                    f"<p><a href='/scan?session={session}'>Scan mode</a></p>", "ok")
    if s == "duplicate":
        return page(f"<p class='big'>!</p><h1>{e(r['name'])}</h1><p>{e(r['roll_no'])}</p>"
                    f"<h2>Already marked — Day {day}, Lecture {session}</h2><p>at {e(r['at'][11:19])}</p>"
                    f"<p><a href='/scan?session={session}'>Scan mode</a></p>", "warn")
    if s == "no_event_day":
        return page(f"<p class='big'>–</p><h1>Not an event day</h1><p>{e(r['name'])} was NOT marked.</p>"
                    f"<p><a href='/scan?session={session}'>Scan mode</a></p>", "grey")
    return page("<p class='big'>✗</p><h1>Unknown code</h1><p>This QR code is not registered.</p>", "bad")


# ---------------------------------------------------------------- routes: auth
@app.get("/", include_in_schema=False)
def root(request: Request):
    return RedirectResponse("/scan" if vol(request) else "/login", 303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/scan", err: str = ""):
    current_vol = vol(request)
    current_info = ""
    if current_vol:
        role = "Admin" if is_admin(request) else "Volunteer"
        current_info = (f"<div style='background:#1e293b;border:1px solid #334155;border-radius:10px;padding:12px;margin:12px 0;font-size:0.95rem'>"
                        f"Currently logged in as <b>{e(current_vol)}</b> ({role})<br style='margin-bottom:6px'>"
                        f"<a href='/scan'>Go to Scanner</a> · <a href='/admin'>Admin</a> · <a href='/logout' style='color:#f87171'>Log out</a></div>")
    msg = f"<p style='color:#fca5a5'>{e(err)}</p>" if err else ""
    return page(f"<h2>Volunteer Login</h2>{current_info}{msg}<form method='post' action='/login'>"
                f"<input name='name' placeholder='Your name' required autocomplete='name'>"
                f"<input name='pin' type='password' inputmode='numeric' placeholder='PIN' required>"
                f"<input type='hidden' name='next' value='{e(next)}'><button>Log in</button></form>"
                f"<p style='margin-top:16px;font-size:0.95rem'><a href='/scan'>Scanner</a> · <a href='/admin'>Admin</a></p>",
                title="Volunteer Login")


@app.post("/login")
def login(request: Request, name: str = Form(...), pin: str = Form(...), next: str = Form("/scan")):
    ip = request.client.host if request.client else "?"
    if rate_limited(ip):
        return RedirectResponse(f"/login?err=Too+many+attempts,+wait+5+min&next={e(next)}", 303)
    name = name.strip()[:40] or "volunteer"
    if compare_digest(pin, ADMIN_PIN):
        request.session.update(vol=name, admin=True)
    elif compare_digest(pin, VOLUNTEER_PIN):
        request.session.update(vol=name)
    else:
        FAILS[ip].append(time.time())
        return RedirectResponse(f"/login?err=Wrong+PIN&next={e(next)}", 303)
    return RedirectResponse(safe_next(next), 303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


# ---------------------------------------------------------------- routes: scanning
@app.get("/t/{code}")
async def tap(code: str, request: Request, session: int = 1):
    code = code.upper()
    session = 2 if session == 2 else 1
    if not vol(request):
        return RedirectResponse(f"/login?next=/t/{code}?session={session}", 303)
    if not CODE_RE.match(code):
        return result_page({"status": "unknown"})
    r = await run_in_threadpool(mark_sync, code, vol(request), session)
    return result_page(r)


@app.post("/api/mark")
async def api_mark(request: Request):
    if not vol(request):
        return JSONResponse({"status": "auth"}, status_code=401)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "invalid_json"}, status_code=400)
    code = str(data.get("code", "")).upper()
    session_val = data.get("session", 1)
    try:
        session = int(session_val)
        if session not in (1, 2):
            return JSONResponse({"status": "invalid_session", "error": "session must be 1 or 2"}, status_code=400)
    except (ValueError, TypeError):
        return JSONResponse({"status": "invalid_session", "error": "session must be 1 or 2"}, status_code=400)

    if not CODE_RE.match(code):
        return {"status": "unknown"}
    return await run_in_threadpool(mark_sync, code, vol(request), session)


SCAN_HTML = """
<div style="display:flex;gap:8px;margin-bottom:12px">
  <button type="button" id="btn-l1" onclick="setSession(1)" style="flex:1;background:#2563eb;color:#fff;font-weight:600;padding:12px;border-radius:10px;border:0;cursor:pointer">Lecture 1</button>
  <button type="button" id="btn-l2" onclick="setSession(2)" style="flex:1;background:#334155;color:#fff;font-weight:600;padding:12px;border-radius:10px;border:0;cursor:pointer">Lecture 2</button>
</div>
<h2>Camera scan</h2>
<video id="v" playsinline style="width:100%;border-radius:12px;background:#000"></video>
<canvas id="c" style="display:none"></canvas>
<div id="box" class="grey" style="border-radius:16px;padding:20px 12px;margin:12px 0">
  <p class="big" id="icon">📷</p><h1 id="who">Point at a QR code</h1><p id="sub">Active: Lecture <span id="cur-lec">1</span></p>
</div>
<p style="font-size:0.95rem;line-height:1.6">
  Volunteer: <b>__VOL_NAME__</b> (<a href="/login">Switch</a> · <a href="/logout">Log out</a>)<br>
  Day: <b>__DAY__</b> · Active Lecture: <span id="lbl-lec">1</span> · <a href="/admin">Admin</a>
</p>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jsqr/1.4.0/jsQR.js"></script>
<script>
const video=document.getElementById('v'),canvas=document.getElementById('c'),ctx=canvas.getContext('2d');
const box=document.getElementById('box'),who=document.getElementById('who'),sub=document.getElementById('sub'),icon=document.getElementById('icon');
const btnL1=document.getElementById('btn-l1'),btnL2=document.getElementById('btn-l2');
const curLec=document.getElementById('cur-lec'),lblLec=document.getElementById('lbl-lec');

let currentSession = parseInt(new URLSearchParams(location.search).get('session') || '__INIT_SESSION__');
if (currentSession !== 1 && currentSession !== 2) currentSession = 1;

function updateSessionUI(){
  if(currentSession === 1){
    btnL1.style.background='#2563eb';
    btnL2.style.background='#334155';
  } else {
    btnL1.style.background='#334155';
    btnL2.style.background='#2563eb';
  }
  if(curLec) curLec.textContent = currentSession;
  if(lblLec) lblLec.textContent = currentSession;
}
function setSession(s){
  currentSession = (s === 2) ? 2 : 1;
  updateSessionUI();
  const url = new URL(window.location);
  url.searchParams.set('session', currentSession);
  window.history.replaceState({}, '', url);
}
updateSessionUI();

let last='',lastAt=0;
function show(cls,i,w,s){box.className=cls;icon.textContent=i;who.textContent=w;sub.textContent=s;
  if(navigator.vibrate)navigator.vibrate(cls==='ok'?80:[80,60,80]);}
async function handle(code){
  const t=Date.now(); if(code===last&&t-lastAt<3000)return; last=code;lastAt=t;
  try{
    const r=await fetch('/api/mark',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({code, session: currentSession})
    });
    if(r.status===401){location='/login?next=' + encodeURIComponent('/scan?session=' + currentSession);return;}
    const d=await r.json();
    if(d.status==='marked')show('ok','✓',d.name,d.roll_no+' · marked Day '+d.day+' (Lecture '+d.session+')');
    else if(d.status==='duplicate')show('warn','!',d.name,'Already marked Day '+d.day+' (Lecture '+d.session+') at '+d.at.slice(11,19));
    else if(d.status==='no_event_day')show('grey','–','Not an event day','NOT marked');
    else show('bad','✗','Unknown code','Not registered');
  }catch(err){show('bad','✗','Network error','Try again');}
}
function tick(){
  if(video.readyState===video.HAVE_ENOUGH_DATA){
    canvas.width=video.videoWidth;canvas.height=video.videoHeight;
    ctx.drawImage(video,0,0,canvas.width,canvas.height);
    const img=ctx.getImageData(0,0,canvas.width,canvas.height);
    const code=jsQR(img.data,img.width,img.height);
    if(code){const m=code.data.match(/\\/t\\/([A-Za-z0-9]{4,10})/i); if(m)handle(m[1].toUpperCase());}
  }
  requestAnimationFrame(tick);
}
navigator.mediaDevices.getUserMedia({video:{facingMode:'environment'}}).then(s=>{
  video.srcObject=s;video.play();requestAnimationFrame(tick);
}).catch(err=>{sub.textContent='Camera error: '+err+' — use your phone camera app to scan instead.';});
</script>
"""


@app.get("/scan", response_class=HTMLResponse)
def scan(request: Request, session: int = 1):
    session = 2 if session == 2 else 1
    v = vol(request)
    if not v:
        return RedirectResponse(f"/login?next=/scan?session={session}", 303)
    d = today_day()
    html_content = (SCAN_HTML
                    .replace("__DAY__", str(d) if d else "none today")
                    .replace("__INIT_SESSION__", str(session))
                    .replace("__VOL_NAME__", e(v)))
    return page(html_content, title="Scan")


# ---------------------------------------------------------------- routes: admin
@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, msg: str = ""):
    if not is_admin(request):
        return RedirectResponse("/login?next=/admin", 303)
    n_days = len(EVENT_DATES) or 5
    total, per_day_session, unknown, recent = await run_in_threadpool(stats_sync)
    stats = "".join(
        f"<tr><td>Day {d} - Lecture 1</td><td>{per_day_session.get((d, 1), 0)} / {total}</td></tr>"
        f"<tr><td>Day {d} - Lecture 2</td><td>{per_day_session.get((d, 2), 0)} / {total}</td></tr>"
        for d in range(1, n_days + 1)
    )
    rows = "".join(
        f"<tr><td>{e((r.get('participants') or {}).get('name') or r['placeholder'])}</td>"
        f"<td>{e((r.get('participants') or {}).get('roll_no') or '')}</td>"
        f"<td>{e(r['placeholder'])}</td><td>D{r['day']}</td><td>L{r.get('session', 1)}</td>"
        f"<td>{e(r['marked_at'][11:19])}</td><td>{e(r['marked_by'])}</td></tr>" for r in recent)
    opts = "".join(f"<option value='{d}'>Day {d}</option>" for d in range(1, n_days + 1))
    note = f"<p style='color:#86efac'>{e(msg)}</p>" if msg else ""
    body = (f"<h2>Admin</h2>{note}<p>Today = Day {today_day() or '—'} · registered: {total} · unknown scans: {unknown}</p>"
            f"<table>{stats}</table><p><a href='/export.csv'>Download attendance CSV</a> · <a href='/scan'>Scan</a> · "
            f"<a href='/logout'>Log out</a></p><h3>Manual mark / undo</h3>"
            f"<form method='post' action='/admin/manual'><input name='placeholder' placeholder='Placeholder e.g. P014' required>"
            f"<select name='day'>{opts}</select>"
            f"<select name='session'><option value='1'>Lecture 1</option><option value='2'>Lecture 2</option></select>"
            f"<select name='action'><option value='mark'>Mark present</option>"
            f"<option value='unmark'>Remove mark</option></select><button>Apply</button></form>"
            f"<h3>Latest 40 marks</h3><table><tr><th>Name</th><th>Roll</th><th>Code</th><th>Day</th><th>Lecture</th><th>Time</th><th>By</th></tr>{rows}</table>")
    return page(body, "admin")


@app.post("/admin/manual")
async def admin_manual(request: Request, placeholder: str = Form(...), day: int = Form(...), session: int = Form(1), action: str = Form(...)):
    if not is_admin(request):
        return RedirectResponse("/login?next=/admin", 303)
    code = placeholder.strip().upper()
    session = 2 if session == 2 else 1
    msg = await run_in_threadpool(manual_mark_sync, code, day, session, action, vol(request))
    if msg is None:
        return RedirectResponse("/admin?msg=Placeholder+not+found", 303)
    return RedirectResponse("/admin?msg=" + msg.replace(" ", "+"), 303)


@app.get("/export.csv")
async def export_csv(request: Request):
    if not is_admin(request):
        return RedirectResponse("/login?next=/export.csv", 303)
    n_days = len(EVENT_DATES) or 5
    rows = await run_in_threadpool(export_rows_sync, n_days)
    out = io.StringIO()
    w = csv.writer(out)
    header = ["placeholder", "roll_no", "name"]
    for d in range(1, n_days + 1):
        header.extend([f"day{d}_L1", f"day{d}_L2"])
    header.append("sessions_attended")
    w.writerow(header)
    w.writerows(rows)
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=attendance.csv"})


@app.get("/healthz")
def healthz():
    return {"ok": True}
