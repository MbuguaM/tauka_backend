"""A small in-memory stand-in for the Supabase client, shared by tests.

Enough PostgREST surface for the services: select / eq / neq / in_ / is_ / not_.is_ /
gte / gt / lte / lt / like / order / limit / insert / update / delete, plus
`rpc` hooks and per-table unique keys (a duplicate insert raises, like the
database). Inserted rows get an `id` when none is given.

    db = FakeDB({"tutor": [...]}, unique={"addon_purchase": [("stripe_session_id",)]})
    with patch("app.services.x.db", db): ...
"""
from types import SimpleNamespace


def _k(a, b):
    num = (int, float)
    if isinstance(a, num) and not isinstance(a, bool) and isinstance(b, num) and not isinstance(b, bool):
        return a, b
    return str(a), str(b)


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f, self.op, self._limit = db, name, [], ("select", None), None
        self._order = None

    def select(self, *a, **k): return self
    def order(self, col=None, desc=False, **k):
        self._order = (col, desc)
        return self
    def limit(self, n, *a, **k):
        self._limit = n
        return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def neq(self, c, v): self.f.append(lambda r: r.get(c) != v); return self
    def in_(self, c, vs): vs = list(vs); self.f.append(lambda r: r.get(c) in vs); return self
    def is_(self, c, v): self.f.append(lambda r: (r.get(c) is None) == (str(v) == "null")); return self

    @property
    def not_(self):
        """`.not_.is_(col, "null")`, as the client spells IS NOT NULL."""
        q = self

        class _Not:
            def is_(self, c, v):
                q.f.append(lambda r: (r.get(c) is None) != (str(v) == "null"))
                return q
        return _Not()
    # Numbers compare as numbers (as in Postgres: "2" <= "10" is wrong for an int
    # column); everything else (ISO dates and timestamps) as strings.
    def gte(self, c, v): self.f.append(lambda r: r.get(c) is not None and _k(r.get(c), v)[0] >= _k(r.get(c), v)[1]); return self
    def gt(self, c, v): self.f.append(lambda r: r.get(c) is not None and _k(r.get(c), v)[0] > _k(r.get(c), v)[1]); return self
    def lte(self, c, v): self.f.append(lambda r: r.get(c) is not None and _k(r.get(c), v)[0] <= _k(r.get(c), v)[1]); return self
    def lt(self, c, v): self.f.append(lambda r: r.get(c) is not None and _k(r.get(c), v)[0] < _k(r.get(c), v)[1]); return self
    def like(self, c, pat):
        pre = pat.rstrip("%")
        self.f.append(lambda r: str(r.get(c) or "").startswith(pre))
        return self
    def insert(self, p): self.op = ("insert", p); return self
    def upsert(self, p, **k): self.op = ("upsert", p); return self
    def update(self, p): self.op = ("update", p); return self
    def delete(self): self.op = ("delete", None); return self

    def execute(self):
        rows = self.db.rows.setdefault(self.name, [])
        kind, p = self.op
        if kind in ("insert", "upsert"):
            items = p if isinstance(p, list) else [p]
            out = []
            for item in items:
                row = {"id": f"{self.name}-{len(rows) + 1}", **item}
                for key in self.db.unique.get(self.name, []):
                    clash = [r for r in rows if all(r.get(k) == row.get(k) for k in key)
                             and all(row.get(k) is not None for k in key)]
                    if clash:
                        if kind == "upsert":
                            clash[0].update(item)
                            out.append(dict(clash[0]))
                            break
                        raise Exception(f"duplicate key value violates unique constraint ({self.name}: {key})")
                else:
                    rows.append(row)
                    out.append(dict(row))
            return SimpleNamespace(data=out)
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if kind == "update":
            for r in hit:
                r.update(p)
        elif kind == "delete":
            self.db.rows[self.name] = [r for r in rows if r not in hit]
        if self._order and self._order[0]:
            col, desc = self._order
            hit = sorted(hit, key=lambda r: str(r.get(col)), reverse=desc)
        if self._limit is not None:
            hit = hit[: self._limit]
        return SimpleNamespace(data=[dict(r) for r in hit])


class FakeDB:
    def __init__(self, rows: dict | None = None, unique: dict | None = None, rpcs: dict | None = None):
        self.rows = {k: [dict(r) for r in v] for k, v in (rows or {}).items()}
        self.unique = unique or {}
        self.rpcs = rpcs or {}
        self.rpc_calls = []

    def schema(self, _): return self
    def table(self, n): return _Q(self, n)

    def rpc(self, name, args):
        self.rpc_calls.append((name, args))
        fn = self.rpcs.get(name)
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=fn(self, args) if fn else None))
