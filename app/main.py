import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.routes import (
    ai, messaging, calling,
    subscriptions, test, supporters, gifts, testimonials,
    referrals, account, tutor_portal, tasks,
)


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Deploy-time misconfigurations that nothing else surfaces: they raise no
    # exception, log nothing on the send path, and are visible only to whoever
    # receives the mail. Logged at ERROR because a deploy that ships the dev
    # .env is the exact case this catches, and a warning would scroll past.
    for problem in settings.deployment_problems():
        logger.error("DEPLOYMENT: %s", problem)

    # No scheduler is started here. Daily jobs are triggered externally via
    # GET /tasks/daily — see app/routes/tasks.py for why in-process scheduling
    # was removed rather than fixed.
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

# Externally-triggered scheduled jobs (Vercel Cron). Guarded by CRON_SECRET.
app.include_router(tasks.router, prefix="/tasks")


@app.exception_handler(404)
async def not_found(request: Request, exc):
    """
    Report the path the application actually received.

    A bare `{"detail":"Not Found"}` cannot distinguish "you asked for a route
    that does not exist" from "the platform rewrote your URL before we saw it" —
    and on Vercel the second is a real failure mode: a catch-all `rewrites` entry
    replaces the request path with the function's own path, so EVERY route 404s,
    including FastAPI's own /docs and /openapi.json. That looked identical to a
    missing route and cost a deploy cycle to identify.

    If `path` below is not the URL you requested, the routing config is the bug,
    not the application.
    """
    return JSONResponse(
        status_code=404,
        content={"detail": "Not Found", "path": request.url.path},
    )


@app.get("/")
async def root():
    """A front door that proves the deployment works, rather than a bare 404."""
    return {"service": "tauka-python", "status": "ok", "docs": "/docs"}


@app.get("/health")
async def health():
    """
    Liveness probe. Deliberately touches nothing — no database, no Redis, no
    mail — so a degraded dependency cannot make the platform kill a process
    that is serving traffic perfectly well for every path that does not need it.
    """
    return {"status": "ok"}
