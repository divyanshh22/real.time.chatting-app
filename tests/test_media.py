"""Image upload tests: storage failures must never surface as HTTP 500s.

Production root cause (reproduced live): ``default_storage.save()`` called
Cloudinary with credentials the API rejected
(``cloudinary.exceptions.AuthorizationRequired``), which the views let
escape as an unhandled exception -> HTTP 500 on both profile-picture upload
and DM photo send.

These tests simulate ANY storage outage at the storage layer itself and
assert that each endpoint degrades gracefully: friendly error, clean
redirect/JSON, no partial rows, and the full happy path still works with
the normal filesystem storage. The Cloudinary environment wiring (enable,
fail-fast on partial config, quote/whitespace stripping) is covered at the
settings level below.
"""

import io
import os

import pytest
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image

from chat.models import Message
from login.models import ProfilePic

CLOUDINARY_KEYS = (
    "CLOUDINARY_CLOUD_NAME",
    "CLOUDINARY_API_KEY",
    "CLOUDINARY_API_SECRET",
)


def png_upload(name="pic.png", size=(32, 32)):
    """A small, valid PNG upload that passes image validation."""
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


@pytest.fixture
def broken_storage(monkeypatch):
    """Make EVERY file save fail, exactly like a rejecting Cloudinary."""

    def _save(self, name, content, max_length=None):
        raise OSError("simulated storage outage")

    monkeypatch.setattr(FileSystemStorage, "save", _save)


# --------------------------------------------------------------------------- #
# Happy path still works with normal storage
# --------------------------------------------------------------------------- #
def test_profile_picture_upload_succeeds_with_working_storage(auth_client, user):
    resp = auth_client.post(
        reverse("profile-view"),
        {
            "first_name": "Pic",
            "last_name": "Tester",
            "email": "pic@example.com",
            "profile_pic": png_upload("ok.png"),
        },
        follow=True,
    )

    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Profile updated!" in html
    profile = ProfilePic.objects.get(user=user)
    assert profile.profile_pic.name.startswith("profile_pics/")
    user.refresh_from_db()
    assert user.first_name == "Pic"


def test_dm_image_send_succeeds_with_working_storage(auth_client, user, other_user):
    resp = auth_client.post(
        reverse("chat:send-message-api", kwargs={"username": other_user.username}),
        {"text": "with photo", "photo": png_upload("dm.png")},
    )

    assert resp.status_code == 200
    message = resp.json()["message"]
    assert message["photo_url"]
    msg = Message.objects.get()
    assert msg.photo.name.startswith("chat_images/")


# --------------------------------------------------------------------------- #
# Storage outage -> graceful, no 500
# --------------------------------------------------------------------------- #
def test_profile_upload_storage_failure_is_not_500(auth_client, user, broken_storage):
    resp = auth_client.post(
        reverse("profile-view"),
        {
            "first_name": "Still",
            "last_name": "Saved",
            "email": "still@example.com",
            "profile_pic": png_upload("doomed.png"),
        },
        follow=True,
    )

    assert resp.status_code == 200  # NOT a 500
    html = resp.content.decode()
    assert "picture could not be uploaded" in html
    assert "Profile details saved" in html

    user.refresh_from_db()
    assert user.first_name == "Still"  # non-file fields still saved
    profile = ProfilePic.objects.get(user=user)
    assert not profile.profile_pic.name  # nothing half-written


def test_profile_upload_failure_keeps_previous_picture(auth_client, user, monkeypatch):
    """A failed REPLACEMENT must not destroy the existing avatar."""
    good = auth_client.post(
        reverse("profile-view"),
        {"first_name": "A", "last_name": "B", "email": "a@b.com", "profile_pic": png_upload("first.png")},
        follow=True,
    )
    assert good.status_code == 200
    before = ProfilePic.objects.get(user=user).profile_pic.name
    assert before

    def flaky(self, name, content, max_length=None):
        # Patched AFTER the first upload succeeded, so every save from here
        # on (i.e. the replacement attempt) fails.
        raise OSError("storage outage")

    monkeypatch.setattr(FileSystemStorage, "save", flaky)

    resp = auth_client.post(
        reverse("profile-view"),
        {"first_name": "A", "last_name": "B", "email": "a@b.com", "profile_pic": png_upload("second.png")},
        follow=True,
    )

    assert resp.status_code == 200
    assert "picture could not be uploaded" in resp.content.decode()
    assert ProfilePic.objects.get(user=user).profile_pic.name == before


def test_send_message_api_storage_failure_returns_json_error(auth_client, user, other_user, broken_storage):
    resp = auth_client.post(
        reverse("chat:send-message-api", kwargs={"username": other_user.username}),
        {"text": "photo incoming", "photo": png_upload("doomed.png")},
    )

    assert resp.status_code == 502  # handled error, not an unhandled 500
    payload = resp.json()
    assert "storage" in payload["error"].lower()
    assert Message.objects.count() == 0  # nothing half-persisted


