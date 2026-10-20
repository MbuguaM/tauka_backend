"""`POST /tutor-applications` — the /teach form, stored (plan §2j J2, §120).

J0 emailed the team and stored nothing; J2 stores the application, sends the
applicant a receipt with a withdraw link, and still copies the team. What
matters:
1. "ok" only when the application was STORED (a store failure is 503); the
   answer is the same however the emails went.
2. A second submission from the same email updates the open application.
3. The withdraw token is stored hashed; withdrawing sets the 12-month delete date.
4. Bad input is refused; rate limits per IP and per email; the honeypot drops.
5. The sweep deletes only rejected/withdrawn rows past `delete_after`.
6. Applicant text is escaped in both emails.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import tutor_application_service as apps

GOOD = {
    "name": "Yonas Tesfaye",
    "email": "Yonas@Example.com",
    "location": "Addis Ababa, Ethiopia",
    "languages": "Amharic (native)",
    "experience": "3–5 years",
    "specialisations": ["Beginners", "Heritage learners"],
    "video": "https://youtu.be/abc",
    "why": "I love teaching.",
}


class _Q:
    def __init__(self, db):
        self.db, self.f, self.op = db, [], ("select", None)

    def select(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self
    def lt(self, c, v): self.f.append(lambda r: r.get(c) is not None and str(r.get(c)) < str(v)); return self
    def insert(self, p): self.op = ("insert", p); return self
    def update(self, p): self.op = ("update", p); return self
    def delete(self): self.op = ("delete", None); return self

    def execute(self):
        kind, p = self.op
        if self.db.broken:
            raise Exception("database down")
        if kind == "insert":
            row = {"id": f"app{len(self.db.rows) + 1}", "status": "submitted", "delete_after": None, **p}
            self.db.rows.append(row)
            return SimpleNamespace(data=[row])
        hit = [r for r in self.db.rows if all(f(r) for f in self.f)]
        if kind == "update":
            for r in hit:
                r.update(p)
        if kind == "delete":
            self.db.rows[:] = [r for r in self.db.rows if r not in hit]
        return SimpleNamespace(data=[dict(r) for r in hit])


class _DB:
    def __init__(self):
        self.rows, self.broken = [], False

    def schema(self, _): return self
    def table(self, _): return _Q(self)


@pytest.fixture
def db():
    fake = _DB()
    with patch("app.services.tutor_application_service.db", fake):
        yield fake


@pytest.fixture
def send():
    mock = AsyncMock(return_value={"id": "x", "status": "sent"})
    with patch("app.services.email_service.send_template_email", mock), \
         patch("app.services.tutor_application_service.settings.admin_email", "team@tauka.io"):
        yield mock


# 1
def test_stored_receipt_and_team_copy(client, db, send):
    resp = client.post("/tutor-applications", json=GOOD)
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    row = db.rows[0]
    assert row["email"] == "yonas@example.com" and row["experience_band"] == "3_5"
    assert row["status"] == "submitted" and row["withdraw_token_hash"]
    to = [c.args[0] for c in send.await_args_list]
    assert to == ["yonas@example.com", "team@tauka.io"]
    receipt = send.await_args_list[0].args[2]
    token = receipt["withdraw_url"].split("token=")[1]
    assert apps._hash(token) == row["withdraw_token_hash"] and token not in str(row)
    assert send.await_args_list[1].kwargs["reply_to"] == "yonas@example.com"


def test_store_failure_is_503_and_mail_failure_is_still_ok(client, db, send):
    send.return_value = {"id": "", "status": "failed"}
    assert client.post("/tutor-applications", json=GOOD).status_code == 200
    db.broken = True
    assert client.post("/tutor-applications", json={**GOOD, "email": "other@example.com"}).status_code == 503


# 2
def test_second_submission_updates_the_open_one(client, db, send):
    client.post("/tutor-applications", json=GOOD)
    client.post("/tutor-applications", json={**GOOD, "why": "Updated."})
    assert len(db.rows) == 1 and db.rows[0]["motivation"] == "Updated."
    db.rows[0]["status"] = "rejected"
    client.post("/tutor-applications", json=GOOD)                     # a decided one isn't reopened
    assert len(db.rows) == 2


# 3
def test_withdraw(client, db, send):
    client.post("/tutor-applications", json=GOOD)
    token = send.await_args_list[0].args[2]["withdraw_url"].split("token=")[1]
    assert client.post("/tutor-applications/withdraw", json={"token": "x" * 43}).json() == {"ok": False}
    assert client.post("/tutor-applications/withdraw", json={"token": token}).json() == {"ok": True}
    row = db.rows[0]
    assert row["status"] == "withdrawn" and row["withdraw_token_hash"] is None and row["delete_after"]
    assert client.post("/tutor-applications/withdraw", json={"token": token}).json() == {"ok": False}


# 4
@pytest.mark.parametrize("patch_body", [
    {"name": ""}, {"email": "nope"}, {"languages": ""}, {"experience": "20 years"},
    {"specialisations": ["Astrology"]}, {"video": "javascript:alert(1)"}, {"why": "x" * 3001},
])
def test_rejects_bad_input(client, db, send, patch_body):
    assert client.post("/tutor-applications", json={**GOOD, **patch_body}).status_code == 422
    assert db.rows == []


def test_rate_limited(client, db, send):
    with patch("app.routes.tutor_applications.check_anonymous_rate_limit", return_value=(False, "Too many")):
        assert client.post("/tutor-applications", json=GOOD).status_code == 429


def test_honeypot_is_dropped(client, db, send):
    assert client.post("/tutor-applications", json={**GOOD, "website": "http://spam"}).json() == {"ok": True}
    assert db.rows == [] and send.await_count == 0


# 5
def test_sweep(db):
    now = datetime(2027, 1, 1, tzinfo=timezone.utc)
    old, new = (now - timedelta(days=1)).isoformat(), (now + timedelta(days=1)).isoformat()
    db.rows += [{"id": "a", "status": "rejected", "delete_after": old},
                {"id": "b", "status": "withdrawn", "delete_after": new},
                {"id": "c", "status": "approved", "delete_after": old}]
    assert apps.sweep(now) == 1
    assert [r["id"] for r in db.rows] == ["b", "c"]


# 6
def test_applicant_text_is_escaped_in_both_emails():
    from app.services.email_service import _jinja_env
    html = _jinja_env.get_template("tutor_application.html").render(
        subject="s", web_base_url="https://tauka.io", api_base_url="https://api.tauka.io",
        name="<script>x</script>", email="a@b.co", location="", languages="Amharic",
        experience=None, specialisations=[], video="https://evil.example/<b>", why="<img src=x>",
    )
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html
    assert "<img src=x>" not in html
    assert 'href="https://evil.example' not in html
    receipt = _jinja_env.get_template("tutor_application_receipt.html").render(
        subject="s", name="<b>Yo</b>", withdraw_url="https://tauka.io/teach/withdraw?token=abc")
    assert "<b>Yo</b>" not in receipt and "/teach/withdraw?token=abc" in receipt
