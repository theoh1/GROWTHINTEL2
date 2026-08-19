from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path


BACKEND_ZIP_URL = "https://growthintel.vercel.app/backend-api-live.zip?v=20260729-fresh-data-guard"
PACKAGED_BACKEND_ROOT: Path | None = None


def _find_app_main(root: Path) -> Path | None:
    for candidate in root.rglob("main.py"):
        if candidate.parent.name == "app":
            return candidate
    return None


def _ensure_backend_source() -> Path:
    target = Path(tempfile.gettempdir()) / "growthintel_backend_live_v20260722_stability_alerts_earlyview"

    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    archive_path = target / "backend-api-live.zip"
    urllib.request.urlretrieve(BACKEND_ZIP_URL, archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()[:12]
        archive.extractall(target)
    extracted_main = _find_app_main(target)
    if extracted_main and extracted_main.exists():
        return extracted_main.parents[1]
    raise RuntimeError(f"Could not find app/main.py after extracting backend zip. First entries: {names}")


def _load_real_app():
    global PACKAGED_BACKEND_ROOT
    source_root = _ensure_backend_source()
    PACKAGED_BACKEND_ROOT = source_root
    real_app_dir = source_root / "app"
    package = sys.modules.get("app")
    if package is not None:
        package.__path__ = [str(real_app_dir)]
        package.__file__ = str(real_app_dir / "__init__.py")
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

    spec = importlib.util.spec_from_file_location(
        "_growthintel_real_app_main",
        real_app_dir / "main.py",
        submodule_search_locations=[str(real_app_dir)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load GrowthIntel backend package.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    real_app = module.app
    _install_natural_ai_route(real_app)
    _install_early_view_route(real_app)
    _install_fresh_market_routes(real_app, source_root)
    _install_membership_routes(real_app)
    return real_app


def _install_membership_routes(real_app):
    from fastapi import HTTPException

    membership_file = Path(__file__).resolve().parents[1] / "membership_bootstrap.py"
    if not membership_file.exists():
        return

    spec = importlib.util.spec_from_file_location("_growthintel_membership_bootstrap", membership_file)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load GrowthIntel membership bootstrap.")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        module.install_membership(real_app)
    except RuntimeError as error:
        message = str(error)

        @real_app.api_route(
            "/api/v1/membership/{path:path}",
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        )
        async def membership_unavailable(path: str):
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "Growth Intel memberships are temporarily unavailable.",
                    "reason": message,
                    "safe_state": "No membership access was granted from temporary storage.",
                },
            )

    membership_routes = [
        route
        for route in real_app.router.routes
        if getattr(route, "path", "").startswith("/api/v1/membership/")
    ]
    other_routes = [
        route
        for route in real_app.router.routes
        if not getattr(route, "path", "").startswith("/api/v1/membership/")
    ]
    real_app.router.routes = membership_routes + other_routes
    real_app.openapi_schema = None


def _install_fresh_market_routes(real_app, source_root: Path):
    from datetime import datetime, timedelta, timezone

    from fastapi import Depends, HTTPException, Query
    from fastapi.routing import APIRoute
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.api.routes import latest_payload
    from app.db.session import get_db
    from app.repositories.screening_repository import ScreeningRepository

    max_age = timedelta(hours=36)
    packaged_db_path = source_root / "canslim.db"
    packaged_engine = create_engine(
        f"sqlite:///{packaged_db_path.as_posix()}",
        connect_args={"check_same_thread": False},
        future=True,
        pool_pre_ping=True,
    )
    PackagedSession = sessionmaker(bind=packaged_engine, autoflush=False, autocommit=False, future=True)

    def fresh_enough(value) -> bool:
        if not value:
            return False
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return datetime.utcnow() - value <= max_age

    def packaged_payload(threshold: float, sector: str | None = None) -> dict | None:
        db = PackagedSession()
        try:
            repository = ScreeningRepository(db)
            payload = latest_payload(repository, threshold=threshold, sector=sector)
            if not payload or not fresh_enough(payload.get("last_updated")):
                return None
            return {
                **payload,
                "data_source": "RECENT_PACKAGED_CACHE",
                "fallback_snapshot": True,
                "fallback_reason": "Render production database was empty or slow, so GrowthIntel served the fresh packaged market scan.",
            }
        finally:
            db.close()

    real_app.router.routes = [
        route
        for route in real_app.router.routes
        if not (isinstance(route, APIRoute) and route.path in {"/api/v1/top-stocks", "/api/v1/status"})
    ]

    @real_app.get("/api/v1/status")
    async def bootstrap_status(db=Depends(get_db)) -> dict:
        latest_run = None
        database_status = "connected"
        try:
            latest_run = ScreeningRepository(db).latest_scan_run()
        except Exception:
            database_status = "degraded"

        packaged = packaged_payload(0)
        packaged_last_updated = packaged.get("last_updated") if packaged else None
        last_successful_refresh = latest_run.completed_at if latest_run else packaged_last_updated
        using_packaged = latest_run is None and packaged is not None

        return {
            "status": "ok" if database_status == "connected" and last_successful_refresh else "degraded",
            "frontend": "online",
            "backend": "online",
            "database": "packaged-cache" if using_packaged else database_status,
            "apis": "live-data-with-fresh-packaged-cache",
            "refresh": {"running": False, "started_at": None, "last_error": None},
            "ai": {"provider": "pollinations-free-ai", "model": "openai", "configured": True},
            "last_successful_refresh": last_successful_refresh,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "fallback_snapshot": using_packaged,
        }

    @real_app.get("/api/v1/top-stocks")
    async def bootstrap_top_stocks(
        threshold: float = Query(default=70, ge=0, le=100),
        sector: str | None = Query(default=None),
        tickers: list[str] | None = Query(default=None),
        refresh: bool = Query(default=False),
        db=Depends(get_db),
    ) -> dict:
        effective_threshold = 0 if refresh else threshold
        if not tickers:
            try:
                live_payload = latest_payload(ScreeningRepository(db), effective_threshold, sector)
                if live_payload and fresh_enough(live_payload.get("last_updated")):
                    live_payload["threshold"] = threshold
                    return live_payload
            except Exception:
                pass

            payload = packaged_payload(effective_threshold, sector)
            if payload:
                payload["threshold"] = threshold
                return payload

            raise HTTPException(status_code=503, detail="No recent GrowthIntel market scan is available yet.")

        payload = packaged_payload(effective_threshold, sector)
        if payload:
            wanted = {ticker.upper() for ticker in tickers}
            payload["results"] = [row for row in payload.get("results", []) if row.get("ticker") in wanted]
            payload["threshold"] = threshold
            return payload
        raise HTTPException(status_code=503, detail="Live ticker refresh is temporarily unavailable.")

    top_route = real_app.router.routes.pop()
    status_route = real_app.router.routes.pop()
    real_app.router.routes.insert(0, status_route)
    real_app.router.routes.insert(0, top_route)


def _install_natural_ai_route(real_app):
    from fastapi import Request
    from fastapi.routing import APIRoute
    import httpx
    from urllib.parse import quote

    real_app.router.routes = [
        route
        for route in real_app.router.routes
        if not (isinstance(route, APIRoute) and route.path == "/api/v1/ai/assistant")
    ]

    @real_app.post("/api/v1/ai/assistant")
    async def bootstrap_natural_ai_assistant(request: Request):
        payload = await request.json()
        question = str(payload.get("question") or "").strip() or "Help me with an investing question."
        prompt = (
            "You are GrowthIntel's investing assistant. Answer naturally and directly. "
            "Do not use a fixed template. Do not force Buy, Wait, Avoid, or Reduce labels unless the user asks. "
            "Do not provide guaranteed financial advice.\n\n"
            f"Question: {question}"
        )
        text = ""
        try:
            encoded = quote(prompt[:3500], safe="")
            async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
                response = await client.get(f"https://text.pollinations.ai/{encoded}?model=openai")
            response.raise_for_status()
            text = response.text.strip()
        except Exception:
            text = "I can help with that, but the live AI provider did not respond. Ask again in a moment and I will retry with the current market context."

        return {
            "title": "AI Assistant",
            "answer": text,
            "summary": text,
            "keyPoints": [],
            "action": None,
            "risks": [],
            "confidence": None,
            "followUps": [],
            "evidence": [
                {"label": "AI Provider", "value": "Pollinations free text AI", "context": "Natural answer route installed by Render bootstrap"},
                {"label": "Model", "value": "openai", "context": "Free hosted text model route"},
            ],
            "source": "pollinations-free-ai",
            "model": "pollinations:openai",
        }

    override_route = real_app.router.routes.pop()
    real_app.router.routes.insert(0, override_route)


def _install_early_view_route(real_app):
    from fastapi import Depends, Query
    from fastapi.routing import APIRoute

    from app.db.session import get_db
    from app.repositories.screening_repository import ScreeningRepository
    from app.services.early_view_service import build_early_view_payload

    real_app.router.routes = [
        route
        for route in real_app.router.routes
        if not (isinstance(route, APIRoute) and route.path == "/api/v1/early-view")
    ]

    @real_app.get("/api/v1/early-view")
    async def bootstrap_early_view(window: int = Query(default=3), db=Depends(get_db)) -> dict:
        repository = ScreeningRepository(db)
        payload = await build_early_view_payload(repository, window_minutes=window)
        rows = repository.latest_scan_rows(threshold=0, limit=320)
        live_bar_count = int(payload.get("stocks_scanned") or 0)
        payload["stocks_scanned"] = max(live_bar_count, len(rows))
        payload.setdefault("symbols_with_intraday", live_bar_count)
        return payload

    override_route = real_app.router.routes.pop()
    real_app.router.routes.insert(0, override_route)


app = _load_real_app()