def test_send_message_api_text_only_works_even_when_storage_down(auth_client, user, other_user, broken_storage):
    resp = auth_client.post(
        reverse("chat:send-message-api", kwargs={"username": other_user.username}),
        {"text": "plain text survives"},
    )

    assert resp.status_code == 200
    assert Message.objects.get().text == "plain text survives"


def test_conversation_post_storage_failure_redirects_with_error(auth_client, user, other_user, broken_storage):
    resp = auth_client.post(
        reverse("chat:conversation-view", kwargs={"username": other_user.username}),
        {"text": "photo incoming", "photo": png_upload("doomed.png")},
        follow=True,
    )

    assert resp.status_code == 200  # NOT a 500
    html = resp.content.decode()
    assert "file storage service is unavailable" in html
    assert Message.objects.count() == 0


# --------------------------------------------------------------------------- #
# Cloudinary environment wiring (chattingapp.settings)
# --------------------------------------------------------------------------- #
def test_clean_env_strips_quotes_and_whitespace(monkeypatch):
    from chattingapp.settings import _clean_env

    monkeypatch.setenv("TEST_CLOUDINARY_PROBE", '  "abc123"  \n')
    assert _clean_env("TEST_CLOUDINARY_PROBE") == "abc123"

    monkeypatch.delenv("TEST_CLOUDINARY_PROBE", raising=False)
    assert _clean_env("TEST_CLOUDINARY_PROBE") == ""


def test_cloudinary_status_enabled_only_with_all_three_values(monkeypatch):
    from chattingapp.settings import cloudinary_status

    for key in CLOUDINARY_KEYS:
        monkeypatch.delenv(key, raising=False)

    creds, enabled, error = cloudinary_status()
    assert (enabled, error) == (False, None)

    monkeypatch.setenv("CLOUDINARY_CLOUD_NAME", " my-cloud ")
    creds, enabled, error = cloudinary_status()
    assert enabled is False
    assert error is not None
    assert "CLOUDINARY_API_KEY" in error
    assert "CLOUDINARY_API_SECRET" in error
    assert creds["CLOUD_NAME"] == "my-cloud"  # stripped even in the error path

    monkeypatch.setenv("CLOUDINARY_API_KEY", "123")
    monkeypatch.setenv("CLOUDINARY_API_SECRET", "shh")
    creds, enabled, error = cloudinary_status()
    assert (enabled, error) == (True, None)
    assert creds == {
        "CLOUD_NAME": "my-cloud",
        "API_KEY": "123",
        "API_SECRET": "shh",
    }


def test_settings_module_wires_cloudinary_storage_and_fails_fast(monkeypatch):
    """Module-level wiring: all three values -> Cloudinary storage + app
    registration; partial config -> ImproperlyConfigured at startup (clear
    Render log line instead of upload-time 500s); none -> local disk.

    Reloading ``chattingapp.settings`` is safe here: the ACTIVE test
    settings module is ``settings_test`` (a snapshot taken at startup), so
    Django's runtime configuration is unaffected.
    """
    import importlib

    from django.core.exceptions import ImproperlyConfigured

    import chattingapp.settings as settings_module

    saved = {key: os.environ.get(key) for key in CLOUDINARY_KEYS}

    def _set(cloud=None, key=None, secret=None):
        for name, value in zip(CLOUDINARY_KEYS, (cloud, key, secret)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    try:
        # (1) complete config -> Cloudinary enabled
        _set("fake_cloud", "12345", "fake_secret")
        importlib.reload(settings_module)
        assert settings_module.STORAGES["default"]["BACKEND"] == "cloudinary_storage.storage.MediaCloudinaryStorage"
        assert "cloudinary_storage" in settings_module.INSTALLED_APPS
        assert settings_module.CLOUDINARY_STORAGE["CLOUD_NAME"] == "fake_cloud"

        # (2) partial config -> fail fast with an explicit message
        _set("fake_cloud", None, "fake_secret")
        with pytest.raises(ImproperlyConfigured, match="Incomplete CLOUDINARY"):
            importlib.reload(settings_module)

        # (3) no config -> local filesystem (dev / tests)
        _set(None, None, None)
        importlib.reload(settings_module)
        assert settings_module.STORAGES["default"]["BACKEND"] == "django.core.files.storage.FileSystemStorage"
        assert "cloudinary_storage" not in settings_module.INSTALLED_APPS
        assert settings_module.CLOUDINARY_STORAGE == {
            "CLOUD_NAME": "",
            "API_KEY": "",
            "API_SECRET": "",
        }
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(settings_module)  # restore original state


# --------------------------------------------------------------------------- #
# Optional local /media/ route exists for filesystem storage deployments
# --------------------------------------------------------------------------- #
def test_media_url_is_served_locally_for_filesystem_storage():
    """Filesystem-storage deployments get a local /media/ route (urls.py);
    Cloudinary deployments intentionally do not."""
    from django.views.static import serve as static_serve

    from django.urls import resolve

    match = resolve("/media/profile_pics/whatever.png")
    assert match.func is static_serve
