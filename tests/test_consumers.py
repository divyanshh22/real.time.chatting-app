"""WebSocket consumer tests (ChatConsumer + PresenceConsumer).

Design notes learned from the real application:

* Route: ``/ws/chat/<username>/`` (1-to-1 DM, group ``chat_{min}_{max}``)
  and ``/ws/presence/`` (global presence group).
* Auth: ``channels.auth.AuthMiddlewareStack`` reads the ``sessionid`` cookie;
  anonymous connections are closed by the consumer (close code 1000).
* On connect the consumer broadcasts a ``user_status`` announce **to the
  connecting client itself**, so tests drain pending frames before asserting.
* Exact broadcast schema for a chat message::

      {"type": "message",
       "message": {"id": int, "sender_id": int, "sender_username": str,
                   "text": str, "photo_url": str|null,
                   "timestamp": ISO-8601 str, "is_read": bool}}

All ORM access inside async tests goes through ``sync_to_async`` (which runs
in a sync thread — never in the event loop).
"""

from datetime import datetime

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from chat.models import Message
from tests.conftest import ws_connect
from tests.factories import MessageFactory

pytestmark = [pytest.mark.websocket, pytest.mark.asyncio]

USER_MODEL = get_user_model()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _drain(ws, max_msgs=8, idle=0.15):
    """Consume any pending frames (status announces etc.), return them."""
    got = []
    for _ in range(max_msgs):
        if await ws.receive_nothing(timeout=idle):
            break
        got.append(await ws.receive_json_from())
    return got


def _count(**filters):
    return Message.objects.filter(**filters).count()


async def _connect_chat(user_cookie, other_user, drain=True):
    ws = ws_connect(f"/ws/chat/{other_user.username}/", cookie=user_cookie)
    connected, _ = await ws.connect()
    assert connected is True
    if drain:
        await _drain(ws)
    return ws


# --------------------------------------------------------------------------- #
# Connection lifecycle
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
async def test_authenticated_user_connects(user, other_user, user_cookie):
    ws = ws_connect(f"/ws/chat/{other_user.username}/", cookie=user_cookie)
    connected, _ = await ws.connect()

    assert connected is True

    events = await _drain(ws)
    status_events = [e for e in events if e["type"] == "user_status"]
    assert any(e["status"] == "online" and e["username"] == user.username for e in status_events)

    is_online = await sync_to_async(lambda: USER_MODEL.objects.get(pk=user.pk).is_online)()
    assert is_online is True
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_anonymous_user_is_rejected(other_user):
    ws = ws_connect(f"/ws/chat/{other_user.username}/")  # no session cookie
    connected, close_code = await ws.connect()

    assert connected is False
    assert close_code == 1000


@pytest.mark.django_db(transaction=True)
async def test_connection_to_nonexistent_user_is_rejected(user_cookie):
    ws = ws_connect("/ws/chat/definitely_ghost/", cookie=user_cookie)
    connected, close_code = await ws.connect()

    assert connected is False
    assert close_code == 1000


@pytest.mark.django_db(transaction=True)
async def test_disconnect_without_connect_does_not_crash(user_cookie):
    """Regression: disconnect() before connect() used to raise AttributeError."""
    ws = ws_connect("/ws/chat/someone/", cookie=user_cookie)
    await ws.disconnect()  # must not raise


# --------------------------------------------------------------------------- #
# Sending messages
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
async def test_valid_message_broadcast_schema(user, other_user, user_cookie):
    ws = await _connect_chat(user_cookie, other_user)

    await ws.send_json_to({"type": "message", "text": "hello world"})
    event = await ws.receive_json_from()

    assert set(event.keys()) == {"type", "message"}
    assert event["type"] == "message"
    message = event["message"]
    assert set(message.keys()) == {
        "id",
        "sender_id",
        "sender_username",
        "text",
        "photo_url",
        "timestamp",
        "is_read",
    }
    assert message["text"] == "hello world"
    assert message["sender_id"] == user.id
    assert message["sender_username"] == user.username
    assert message["photo_url"] is None
    assert message["is_read"] is False
    assert isinstance(message["id"], int)
    parsed = datetime.fromisoformat(message["timestamp"])  # valid ISO-8601
    assert parsed.tzinfo is not None
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_message_persisted_exactly_once_with_correct_links(user, other_user, user_cookie):
    ws = await _connect_chat(user_cookie, other_user)
    before = await sync_to_async(_count)()

    await ws.send_json_to({"type": "message", "text": "persist me"})
    event = await ws.receive_json_from()
    await ws.disconnect()

    after = await sync_to_async(_count)()
    assert after == before + 1

    msg = await sync_to_async(lambda: Message.objects.get(id=event["message"]["id"]))()
    assert msg.sender_id == user.id
    assert msg.receiver_id == other_user.id
    assert msg.text == "persist me"
    assert msg.is_read is False


