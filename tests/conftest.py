"""Shared fixtures for the chattingapp pytest suite.

Auth mechanism under test
-------------------------
The app uses Django *session* authentication end-to-end:

* HTTP views use ``@login_required`` with cookie sessions.
* WebSockets are wrapped in ``channels.auth.AuthMiddlewareStack``
  (see ``chattingapp/asgi.py``), which reads the ``sessionid`` cookie and
  puts ``scope["user"]`` in place.

So the authenticated-WebSocket helper below creates a real session with
``Client.force_login`` and passes its ``sessionid`` cookie as a WebSocket
header — exercising the exact same middleware stack as production.
"""

import socket

import pytest
from channels.testing import WebsocketCommunicator
from django.test import Client

from chattingapp.asgi import application as asgi_application

from tests.factories import MessageFactory, UserFactory


# --------------------------------------------------------------------------- #
# Autouse: never touch the real network (Cloudinary, Redis, APIs, ...)
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    """Fail loudly if a test tries to reach a **non-local** host.

    Loopback (127.0.0.1 / ::1 / localhost) stays open on purpose:

    * asyncio's event loop self-pipe uses a loopback ``socketpair()``,
    * CI runs PostgreSQL/Redis as localhost services.

    Everything else — Cloudinary, external APIs, DNS for arbitrary hosts —
    raises immediately, so accidental calls to real services are impossible
    to miss. All external services must be mocked/replaced in tests.
    """
    local_hosts = {
        "localhost",
        "127.0.0.1",
        "::1",
        "0.0.0.0",
        "",
        "ip6-localhost",
        "ip6-loopback",
    }

    def _is_local(host):
        if host is None:
            return True
        if isinstance(host, bytes):
            host = host.decode("latin1", "ignore")
        if not isinstance(host, str):
            return True
        host = host.strip("[]").lower()
        return host in local_hosts or host.startswith("127.") or host.startswith("::ffff:127.")

    def _blocked(host):
        raise RuntimeError(
            f"Real network access is blocked during tests (host={host!r}). Mock external services instead."
        )

    real_getaddrinfo = socket.getaddrinfo
    real_gethostbyname = socket.gethostbyname
    real_connect = socket.socket.connect

    def _guarded_getaddrinfo(host, *args, **kwargs):
        if not _is_local(host):
            _blocked(host)
        return real_getaddrinfo(host, *args, **kwargs)

    def _guarded_gethostbyname(host):
        if not _is_local(host):
            _blocked(host)
        return real_gethostbyname(host)

    def _guarded_connect(sock, address):
        # AF_UNIX-style string paths and loopback tuples pass through;
        # anything pointing at an external host/IP is blocked.
        if isinstance(address, tuple) and address and not _is_local(address[0]):
            _blocked(address[0])
        return real_connect(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", _guarded_gethostbyname)
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
@pytest.fixture
def user(db):
    """The primary user of a test ('me')."""
    return UserFactory()


@pytest.fixture
def other_user(db):
    """A second user ('the chat partner')."""
    return UserFactory()


@pytest.fixture
def third_user(db):
    """A third user (used for isolation / access-control tests)."""
    return UserFactory()


# --------------------------------------------------------------------------- #
# Clients (plain pytest fixtures — no unittest/django.test.TestCase anywhere)
# --------------------------------------------------------------------------- #
@pytest.fixture
def anon_client():
    """Unauthenticated Django test client."""
    return Client()


@pytest.fixture
def auth_client(user):
    """Django test client logged in as ``user`` (session-authenticated)."""
    client = Client()
    client.force_login(user)
    return client


# --------------------------------------------------------------------------- #
# Chat threads & messages
#
# NOTE: this app has no ``Room``/``Thread`` model — a conversation is the
# 1-to-1 pair between two users (DM), keyed by the group name
# ``chat_{min(id)}_{max(id)}`` in ``chat/consumers.py``.
# --------------------------------------------------------------------------- #
@pytest.fixture
def thread(user, other_user):
    """A chat thread = the (me, other) user pair."""
    return {"me": user, "other": other_user}


@pytest.fixture
def message_factory():
    """Expose the Message factory for tests that need several messages."""
    return MessageFactory


@pytest.fixture
def message(user, other_user):
    """A single persisted message from ``user`` to ``other_user``."""
    return MessageFactory(sender=user, receiver=other_user)


# --------------------------------------------------------------------------- #
# WebSocket helpers
# --------------------------------------------------------------------------- #
def session_cookie_for(user):
    """Return the value of the ``sessionid`` cookie for an authenticated user.

    Must be called from *synchronous* code (fixture setup), because it uses
    the ORM to persist the session. Async tests receive cookies through the
    ``*_cookie`` fixtures below.
    """
    client = Client()
    client.force_login(user)
    return client.cookies["sessionid"].value


def ws_connect(path, *, cookie=None, user=None, headers=None, subprotocols=None):
    """Build a ``WebsocketCommunicator`` against the **real** ASGI app.

    The real app (``chattingapp.asgi.application``) is a ``ProtocolTypeRouter``
    with ``AuthMiddlewareStack(URLRouter(...))``, so every connection made
    through this helper exercises production routing + auth middleware.

    Pass either:
      * ``cookie`` — a ``sessionid`` value (from a ``*_cookie`` fixture), or
      * ``user``   — creates the session inline (synchronous context only).
    """
    all_headers = list(headers or [])
    if cookie is None and user is not None:
        cookie = session_cookie_for(user)
    if cookie:
        all_headers.append((b"cookie", f"sessionid={cookie}".encode()))
    return WebsocketCommunicator(asgi_application, path, headers=all_headers, subprotocols=subprotocols)


@pytest.fixture
def user_cookie(user):
    """Session cookie for ``user`` (usable inside async tests)."""
    return session_cookie_for(user)


@pytest.fixture
def other_user_cookie(other_user):
    """Session cookie for ``other_user`` (usable inside async tests)."""
    return session_cookie_for(other_user)


@pytest.fixture
def third_user_cookie(third_user):
    """Session cookie for ``third_user`` (usable inside async tests)."""
    return session_cookie_for(third_user)
