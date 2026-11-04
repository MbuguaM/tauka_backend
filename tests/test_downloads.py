"""Installers behind sign-in (§155).

Pinned:
1. A link needs a signed-in person, and availability never holds an address.
2. A link is short-lived and only for a configured platform.
3. The update feed needs a good, unexpired token, and its installers become signed links.
4. A token can't be forged or edited.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.services import download_service as svc

SECRET = "feed-secret"
FEED = '''<rss><channel><item>
<enclosure url="https://tauka.io/dl/Tauka-1.2.0.exe" sparkle:dsaSignature="abc" length="5" />
<enclosure sparkle:version="2" url='Tauka-1.2.0.dmg' length="6" />
</item></channel></rss>'''


@pytest.fixture
def storage():
    bucket = MagicMock()
    bucket.create_signed_url.side_effect = lambda path, secs, opts: {"signedURL": f"https://s.test/{path}?token=t&exp={secs}"}
    bucket.download.return_value = FEED.encode()
    fake = MagicMock()
    fake.storage.from_.return_value = bucket
    with patch.object(svc, "db", fake), patch.object(svc.settings, "download_token_secret", SECRET), \
         patch.object(svc.settings, "download_path_windows", "Tauka-1.2.0.exe"), \
         patch.object(svc.settings, "download_path_macos", ""), patch.object(svc.settings, "download_path_android", ""), \
         patch.object(svc.settings, "api_base_url", "https://api.tauka.io"):
        yield bucket


# 1
def test_a_link_needs_a_signed_in_person(client, storage):
    assert client.post("/downloads/windows/link").status_code == 401
    assert client.post("/downloads/update-feed").status_code == 401
    storage.create_signed_url.assert_not_called()


def test_availability_is_public_and_holds_no_address(client, storage):
    res = client.get("/downloads/availability")
    assert res.status_code == 200
    assert res.json() == {"windows": True, "macos": False, "android": False}


# 2
def test_a_signed_in_person_gets_a_short_link(client, auth_headers, storage):
    res = client.post("/downloads/windows/link", headers=auth_headers)
    assert res.status_code == 200 and res.json()["expires_in"] == 300
    assert storage.create_signed_url.call_args.args[:2] == ("Tauka-1.2.0.exe", 300)


@pytest.mark.parametrize("platform", ["macos", "linux", "..%2Fsecret"])
def test_no_link_for_a_platform_with_no_installer(client, auth_headers, storage, platform):
    assert client.post(f"/downloads/{platform}/link", headers=auth_headers).status_code in (404, 405)
    storage.create_signed_url.assert_not_called()


def test_a_storage_failure_is_reported(client, auth_headers, storage):
    storage.create_signed_url.side_effect = RuntimeError("down")
    assert client.post("/downloads/windows/link", headers=auth_headers).status_code == 503


# 3
def test_a_signed_in_app_gets_a_feed_that_serves_signed_links(client, auth_headers, storage):
    url = client.post("/downloads/update-feed", headers=auth_headers).json()["feed_url"]
    assert url.startswith("https://api.tauka.io/downloads/appcast.xml?token=")
    res = client.get("/downloads/appcast.xml", params={"token": url.split("token=")[1]})
    assert res.status_code == 200 and res.headers["content-type"].startswith("application/xml")
    assert "tauka.io/dl" not in res.text
    assert "https://s.test/Tauka-1.2.0.exe?token=t&amp;exp=3600" in res.text
    assert "https://s.test/Tauka-1.2.0.dmg" in res.text
    assert 'sparkle:dsaSignature="abc"' in res.text


@pytest.mark.parametrize("token", ["", "nonsense", "1.2.3"])
def test_the_feed_refuses_a_bad_token(client, storage, token):
    assert client.get("/downloads/appcast.xml", params={"token": token}).status_code == 401
    storage.download.assert_not_called()


def test_the_feed_refuses_an_expired_token(storage):
    old = svc.feed_token("u1", datetime.now(timezone.utc) - timedelta(days=8))
    assert svc.token_ok(old) is False
    assert svc.token_ok(svc.feed_token("u1")) is True


def test_a_feed_name_cannot_leave_the_top_of_the_bucket(storage):
    svc.signed_appcast('<enclosure url="https://x.test/a/../../secret.exe" />')
    assert storage.create_signed_url.call_args.args[0] == "secret.exe"


# 4
def test_a_token_cannot_be_edited(storage):
    token = svc.feed_token("u1")
    expires, user, sig = token.split(".")
    assert svc.token_ok(f"{int(expires) + 999}.{user}.{sig}") is False
    assert svc.token_ok(f"{expires}.u2.{sig}") is False


def test_no_secret_means_no_feed(storage):
    with patch.object(svc.settings, "download_token_secret", ""):
        assert svc.token_ok("1.u.x") is False
        with pytest.raises(svc.DownloadRefused):
            svc.feed_token("u1")
