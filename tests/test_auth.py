"""Authentication & session tests (signup, login, logout, session lifecycle).

The app uses Django session auth end-to-end; these tests exercise the real
login/register/logout views and the session cookie lifecycle.
"""

import pytest
from django.contrib.auth import SESSION_KEY, get_user_model
from django.contrib.auth.hashers import identify_hasher
from django.test import Client
from django.urls import reverse
from django.utils.module_loading import import_string

from tests.factories import UserFactory

pytestmark = pytest.mark.security


def _register(client, username="authuser", **overrides):
    data = {
        "username": username,
        "first_name": "Auth",
        "last_name": "User",
        "email": f"{username}@example.com",
        "password1": "Str0ng!Passw0rd",
        "password2": "Str0ng!Passw0rd",
    }
    data.update(overrides)
    return client.post("/register/", data)


# --------------------------------------------------------------------------- #
# Signup
# --------------------------------------------------------------------------- #
def test_signup_creates_user_and_redirects_to_login(db):
    """The app's design: registration does NOT log in, it redirects to login."""
    client = Client()
    resp = _register(client)

    assert resp.status_code == 302
    assert resp["Location"] == reverse("login-view")
    user = get_user_model().objects.get(username="authuser")
    assert user.check_password("Str0ng!Passw0rd")
    # signup itself must not authenticate the session
    assert SESSION_KEY not in client.session


def test_signup_password_is_stored_hashed(db):
    client = Client()
    _register(client, username="hashcheck")

    stored = get_user_model().objects.get(username="hashcheck").password
    assert "Str0ng!Passw0rd" not in stored
    from django.conf import settings

    # PASSWORD_HASHERS stores dotted paths; check the stored hash uses one of them
    allowed = {import_string(path).algorithm for path in settings.PASSWORD_HASHERS}
    assert identify_hasher(stored).algorithm in allowed


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #
def test_login_success_authenticates_session(db):
    UserFactory(username="alice", password="Str0ng!Passw0rd")
    client = Client()
    resp = client.post("/", {"username": "alice", "password": "Str0ng!Passw0rd"})

    assert resp.status_code == 302
    assert resp["Location"] == reverse("chat:home-view")
    assert client.session[SESSION_KEY] is not None


def test_login_wrong_password_leaves_session_unauthenticated(db):
    UserFactory(username="alice", password="Str0ng!Passw0rd")
    client = Client()
    resp = client.post("/", {"username": "alice", "password": "wrong-password"})

    assert resp.status_code == 302
    assert resp["Location"] == reverse("login-view")
    assert SESSION_KEY not in client.session


def test_login_nonexistent_user_leaves_session_unauthenticated(db):
    client = Client()
    resp = client.post("/", {"username": "ghost", "password": "irrelevant"})

    assert resp.status_code == 302
    assert SESSION_KEY not in client.session


def test_login_with_missing_password_key_does_not_crash(db):
    UserFactory(username="alice", password="Str0ng!Passw0rd")
    client = Client(raise_request_exception=False)
    resp = client.post("/", {"username": "alice"})

    assert resp.status_code == 302
    assert SESSION_KEY not in client.session


def test_inactive_user_cannot_log_in(db):
    user = UserFactory(username="inactive", password="Str0ng!Passw0rd", is_active=False)
    client = Client()
    resp = client.post("/", {"username": "inactive", "password": "Str0ng!Passw0rd"})

    assert resp.status_code == 302
    assert SESSION_KEY not in client.session
    user.refresh_from_db()
    assert user.is_active is False


def test_inactive_users_session_is_treated_as_anonymous(db):
    """Even with a session cookie, an inactive user must hit login_required."""
    user = UserFactory(username="inactive2", is_active=False)
    client = Client()
    client.force_login(user)  # session exists, but the backend must refuse it
    assert SESSION_KEY in client.session

    resp = client.get(reverse("chat:home-view"))

    assert resp.status_code == 302
    assert resp["Location"].startswith(reverse("login-view"))


def test_login_post_redirects_to_chat_home_even_with_next_param(db):
    """KNOWN LIMITATION (documented): the app does not honor ?next= on login POST."""
    UserFactory(username="alice", password="Str0ng!Passw0rd")
    client = Client()
    resp = client.post("/?next=/profile/", {"username": "alice", "password": "Str0ng!Passw0rd"})

    assert resp.status_code == 302
    assert resp["Location"] == reverse("chat:home-view")


# --------------------------------------------------------------------------- #
# Logout
# --------------------------------------------------------------------------- #
def test_logout_clears_session(db):
    UserFactory(username="alice", password="Str0ng!Passw0rd")
    client = Client()
    client.post("/", {"username": "alice", "password": "Str0ng!Passw0rd"})
    assert SESSION_KEY in client.session

    resp = client.get("/logout/")

    assert resp.status_code == 302
    assert resp["Location"] == reverse("login-view")
    assert SESSION_KEY not in client.session

    # after logout the chat page redirects back to login
    follow = client.get(reverse("chat:home-view"))
    assert follow.status_code == 302
    assert SESSION_KEY not in client.session


def test_logout_requires_login(db):
    client = Client()
    resp = client.get("/logout/")
    assert resp.status_code == 302
    assert resp["Location"].startswith(reverse("login-view"))


# --------------------------------------------------------------------------- #
# Redirects & session persistence
# --------------------------------------------------------------------------- #
def test_login_required_redirect_carries_next_param(db):
    client = Client()
    resp = client.get(reverse("chat:home-view"))

    assert resp.status_code == 302
    location = resp["Location"]
    assert location.startswith(reverse("login-view"))
    assert "next=" in location
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(location).query)
    assert query["next"][0] == reverse("chat:home-view")


def test_authenticated_user_redirected_away_from_login(db):
    UserFactory(username="alice", password="Str0ng!Passw0rd")
    client = Client()
    client.post("/", {"username": "alice", "password": "Str0ng!Passw0rd"})

    resp = client.get(reverse("login-view"))
    assert resp.status_code == 302
    assert resp["Location"] == reverse("chat:home-view")


def test_authenticated_user_redirected_away_from_register(db):
    UserFactory(username="alice", password="Str0ng!Passw0rd")
    client = Client()
    client.post("/", {"username": "alice", "password": "Str0ng!Passw0rd"})

    resp = client.get(reverse("register-view"))
    assert resp.status_code == 302
    assert resp["Location"] == reverse("chat:home-view")


def test_session_persists_across_requests(db):
    UserFactory(username="alice", password="Str0ng!Passw0rd")
    client = Client()
    client.post("/", {"username": "alice", "password": "Str0ng!Passw0rd"})

    first = client.get(reverse("chat:home-view"))
    second = client.get(reverse("chat:home-view"))

    assert first.status_code == 200
    assert second.status_code == 200
    assert "alice" in second.content.decode()
    assert SESSION_KEY in client.session
