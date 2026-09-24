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
    if DAY_OVERRIDE:
        return int(DAY_OVERRIDE)
    today = datetime.now(TZ).date().isoformat()
    return EVENT_DATES.index(today) + 1 if today in EVENT_DATES else None


def current_lecture() -> int:
    raw = os.getenv("CURRENT_LECTURE") or os.getenv("LECTURE_OVERRIDE") or "1"
    try:
        lec = int(raw)
        return lec if lec in (1, 2) else 1
    except (ValueError, TypeError):
        return 1


def get_session_day(request: Request) -> int:
    val = request.session.get("active_day")
    if val is not None:
        try:
            return int(val)
        except (ValueError, TypeError):
            pass
    td = today_day()
    return td if td is not None else 1


def get_session_lecture(request: Request) -> int:
    val = request.session.get("active_lecture")
    if val is not None:
        try:
            lec = int(val)
            return lec if lec in (1, 2) else 1
        except (ValueError, TypeError):
            pass
    return current_lecture()


def get_day_label(d: int) -> str:
    if d - 1 < len(EVENT_DATES):
        date_str = EVENT_DATES[d - 1]
        suffix = " - Test Day" if date_str == "2026-09-24" else ""
        return f"Day {d} ({date_str}{suffix})"
    return f"Day {d}"


def display_name(p: dict) -> str:
    return p.get("name") or p["placeholder"]


def is_unique_violation(exc: Exception) -> bool:
    """Supabase/Postgrest raises on the (placeholder, day, lecture) primary key conflict.
    Match on the Postgres unique-violation code (23505) or the message text,
    since exact exception shape varies across supabase-py versions."""
    msg = str(getattr(exc, "message", "") or exc)
    code = getattr(exc, "code", "") or ""
    return "23505" in str(code) or "duplicate key" in msg.lower()


# ---------------------------------------------------------------- core logic (sync -- run via threadpool)
def mark_sync(code: str, volunteer: str, day: int, lecture: int) -> dict:
    ts = now_iso()

    res = sb.table("participants").select("*").eq("placeholder", code).execute()
    if not res.data:
        sb.table("scan_log").insert({"ts": ts, "code": code, "result": "unknown", "volunteer": volunteer}).execute()
        return {"status": "unknown"}

    p = res.data[0]
    base = {"name": display_name(p), "roll_no": p.get("roll_no") or "", "day": day, "lecture": lecture}
    if day is None:
        return {"status": "no_event_day", **base}

    try:
        sb.table("attendance").insert(
            {"placeholder": code, "day": day, "lecture": lecture, "marked_at": ts, "marked_by": volunteer}
        ).execute()
        status, at = "marked", ts
    except Exception as ex:
        if not is_unique_violation(ex):
            raise
        existing = (sb.table("attendance").select("marked_at")
                    .eq("placeholder", code).eq("day", day).eq("lecture", lecture).execute())
        status, at = "duplicate", (existing.data[0]["marked_at"] if existing.data else ts)

    sb.table("scan_log").insert({"ts": ts, "code": code, "result": status, "volunteer": volunteer}).execute()
    return {"status": status, "at": at, **base}


def stats_sync():
    total = len(sb.table("participants").select("placeholder").execute().data)
    att = sb.table("attendance").select("day,lecture").execute().data
    per_day_lec = Counter((r["day"], r.get("lecture", 1)) for r in att)
    unknown = len(sb.table("scan_log").select("id").eq("result", "unknown").execute().data)
    # Embedded select pulls the participant's name/roll_no in the same query via the FK.
    recent = (sb.table("attendance")
              .select("day,lecture,marked_at,marked_by,placeholder,participants(name,roll_no)")
              .order("marked_at", desc=True).limit(40).execute().data)
    return total, per_day_lec, unknown, recent


def export_rows_sync(n_days: int):
    participants = sb.table("participants").select("*").order("placeholder").execute().data
    att = sb.table("attendance").select("placeholder,day,lecture,marked_at").execute().data
    marks = defaultdict(dict)
    for r in att:
        marks[r["placeholder"]][(r["day"], r.get("lecture", 1))] = r["marked_at"]
    rows = []
    for p in participants:
        m = marks[p["placeholder"]]
        lec_cols = [m.get((d, l), "") for d in range(1, n_days + 1) for l in (1, 2)]
        rows.append([p["placeholder"], p.get("roll_no") or "", p.get("name") or ""] +
                    lec_cols + [len(m)])
    return rows


