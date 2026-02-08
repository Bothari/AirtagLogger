import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.database import init_db
from app.poller import start_polling, stop_polling
from app.routes import router

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    start_polling()
    yield
    await stop_polling()


app = FastAPI(title="AirTag Logger", lifespan=lifespan)
app.include_router(router, prefix="/api")
app.mount("/", StaticFiles(directory="static", html=True), name="static")
