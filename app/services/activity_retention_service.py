"""
Activity retention — the server half.

See `activity-telemetry-plan.md` §11–§12 and
`tauka/shared-db/domains/activity.md`.

WHY THERE ARE TWO HALVES AND NEITHER IS OPTIONAL
------------------------------------------------
Raw activity events live as per-device JSONL in the `activity` Storage bucket;
`app.activity_file` is only a catalogue of what is in there. That shape means a
single retention rule cannot do the job:

  * A file that straddles the cutoff holds expired AND current events. Shedding
    the expired half means REWRITING the object, and only the owning device can
    do that sensibly — it already rewrites on compaction, so it filters there.
    A live device's merged file always reaches today, so no rule this job could
    express would ever fire on it.

  * A file entirely past the cutoff can be deleted without being parsed, and is
    the case the device will never handle: a phone that was reinstalled, sold,
    or simply never opened again does not come back to tidy up after itself.
    That is this job.

The two do not overlap and together they cover every file.

WHY THIS RUNS AS tauka-python AND NOT AS pg_cron
------------------------------------------------
Deleting a row from `storage.objects` in SQL removes the metadata and ORPHANS
the underlying blob — the bytes stay and keep costing money. Only the Storage
API actually deletes. So the deleting party has to be something that can make
HTTP calls with the service key, which is this service. The database's part is
answering *which* paths, which is what the two helper functions do.
"""
import logging
from typing import Any

from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)

BUCKET = "activity"

#: Matches `kActivityRetentionDays` in `tauka/lib/src/const.dart`. The client
#: sheds on compaction using its own copy; if the two ever disagree the wider
#: one simply wins and nothing breaks, but they are meant to be one number.
RETENTION_DAYS = 90

#: An object younger than this with no catalogue row is not an orphan — it is a
#: chunk uploaded seconds ago whose `activity_file` insert has not landed yet.
#: That window is milliseconds; a day is not a close call.
ORPHAN_GRACE_HOURS = 24

#: Storage `remove()` takes a list. Kept well under any URL/payload limit, and
#: small enough that one failing batch loses little work.
_DELETE_BATCH = 100

#: Ceiling per run, so a first run against a long-neglected bucket cannot hold a
#: serverless invocation open past its timeout. What is left is picked up
#: tomorrow — retention has no deadline, and a job that times out half way
#: through is worse than one that makes steady progress.
_MAX_PER_RUN = 1000


def _remove_objects(paths: list[str]) -> int:
    """
    Delete objects from the bucket, in batches, tolerating partial failure.

    A path that is already gone is a success, not an error: this job races
    nothing, but it does re-run over work a previous run half-finished.
    """
    removed = 0
    for start in range(0, len(paths), _DELETE_BATCH):
        batch = paths[start:start + _DELETE_BATCH]
        try:
            db.storage.from_(BUCKET).remove(batch)
            removed += len(batch)
        except Exception as exc:
            # One bad batch must not abandon the rest. Anything missed keeps its
            # catalogue row (see the ordering note in prune_expired_files) or
            # stays an orphan, and either way the next run finds it again.
            logger.warning(
                "activity retention: failed to remove %d objects: %s",
                len(batch), exc,
            )
    return removed


def prune_expired_files(retention_days: int = RETENTION_DAYS) -> dict[str, Any]:
    """
    Delete whole files whose NEWEST event is already past the window.

    `covers_to < cutoff` is the entire safety argument: `covers_to` is the
    maximum `local_date` inside the object, so every event in it is expired and
    the file can go without being read. Files that straddle the cutoff are
    deliberately left to the owning device.

    ORDER: the object is deleted BEFORE its catalogue row.

    Both orders self-heal, and this one heals inside this same function. Object
    first, then a crash, leaves a row pointing at nothing — which the very next
    run re-selects (its `covers_to` is still past the cutoff) and finishes. Row
    first, then a crash, leaves an orphaned object that only the *other* sweep
    can find, which is a day's delay and a second mechanism for no gain.

    A row pointing at a missing object is harmless meanwhile: readers treat a
    404 as "already merged" and skip it.
    """
    try:
        rows = db.schema("app").rpc(
            "expired_activity_files",
            {"p_retention_days": retention_days, "p_limit": _MAX_PER_RUN},
        ).execute().data or []
    except Exception as exc:
        logger.error("activity retention: expired_activity_files failed: %s", exc)
        return {"files": 0, "objects": 0, "error": str(exc)}

    if not rows:
        return {"files": 0, "objects": 0}

    paths = [r["path"] for r in rows if r.get("path")]
    ids = [r["id"] for r in rows if r.get("id")]

    removed = _remove_objects(paths)

    deleted_rows = 0
    for start in range(0, len(ids), _DELETE_BATCH):
        batch = ids[start:start + _DELETE_BATCH]
        try:
            db.schema("app").table("activity_file").delete().in_("id", batch).execute()
            deleted_rows += len(batch)
        except Exception as exc:
            logger.warning(
                "activity retention: failed to delete %d catalogue rows: %s",
                len(batch), exc,
            )

    logger.info(
        "activity retention: pruned %d expired files (%d objects removed), "
        "window=%dd",
        deleted_rows, removed, retention_days,
    )
    return {"files": deleted_rows, "objects": removed}


def sweep_orphaned_objects(grace_hours: int = ORPHAN_GRACE_HOURS) -> dict[str, Any]:
    """
    Delete bucket objects that no catalogue row claims.

    This is the only thing that makes the domain doc's privacy claim true.
    `app.activity_file` CASCADEs from `user_profiles`, so deleting an account
    removes the catalogue — but the bucket has no foreign key and keeps the
    JSONL. Without this sweep, "account deletion actually deletes" is false, and
    what survives is a minute-by-minute record of what a person did.

    The other source is a crash between `putObject` and `recordChunk` on the
    client. Those events were never removed from the send queue and went up
    again as a new chunk, so the orphan is pure garbage.
    """
    try:
        rows = db.schema("app").rpc(
            "orphaned_activity_objects",
            {"p_older_than_hours": grace_hours, "p_limit": _MAX_PER_RUN},
        ).execute().data or []
    except Exception as exc:
        logger.error("activity retention: orphaned_activity_objects failed: %s", exc)
        return {"objects": 0, "error": str(exc)}

    if not rows:
        return {"objects": 0}

    paths = [r["path"] for r in rows if r.get("path")]
    removed = _remove_objects(paths)
    logger.info("activity retention: swept %d orphaned objects", removed)
    return {"objects": removed}


async def run_activity_retention() -> dict[str, Any]:
    """
    Both halves, in order.

    Expired files first, and the order earns its keep: if a batch of objects
    fails to delete but their catalogue rows go anyway, those objects are
    orphans by the time the sweep runs — and they are old, so they are past the
    grace period and get cleaned up in this same pass rather than tomorrow's.
    """
    pruned = prune_expired_files()
    swept = sweep_orphaned_objects()
    return {"pruned": pruned, "orphans": swept}
