from fastapi import APIRouter
from backend.app.api.companies import router as companies_router
from backend.app.api.feed import router as feed_router
from backend.app.api.insights import router as insights_router
from backend.app.api.questions import router as questions_router
from backend.app.api.analytics import router as analytics_router
from backend.app.api.metrics import router as metrics_router
from backend.app.api.streams import router as streams_router

api_router = APIRouter()

api_router.include_router(companies_router)
api_router.include_router(feed_router)
api_router.include_router(insights_router)
api_router.include_router(questions_router)
api_router.include_router(analytics_router)
api_router.include_router(metrics_router)
api_router.include_router(streams_router)
