"""Input-validation tests.

IMPORTANT — this project has **no forms.py / Django Form classes**.
All validation lives inline in the views (``login/views.py`` and
``chat/views.py``), so this file tests the *view-level* validation behavior:

* registration input (required fields, duplicates, email format, password
  strength, password mismatch),
* login input (missing fields),
* message input (empty / whitespace-only / overly long text),
* file-upload validation (type and size) for profile pictures and message photos.
"""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from PIL import Image

from chat.models import Message
from login.models import CustomUser, ProfilePic

pytestmark = pytest.mark.integration


def _png_upload(name="pic.png"):
    """Build a small valid PNG upload in memory."""
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 0, 0)).save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


def _txt_upload(name="evil.txt", content=b"definitely not an image"):
    return SimpleUploadedFile(name, content, content_type="text/plain")


def _register(client, **overrides):
    data = {
        "username": "newuser",
        "first_name": "New",
        "last_name": "User",
        "email": "newuser@example.com",
        "password1": "Str0ng!Passw0rd",
        "password2": "Str0ng!Passw0rd",
    }
    data.update(overrides)
    return client.post("/register/", data)


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def test_register_valid_data_creates_user(db):
    client = Client()
    resp = _register(client)

    assert resp.status_code == 302
    assert resp["Location"] == "/"  # redirects to login-view
    user = CustomUser.objects.get(username="newuser")
    assert user.check_password("Str0ng!Passw0rd")
    assert user.email == "newuser@example.com"


def test_register_password_mismatch_rejected(db):
    client = Client()
    resp = _register(client, password2="Different!Pass9")

    assert resp.status_code == 302
    assert resp["Location"] == "/register/"
    assert not CustomUser.objects.filter(username="newuser").exists()


def test_register_duplicate_username_rejected(db):
    from tests.factories import UserFactory

    UserFactory(username="newuser")
    client = Client()
    resp = _register(client)

    assert resp.status_code == 302
    assert CustomUser.objects.filter(username="newuser").count() == 1


def test_register_duplicate_email_rejected(db):
    from tests.factories import UserFactory

    UserFactory(username="someoneelse", email="newuser@example.com")
    client = Client()
    resp = _register(client)

    assert resp.status_code == 302
    assert resp["Location"] == "/register/"
    assert not CustomUser.objects.filter(username="newuser").exists()


def test_register_missing_required_field_rejected_without_500(db):
    """A POST that omits a required field must not raise (was: KeyError -> 500)."""
    client = Client(raise_request_exception=False)
    resp = _register(client, username="")  # required field missing/empty

    assert resp.status_code == 302
    assert resp["Location"] == "/register/"
    assert not CustomUser.objects.filter(username="newuser").exists()


def test_register_missing_password_key_rejected_without_500(db):
    client = Client(raise_request_exception=False)
    data = {
        "username": "newuser",
        "first_name": "New",
        "last_name": "User",
        "email": "newuser@example.com",
        # password1 / password2 keys absent entirely
    }
    resp = client.post("/register/", data)

    assert resp.status_code == 302
    assert not CustomUser.objects.filter(username="newuser").exists()


def test_register_invalid_email_rejected(db):
    client = Client()
    resp = _register(client, email="not-an-email")

    assert resp.status_code == 302
    assert resp["Location"] == "/register/"
    assert not CustomUser.objects.filter(username="newuser").exists()


def test_register_weak_password_rejected(db):
    """AUTH_PASSWORD_VALIDATORS (min length etc.) must actually be enforced."""
    client = Client()
    resp = _register(client, password1="123", password2="123")

    assert resp.status_code == 302
    assert resp["Location"] == "/register/"
    assert not CustomUser.objects.filter(username="newuser").exists()


def test_register_empty_username_rejected(db):
    client = Client()
    resp = _register(client, username="")

    assert resp.status_code == 302
    assert not CustomUser.objects.filter(username="").exists()


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #
def test_login_missing_password_field_rejected_without_500(db):
    client = Client(raise_request_exception=False)
    resp = client.post("/", {"username": "someone"})

    assert resp.status_code == 302
    assert "_auth_user_id" not in client.session


def test_login_missing_username_field_rejected_without_500(db):
    client = Client(raise_request_exception=False)
    resp = client.post("/", {"password": "whatever"})

    assert resp.status_code == 302
    assert "_auth_user_id" not in client.session


# --------------------------------------------------------------------------- #
# Message input
# --------------------------------------------------------------------------- #
def test_send_message_whitespace_only_rejected(auth_client, user, other_user):
    resp = auth_client.post(f"/chat/api/messages/{other_user.username}/", {"text": "   \n\t  "})

    assert resp.status_code == 400
    assert Message.objects.count() == 0


