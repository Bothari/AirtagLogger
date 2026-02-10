import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import aiosqlite

from app.database import init_db, DB_PATH
from app.poller import start_polling, stop_polling, set_poll_interval
from app.routes import router

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Load saved poll interval from DB
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = 'poll_interval'")
        row = await cursor.fetchone()
        if row:
            set_poll_interval(int(row[0]))
    start_polling()
    yield
    await stop_polling()


app = FastAPI(title="AirTag Logger", lifespan=lifespan)
app.include_router(router, prefix="/api")
app.mount("/", StaticFiles(directory="static", html=True), name="static")
