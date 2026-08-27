from __future__ import annotations

import hmac
import importlib.util
import os
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

BACKEND_ZIP_URL = "https://www.growthintel.app/backend-api-live.zip?v=20260826-membership-resilience"


def _find_app_main(root: Path) -> Path | None:
    for candidate in root.rglob("main.py"):
        if candidate.parent.name == "app":
            return candidate
    return None


def _ensure_backend_source() -> Path:
    target = Path(tempfile.gettempdir()) / "growthintel_backend_live_v20260826_membership_resilience"
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


def _install_internal_service_membership_bypass(membership_module) -> None:
    original_session_user = getattr(membership_module, "_session_user", None)
    if not callable(original_session_user):
        return

    def session_user_or_internal_service(request, connection):
        expected = os.environ.get("GROWTHINTEL_CRON_SECRET", "").strip() or os.environ.get("CRON_SECRET", "").strip()
        auth = request.headers.get("authorization") or ""
        bearer = auth.removeprefix("Bearer ").strip() if auth.lower().startswith("bearer ") else ""
        supplied = request.headers.get("x-growthintel-cron-secret") or bearer or request.query_params.get("secret")
        if expected and supplied and hmac.compare_digest(str(supplied), expected):
            now = int(time.time())
            return {
                "id": 0,
                "email": "internal-service@growthintel.local",
                "name": "GrowthIntel Internal Service",
                "gi_reference": "GI-SVC00",
                "membership_state": "ACTIVE",
                "membership_expires_at": now + 3600,
                "membership_started_at": now,
                "membership_last_verified_at": now,
                "membership_cancel_at_period_end": 0,
                "plan_version": "premium_v1",
                "affiliate_referral_code": None,
                "password_hash": "",
                "created_at": now,
            }
        return original_session_user(request, connection)

    membership_module._session_user = session_user_or_internal_service


def _install_membership_routes(real_app):
    membership_file = Path(__file__).resolve().parents[1] / "membership_bootstrap.py"
    if not membership_file.exists():
        return "membership_bootstrap.py is missing"

    spec = importlib.util.spec_from_file_location("_growthintel_membership_bootstrap", membership_file)
    if spec is None or spec.loader is None:
        return "Could not load GrowthIntel membership bootstrap"

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _install_internal_service_membership_bypass(module)
    try:
        module.install_membership(real_app)
        return "ready"
    except RuntimeError as error:
        reason = str(error)
        from fastapi import HTTPException

        @real_app.api_route(
            "/api/v1/membership/{path:path}",
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        )
        async def membership_unavailable(path: str):
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "Growth Intel memberships are temporarily unavailable.",
                    "reason": reason,
                    "safe_state": "No membership access was granted from temporary storage.",
                },
            )

        return reason


def _load_real_app():
    source_root = _ensure_backend_source()
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
    real_app.state.membership_status = _install_membership_routes(real_app)
    return real_app


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
    try:
        from fastapi import Depends, Query
        from fastapi.routing import APIRoute

        from app.db.session import get_db
        from app.repositories.screening_repository import ScreeningRepository
        from app.services.early_view_service import build_early_view_payload
    except ModuleNotFoundError:
        return

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
