"""
Web server: dashboard, API, and the CSP upload portal.

Background jobs are NOT started here; they run in `python -m app.worker`,
so running several uvicorn workers can't duplicate them.
"""
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .auth import require_admin, warn_if_open
from .db import engine
from .logging_config import configure_logging
from .scheduler import get_scheduler_status
from .api import agent, csp, agreements, documents, ingest, review, campaigns, dashboard, hub, portal

configure_logging()
logger = logging.getLogger(__name__)
WEB = Path(__file__).resolve().parent / "web"

app = FastAPI(title="Eko CSP Renewal Agent")
warn_if_open()

admin = [Depends(require_admin)]

# Public: the CSP upload portal (token-protected) and health checks.
app.include_router(portal.router, tags=["portal"])
# The WhatsApp agent's own API (X-Agent-Key).
app.include_router(agent.router, prefix="/api/agent", tags=["whatsapp-agent"])
# Everything else needs X-API-Key.
app.include_router(hub.router, prefix="/api/hub", tags=["dashboard"])
app.include_router(dashboard.router, tags=["legacy-dashboard"], dependencies=admin)
app.include_router(csp.router, prefix="/api/csp", tags=["csp"], dependencies=admin)
app.include_router(agreements.router, prefix="/api/agreements", tags=["agreements"], dependencies=admin)
app.include_router(documents.router, prefix="/api/documents", tags=["documents"], dependencies=admin)
app.include_router(ingest.router, prefix="/api/ingest", tags=["ingest"], dependencies=admin)
app.include_router(review.router, prefix="/api/review", tags=["review"], dependencies=admin)
app.include_router(campaigns.router, prefix="/api/campaigns", tags=["campaigns"], dependencies=admin)

app.mount("/static", StaticFiles(directory=str(WEB / "static")), name="static")


@app.get("/", include_in_schema=False)
@app.get("/dashboard", include_in_schema=False)
def dashboard_page():
    # The page holds no data; it asks for the API key and calls /api/hub/*.
    return FileResponse(WEB / "dashboard.html", media_type="text/html")


@app.get("/api/scheduler/status", dependencies=admin)
def scheduler_telemetry():
    return get_scheduler_status()


@app.get("/health")
def health_check():
    return {"status": "alive"}


@app.get("/ready")
def ready_check():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        logger.exception("readiness_db_check_failed")
        raise HTTPException(status_code=503, detail={"database": "unavailable"})
    return {"status": "ready", "checks": {"database": "ok"}}
