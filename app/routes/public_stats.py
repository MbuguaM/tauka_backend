"""Public, unauthenticated figures for the marketing site.

WHY THIS EXISTS. `signup.jsx` told every visitor "Joining 1,047 learners." while
the platform had two. A number on a sign-up page is a representation to a
consumer (Kenya's Consumer Protection and Competition Acts; US and UK advertising
rules — see `tauka-enrolment-playbook.md`, Legal framing), so the page now shows
the real count or nothing.

WHY A ROUTE AND NOT A SUPABASE QUERY FROM THE WEB. `app.student` has no public
read policy and must not get one. The service role counts; the response carries
a number and nothing else.

A failed count returns `None`, never 0 and never a guess: the page hides the line
rather than telling a visitor something we do not know.
"""

import logging

from fastapi import APIRouter

from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)
router = APIRouter()


def learner_count() -> int | None:
    """Accounts with an `app.student` row that has not been soft-deleted.

    Tutors and admins have no `app.student` row, so they are not counted.

    `app.student` is tauka-flutter's table (student_tutor domain). We expect one
    row per learner, created by `POST /auth/provision`, with `deleted_at`
    (timestamptz, NULL while the account is live) as the soft-delete marker. We
    read the row count only, never a column value.
    """
    try:
        result = (
            db.schema("app")
            .table("student")
            .select("id", count="exact")
            .is_("deleted_at", "null")
            .execute()
        )
        if result.count is not None:
            return result.count
        return len(result.data or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("public learner count failed: %s", exc)
        return None


@router.get("/stats")
async def public_stats():
    """PUBLIC — real platform figures for the marketing site."""
    return {"learners": learner_count()}