@pytest.mark.django_db(transaction=True)
async def test_sender_identity_cannot_be_spoofed(user, other_user, third_user, user_cookie):
    """Client-supplied identity fields must be ignored — scope['user'] wins."""
    ws = await _connect_chat(user_cookie, other_user)

    await ws.send_json_to(
        {
            "type": "message",
            "text": "trust me",
            "user": third_user.username,
            "sender_id": 999999,
            "sender_username": "mallory",
        }
    )
    event = await ws.receive_json_from()

    assert event["message"]["sender_id"] == user.id
    assert event["message"]["sender_username"] == user.username

    msg = await sync_to_async(lambda: Message.objects.get(text="trust me"))()
    assert msg.sender_id == user.id
    assert msg.receiver_id == other_user.id
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_group_broadcast_reaches_room_members_only(
    user, other_user, third_user, user_cookie, other_user_cookie, third_user_cookie
):
    """Two clients in room {user, other} both receive; a client in the
    room {third, user} must receive nothing."""
    a = ws_connect(f"/ws/chat/{other_user.username}/", cookie=user_cookie)  # {user, other}
    b = ws_connect(f"/ws/chat/{user.username}/", cookie=other_user_cookie)  # {user, other}
    c = ws_connect(f"/ws/chat/{user.username}/", cookie=third_user_cookie)  # {third, user}
    assert (await a.connect())[0] is True
    assert (await b.connect())[0] is True
    assert (await c.connect())[0] is True
    await _drain(a), await _drain(b), await _drain(c)

    await a.send_json_to({"type": "message", "text": "for the room"})

    a_event = await a.receive_json_from()
    b_event = await b.receive_json_from()
    assert a_event["message"]["text"] == "for the room"
    assert b_event["message"]["text"] == "for the room"
    assert await c.receive_nothing(timeout=0.3) is True

    assert await sync_to_async(_count)() == 1
    await a.disconnect()
    await b.disconnect()
    await c.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_rapid_50_messages_preserved_in_order(user, other_user, user_cookie):
    ws = await _connect_chat(user_cookie, other_user)

    for i in range(50):
        await ws.send_json_to({"type": "message", "text": f"rapid {i}"})

    received = []
    for _ in range(50):
        received.append((await ws.receive_json_from(timeout=2))["message"]["text"])

    assert received == [f"rapid {i}" for i in range(50)]
    assert await sync_to_async(_count)() == 50
    stored = await sync_to_async(lambda: list(Message.objects.order_by("id").values_list("text", flat=True)))()
    assert stored == [f"rapid {i}" for i in range(50)]
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_xss_payload_stored_verbatim_via_websocket(user, other_user, user_cookie, auth_client):
    """Stored raw in the DB (no server-side mangling), but the rendered page
    must escape it (asserted on the HTTP side below)."""
    from django.urls import reverse

    payload = "<script>alert(1)</script>"
    ws = await _connect_chat(user_cookie, other_user)

    await ws.send_json_to({"type": "message", "text": payload})
    event = await ws.receive_json_from()
    assert event["message"]["text"] == payload
    stored = await sync_to_async(lambda: Message.objects.get().text)()
    assert stored == payload
    await ws.disconnect()

    resp = await sync_to_async(auth_client.get)(
        reverse("chat:conversation-view", kwargs={"username": other_user.username})
    )
    html = resp.content.decode()
    assert payload not in html  # never rendered raw
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html  # rendered escaped


