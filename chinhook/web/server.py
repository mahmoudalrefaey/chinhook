"""The web server: the page, its assets, and the API behind them.

Run with: uvicorn chinhook.web.server:app (the Dockerfile does). One process: sessions live in
its memory (see chinhook/web/sessions.py).

The page is plain HTML, CSS and JavaScript under chinhook/web/static, served as files with no
build step. The brand images under the repository's static/ are served at /static.

Everything the API needs is imported inside a try, so a deployment that is missing something
(QDRANT_URL unset, say) still starts and says what is wrong on the page, instead of failing
to start and leaving nothing to look at but the platform's logs.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

WEB = Path(__file__).parent / "static"
BRAND = Path(__file__).resolve().parents[2] / "static"

# The largest request body accepted. The biggest legitimate one is an exported configuration
# file of a few hundred bytes; the limit leaves plenty of room above that.
MAX_BODY = 1_000_000

# vercel.json sends the same headers with the page when Vercel serves it: keep the two alike.
_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)

try:
    from chinhook.web.api import app
except Exception as exc:  # noqa: BLE001
    _startup_error = f"{type(exc).__name__}: {exc}"
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.api_route("/api/{path:path}", methods=["GET", "POST"])
    def _unavailable(path: str) -> JSONResponse:
        return JSONResponse(
            {
                "error": "This deployment could not start. Check its environment and logs.",
                "code": "startup",
                "detail": _startup_error,
            },
            status_code=503,
        )


@app.middleware("http")
async def _guard(request: Request, call_next):
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY:
        return JSONResponse({"error": "That request is too large."}, status_code=413)
    response = await call_next(request)
    headers = response.headers
    headers.setdefault("Content-Security-Policy", _CSP)
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "no-referrer")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.url.path.startswith("/api/"):
        headers.setdefault("Cache-Control", "no-store")
    return response


@app.get("/healthz", include_in_schema=False)
def _health() -> dict:
    # Liveness only: the process is up. The platform restarts the app when this fails, which
    # would not help when the problem is Qdrant, so Qdrant is checked by /readyz instead.
    return {"ok": True}


@app.get("/readyz", include_in_schema=False)
def _ready() -> JSONResponse:
    # Whether a visitor could use the app right now: it started, and its Qdrant answers. For an
    # uptime monitor; polling it also counts as use of a Qdrant Cloud free cluster, which is
    # suspended after a week without any. The reason is left to the logs, never shown here.
    try:
        from chinhook.db.clients import qdrant

        qdrant.get_collections()
    except Exception as exc:  # noqa: BLE001
        print(f"/readyz: not ready: {type(exc).__name__}: {exc}")
        return JSONResponse({"ok": False}, status_code=503)
    return JSONResponse({"ok": True})


@app.get("/", include_in_schema=False)
def _page() -> FileResponse:
    # Revalidated on every visit, so a new deployment's page is picked up straight away.
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/assets", StaticFiles(directory=WEB), name="assets")
app.mount("/static", StaticFiles(directory=BRAND), name="static")
