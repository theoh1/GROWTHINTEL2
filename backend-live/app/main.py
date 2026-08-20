from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware


app = FastAPI(title="GrowthIntel live backend", version="membership-stability-2026-08-20")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://growthintel.vercel.app",
        "https://www.growthintel.vercel.app",
        "http://127.0.0.1:3175",
        "http://localhost:3175",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _install_membership_routes() -> str:
    membership_file = Path(__file__).resolve().parents[1] / "membership_bootstrap.py"
    if not membership_file.exists():
        return "membership_bootstrap.py is missing"

    spec = importlib.util.spec_from_file_location("_growthintel_membership_bootstrap", membership_file)
    if spec is None or spec.loader is None:
        return "Could not load GrowthIntel membership bootstrap"

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        module.install_membership(app)
        return "ready"
    except RuntimeError as error:
        reason = str(error)

        @app.api_route(
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


MEMBERSHIP_STATUS = _install_membership_routes()


@app.get("/")
async def root() -> dict:
    return {
        "service": "GrowthIntel backend",
        "status": "online",
        "membership": "ready" if MEMBERSHIP_STATUS == "ready" else "degraded",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/v1/health")
async def health() -> dict:
    return {
        "ok": True,
        "backend": "online",
        "membership": "ready" if MEMBERSHIP_STATUS == "ready" else "degraded",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/v1/status")
async def status() -> dict:
    return {
        "status": "ok" if MEMBERSHIP_STATUS == "ready" else "degraded",
        "frontend": "online",
        "backend": "online",
        "database": "connected" if MEMBERSHIP_STATUS == "ready" else "membership-config-needed",
        "apis": "membership-service-online; market-data-served-through-vercel-fallback-when-live-provider-is-unavailable",
        "membership": {
            "status": "ready" if MEMBERSHIP_STATUS == "ready" else "degraded",
            "detail": None if MEMBERSHIP_STATUS == "ready" else MEMBERSHIP_STATUS,
        },
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


@app.api_route("/api/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def unsupported_live_route(path: str):
    raise HTTPException(
        status_code=404,
        detail={
            "message": "This live route is not served by the lightweight membership backend.",
            "route": path,
            "fallback": "GrowthIntel frontend may serve a recent cached scan for supported market-data routes.",
        },
    )