def test_send_message_empty_rejected(auth_client, user, other_user):
    resp = auth_client.post(f"/chat/api/messages/{other_user.username}/", {})

    assert resp.status_code == 400
    assert Message.objects.count() == 0


def test_conversation_whitespace_only_not_saved(auth_client, user, other_user):
    resp = auth_client.post(f"/chat/{other_user.username}/", {"text": "    "}, follow=False)

    assert resp.status_code == 302
    assert Message.objects.count() == 0


def test_send_message_overly_long_text_accepted(auth_client, user, other_user):
    """There is no server-side length limit (TextField) — documents actual behavior."""
    long_text = "y" * 100_000
    resp = auth_client.post(f"/chat/api/messages/{other_user.username}/", {"text": long_text})

    assert resp.status_code == 200
    msg = Message.objects.get()
    assert msg.text == long_text


# --------------------------------------------------------------------------- #
# File upload validation (type)
# --------------------------------------------------------------------------- #
def test_profile_rejects_non_image_file(auth_client, user):
    resp = auth_client.post(
        "/profile/",
        {
            "first_name": "X",
            "last_name": "Y",
            "email": "x@y.com",
            "profile_pic": _txt_upload(),
        },
        follow=False,
    )

    profile = ProfilePic.objects.get(user=user)
    assert not profile.profile_pic.name  # nothing stored
    assert resp.status_code == 302


def test_profile_accepts_valid_image(auth_client, user):
    resp = auth_client.post(
        "/profile/",
        {
            "first_name": "X",
            "last_name": "Y",
            "email": "x@y.com",
            "profile_pic": _png_upload(),
        },
        follow=False,
    )

    profile = ProfilePic.objects.get(user=user)
    assert resp.status_code == 302
    assert profile.profile_pic.name.startswith("profile_pics/")


def test_message_photo_rejects_non_image(auth_client, user, other_user):
    resp = auth_client.post(
        f"/chat/api/messages/{other_user.username}/",
        {"text": "hi", "photo": _txt_upload()},
    )

    assert resp.status_code == 400
    assert Message.objects.count() == 0


def test_message_photo_accepts_valid_image(auth_client, user, other_user):
    resp = auth_client.post(
        f"/chat/api/messages/{other_user.username}/",
        {"text": "with photo", "photo": _png_upload("photo.png")},
    )

    assert resp.status_code == 200
    msg = Message.objects.get()
    assert msg.photo.name.startswith("chat_images/")


def test_conversation_post_rejects_non_image(auth_client, user, other_user):
    """Invalid photo on the conversation page must redirect with an error,
    never 500 (regression test: the view's local `messages` QuerySet used to
    shadow the Django messages framework)."""
    resp = auth_client.post(
        reverse("chat:conversation-view", kwargs={"username": other_user.username}),
        {"text": "with bad photo", "photo": _txt_upload()},
        follow=False,
    )

    assert resp.status_code == 302
    assert resp["Location"] == reverse("chat:conversation-view", kwargs={"username": other_user.username})
    assert Message.objects.count() == 0  # nothing stored


def test_conversation_post_rejects_forged_image_content_type(auth_client, user, other_user):
    """A .txt payload declared as image/png must fail the Pillow decode check."""
    forged = SimpleUploadedFile("fake.png", b"this is definitely not a png", content_type="image/png")
    resp = auth_client.post(
        reverse("chat:conversation-view", kwargs={"username": other_user.username}),
        {"text": "forged", "photo": forged},
        follow=False,
    )

    assert resp.status_code == 302
    assert Message.objects.count() == 0


def test_message_api_rejects_forged_image_content_type(auth_client, user, other_user):
    forged = SimpleUploadedFile("fake.png", b"this is definitely not a png", content_type="image/png")
    resp = auth_client.post(
        f"/chat/api/messages/{other_user.username}/",
        {"text": "forged", "photo": forged},
    )

    assert resp.status_code == 400
    assert Message.objects.count() == 0


def test_message_photo_rejects_oversized_image(auth_client, user, other_user):
    """A real image larger than the 5 MB limit must be rejected."""
    import os

    # random noise makes a PNG that cannot be compressed below 5 MB
    width = height = 1600  # 1600*1600*3 = 7.68 MB raw
    buf = io.BytesIO()
    Image.frombytes("RGB", (width, height), os.urandom(width * height * 3)).save(buf, format="PNG")
    payload = buf.getvalue()
    assert len(payload) > 5 * 1024 * 1024  # sanity: really over the limit

    resp = auth_client.post(
        f"/chat/api/messages/{other_user.username}/",
        {"text": "huge", "photo": SimpleUploadedFile("big.png", payload, content_type="image/png")},
    )

    assert resp.status_code == 400
    assert Message.objects.count() == 0
