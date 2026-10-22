import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from backend.app.core.config import settings
from backend.app.core.logging import setup_logging, logger
from backend.app.db.session import init_db
from backend.app.api.router import api_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: setup logging, initialize DB tables
    setup_logging()
    logger.info("Initializing database schema...")
    await init_db()
    logger.info(f"{settings.PROJECT_NAME} backend core started successfully.")
    yield
    logger.info("Shutting down backend...")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="Backend API для персонализированной новостной аналитики крупного бизнеса (Альфа-Банк).",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc"
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Request timing and audit logging middleware
@app.middleware("http")
async def audit_logging_middleware(request: Request, call_next):
    start_time = time.time()
    role = request.headers.get("X-User-Role", "CEO")
    response = await call_next(request)
    duration_ms = (time.time() - start_time) * 1000
    logger.info(
        f"{request.method} {request.url.path} | Status: {response.status_code} | "
        f"Role: {role} | {duration_ms:.2f}ms"
    )
    return response


# Include API v1 Router
app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/health", tags=["Health Checks"])
async def health_check():
    return {"status": "ok", "service": settings.PROJECT_NAME, "version": settings.VERSION}


@app.get("/", tags=["Root"])
async def root():
    return {
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "docs": "/docs",
        "api_v1": settings.API_V1_PREFIX
    }