# --------------------------------------------------------------------------- #
# Malformed input — consumer must never crash
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
async def test_invalid_json_is_ignored_and_connection_survives(user, other_user, user_cookie):
    ws = await _connect_chat(user_cookie, other_user)

    await ws.send_to(text_data="{ this is not json")
    assert await ws.receive_nothing(timeout=0.3) is True  # no reply, no crash

    await ws.send_json_to({"type": "message", "text": "still alive"})
    event = await ws.receive_json_from()
    assert event["message"]["text"] == "still alive"
    assert await sync_to_async(_count)() == 1
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_non_dict_payload_is_ignored(user, other_user, user_cookie):
    ws = await _connect_chat(user_cookie, other_user)

    await ws.send_to(text_data='["not", "a", "dict"]')
    assert await ws.receive_nothing(timeout=0.3) is True

    await ws.send_json_to({"type": "message", "text": "alive"})
    event = await ws.receive_json_from()
    assert event["message"]["text"] == "alive"
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_missing_type_key_is_ignored(user, other_user, user_cookie):
    ws = await _connect_chat(user_cookie, other_user)

    await ws.send_json_to({"text": "no type field"})
    assert await ws.receive_nothing(timeout=0.3) is True
    assert await sync_to_async(_count)() == 0

    await ws.send_json_to({"type": "message", "text": "alive"})
    assert (await ws.receive_json_from())["message"]["text"] == "alive"
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_empty_text_message_not_saved(user, other_user, user_cookie):
    ws = await _connect_chat(user_cookie, other_user)

    await ws.send_json_to({"type": "message", "text": ""})
    assert await ws.receive_nothing(timeout=0.3) is True
    assert await sync_to_async(_count)() == 0

    await ws.send_json_to({"type": "message", "text": "alive"})
    assert (await ws.receive_json_from())["message"]["text"] == "alive"
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_whitespace_only_message_not_saved(user, other_user, user_cookie):
    ws = await _connect_chat(user_cookie, other_user)

    await ws.send_json_to({"type": "message", "text": "   \n\t   "})
    assert await ws.receive_nothing(timeout=0.3) is True
    assert await sync_to_async(_count)() == 0
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("bad_text", [123, 3.14, True, ["list"], {"k": "v"}])
async def test_non_string_text_is_ignored(user, other_user, user_cookie, bad_text):
    ws = await _connect_chat(user_cookie, other_user)

    await ws.send_json_to({"type": "message", "text": bad_text})
    assert await ws.receive_nothing(timeout=0.3) is True
    assert await sync_to_async(_count)() == 0

    await ws.send_json_to({"type": "message", "text": "alive"})
    assert (await ws.receive_json_from())["message"]["text"] == "alive"
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_extremely_long_message_accepted(user, other_user, user_cookie):
    ws = await _connect_chat(user_cookie, other_user)
    long_text = "z" * 100_000

    await ws.send_json_to({"type": "message", "text": long_text})
    event = await ws.receive_json_from()

    assert event["message"]["text"] == long_text
    stored = await sync_to_async(lambda: Message.objects.get().text)()
    assert stored == long_text
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_binary_frame_is_ignored_and_connection_survives(user, other_user, user_cookie):
    ws = await _connect_chat(user_cookie, other_user)

    await ws.send_to(bytes_data=b"\x00\x01\x02binary")
    assert await ws.receive_nothing(timeout=0.3) is True
    assert await sync_to_async(_count)() == 0

    await ws.send_json_to({"type": "message", "text": "alive after binary"})
    assert (await ws.receive_json_from())["message"]["text"] == "alive after binary"
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_read_with_malformed_ids_does_not_crash(user, other_user, user_cookie):
    ws = await _connect_chat(user_cookie, other_user)

    await ws.send_json_to({"type": "read", "message_ids": "not-a-list"})
    assert await ws.receive_nothing(timeout=0.3) is True

    await ws.send_json_to({"type": "read", "message_ids": ["abc", None]})
    assert await ws.receive_nothing(timeout=0.3) is True

    await ws.send_json_to({"type": "message", "text": "alive"})
    assert (await ws.receive_json_from())["message"]["text"] == "alive"
    await ws.disconnect()


