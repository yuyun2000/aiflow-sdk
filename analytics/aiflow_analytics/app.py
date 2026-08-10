import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from . import __version__
from .analytics import Analytics, Period
from .config import Settings, settings_summary
from .database import Database
from .sync import SyncService
from .tls_client import TLSLogClient

LOGGER = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)


class SyncRequest(BaseModel):
    start_date: date
    end_date: date
    force: bool = False


def _period_from_dates(
    settings: Settings,
    start_date: date | None,
    end_date: date | None,
) -> Period:
    timezone = ZoneInfo(settings.timezone)
    today = datetime.now(timezone).date()
    effective_end = end_date or today
    effective_start = start_date or (
        effective_end - timedelta(days=settings.default_range_days - 1)
    )
    if effective_start > effective_end:
        raise HTTPException(status_code=422, detail="start_date must be on or before end_date")
    start = datetime.combine(effective_start, time.min, tzinfo=timezone)
    end = datetime.combine(effective_end + timedelta(days=1), time.min, tzinfo=timezone)
    return Period(int(start.timestamp() * 1000), int(end.timestamp() * 1000))


def create_app(
    settings: Settings,
    *,
    database: Database | None = None,
    tls_client: TLSLogClient | None = None,
    sync_service: SyncService | None = None,
) -> FastAPI:
    db = database or Database(settings.database_path, settings.tls_schema_version)
    client = tls_client or TLSLogClient(settings)
    sync = sync_service or SyncService(settings, db, client)
    analytics = Analytics(db, settings.timezone)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await asyncio.to_thread(db.initialize)
        sync.start()
        try:
            yield
        finally:
            await asyncio.to_thread(sync.shutdown)

    app = FastAPI(
        title="AIFlow Conversation Analytics",
        version=__version__,
        description=(
            "Pull, reconstruct, and analyze AIFlow Schema V2 conversation telemetry "
            "from Volcengine TLS."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = db
    app.state.sync_service = sync
    app.state.analytics = analytics

    async def require_auth(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
    ) -> None:
        if settings.auth_disabled:
            return
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not hmac.compare_digest(credentials.credentials, settings.api_token)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="valid Bearer token required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def period_dependency(
        start_date: Annotated[date | None, Query()] = None,
        end_date: Annotated[date | None, Query()] = None,
    ) -> Period:
        return _period_from_dates(settings, start_date, end_date)

    PeriodDependency = Annotated[Period, Depends(period_dependency)]

    @app.get("/")
    async def root() -> dict[str, Any]:
        return {
            "service": "aiflow-conversation-analytics",
            "version": __version__,
            "health": "/health",
            "ready": "/ready",
            "docs": "/docs",
            "api": "/api/v1/dashboard",
        }

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "service": "aiflow-conversation-analytics"}

    @app.get("/ready")
    async def ready() -> dict[str, Any]:
        database_ready = await asyncio.to_thread(db.ping)
        if not database_ready:
            raise HTTPException(status_code=503, detail="analytics database is unavailable")
        return {
            "status": "ready",
            "database": True,
            "tls_configured": client.configured,
        }

    @app.get("/api/v1/status", dependencies=[Depends(require_auth)])
    async def service_status() -> dict[str, Any]:
        return {
            "config": settings_summary(settings),
            "sync": await asyncio.to_thread(sync.status),
        }

    @app.post(
        "/api/v1/sync",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_auth)],
    )
    async def trigger_sync(request: SyncRequest) -> dict[str, Any]:
        if request.start_date > request.end_date:
            raise HTTPException(status_code=422, detail="start_date must be on or before end_date")
        if not client.configured:
            raise HTTPException(status_code=503, detail="TLS credentials are not configured")
        started = await asyncio.to_thread(
            sync.trigger,
            request.start_date,
            request.end_date,
            force=request.force,
        )
        if not started:
            raise HTTPException(status_code=409, detail="a log sync is already running")
        return {
            "accepted": True,
            "start_date": request.start_date,
            "end_date": request.end_date,
            "force": request.force,
        }

    @app.get("/api/v1/overview", dependencies=[Depends(require_auth)])
    async def overview(period: PeriodDependency) -> dict[str, Any]:
        return await asyncio.to_thread(analytics.overview, period)

    @app.get("/api/v1/compare", dependencies=[Depends(require_auth)])
    async def compare(period: PeriodDependency) -> dict[str, Any]:
        return await asyncio.to_thread(analytics.compare, period)

    @app.get("/api/v1/trends", dependencies=[Depends(require_auth)])
    async def trends(
        period: PeriodDependency,
        bucket: str = Query(default="day", pattern="^(hour|day|week)$"),
    ) -> dict[str, Any]:
        return await asyncio.to_thread(analytics.trends, period, bucket)

    @app.get("/api/v1/breakdowns", dependencies=[Depends(require_auth)])
    async def breakdowns(
        period: PeriodDependency,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        return await asyncio.to_thread(analytics.breakdowns, period, limit)

    @app.get("/api/v1/dashboard", dependencies=[Depends(require_auth)])
    async def dashboard(
        period: PeriodDependency,
        bucket: str = Query(default="day", pattern="^(hour|day|week)$"),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        return await asyncio.to_thread(analytics.dashboard, period, bucket, limit)

    @app.get("/api/v1/conversations", dependencies=[Depends(require_auth)])
    async def conversations(
        period: PeriodDependency,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
        project_id: str = Query(default="", max_length=100),
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            analytics.conversations,
            period,
            page=page,
            page_size=page_size,
            project_id=project_id,
        )

    @app.get("/api/v1/turns", dependencies=[Depends(require_auth)])
    async def turns(
        period: PeriodDependency,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
        status_filter: str = Query(default="", alias="status", max_length=32),
        project_id: str = Query(default="", max_length=100),
        conversation_id: str = Query(default="", max_length=100),
        model: str = Query(default="", max_length=200),
        tool_name: str = Query(default="", max_length=200),
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            analytics.turns,
            period,
            page=page,
            page_size=page_size,
            status=status_filter,
            project_id=project_id,
            conversation_id=conversation_id,
            model=model,
            tool_name=tool_name,
        )

    @app.get("/api/v1/turns/{turn_id}", dependencies=[Depends(require_auth)])
    async def turn_detail(turn_id: str) -> dict[str, Any]:
        item = await asyncio.to_thread(analytics.turn_detail, turn_id)
        if item is None:
            raise HTTPException(status_code=404, detail="turn not found")
        return item

    @app.get("/api/v1/data-quality", dependencies=[Depends(require_auth)])
    async def data_quality(period: PeriodDependency) -> dict[str, Any]:
        return await asyncio.to_thread(analytics.data_quality, period)

    return app
