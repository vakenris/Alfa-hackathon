from fastapi import APIRouter
from backend.app.api.v1.companies import router as companies_router
from backend.app.api.v1.feed import router as feed_router
from backend.app.api.v1.insights import router as insights_router
from backend.app.api.v1.jobs import router as jobs_router
from backend.app.api.v1.questions import router as questions_router
from backend.app.api.v1.health import router as health_router

api_v1_router = APIRouter()

api_v1_router.include_router(health_router)
api_v1_router.include_router(companies_router)
api_v1_router.include_router(feed_router)
api_v1_router.include_router(insights_router)
api_v1_router.include_router(jobs_router)
api_v1_router.include_router(questions_router)