# --------------------------------------------------------------------------- #
# Disconnect / reconnect
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
async def test_disconnect_announces_offline_and_stops_delivery(user, other_user, user_cookie, other_user_cookie):
    a = ws_connect(f"/ws/chat/{other_user.username}/", cookie=user_cookie)
    b = ws_connect(f"/ws/chat/{user.username}/", cookie=other_user_cookie)
    assert (await a.connect())[0] is True
    assert (await b.connect())[0] is True
    await _drain(a), await _drain(b)

    await a.disconnect()

    b_events = await _drain(b)
    assert any(
        e["type"] == "user_status" and e["status"] == "offline" and e["username"] == user.username for e in b_events
    )

    # the disconnected client receives nothing anymore
    assert await a.receive_nothing(timeout=0.2) is True

    # messages sent after the disconnect are not delivered to a
    await b.send_json_to({"type": "message", "text": "after A left"})
    assert await a.receive_nothing(timeout=0.3) is True
    b_event = await b.receive_json_from()
    assert b_event["message"]["text"] == "after A left"
    await b.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_reconnect_works(user, other_user, user_cookie):
    first = await _connect_chat(user_cookie, other_user)
    await first.disconnect()

    second = await _connect_chat(user_cookie, other_user)
    await second.send_json_to({"type": "message", "text": "after reconnect"})
    event = await second.receive_json_from()

    assert event["message"]["text"] == "after reconnect"
    assert await sync_to_async(_count)() == 1
    await second.disconnect()


# --------------------------------------------------------------------------- #
# Features that exist in this app: typing indicator + read receipts
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
async def test_typing_indicator_broadcast(user, other_user, user_cookie, other_user_cookie):
    a = ws_connect(f"/ws/chat/{other_user.username}/", cookie=user_cookie)
    b = ws_connect(f"/ws/chat/{user.username}/", cookie=other_user_cookie)
    assert (await a.connect())[0] is True
    assert (await b.connect())[0] is True
    await _drain(a), await _drain(b)

    await a.send_json_to({"type": "typing", "is_typing": True})

    event = await b.receive_json_from()
    assert event == {"type": "typing", "username": user.username, "is_typing": True}

    await a.send_json_to({"type": "typing", "is_typing": False})
    event = await b.receive_json_from()
    assert event["is_typing"] is False
    await a.disconnect()
    await b.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_read_receipt_marks_messages_and_broadcasts(user, other_user, user_cookie):
    created = await sync_to_async(
        lambda: [
            MessageFactory(sender=other_user, receiver=user, text="read r1"),
            MessageFactory(sender=other_user, receiver=user, text="read r2"),
        ]
    )()

    ws = await _connect_chat(user_cookie, other_user)
    await ws.send_json_to({"type": "read", "message_ids": [m.id for m in created]})

    # the consumer broadcasts AFTER updating the DB, so awaiting the event
    # guarantees the update has landed before we query
    event = await ws.receive_json_from()
    assert event == {
        "type": "messages_read",
        "message_ids": [m.id for m in created],
    }

    flags = await sync_to_async(
        lambda: list(Message.objects.filter(id__in=[m.id for m in created]).values_list("is_read", flat=True))
    )()
    assert flags == [True, True]
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_read_receipt_cannot_mark_other_peoples_messages(user, other_user, third_user, user_cookie):
    """A message from other_user -> third_user must stay unread for user."""
    created = await sync_to_async(lambda: MessageFactory(sender=other_user, receiver=third_user, text="not yours"))()

    ws = await _connect_chat(user_cookie, other_user)
    await ws.send_json_to({"type": "read", "message_ids": [created.id]})

    assert await ws.receive_nothing(timeout=0.3) is True  # no broadcast
    still_unread = await sync_to_async(lambda: Message.objects.get(id=created.id).is_read)()
    assert still_unread is False
    await ws.disconnect()


