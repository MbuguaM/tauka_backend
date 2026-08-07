from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.routes import (
    ai, messaging, calling,
    subscriptions, test, supporters, gifts, testimonials,
    referrals, account, tutor_portal,
)
from app.tasks.scheduler import start_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield


app = FastAPI(lifespan=lifespan)

# tauka-react-web is a different origin in every environment (:5173 in dev,
# www.tauka.com in prod), and every apiPost sends `Content-Type:
# application/json` — a non-safelisted value, so the browser preflights with
# OPTIONS first. Without this middleware Starlette answers that preflight with
# 405 and no Access-Control-Allow-Origin, the browser blocks the call, and the
# placement test silently falls back to its unscorable preview bank.
# Origins come from CORS_ORIGINS so prod can be locked down without a release.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Existing routes
app.include_router(ai.router, prefix="/ai")
app.include_router(messaging.router, prefix="/messages")
app.include_router(calling.router, prefix="/calls")

# Segment A
app.include_router(subscriptions.router, prefix="/subscriptions")

# Segment C
app.include_router(test.router, prefix="/test")

# Segment C.5 — Referrals
app.include_router(referrals.router, prefix="/referrals")

# Segment D
app.include_router(supporters.router, prefix="/supporters")

# Segment E
app.include_router(gifts.router, prefix="/gifts")

# Segment F
app.include_router(testimonials.router, prefix="/testimonials")

# Account portal
app.include_router(account.router, prefix="/account")

# Tutor portal
app.include_router(tutor_portal.router, prefix="/tutor")