def manual_mark_sync(code: str, day: int, lecture: int, action: str, volunteer: str):
    res = sb.table("participants").select("placeholder,name").eq("placeholder", code).execute()
    if not res.data:
        return None
    p = res.data[0]
    if action == "unmark":
        sb.table("attendance").delete().eq("placeholder", code).eq("day", day).eq("lecture", lecture).execute()
        return f"Removed Day {day} Lecture {lecture} mark for {display_name(p)}"
    try:
        sb.table("attendance").insert(
            {"placeholder": code, "day": day, "lecture": lecture, "marked_at": now_iso(), "marked_by": volunteer}
        ).execute()
    except Exception as ex:
        if not is_unique_violation(ex):
            raise  # already marked -- fine, treat as a no-op
    return f"Marked {display_name(p)} present for Day {day} Lecture {lecture}"


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
.selector-bar{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0 14px;text-align:left}
.selector-group label{font-size:.82rem;font-weight:600;opacity:.8;display:block;margin-bottom:4px}
.selector-group select{margin:0;padding:10px 12px;font-size:1rem;background:#1e293b;color:#f8fafc;border:1px solid #475569;border-radius:8px;cursor:pointer;width:100%}
.selector-group select:focus{outline:2px solid #3b82f6}
.badge-bar{font-size:.92rem;background:#1e293b;padding:8px 14px;border-radius:20px;display:inline-block;margin-bottom:12px;border:1px solid #334155}
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
    day_val = r.get("day")
    lec_val = r.get("lecture", 1)
    day_str = f"Day {day_val}" if day_val is not None else ""
    lec_str = f"Lecture {lec_val}"
    if s == "marked":
        return page(f"<p class='big'>✓</p><h1>{e(r['name'])}</h1><p>{e(r['roll_no'])}</p>"
                    f"<h2>Marked present — {day_str}, {lec_str}</h2><p>{e(r['at'][11:19])}</p>"
                    f"<p><a href='/scan'>Scan mode</a></p>", "ok")
    if s == "duplicate":
        return page(f"<p class='big'>!</p><h1>{e(r['name'])}</h1><p>{e(r['roll_no'])}</p>"
                    f"<h2>Already marked — {day_str}, {lec_str}</h2><p>at {e(r['at'][11:19])}</p>", "warn")
    if s == "no_event_day":
        return page(f"<p class='big'>–</p><h1>Not an event day</h1><p>{e(r['name'])} was NOT marked.</p>", "grey")
    return page("<p class='big'>✗</p><h1>Unknown code</h1><p>This QR code is not registered.</p>", "bad")


# ---------------------------------------------------------------- routes: auth
@app.get("/", include_in_schema=False)
def root(request: Request):
    return RedirectResponse("/scan" if vol(request) else "/login", 303)


@app.get("/login", response_class=HTMLResponse)
def login_form(next: str = "/scan", err: str = ""):
    msg = f"<p style='color:#fca5a5'>{e(err)}</p>" if err else ""
    return page(f"<h2>Volunteer login</h2>{msg}<form method='post' action='/login'>"
                f"<input name='name' placeholder='Your name' required autocomplete='name'>"
                f"<input name='pin' type='password' inputmode='numeric' placeholder='PIN' required>"
                f"<input type='hidden' name='next' value='{e(next)}'><button>Log in</button></form>")


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
async def tap(code: str, request: Request):
    code = code.upper()
    if not vol(request):
        return RedirectResponse(f"/login?next=/t/{code}", 303)
    if not CODE_RE.match(code):
        return result_page({"status": "unknown"})
    day = get_session_day(request)
    lecture = get_session_lecture(request)
    r = await run_in_threadpool(mark_sync, code, vol(request), day, lecture)
    return result_page(r)


@app.post("/api/set-active")
async def set_active(request: Request):
    if not vol(request):
        return JSONResponse({"status": "auth"}, status_code=401)
    data = await request.json()
    try:
        day = int(data.get("day", 1))
        lec = int(data.get("lecture", 1))
        request.session["active_day"] = day
        request.session["active_lecture"] = 1 if lec not in (1, 2) else lec
        return {"ok": True, "day": request.session["active_day"], "lecture": request.session["active_lecture"]}
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)


@app.post("/api/mark")
async def api_mark(request: Request):
    if not vol(request):
        return JSONResponse({"status": "auth"}, status_code=401)
    data = await request.json()
    code = str(data.get("code", "")).upper()
    if not CODE_RE.match(code):
        return {"status": "unknown"}
    req_day = data.get("day")
    req_lec = data.get("lecture")
    try:
        day = int(req_day) if req_day is not None else get_session_day(request)
    except (ValueError, TypeError):
        day = get_session_day(request)
    try:
        lecture = int(req_lec) if req_lec is not None else get_session_lecture(request)
        if lecture not in (1, 2):
            lecture = 1
    except (ValueError, TypeError):
        lecture = get_session_lecture(request)
    request.session["active_day"] = day
    request.session["active_lecture"] = lecture
    return await run_in_threadpool(mark_sync, code, vol(request), day, lecture)


SCAN_HTML = """
<h2>Volunteer Scanner</h2>
<div class="selector-bar">
  <div class="selector-group">
    <label for="daySelect">Event Date / Day</label>
    <select id="daySelect" onchange="updateActiveSession()">
      __DAY_OPTIONS__
    </select>
  </div>
  <div class="selector-group">
    <label for="lecSelect">Lecture</label>
    <select id="lecSelect" onchange="updateActiveSession()">
      __LEC_OPTIONS__
    </select>
  </div>
</div>
<div class="badge-bar" id="activeBadge">
  Scanning for: <strong id="activeText">__ACTIVE_TEXT__</strong>
</div>
<video id="v" playsinline style="width:100%;border-radius:12px;background:#000"></video>
<canvas id="c" style="display:none"></canvas>
<div id="box" class="grey" style="border-radius:16px;padding:20px 12px;margin:12px 0">
  <p class="big" id="icon">📷</p><h1 id="who">Point at a QR code</h1><p id="sub"></p>
</div>
<p><a href="/logout">Log out</a> · Volunteer: __VOLUNTEER__</p>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jsqr/1.4.0/jsQR.js"></script>
<script>
const video=document.getElementById('v'),canvas=document.getElementById('c'),ctx=canvas.getContext('2d');
const box=document.getElementById('box'),who=document.getElementById('who'),sub=document.getElementById('sub'),icon=document.getElementById('icon');
const daySelect=document.getElementById('daySelect'),lecSelect=document.getElementById('lecSelect'),activeText=document.getElementById('activeText');
let last='',lastAt=0;
function show(cls,i,w,s){box.className=cls;icon.textContent=i;who.textContent=w;sub.textContent=s;
  if(navigator.vibrate)navigator.vibrate(cls==='ok'?80:[80,60,80]);}
function updateBadge(){
  const dText=daySelect.selectedOptions[0]?daySelect.selectedOptions[0].text:('Day '+daySelect.value);
  const lText=lecSelect.selectedOptions[0]?lecSelect.selectedOptions[0].text:('Lecture '+lecSelect.value);
  activeText.textContent=dText+' · '+lText;
}
async function updateActiveSession(){
  updateBadge();
  const day=parseInt(daySelect.value,10)||1;
  const lecture=parseInt(lecSelect.value,10)||1;
  try{
    await fetch('/api/set-active',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({day,lecture})
    });
  }catch(e){console.error('Failed to sync session',e);}
}
async function handle(code){
  const t=Date.now(); if(code===last&&t-lastAt<3000)return; last=code;lastAt=t;
  const day=parseInt(daySelect.value,10)||1;
  const lecture=parseInt(lecSelect.value,10)||1;
  try{
    const r=await fetch('/api/mark',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({code,day,lecture})
    });
    if(r.status===401){location='/login?next=/scan';return;}
    const d=await r.json();
    if(d.status==='marked')show('ok','✓',d.name,(d.roll_no?d.roll_no+' · ':'')+'marked Day '+d.day+', Lec '+d.lecture);
    else if(d.status==='duplicate')show('warn','!',d.name,'Already marked Day '+d.day+', Lec '+d.lecture+' at '+(d.at?d.at.slice(11,19):''));
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
def scan(request: Request):
    if not vol(request):
        return RedirectResponse("/login?next=/scan", 303)
    active_d = get_session_day(request)
    active_lec = get_session_lecture(request)
    n_days = len(EVENT_DATES) or 5
    day_options = []
    active_day_label = get_day_label(active_d)
    for d in range(1, n_days + 1):
        selected = " selected" if d == active_d else ""
        label = get_day_label(d)
        day_options.append(f"<option value='{d}'{selected}>{label}</option>")

    lec_options = []
    active_lec_label = f"Lecture {active_lec}"
    for l in (1, 2):
        selected = " selected" if l == active_lec else ""
        label = f"Lecture {l}"
        if l == active_lec:
            active_lec_label = label
        lec_options.append(f"<option value='{l}'{selected}>{label}</option>")

    html_str = (
        SCAN_HTML
        .replace("__DAY_OPTIONS__", "".join(day_options))
        .replace("__LEC_OPTIONS__", "".join(lec_options))
        .replace("__ACTIVE_TEXT__", f"{active_day_label} · {active_lec_label}")
        .replace("__VOLUNTEER__", e(vol(request)))
    )
    return page(html_str, title="Scan")


# ---------------------------------------------------------------- routes: admin
@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, msg: str = ""):
    if not is_admin(request):
        return RedirectResponse("/login?next=/admin", 303)
    n_days = len(EVENT_DATES) or 5
    total, per_day_lec, unknown, recent = await run_in_threadpool(stats_sync)
    stats = "".join(
        f"<tr><td>{get_day_label(d)}</td><td>L1: {per_day_lec.get((d, 1), 0)} / {total}</td><td>L2: {per_day_lec.get((d, 2), 0)} / {total}</td></tr>"
        for d in range(1, n_days + 1)
    )
    rows = "".join(
        f"<tr><td>{e((r.get('participants') or {}).get('name') or r['placeholder'])}</td>"
        f"<td>{e((r.get('participants') or {}).get('roll_no') or '')}</td>"
        f"<td>{e(r['placeholder'])}</td><td>D{r['day']}</td><td>L{r.get('lecture', 1)}</td>"
        f"<td>{e(r['marked_at'][11:19])}</td><td>{e(r['marked_by'])}</td></tr>"
        for r in recent
    )
    opts_day = "".join(f"<option value='{d}'>{get_day_label(d)}</option>" for d in range(1, n_days + 1))
    opts_lec = "<option value='1'>Lecture 1</option><option value='2'>Lecture 2</option>"
    note = f"<p style='color:#86efac'>{e(msg)}</p>" if msg else ""
    body = (
        f"<h2>Admin</h2>{note}"
        f"<p>Today = {get_day_label(today_day()) if today_day() else '—'} · Active = Lecture {current_lecture()} · registered: {total} · unknown scans: {unknown}</p>"
        f"<table><tr><th>Day</th><th>Lecture 1</th><th>Lecture 2</th></tr>{stats}</table>"
        f"<p><a href='/export.csv'>Download attendance CSV</a> · <a href='/scan'>Scan</a> · <a href='/logout'>Log out</a></p>"
        f"<h3>Manual mark / undo</h3>"
        f"<form method='post' action='/admin/manual'>"
        f"<input name='placeholder' placeholder='Placeholder e.g. P014' required>"
        f"<select name='day'>{opts_day}</select>"
        f"<select name='lecture'>{opts_lec}</select>"
        f"<select name='action'><option value='mark'>Mark present</option><option value='unmark'>Remove mark</option></select>"
        f"<button>Apply</button></form>"
        f"<h3>Latest 40 marks</h3>"
        f"<table><tr><th>Name</th><th>Roll</th><th>Code</th><th>Day</th><th>Lec</th><th>Time</th><th>By</th></tr>{rows}</table>"
    )
    return page(body, "admin")


@app.post("/admin/manual")
async def admin_manual(
    request: Request,
    placeholder: str = Form(...),
    day: int = Form(...),
    lecture: int = Form(1),
    action: str = Form(...),
):
    if not is_admin(request):
        return RedirectResponse("/login?next=/admin", 303)
    code = placeholder.strip().upper()
    msg = await run_in_threadpool(manual_mark_sync, code, day, lecture, action, vol(request))
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
    header = ["placeholder", "roll_no", "name"] + [f"day{d}_lec{l}" for d in range(1, n_days + 1) for l in (1, 2)] + ["total_lectures"]
    w.writerow(header)
    w.writerows(rows)
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=attendance.csv"})


@app.get("/healthz")
def healthz():
    return {"ok": True}
