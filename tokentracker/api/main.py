from __future__ import annotations

from importlib.resources import files

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from tokentracker.api.routes import router
from tokentracker.collector.config import get_settings
from tokentracker.collector.database import UsageDatabase
from tokentracker.collector.memory import release_memory


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Token Tracker", version="0.1.0")
    # One handle for every request: creating it runs the schema setup and migrations.
    app.state.database = UsageDatabase(settings.db_path, hostname=settings.hostname)
    app.include_router(router, prefix="/api")

    static_dir = files("tokentracker.dashboard") / "static"
    app.mount("/assets", StaticFiles(directory=str(static_dir)), name="assets")

    @app.middleware("http")
    async def revalidate_dashboard_files(request: Request, call_next):
        # Without this, browsers may reuse an old app.js or styles.css after an
        # upgrade. "no-cache" still allows a cheap 304 when nothing changed.
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    in_flight = 0

    @app.middleware("http")
    async def release_memory_when_idle(request: Request, call_next):
        # Query results are freed after each response, but glibc keeps the pages.
        nonlocal in_flight
        in_flight += 1
        try:
            return await call_next(request)
        finally:
            in_flight -= 1
            if in_flight == 0:
                release_memory()

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
