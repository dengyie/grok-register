"""FastAPI application factory for the project control plane."""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from apps.control_api.auth import auth_is_required, require_auth
from apps.control_api.schemas import HealthOut, OverviewOut
from apps.control_api.settings import get_settings
from apps.control_api.users import ensure_bootstrap_user, has_any_user


def create_app() -> FastAPI:
    app = FastAPI(title="ai-register-machine control API", version="1.1.0")

    @app.on_event("startup")
    def _bootstrap_user() -> None:
        s = get_settings()
        if not s.password_login_enabled:
            return
        created = ensure_bootstrap_user(
            s.project_root,
            username=s.bootstrap_user,
            password=s.bootstrap_password,
        )
        if created:
            # Do not log password; username only.
            print(f"[control_api] bootstrap operator created: {created}", flush=True)

    @app.get("/api/health", response_model=HealthOut)
    def health() -> HealthOut:
        s = get_settings()
        return HealthOut(
            ok=True,
            project_root=str(s.project_root),
            token_required=bool(s.token),
            password_login_enabled=s.password_login_enabled,
            users_configured=has_any_user(s.project_root),
            auth_required=auth_is_required(s.project_root),
        )

    @app.get("/api/overview", response_model=OverviewOut, dependencies=[Depends(require_auth)])
    def overview() -> OverviewOut:
        from apps.control_api.overview import build_overview

        s = get_settings()
        return OverviewOut(**build_overview(s.project_root))

    try:
        from apps.control_api.routes_auth import router as auth_router

        app.include_router(auth_router)
    except ImportError:
        pass
    try:
        from apps.control_api.routes_config import router as config_router

        app.include_router(config_router, dependencies=[Depends(require_auth)])
    except ImportError:
        pass
    try:
        from apps.control_api.routes_runs import router as runs_router

        app.include_router(runs_router, dependencies=[Depends(require_auth)])
    except ImportError:
        pass
    try:
        from apps.control_api.routes_import import router as import_router

        app.include_router(import_router, dependencies=[Depends(require_auth)])
    except ImportError:
        pass

    web_root = Path(__file__).resolve().parents[1] / "web"
    web_dist = web_root / "dist"
    # Prefer Vite build (console10); fall back to flat static root / legacy.
    web_dir = web_dist if (web_dist / "index.html").is_file() else web_root
    if web_dir.is_dir() and (web_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")

    @app.exception_handler(ValueError)
    async def _value_error(_request, exc: ValueError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    return app
