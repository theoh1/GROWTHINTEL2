from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path


BACKEND_ZIP_URL = "https://growthintel.vercel.app/backend-api-live.zip?v=20260722-stability-alerts-earlyview"


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


app = _load_real_app()
