from __future__ import annotations

from importlib.resources import files

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from tokentracker.api.routes import router
from tokentracker.collector.config import get_settings
from tokentracker.collector.database import UsageDatabase


def create_app() -> FastAPI:
    settings = get_settings()
    UsageDatabase(settings.db_path)
    app = FastAPI(title="Token Tracker", version="0.1.0")
    app.include_router(router, prefix="/api")

    static_dir = files("tokentracker.dashboard") / "static"
    app.mount("/assets", StaticFiles(directory=str(static_dir)), name="assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(static_dir / "index.html"))

    return app


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run("tokentracker.api.main:app", host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
