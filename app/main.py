import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.alerts import router as alerts_router
from app.auth import init_auth
from app.database import connect, init_db
from app.poller import start_polling, stop_polling, set_poll_interval, set_retention_days
from app.routes import router
from app.routes_auth import router as auth_router

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with connect() as db:
        await init_auth(db)
        # Load saved poll interval from DB
        cursor = await db.execute("SELECT key, value FROM settings")
        rows = await cursor.fetchall()
        for row in rows:
            if row[0] == "poll_interval":
                set_poll_interval(int(row[1]))
            elif row[0] == "retention_days":
                set_retention_days(int(row[1]))
    start_polling()
    yield
    await stop_polling()


app = FastAPI(title="AirTag Logger", lifespan=lifespan)


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    """State-changing API calls must carry X-Requested-With, which a cross-site form
    or <img> cannot set. SameSite=Lax already blocks cross-site cookies, but other
    *.bothari.com subdomains count as same-site, so this closes that gap."""
    if (request.method not in ("GET", "HEAD", "OPTIONS")
            and request.url.path.startswith("/api/")
            and request.headers.get("x-requested-with") != "fetch"):
        return JSONResponse({"detail": "Missing X-Requested-With header"}, status_code=403)
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        # Revalidate the app shell on every load (cheap: ETag -> 304), so a deploy
        # is picked up immediately instead of browsers running a stale app.js.
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


app.include_router(auth_router, prefix="/api")
app.include_router(router, prefix="/api")
app.include_router(alerts_router, prefix="/api")
app.mount("/", StaticFiles(directory="static", html=True), name="static")
