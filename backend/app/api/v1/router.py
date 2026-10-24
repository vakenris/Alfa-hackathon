from fastapi import APIRouter
from backend.app.api.v1.companies import router as companies_router
from backend.app.api.v1.feed import router as feed_router
from backend.app.api.v1.insights import router as insights_router
from backend.app.api.v1.jobs import router as jobs_router
from backend.app.api.v1.questions import router as questions_router
from backend.app.api.v1.health import router as health_router
from backend.app.api.v1.analytics import router as analytics_router
from backend.app.api.v1.metrics import router as metrics_router
from backend.app.api.v1.streams import router as streams_router

api_v1_router = APIRouter()

api_v1_router.include_router(health_router)
api_v1_router.include_router(companies_router)
api_v1_router.include_router(feed_router)
api_v1_router.include_router(insights_router)
api_v1_router.include_router(jobs_router)
api_v1_router.include_router(questions_router)
api_v1_router.include_router(analytics_router)
api_v1_router.include_router(metrics_router)
api_v1_router.include_router(streams_router)
