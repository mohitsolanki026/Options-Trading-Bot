"""
The dashboard server, running inside the bot process.

It is deliberately not a separate service. The things a useful dashboard has to
show — open positions, live tick prices, why the gate refused an entry, whether
the feed is connected — exist only in this process's memory. A separate service
could read the JSON and SQLite files and would show a stale, partial picture
with no way to stop anything.

It runs on a daemon thread beside the monitor and tick loops, binds to loopback
by default, and never mutates trading state directly: see ``web/api.py``.
"""

import logging
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from config.settings import WEB_UI_HOST, WEB_UI_PORT
from web import auth
from web.api import router as api_router

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"

# Paths reachable without a token. Everything else needs one.
PUBLIC_PATHS = {"/login", "/api/login", "/api/logout", "/favicon.ico", "/healthz"}

LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Theta Desk</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700;12..96,800&family=Manrope:wght@400;600;700&display=swap">
<style>
  :root{color-scheme:dark}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;
    font-family:Manrope,system-ui,sans-serif;color:#EDF4F8;
    background:radial-gradient(900px 600px at 20% 0%,rgba(62,233,255,.16),transparent 60%),
      radial-gradient(700px 500px at 85% 10%,rgba(57,245,196,.12),transparent 62%),#05080D}
  .card{width:min(400px,100%);padding:30px;border-radius:18px;
    background:linear-gradient(180deg,rgba(255,255,255,.06),rgba(255,255,255,.02));
    border:1px solid rgba(255,255,255,.09);backdrop-filter:blur(20px);
    box-shadow:0 0 0 1px rgba(62,233,255,.18),0 0 40px rgba(62,233,255,.12)}
  h1{font-family:"Bricolage Grotesque",Manrope,sans-serif;font-size:25px;margin:0 0 6px;
    letter-spacing:-.035em;background:linear-gradient(96deg,#3EE9FF,#39F5C4);
    -webkit-background-clip:text;background-clip:text;color:transparent}
  p{margin:0 0 20px;font-size:13.5px;color:#9EB3C2;line-height:1.5}
  label{display:block;font-size:10.5px;font-weight:700;letter-spacing:.12em;
    text-transform:uppercase;color:#68808F;margin-bottom:7px}
  input{width:100%;padding:12px 13px;border-radius:11px;font:inherit;font-size:15px;
    background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.16);color:#EDF4F8}
  input:focus{outline:2px solid #3EE9FF;outline-offset:2px}
  button{width:100%;margin-top:14px;padding:12px;border:0;border-radius:11px;font:inherit;
    font-weight:700;font-size:15px;cursor:pointer;color:#03131A;
    background:linear-gradient(96deg,#3EE9FF,#39F5C4);box-shadow:0 0 26px rgba(62,233,255,.3)}
  .err{margin-top:13px;font-size:13px;color:#FF5C7A;min-height:18px}
  .hint{margin-top:18px;font-size:12px;color:#68808F;line-height:1.5}
  code{font-family:ui-monospace,monospace;color:#9EB3C2}
</style></head><body>
<form class="card" onsubmit="go(event)">
  <h1>Theta Desk</h1>
  <p>Enter the access token to open your trading dashboard.</p>
  <label for="t">Access token</label>
  <input id="t" type="password" autocomplete="current-password" autofocus>
  <button type="submit">Open dashboard</button>
  <div class="err" id="e"></div>
  <p class="hint">The token is in <code>data/web_token.txt</code> on the machine
  running the bot, or whatever you set as <code>WEB_UI_TOKEN</code>.</p>
</form>
<script>
async function go(ev){
  ev.preventDefault();
  const e = document.getElementById('e'); e.textContent = '';
  const r = await fetch('/api/login', {method:'POST', headers:{'content-type':'application/json'},
    body: JSON.stringify({token: document.getElementById('t').value})});
  if(r.ok){ location.href = '/'; }
  else { e.textContent = 'That token is not right. Check the file and try again.'; }
}
</script></body></html>"""


def create_app() -> FastAPI:
    app = FastAPI(title="Theta Desk", docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)
        if auth.request_ok(request):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Sign in to use the dashboard."},
                                status_code=401)
        return RedirectResponse("/login", status_code=303)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        return HTMLResponse(LOGIN_PAGE)

    @app.post("/api/login")
    async def login(payload: dict):
        if not auth.token_ok((payload or {}).get("token", "")):
            return JSONResponse({"detail": "Wrong token."}, status_code=401)
        response = JSONResponse({"ok": True})
        response.set_cookie(
            auth.COOKIE_NAME, auth.get_token(),
            httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30, path="/",
        )
        return response

    @app.post("/api/logout")
    async def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(auth.COOKIE_NAME, path="/")
        return response

    app.include_router(api_router)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        if not INDEX_FILE.exists():
            return HTMLResponse(
                "<h1>Dashboard files are missing</h1>"
                "<p>Expected web/static/index.html next to the server.</p>",
                status_code=500)
        return HTMLResponse(INDEX_FILE.read_text(encoding="utf-8"))

    return app


def start_web_server(host: str = None, port: int = None) -> threading.Thread:
    """
    Run the dashboard on a daemon thread. Returns the thread, or None if the
    server could not start — a dashboard failure must never stop the bot.
    """
    import uvicorn

    host = host or WEB_UI_HOST
    port = int(port or WEB_UI_PORT)

    config = uvicorn.Config(create_app(), host=host, port=port,
                            log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    # Signal handlers can only be installed on the main thread, and the bot
    # owns that; without this uvicorn raises on startup.
    server.install_signal_handlers = lambda: None

    thread = threading.Thread(target=server.run, daemon=True, name="WebUI")
    thread.start()

    where = f"http://{host}:{port}"
    if auth.auth_required():
        logger.info(f"🖥️ Dashboard on {where} — token in {auth.TOKEN_FILE}")
    else:
        logger.warning(f"🖥️ Dashboard on {where} with NO login (WEB_UI_AUTH=off)")
    return thread
