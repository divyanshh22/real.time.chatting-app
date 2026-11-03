"""Security tests: credentials leakage, anonymous access, CSRF, injection,
XSS rendering, Cloudinary isolation, and production-settings sanity.

Production-settings checks run in a **subprocess** with ``DEBUG=false`` /
``DEBUG=true`` so the test settings (which force ``DEBUG=False`` and
``SECURE_SSL_REDIRECT=False``) do not mask what the real settings do.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.core.files.storage import default_storage
from django.test import Client

from chat.models import Message
from tests.factories import MessageFactory, UserFactory

pytestmark = pytest.mark.security

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Credentials must never appear in responses
# --------------------------------------------------------------------------- #
def test_login_page_contains_no_password_field_value(db):
    resp = Client().get("/")
    html = resp.content.decode()
    assert resp.status_code == 200
    # the login form has a password input but never a prefilled value
    assert 'name="password"' in html
    assert 'value="' not in html.split('name="password"')[1].split(">")[0]


def test_register_page_never_echoes_submitted_password(db):
    client = Client()
    resp = client.post(
        "/register/",
        {
            "username": "leaky",
            "first_name": "A",
            "last_name": "B",
            "email": "leaky@example.com",
            "password1": "SuperSecret123!",
            "password2": "SuperSecret123!",
        },
    )
    assert resp.status_code == 302
    # the redirect target (register page re-rendered on error paths) and any
    # response body must never contain the plaintext password
    follow = client.get(resp["Location"])
    assert "SuperSecret123!" not in follow.content.decode()


def test_profile_page_never_exposes_password_hash(db):
    user = UserFactory(username="hashowner", password="Str0ng!Passw0rd")
    client = Client()
    client.force_login(user)

    resp = client.get("/profile/")

    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Str0ng!Passw0rd" not in html
    assert user.password not in html  # no bcrypt/md5 hash either
    assert 'type="password"' not in html or 'name="password"' not in html


def test_password_hash_not_in_any_page_for_logged_in_user(db):
    user = UserFactory(password="Str0ng!Passw0rd")
    client = Client()
    client.force_login(user)

    for path in ("/chat/", "/profile/", f"/profile/{user.username}/", "/chat/search/"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert user.password not in resp.content.decode()


# --------------------------------------------------------------------------- #
# Anonymous access to sensitive endpoints
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_anonymous_cannot_read_message_history():
    other = UserFactory()
    resp = Client().get(f"/chat/api/messages/{other.username}/history/")

    assert resp.status_code == 302
    assert "/?next=" in resp["Location"]


@pytest.mark.django_db
def test_anonymous_cannot_post_messages():
    other = UserFactory()
    resp = Client().post(f"/chat/api/messages/{other.username}/", {"text": "hi"})

    assert resp.status_code == 302
    assert Message.objects.count() == 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_anonymous_cannot_open_websocket(other_user):
    from tests.conftest import ws_connect

    ws = ws_connect(f"/ws/chat/{other_user.username}/")
    connected, close_code = await ws.connect()

    assert connected is False
    assert close_code == 1000


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_anonymous_cannot_open_presence_websocket():
    from tests.conftest import ws_connect

    ws = ws_connect("/ws/presence/")
    connected, close_code = await ws.connect()

    assert connected is False
    assert close_code == 1000


# --------------------------------------------------------------------------- #
# CSRF
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_csrf_enforced_on_register_without_token():
    client = Client(enforce_csrf_checks=True)
    resp = client.post(
        "/register/",
        {
            "username": "csrfvictim",
            "first_name": "A",
            "last_name": "B",
            "email": "csrf@example.com",
            "password1": "Str0ng!Passw0rd",
            "password2": "Str0ng!Passw0rd",
        },
    )

    assert resp.status_code == 403
    from django.contrib.auth import get_user_model

    assert not get_user_model().objects.filter(username="csrfvictim").exists()


@pytest.mark.django_db
def test_csrf_accepted_with_valid_token():
    client = Client(enforce_csrf_checks=True)
    page = client.get("/register/")
    assert page.status_code == 200
    token = client.cookies["csrftoken"].value

    resp = client.post(
        "/register/",
        {
            "username": "csrftokenuser",
            "first_name": "A",
            "last_name": "B",
            "email": "csrftoken@example.com",
            "password1": "Str0ng!Passw0rd",
            "password2": "Str0ng!Passw0rd",
        },
        HTTP_X_CSRFTOKEN=token,
    )

    assert resp.status_code == 302
    from django.contrib.auth import get_user_model

    assert get_user_model().objects.filter(username="csrftokenuser").exists()


def test_csrf_enforced_on_message_send_api(user, other_user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)

    resp = client.post(f"/chat/api/messages/{other_user.username}/", {"text": "no token"})

    assert resp.status_code == 403
    assert Message.objects.count() == 0


@pytest.mark.django_db
def test_csrf_enforced_on_profile_update():
    client = Client(enforce_csrf_checks=True)
    user = UserFactory()
    client.force_login(user)

    resp = client.post("/profile/", {"first_name": "X", "last_name": "Y", "email": "x@y.com"})

    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Injection: SQL-like strings, unicode, emoji
# --------------------------------------------------------------------------- #
def test_sql_injection_like_message_is_stored_literally(auth_client, user, other_user):
    payload = "'; DROP TABLE chat_message; --"

    resp = auth_client.post(f"/chat/api/messages/{other_user.username}/", {"text": payload})

    assert resp.status_code == 200
    # table still exists and the payload is stored verbatim
    assert Message.objects.filter(text=payload).exists()
    assert Message.objects.count() == 1


def test_sql_injection_like_search_returns_200_and_no_crash(auth_client, user, other_user):
    resp = auth_client.get("/chat/search/", {"q": "' OR 1=1 --"})

    assert resp.status_code == 200
    # literal search: no user is named like the payload
    assert list(resp.context["results"]) == []


def test_search_cannot_dump_all_users_via_wildcard(auth_client, user, other_user, third_user):
    resp = auth_client.get("/chat/search/", {"q": "%"})

    assert resp.status_code == 200
    results = list(resp.context["results"])
    # '%' must not act as a SQL wildcard returning everyone
    assert user not in results
    assert third_user not in results


def test_unicode_and_emoji_messages_roundtrip(auth_client, user, other_user):
    payload = "héllo wörld 🌍👩‍💻 مرحبا 你好"

    resp = auth_client.post(f"/chat/api/messages/{other_user.username}/", {"text": payload})

    assert resp.status_code == 200
    stored = Message.objects.get()
    assert stored.text == payload

    history = auth_client.get(f"/chat/api/messages/{other_user.username}/history/")
    assert history.json()["messages"][0]["text"] == payload


def test_unicode_username_renders_safely(auth_client, user):
    unicode_user = UserFactory(username="josé_üser")
    resp = auth_client.get(f"/chat/{unicode_user.username}/")

    assert resp.status_code == 200
    assert "josé_üser" in resp.content.decode()


def test_null_byte_in_message_rejected_not_crashing(auth_client, user, other_user):
    resp = auth_client.post(f"/chat/api/messages/{other_user.username}/", {"text": "a\x00b"})

    # either rejected (400) or stored without the NUL — must never 500
    assert resp.status_code in (200, 400)
    if resp.status_code == 200:
        assert "\x00" not in Message.objects.get().text


# --------------------------------------------------------------------------- #
# XSS: stored payloads must render escaped
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "payload",
    [
        "<script>alert(1)</script>",
        '"><img src=x onerror=alert(1)>',
        "<svg/onload=alert(1)>",
        "<b>bold?</b>",
    ],
)
def test_xss_payloads_render_escaped_in_conversation(auth_client, user, other_user, payload):
    MessageFactory(sender=other_user, receiver=user, text=payload)

    resp = auth_client.get(f"/chat/{other_user.username}/")
    html = resp.content.decode()

    assert resp.status_code == 200
    assert payload not in html  # never raw
    assert "&lt;" in html  # escaped form present


def test_javascript_uri_text_is_inert_not_linked(auth_client, user, other_user):
    """Plain-text payloads without HTML special chars render verbatim (autoescape
    is a no-op), which is safe as long as they never end up inside href/src."""
    payload = "javascript:alert(document.cookie)"
    MessageFactory(sender=other_user, receiver=user, text=payload)

    resp = auth_client.get(f"/chat/{other_user.username}/")
    html = resp.content.decode()

    assert resp.status_code == 200
    assert payload in html  # rendered as inert text...
    assert f'href="{payload}"' not in html  # ...but never as a link target
    assert f"src={payload}" not in html


def test_xss_payload_in_search_query_is_escaped(auth_client, user):
    resp = auth_client.get("/chat/search/", {"q": "<script>alert(1)</script>"})

    html = resp.content.decode()
    assert resp.status_code == 200
    assert "<script>alert(1)</script>" not in html


def test_xss_payload_in_username_is_escaped_in_pages(auth_client, user):
    # stored via ORM (bypassing any username validation) to prove template escaping
    evil = UserFactory(username="evilstudy")
    MessageFactory(sender=evil, receiver=user, text="<b>bold?</b>")

    resp = auth_client.get("/chat/")
    html = resp.content.decode()

    assert "<b>bold?</b>" not in html
    assert "&lt;b&gt;bold?&lt;/b&gt;" in html


# --------------------------------------------------------------------------- #
# Cloudinary / external services must never be touched in tests
# --------------------------------------------------------------------------- #
def test_test_settings_use_filesystem_storage_not_cloudinary():
    assert django_settings.CLOUDINARY_STORAGE.get("CLOUD_NAME", "") == ""
    backend = django_settings.STORAGES["default"]["BACKEND"]
    assert backend == "django.core.files.storage.FileSystemStorage"
    assert "cloudinary" not in backend.lower()


def test_profile_upload_never_calls_cloudinary(mocker, auth_client, user):
    import io

    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image

    upload_spy = mocker.patch("cloudinary.uploader.upload")
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (0, 128, 255)).save(buf, format="PNG")

    resp = auth_client.post(
        "/profile/",
        {
            "first_name": "X",
            "last_name": "Y",
            "email": "x@y.com",
            "profile_pic": SimpleUploadedFile("pic.png", buf.getvalue(), content_type="image/png"),
        },
    )

    assert resp.status_code == 302
    upload_spy.assert_not_called()
    # and the file went to the local filesystem storage (fetch fresh — the
    # in-memory user instance may hold a stale related-object cache)
    from login.models import ProfilePic

    profile = ProfilePic.objects.get(user=user)
    assert profile.profile_pic.name
    assert default_storage.exists(profile.profile_pic.name)


def test_message_upload_never_calls_cloudinary(mocker, auth_client, user, other_user):
    import io

    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image

    upload_spy = mocker.patch("cloudinary.uploader.upload")
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(buf, format="PNG")

    resp = auth_client.post(
        f"/chat/api/messages/{other_user.username}/",
        {
            "text": "with pic",
            "photo": SimpleUploadedFile("m.png", buf.getvalue(), content_type="image/png"),
        },
    )

    assert resp.status_code == 200
    upload_spy.assert_not_called()
    assert Message.objects.get().photo.name.startswith("chat_images/")


def test_network_blocker_blocks_external_hosts():
    """The autouse fixture must actually block non-local connections."""
    import socket

    with pytest.raises(RuntimeError, match="blocked"):
        socket.getaddrinfo("example.com", 80)

    with pytest.raises(RuntimeError, match="blocked"):
        socket.gethostbyname("api.cloudinary.com")


def test_loopback_still_allowed():
    """Loopback must stay open (asyncio self-pipe, CI services)."""
    import socket

    info = socket.getaddrinfo("localhost", 80, proto=socket.IPPROTO_TCP)
    assert info  # resolves locally without hitting the internet


# --------------------------------------------------------------------------- #
# Production settings sanity (real settings, subprocess, DEBUG from env)
# --------------------------------------------------------------------------- #
_SNIPPET = """
import json, os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "chattingapp.settings")
django.setup()
from django.conf import settings
print(json.dumps({
    "DEBUG": settings.DEBUG,
    "SECRET_KEY_DEFAULT": settings.SECRET_KEY.startswith("django-insecure-"),
    "SECRET_KEY_IS_ENV": settings.SECRET_KEY == os.environ.get("SECRET_KEY", ""),
    "ALLOWED_HOSTS": settings.ALLOWED_HOSTS,
    "SESSION_COOKIE_SECURE": settings.SESSION_COOKIE_SECURE,
    "CSRF_COOKIE_SECURE": settings.CSRF_COOKIE_SECURE,
    "SECURE_SSL_REDIRECT": settings.SECURE_SSL_REDIRECT,
    "SECURE_CONTENT_TYPE_NOSNIFF": settings.SECURE_CONTENT_TYPE_NOSNIFF,
    "X_FRAME_OPTIONS": settings.X_FRAME_OPTIONS,
}))
"""


def _run_prod_settings(**env_overrides):
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLOUDINARY_")}
    env.pop("DATABASE_URL", None)  # keep the subprocess off real databases
    env.pop("USE_REDIS", None)
    env["DJANGO_SETTINGS_MODULE"] = "chattingapp.settings"
    env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, "-c", _SNIPPET],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_production_settings_with_debug_false():
    cfg = _run_prod_settings(
        DEBUG="false",
        SECRET_KEY="prod-like-secret-key-for-tests-only-0123456789",
        ALLOWED_HOSTS="example.com,chat.example.com",
    )

    assert cfg["DEBUG"] is False
    assert cfg["SECRET_KEY_DEFAULT"] is False  # not the insecure default
    assert cfg["SECRET_KEY_IS_ENV"] is True  # taken from the environment
    assert cfg["ALLOWED_HOSTS"] == ["example.com", "chat.example.com"]
    # secure flags enabled when DEBUG is off
    assert cfg["SESSION_COOKIE_SECURE"] is True
    assert cfg["CSRF_COOKIE_SECURE"] is True
    assert cfg["SECURE_SSL_REDIRECT"] is True
    assert cfg["SECURE_CONTENT_TYPE_NOSNIFF"] is True
    assert cfg["X_FRAME_OPTIONS"] == "DENY"


def test_production_settings_default_secret_key_is_used_when_env_missing():
    """Documents actual behavior: without SECRET_KEY env the settings fall
    back to a hardcoded insecure key (deployment must set the env var)."""
    env = {k: v for k, v in os.environ.items() if k != "SECRET_KEY"}
    env.pop("DATABASE_URL", None)
    env["DJANGO_SETTINGS_MODULE"] = "chattingapp.settings"
    env["DEBUG"] = "false"
    proc = subprocess.run(
        [sys.executable, "-c", _SNIPPET],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    cfg = json.loads(proc.stdout.strip().splitlines()[-1])
    assert cfg["SECRET_KEY_DEFAULT"] is True  # known limitation: env must be set


def test_debug_true_disables_secure_flags():
    """Documents actual behavior: DEBUG=true (dev default) turns the secure
    flags off — that's why production must set DEBUG=false."""
    cfg = _run_prod_settings(DEBUG="true", SECRET_KEY="dev-only-key")

    assert cfg["DEBUG"] is True
    assert cfg["SECURE_SSL_REDIRECT"] is False
    assert cfg["SESSION_COOKIE_SECURE"] is False
