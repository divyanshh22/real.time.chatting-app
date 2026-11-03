"""Routing tests: the ASGI application wiring from ``chattingapp/asgi.py``.

* ``ProtocolTypeRouter`` with both ``http`` and ``websocket`` handlers
* ``URLRouter`` maps ``/ws/chat/<username>/`` and ``/ws/presence/``
* unknown WebSocket paths are rejected with ``ValueError: No route found``
* ``AuthMiddlewareStack`` places the session user into ``scope['user']``
  (asserted behaviorally: cookie -> authenticated consumer flow works and
  message attribution matches the cookie's user; no cookie -> rejected)
"""

import pytest
from channels.routing import ProtocolTypeRouter, URLRouter

import chat.routing
from chattingapp.asgi import application as asgi_application
from tests.conftest import ws_connect

pytestmark = pytest.mark.websocket


def test_asgi_application_is_protocol_type_router():
    assert isinstance(asgi_application, ProtocolTypeRouter)
    mapping = asgi_application.application_mapping
    assert "http" in mapping
    assert "websocket" in mapping


def test_websocket_urlpatterns_cover_expected_paths():
    patterns = chat.routing.websocket_urlpatterns
    assert len(patterns) == 2

    def normalized(pattern):
        # Django's path() compiles '$' to '\Z'; normalize for comparison
        return pattern.regex.pattern.replace("\\Z", "$")

    chat_pattern = patterns[0].pattern
    presence_pattern = patterns[1].pattern

    assert normalized(chat_pattern) == r"^ws/chat/(?P<username>[^/]+)/$"
    assert normalized(presence_pattern) == r"^ws/presence/$"

    assert chat_pattern.regex.match("ws/chat/alice/")
    assert presence_pattern.regex.match("ws/presence/")
    # nothing else matches
    assert not any(p.pattern.regex.match("ws/other/") for p in patterns)
    assert not any(p.pattern.regex.match("ws/chat/") for p in patterns)


def test_websocket_urlpatterns_are_wrapped_in_urlrouter():
    # the ASGI app wraps these patterns in a URLRouter inside AuthMiddlewareStack
    ws_handler = asgi_application.application_mapping["websocket"]
    # AuthMiddlewareStack -> CookieMiddleware -> SessionMiddleware -> AuthMiddleware -> URLRouter
    inner = ws_handler
    found_url_router = False
    for _ in range(10):
        if isinstance(inner, URLRouter):
            found_url_router = True
            break
        inner = getattr(inner, "inner", None)
        if inner is None:
            break
    assert found_url_router, "URLRouter not found inside the websocket middleware stack"


@pytest.mark.django_db(transaction=True)
async def test_chat_ws_path_connects_through_real_application(user, other_user, user_cookie):
    ws = ws_connect(f"/ws/chat/{other_user.username}/", cookie=user_cookie)
    connected, _ = await ws.connect()
    assert connected is True
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_presence_ws_path_connects_through_real_application(user_cookie):
    ws = ws_connect("/ws/presence/", cookie=user_cookie)
    connected, _ = await ws.connect()
    assert connected is True
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_unknown_ws_path_is_rejected(user_cookie):
    ws = ws_connect("/ws/definitely/not/routed/", cookie=user_cookie)
    with pytest.raises(ValueError, match="No route found"):
        await ws.connect()


@pytest.mark.django_db(transaction=True)
async def test_auth_middleware_places_session_user_in_scope(user, other_user, user_cookie):
    """Behavioral proof that AuthMiddlewareStack populated scope['user']:

    the consumer only knows 'me' from scope['user'], so a message broadcast
    must be attributed to the session user from the cookie — not anonymous,
    not someone else.
    """
    ws = ws_connect(f"/ws/chat/{other_user.username}/", cookie=user_cookie)
    connected, _ = await ws.connect()
    assert connected is True

    # drain the self status announce (it carries the authenticated username)
    events = []
    for _ in range(4):
        if await ws.receive_nothing(timeout=0.2):
            break
        events.append(await ws.receive_json_from())
    assert any(e.get("username") == user.username for e in events)

    await ws.send_json_to({"type": "message", "text": "attributed correctly"})
    event = await ws.receive_json_from()
    assert event["message"]["sender_id"] == user.id
    assert event["message"]["sender_username"] == user.username
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_without_cookie_scope_user_is_anonymous(other_user):
    """No session cookie -> AuthMiddleware puts AnonymousUser in scope['user']
    and the consumer closes the connection."""
    ws = ws_connect(f"/ws/chat/{other_user.username}/")
    connected, close_code = await ws.connect()
    assert connected is False
    assert close_code == 1000
