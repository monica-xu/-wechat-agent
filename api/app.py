"""FastAPI application entry point with CORS, startup/shutdown hooks."""

import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_SESSION_ID = os.getenv("DEFAULT_SESSION_ID", "default")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup: init DB, start scheduler. Shutdown: stop scheduler."""
    logger.info("=== WeChat Agent starting ===")

    # Init database
    from db._base import init_db
    init_db()
    logger.info("Database initialized")

    # Start scheduler
    from infra.scheduler import start_scheduler, stop_scheduler
    start_scheduler(DEFAULT_SESSION_ID)
    logger.info("Scheduler started")

    yield

    # Shutdown
    stop_scheduler()
    logger.info("=== WeChat Agent stopped ===")


app = FastAPI(
    title="WeChat Article Agent",
    description="AI-powered WeChat Official Account content publishing agent",
    version="1.0.0",
    lifespan=_lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Import routes
from api.routes import agent, articles, config, webhook, dashboard  # noqa: E402
app.include_router(agent.router, prefix="/api", tags=["agent"])
app.include_router(articles.router, prefix="/api", tags=["articles"])
app.include_router(config.router, prefix="/api", tags=["config"])
app.include_router(webhook.router, prefix="/api", tags=["webhook"])
app.include_router(dashboard.router, prefix="/api", tags=["dashboard"])

# Serve dashboard static files
from fastapi.responses import HTMLResponse
_static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_static_dir, exist_ok=True)
# Cache the dashboard HTML in memory for fast response
_DASHBOARD_HTML: str = ""
def _load_dashboard_html() -> str:
    global _DASHBOARD_HTML
    if not _DASHBOARD_HTML:
        _html_path = os.path.join(_static_dir, "index.html")
        if os.path.exists(_html_path):
            with open(_html_path, "r", encoding="utf-8") as f:
                _DASHBOARD_HTML = f.read()
    return _DASHBOARD_HTML

@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def serve_dashboard():
    return _load_dashboard_html()

@app.get("/dashboard/", response_class=HTMLResponse, include_in_schema=False)
async def serve_dashboard_slash():
    return _load_dashboard_html()


@app.get("/")
async def root():
    return {"service": "WeChat Article Agent", "status": "running", "version": "1.0.0"}
