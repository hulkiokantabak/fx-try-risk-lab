from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.bootstrap import bootstrap_application
from app.config import get_settings
from app.db import SessionLocal
from app.middleware import SecurityHeadersMiddleware
from app.routes.web import router as web_router

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    with SessionLocal() as session:
        bootstrap_application(settings, session)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.allowed_hosts,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        same_site="lax",
        https_only=settings.secure_cookies,
    )
    app.add_middleware(
        SecurityHeadersMiddleware,
        enable_hsts=settings.secure_cookies,
    )
    app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
    app.include_router(web_router)
    return app


app = create_app()
