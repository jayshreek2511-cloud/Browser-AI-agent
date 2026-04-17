from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import sys

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router as api_router
from app.core.config import get_settings
from app.core.database import init_db
from app.core.logging import configure_logging


if sys.platform.startswith("win"):
    # Required for Playwright subprocess startup in Windows child processes (e.g. reload workers).
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    configure_logging(settings.app_env)
    await init_db()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.include_router(api_router)
    ui_directory = Path(__file__).resolve().parent / "ui"
    app.mount("/ui", StaticFiles(directory=str(ui_directory)), name="ui")
    return app


app = create_app()
