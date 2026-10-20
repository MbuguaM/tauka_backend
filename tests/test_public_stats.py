"""`GET /public/stats` — the real learner count behind the signup page.

The page used to print a fabricated "1,047". The two properties that matter are
that the number comes from the database and that a failure is `None`, which the
page hides, rather than 0 or a guess.
"""

from types import SimpleNamespace
from unittest.mock import patch


class _FakeQuery:
    def __init__(self, result):
        self._result = result
        self.filters = []

    def select(self, *a, **k): return self

    def is_(self, column, value):
        self.filters.append((column, value))
        return self

    def execute(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeDB:
    def __init__(self, result):
        self.query = _FakeQuery(result)
        self.tables = []

    def schema(self, _name): return self

    def table(self, name):
        self.tables.append(name)
        return self.query


def test_reports_exact_count_of_live_learners(client):
    fake = _FakeDB(SimpleNamespace(data=[], count=2))
    with patch("app.routes.public_stats.db", fake):
        res = client.get("/public/stats")
    assert res.status_code == 200
    assert res.json() == {"learners": 2}
    assert fake.tables == ["student"]
    assert ("deleted_at", "null") in fake.query.filters


def test_zero_is_reported_as_zero(client):
    with patch("app.routes.public_stats.db", _FakeDB(SimpleNamespace(data=[], count=0))):
        assert client.get("/public/stats").json() == {"learners": 0}


def test_failure_is_none_not_zero(client):
    with patch("app.routes.public_stats.db", _FakeDB(RuntimeError("down"))):
        res = client.get("/public/stats")
    assert res.status_code == 200
    assert res.json() == {"learners": None}


def test_needs_no_auth(client):
    with patch("app.routes.public_stats.db", _FakeDB(SimpleNamespace(data=[], count=1))):
        assert client.get("/public/stats").status_code == 200
