from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.api.v1.router import api_v1_router
from backend.app.core.audit import AuditMiddleware
from backend.app.core.config import settings
from backend.app.core.logging import logger, setup_logging
from backend.app.db.session import async_session_factory, init_db
from backend.app.ingestion.seeder import seed_data


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    setup_logging()

    logger.info("Initializing database tables...")
    await init_db()

    if settings.AUTO_SEED_DEMO:
        async with async_session_factory() as session:
            await seed_data(session)

    logger.info(
        "%s backend started successfully.",
        settings.PROJECT_NAME,
    )

    yield

    logger.info("Shutting down backend...")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description=(
        "Backend API для персонализированной новостной аналитики "
        "крупного бизнеса (Альфа-Банк)."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(AuditMiddleware)

app.include_router(
    api_v1_router,
    prefix=settings.API_V1_PREFIX,
)


@app.get("/", tags=["Root"])
async def root():
    return {
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "docs": "/docs",
        "api_v1": settings.API_V1_PREFIX,
    }


@app.get("/health", tags=["Health Checks"])
async def health_check():
    return {
        "status": "ok",
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
    }