# --------------------------------------------------------------------------- #
# PresenceConsumer (/ws/presence/)
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
async def test_presence_authenticated_user_connects_and_announces(user, user_cookie):
    ws = ws_connect("/ws/presence/", cookie=user_cookie)
    connected, _ = await ws.connect()
    assert connected is True

    events = await _drain(ws)
    assert any(
        e["type"] == "presence_update" and e["status"] == "online" and e["username"] == user.username for e in events
    )

    is_online = await sync_to_async(lambda: USER_MODEL.objects.get(pk=user.pk).is_online)()
    assert is_online is True
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_presence_anonymous_user_is_rejected():
    ws = ws_connect("/ws/presence/")  # no cookie
    connected, close_code = await ws.connect()
    assert connected is False
    assert close_code == 1000


@pytest.mark.django_db(transaction=True)
async def test_presence_broadcasts_joins_and_leaves(user, other_user, user_cookie, other_user_cookie):
    first = ws_connect("/ws/presence/", cookie=user_cookie)
    assert (await first.connect())[0] is True
    await _drain(first)

    second = ws_connect("/ws/presence/", cookie=other_user_cookie)
    assert (await second.connect())[0] is True
    await _drain(second)

    join_events = await _drain(first)
    assert any(
        e["type"] == "presence_update" and e["username"] == other_user.username and e["status"] == "online"
        for e in join_events
    )

    await second.disconnect()
    leave_events = await _drain(first)
    assert any(
        e["type"] == "presence_update"
        and e["username"] == other_user.username
        and e["status"] == "offline"
        and e.get("last_seen")
        for e in leave_events
    )
    await first.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_presence_bulk_status_query(user, other_user, user_cookie):
    ws = ws_connect("/ws/presence/", cookie=user_cookie)
    assert (await ws.connect())[0] is True
    await _drain(ws)

    await ws.send_json_to({"type": "get_status", "user_ids": [user.id, other_user.id]})
    event = await ws.receive_json_from()

    assert event["type"] == "bulk_status"
    assert set(event["statuses"].keys()) == {str(user.id), str(other_user.id)}
    for info in event["statuses"].values():
        assert set(info.keys()) == {"status", "last_seen"}
        assert info["status"] in {"online", "offline"}
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_presence_invalid_json_ignored(user_cookie):
    ws = ws_connect("/ws/presence/", cookie=user_cookie)
    assert (await ws.connect())[0] is True
    await _drain(ws)

    await ws.send_to(text_data="{ broken json")
    assert await ws.receive_nothing(timeout=0.3) is True

    await ws.send_json_to({"type": "get_status", "user_ids": []})
    event = await ws.receive_json_from()
    assert event["type"] == "bulk_status"  # still alive
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_presence_non_dict_payload_ignored(user_cookie):
    ws = ws_connect("/ws/presence/", cookie=user_cookie)
    assert (await ws.connect())[0] is True
    await _drain(ws)

    await ws.send_to(text_data="[1, 2, 3]")
    assert await ws.receive_nothing(timeout=0.3) is True

    await ws.send_json_to({"type": "get_status", "user_ids": []})
    assert (await ws.receive_json_from())["type"] == "bulk_status"
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_presence_binary_frame_ignored(user_cookie):
    ws = ws_connect("/ws/presence/", cookie=user_cookie)
    assert (await ws.connect())[0] is True
    await _drain(ws)

    await ws.send_to(bytes_data=b"\x00\x01")
    assert await ws.receive_nothing(timeout=0.3) is True

    await ws.send_json_to({"type": "get_status", "user_ids": []})
    assert (await ws.receive_json_from())["type"] == "bulk_status"
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_presence_malformed_user_ids_ignored(user, user_cookie):
    ws = ws_connect("/ws/presence/", cookie=user_cookie)
    assert (await ws.connect())[0] is True
    await _drain(ws)

    await ws.send_json_to({"type": "get_status", "user_ids": "not-a-list"})
    assert await ws.receive_nothing(timeout=0.3) is True  # ignored entirely

    await ws.send_json_to({"type": "get_status", "user_ids": ["abc", None, True]})
    event = await ws.receive_json_from()  # invalid ids dropped, valid reply shape
    assert event == {"type": "bulk_status", "statuses": {}}

    await ws.send_json_to({"type": "get_status", "user_ids": [user.id]})
    event = await ws.receive_json_from()
    assert event["type"] == "bulk_status"
    assert set(event["statuses"].keys()) == {str(user.id)}
    await ws.disconnect()
