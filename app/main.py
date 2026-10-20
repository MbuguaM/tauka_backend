import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.routes import (
    ai, messaging, calling,
    subscriptions, test, supporters, gifts, testimonials,
    referrals, account, tutor_portal, tasks, tutoring, payouts, commitments,
    trust, auth, matching, lyric_annotation, public_stats, makeups, waitlist,
    tutor_applications, enrolment, test_outs, hierarchy, clinics, complaints, concerns, pods, handoff_notes, tracker, cover, checkpoints, schedule_changes, slots, fidel,
    groups, addons, mpesa, whatsapp, tutor_ops, peer, dashboard, assessments, commerce, pilots,
    unit_assessments, exchange, summaries,
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
# Lyric annotation is deliberately a separate module from the chat path:
# different rate-limit namespace, different ledger mode, and an overflow
# path /ai/generate has no way to express.
app.include_router(lyric_annotation.router, prefix="/ai")
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

# Home/guest relation + the 1-on-1 booking lane (plan Phase 2)
app.include_router(tutoring.router, prefix="/tutoring")

# The payout rail (plan Phase 3a). Computes and records; disburses nothing.
app.include_router(payouts.router, prefix="/payouts")

# Termly commitments (plan Phase 4). Checkout is 503 until D-18's placeholder
# Stripe price objects are replaced with real ones.
app.include_router(commitments.router, prefix="/commitments")

# Trust (plan Phase 5a). Anonymous counters only — no flags, no user ids.
app.include_router(trust.router, prefix="/trust")

# Account provisioning — the server half of "signup happens on the web"
# (2026-08-28). tauka-react-web creates the auth.users row through Supabase, then
# calls POST /auth/provision to have the service role create app.user_profiles
# and app.student. See app/routes/auth.py for why the split is where it is.
app.include_router(auth.router, prefix="/auth")

# Cohort matching (tutor-cohort-matching-plan.md Phases 3-4, backlog O-16).
# Student-facing offers plus the admin's dry run. There is deliberately no
# tutor-facing route: a tutor learns about a cohort when it exists, not while it
# is being proposed.
app.include_router(matching.router, prefix="/matching")

# Make-up sessions for learners with no pod (enrolment plan §2f/§2g). The
# planner proposes a tutor and time and an admin books it; tutors mark
# attendance and learners see their next session through /makeups/mine (§103).
app.include_router(makeups.router, prefix="/makeups")

# Public figures for the marketing site (2026-10-17). Real counts only — the
# signup page's "Joining 1,047 learners." was a fabricated number.
app.include_router(public_stats.router, prefix="/public")

# The founding-pod waitlist (enrolment plan H2, §95). Public, rate-limited by
# hashed IP, and answers the same whatever happened.
app.include_router(waitlist.router, prefix="/waitlist")

# The /teach form, emailed to the team (enrolment plan §2j J0). A stopgap until
# the tutor_application table exists; stores nothing.
app.include_router(tutor_applications.router, prefix="/tutor-applications")

# The learner's own enrolment state: Phase 0 start, pace pledge, band, target
# intake (enrolment plan 1.3, §98). The only writer of app.enrolment_state.
app.include_router(enrolment.router, prefix="/enrolment")

# Heritage test-outs of Phase 0 and tutor rungs (enrolment plan §2k, §99). Who
# may request, assign, assess and promote is decided in the database.
app.include_router(test_outs.router, prefix="/test-outs")
app.include_router(hierarchy.router, prefix="/hierarchy")

# Phase 0 clinics (enrolment plan 1.5, §105): ops schedules, learners book
# within their tier's allowance, tutors mark attendance.
app.include_router(clinics.router, prefix="/clinics")

# Session complaints (enrolment plan §2f item 7, §106): conduct to safeguarding,
# the rest to ops; upheld -> one session credit.
app.include_router(complaints.router, prefix="/complaints")

# Concern reports to safeguarding, and tutors' rating averages (plan 4.6, §116-§117).
app.include_router(concerns.router, prefix="/concerns")

# First-session norms and the two-week fit survey (plan 4.1, §118).
app.include_router(pods.router, prefix="/pods")

# Handoff notes for guest-led sessions and warm handoffs (plan 4.3, §119).
app.include_router(handoff_notes.router, prefix="/handoff-notes")

# The milestone tracker (plan 4.7): display only, never a gate (E-16).
app.include_router(tracker.router, prefix="/tracker")

# Cover requests (plan §2h, §123; CONFLICT-058): first eligible tutor to accept takes the session.
app.include_router(cover.router, prefix="/cover")

# The pod slot catalogue editor (plan 3.1, §104), ops only.
app.include_router(slots.router, prefix="/slots")

# Fidel approval (plan 1.4, §107): learners submit a reading per alphabet group;
# rung 1+ reviewers approve or ask for a redo. Feedback, not a gate.
app.include_router(fidel.router, prefix="/fidel")

# Friend groups and private pods (plan 3.2, §108): a friend group is matched as
# one unit; private pods are formed by ops.
app.include_router(groups.router, prefix="/groups")

# Monthly pod checkpoints (plan 4.2, §109): opened on Ethiopic day 1, reviewed
# once by ops (outcome, guest rota, pace moves to the wait pool).
app.include_router(checkpoints.router, prefix="/checkpoints")

# Moving a pod's time (plan 4.5, §113): ops proposes, every member agrees in the
# app, applied on the start day (CONFLICT-056).
app.include_router(schedule_changes.router, prefix="/schedule-changes")

# 1:1 add-ons (plan 2.6, E-15, §129): one-time checkouts; payment grants sessions.
app.include_router(addons.router, prefix="/addons")

# M-Pesa (plan 2.4, §130): STK push against Stripe send_invoice subscriptions.
app.include_router(mpesa.router, prefix="/mpesa")

# WhatsApp reminders (plan 6.1, §133), behind the whatsapp_reminders flag.
app.include_router(whatsapp.router, prefix="/whatsapp")

# The tutor side (plan 8.3, 8.4, 8.7, 8.8; §134); public tutor profiles at /public/tutors.
app.include_router(tutor_ops.router, prefix="/tutor-ops")
app.include_router(tutor_ops.public_router, prefix="/public")

# Peer sessions (Track 5, §135), behind the peer_sessions flag.
app.include_router(peer.router, prefix="/peer")

# Ops dashboards (Track 9): display only (E-16).
app.include_router(dashboard.router, prefix="/dashboard")

# Exit assessments, certificates, the guarantee (Track 10, §138); public verification.
app.include_router(assessments.router, prefix="/assessments")
app.include_router(assessments.public_router, prefix="/public")

# Institutions, community partners, gift cards (Track 10, §139).
app.include_router(commerce.router, prefix="/commerce")

# Track 10 pilots and the plan's last remainders (§140).
app.include_router(pilots.router, prefix="/pilots")

# Unit assessments: a quiz and a speaking check per block of ten lessons (§141).
app.include_router(unit_assessments.router, prefix="/unit-assessments")

# Exchange partners (features addendum A, §142), behind the exchange_partners flag.
app.include_router(exchange.router, prefix="/exchange")

# Learner-language lesson summaries (features addendum B, §143), behind lesson_summaries.
app.include_router(summaries.router, prefix="/summaries")

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
