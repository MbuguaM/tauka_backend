"""
Phase 12 — activity retention.

Everything here is about ORDER and PARTIAL FAILURE, which is the only part of
this job that can go wrong in a way that matters. The happy path deletes things;
the interesting paths are the ones where half of it deletes and the process then
stops, because that is what a serverless timeout looks like.

The `db` handle is a MagicMock (see conftest), so each test wires the two RPCs
and the storage client and then asserts on the call sequence.
"""
import pytest
from unittest.mock import MagicMock

from app.services import activity_retention_service as svc


def _rpc_returning(mapping: dict[str, list[dict]]):
    """
    Build a fake `db.schema("app")` whose `.rpc(name, ...)` answers from
    `mapping`, and whose `.table(...).delete().in_(...).execute()` records calls.
    """
    schema = MagicMock()

    def rpc(name, params=None, **kw):
        builder = MagicMock()
        builder.execute.return_value = MagicMock(data=mapping.get(name, []))
        return builder

    schema.rpc.side_effect = rpc
    return schema


@pytest.fixture
def db(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(svc, "db", fake)
    return fake


# ---------------------------------------------------------------------------
# prune_expired_files
# ---------------------------------------------------------------------------

def test_nothing_expired_touches_nothing(db):
    db.schema.return_value = _rpc_returning({"expired_activity_files": []})

    result = svc.prune_expired_files()

    assert result == {"files": 0, "objects": 0}
    db.storage.from_.assert_not_called()


def test_object_is_deleted_before_its_catalogue_row(db):
    """
    The ordering rule. Object first: a crash after it leaves a row pointing at
    nothing, which the NEXT run re-selects and finishes, because covers_to is
    still past the cutoff. The other order leaks an object that only the orphan
    sweep can find.
    """
    order: list[str] = []
    schema = _rpc_returning({
        "expired_activity_files": [
            {"id": "f1", "user_id": "u1", "path": "u1/d1/a.jsonl", "covers_to": "2026-01-01"},
        ]
    })
    schema.table.return_value.delete.return_value.in_.return_value.execute.side_effect = (
        lambda: order.append("row")
    )
    db.schema.return_value = schema
    db.storage.from_.return_value.remove.side_effect = lambda paths: order.append("object")

    svc.prune_expired_files()

    assert order == ["object", "row"], "object must be removed before its row"


def test_expired_files_are_removed_and_counted(db):
    rows = [
        {"id": f"f{i}", "user_id": "u1", "path": f"u1/d1/{i}.jsonl", "covers_to": "2026-01-01"}
        for i in range(3)
    ]
    db.schema.return_value = _rpc_returning({"expired_activity_files": rows})

    result = svc.prune_expired_files()

    assert result == {"files": 3, "objects": 3}
    db.storage.from_.assert_called_with("activity")
    db.storage.from_.return_value.remove.assert_called_once_with(
        ["u1/d1/0.jsonl", "u1/d1/1.jsonl", "u1/d1/2.jsonl"]
    )


def test_retention_window_is_passed_through(db):
    schema = _rpc_returning({"expired_activity_files": []})
    db.schema.return_value = schema

    svc.prune_expired_files(retention_days=30)

    name, params = schema.rpc.call_args[0]
    assert name == "expired_activity_files"
    assert params["p_retention_days"] == 30


def test_default_window_is_ninety_days(db):
    """Matches kActivityRetentionDays in the Flutter client."""
    schema = _rpc_returning({"expired_activity_files": []})
    db.schema.return_value = schema

    svc.prune_expired_files()

    assert schema.rpc.call_args[0][1]["p_retention_days"] == 90
    assert svc.RETENTION_DAYS == 90


def test_a_failing_object_batch_still_deletes_the_rest(db):
    """
    Partial failure must not abandon the run. 150 paths is two batches; the
    first throws.
    """
    rows = [
        {"id": f"f{i}", "user_id": "u1", "path": f"u1/d1/{i}.jsonl", "covers_to": "2026-01-01"}
        for i in range(150)
    ]
    db.schema.return_value = _rpc_returning({"expired_activity_files": rows})

    calls = {"n": 0}

    def flaky(paths):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("storage 500")

    db.storage.from_.return_value.remove.side_effect = flaky

    result = svc.prune_expired_files()

    assert calls["n"] == 2, "the second batch must still be attempted"
    assert result["objects"] == 50, "only the batch that succeeded is counted"
    assert result["files"] == 150, "catalogue rows go regardless — see the orphan sweep"


def test_a_failing_rpc_reports_rather_than_raises(db):
    schema = MagicMock()
    schema.rpc.side_effect = RuntimeError("function does not exist")
    db.schema.return_value = schema

    result = svc.prune_expired_files()

    assert result["files"] == 0
    assert "error" in result


def test_rows_without_a_path_are_skipped_not_crashed_on(db):
    db.schema.return_value = _rpc_returning({
        "expired_activity_files": [
            {"id": "f1", "user_id": "u1", "path": None, "covers_to": "2026-01-01"},
            {"id": "f2", "user_id": "u1", "path": "u1/d1/b.jsonl", "covers_to": "2026-01-01"},
        ]
    })

    result = svc.prune_expired_files()

    db.storage.from_.return_value.remove.assert_called_once_with(["u1/d1/b.jsonl"])
    assert result["files"] == 2


# ---------------------------------------------------------------------------
# sweep_orphaned_objects
# ---------------------------------------------------------------------------

def test_no_orphans_touches_nothing(db):
    db.schema.return_value = _rpc_returning({"orphaned_activity_objects": []})

    assert svc.sweep_orphaned_objects() == {"objects": 0}
    db.storage.from_.assert_not_called()


def test_orphans_are_removed(db):
    db.schema.return_value = _rpc_returning({
        "orphaned_activity_objects": [
            {"path": "gone-user/d1/a.jsonl", "created_at": "2026-01-01T00:00:00Z"},
            {"path": "gone-user/d2/b.jsonl", "created_at": "2026-01-01T00:00:00Z"},
        ]
    })

    result = svc.sweep_orphaned_objects()

    assert result == {"objects": 2}
    db.storage.from_.return_value.remove.assert_called_once_with(
        ["gone-user/d1/a.jsonl", "gone-user/d2/b.jsonl"]
    )


def test_grace_period_is_passed_through_and_defaults_to_a_day(db):
    """
    The guard that stops the sweep eating a chunk uploaded seconds ago whose
    catalogue row has not landed yet.
    """
    schema = _rpc_returning({"orphaned_activity_objects": []})
    db.schema.return_value = schema

    svc.sweep_orphaned_objects()
    assert schema.rpc.call_args[0][1]["p_older_than_hours"] == 24

    svc.sweep_orphaned_objects(grace_hours=6)
    assert schema.rpc.call_args[0][1]["p_older_than_hours"] == 6


def test_a_failing_orphan_rpc_reports_rather_than_raises(db):
    schema = MagicMock()
    schema.rpc.side_effect = RuntimeError("permission denied")
    db.schema.return_value = schema

    result = svc.sweep_orphaned_objects()

    assert result["objects"] == 0
    assert "error" in result


# ---------------------------------------------------------------------------
# The pass as a whole
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prune_runs_before_the_orphan_sweep(db):
    """
    Order matters for a reason worth keeping: objects whose row was deleted but
    whose delete failed are orphans, and they are old, so the sweep cleans them
    up in this same pass instead of tomorrow's.
    """
    seen: list[str] = []

    schema = MagicMock()

    def rpc(name, params=None, **kw):
        seen.append(name)
        builder = MagicMock()
        builder.execute.return_value = MagicMock(data=[])
        return builder

    schema.rpc.side_effect = rpc
    db.schema.return_value = schema

    result = await svc.run_activity_retention()

    assert seen == ["expired_activity_files", "orphaned_activity_objects"]
    assert result == {"pruned": {"files": 0, "objects": 0}, "orphans": {"objects": 0}}


@pytest.mark.asyncio
async def test_the_task_wrapper_swallows_failure(db):
    """
    A failure to delete is retained data, never lost data — so it must not stop
    the other daily jobs.
    """
    from app.tasks.activity_retention import run_activity_retention_task

    schema = MagicMock()
    schema.rpc.side_effect = RuntimeError("boom")
    db.schema.return_value = schema

    await run_activity_retention_task()  # must not raise


def test_activity_retention_is_registered_in_the_daily_runner():
    """
    A task nothing calls is the failure this codebase has already had once
    (ActivityQueue.flush had no caller for three sessions).
    """
    import inspect
    from app.routes import tasks

    source = inspect.getsource(tasks.run_daily_tasks)
    assert "run_activity_retention_task" in source
    assert '"activity_retention"' in source
