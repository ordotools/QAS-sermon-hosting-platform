import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.config import get_settings
from app.database import async_session, init_db
from app.routers import admin, auth, public, stream, tus, upload
from app.routers.upload import schedule_processing
from app.services.auth import bootstrap_superuser
from app.services.media import cleanup_abandoned_temps, recover_stale_processing
from app.services.media_formats import ffprobe_available, media_tools_error
from app.storage import get_storage

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    Path(settings.local_storage_path).mkdir(parents=True, exist_ok=True)
    (Path(settings.local_storage_path) / ".tus").mkdir(parents=True, exist_ok=True)
    Path("data").mkdir(parents=True, exist_ok=True)
    await init_db()
    async with async_session() as session:
        await bootstrap_superuser(session, settings.superuser_email, settings.superuser_password)
        to_retry = await recover_stale_processing(session, older_than_minutes=0)
        await cleanup_abandoned_temps(session)
    for media_id, temp_path in to_retry:
        logger.info("Re-queueing interrupted processing for media %s", media_id)
        schedule_processing(media_id, temp_path)
    logger.info("Storage backend: %s", settings.storage_backend)
    if settings.storage_backend == "b2":
        try:
            await get_storage().check()
        except Exception:
            logger.exception(
                "B2 bucket check failed (endpoint=%s bucket=%s). "
                "GET /health will fail until this is fixed.",
                settings.b2_endpoint,
                settings.b2_bucket,
            )
    if not ffprobe_available():
        logger.warning(media_tools_error())
    yield


app = FastAPI(title="QAS Sermon Platform", lifespan=lifespan)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(public.router)
app.include_router(auth.router)
app.include_router(upload.router)
app.include_router(tus.router)
app.include_router(stream.router)
app.include_router(admin.router)


@app.get("/health")
async def health():
    settings = get_settings()
    payload = {"status": "ok", "storage": settings.storage_backend}
    if settings.storage_backend != "b2":
        return payload
    try:
        await get_storage().check()
    except Exception as exc:
        logger.exception("B2 health check failed")
        return JSONResponse(
            {
                "status": "error",
                "storage": "b2",
                "error": str(exc) or type(exc).__name__,
            },
            status_code=503,
        )
    return payload


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse("app/static/manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        "app/static/sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )
