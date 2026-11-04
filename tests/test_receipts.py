"""Message delivery / read receipt tests (single tick -> double tick -> read).

Tick semantics implemented by the app:

* sent        -> single ✓   (persisted + broadcast, receiver not looking)
* delivered   -> double ✓✓  (receiver's conversation is open; flagged
                ``Message.is_delivered`` and pushed over the room group)
* read        -> double ✓✓ green (``Message.is_read``, pushed the same way)

The bug being regression-tested: read/delivered state used to be written
server-side without any broadcast, so the SENDER only saw ticks change
after they closed and reopened the DM.
"""

import pytest
from asgiref.sync import sync_to_async
from django.urls import reverse

from chat.models import Message
from tests.conftest import ws_connect
from tests.factories import MessageFactory


async def _drain(ws, max_msgs=8, idle=0.15):
    got = []
    for _ in range(max_msgs):
        if await ws.receive_nothing(timeout=idle):
            break
        got.append(await ws.receive_json_from())
    return got


class RecordingLayer:
    """Captures group_send calls made by the views (no real consumers)."""

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def group_send(self, group, event):
        if self.fail:
            raise RuntimeError("channel layer down")
        self.sent.append((group, event))


# --------------------------------------------------------------------------- #
# Receiver connects -> pending messages become delivered (double tick)
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
async def test_receiver_connect_delivers_pending_messages(user, other_user, user_cookie):
    created = await sync_to_async(
        lambda: [
            MessageFactory(sender=other_user, receiver=user, text="pending 1"),
            MessageFactory(sender=other_user, receiver=user, text="pending 2"),
        ]
    )()

    ws = ws_connect(f"/ws/chat/{other_user.username}/", cookie=user_cookie)
    assert (await ws.connect())[0] is True
    events = await _drain(ws)

    delivered_events = [e for e in events if e["type"] == "message_delivered"]
    assert delivered_events, f"expected delivery broadcast, got: {events}"
    assert set(delivered_events[0]["message_ids"]) == {m.id for m in created}

    flags = await sync_to_async(
        lambda: list(Message.objects.filter(id__in=[m.id for m in created]).values_list("is_delivered", flat=True))
    )()
    assert flags == [True, True]
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_message_stays_single_tick_while_receiver_offline(user, other_user, user_cookie):
    ws = ws_connect(f"/ws/chat/{other_user.username}/", cookie=user_cookie)
    assert (await ws.connect())[0] is True
    await _drain(ws)

    await ws.send_json_to({"type": "message", "text": "nobody is watching"})
    echo = await ws.receive_json_from()
    assert echo["message"]["is_delivered"] is False

    # no receiver connection => nobody marks it delivered, no broadcast
    assert await ws.receive_nothing(timeout=0.3) is True
    stored = await sync_to_async(lambda: Message.objects.get().is_delivered)()
    assert stored is False
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_open_conversation_delivers_live_message_instantly(user, other_user, user_cookie, other_user_cookie):
    """The reported bug: ticks must update on the sender's screen WITHOUT
    them reopening the DM."""
    sender = ws_connect(f"/ws/chat/{other_user.username}/", cookie=user_cookie)
    receiver = ws_connect(f"/ws/chat/{user.username}/", cookie=other_user_cookie)
    assert (await sender.connect())[0] is True
    assert (await receiver.connect())[0] is True
    await _drain(sender), await _drain(receiver)

    await sender.send_json_to({"type": "message", "text": "delivered?"})

    sender_message = await sender.receive_json_from()
    assert sender_message["type"] == "message"
    assert sender_message["message"]["is_delivered"] is False  # sent state first

    # receiver's consumer marks it delivered and pushes to the room
    sender_delivery = await sender.receive_json_from()
    assert sender_delivery == {
        "type": "message_delivered",
        "message_ids": [sender_message["message"]["id"]],
    }

    receiver_frames = await _drain(receiver)
    assert any(f["type"] == "message" and f["message"]["text"] == "delivered?" for f in receiver_frames)

    stored = await sync_to_async(lambda: Message.objects.get().is_delivered)()
    assert stored is True
    await sender.disconnect()
    await receiver.disconnect()


# --------------------------------------------------------------------------- #
# Opening the conversation page (GET) marks read+delivered AND broadcasts
# --------------------------------------------------------------------------- #
def test_conversation_get_marks_read_delivered_and_broadcasts(auth_client, user, other_user, monkeypatch):
    created = [
        MessageFactory(sender=other_user, receiver=user, text="waiting 1"),
        MessageFactory(sender=other_user, receiver=user, text="waiting 2"),
    ]
    layer = RecordingLayer()
    monkeypatch.setattr("chat.views.get_channel_layer", lambda: layer)

    resp = auth_client.get(reverse("chat:conversation-view", kwargs={"username": other_user.username}))
    assert resp.status_code == 200

    flags = list(Message.objects.filter(id__in=[m.id for m in created]).values_list("is_read", "is_delivered"))
    assert flags == [(True, True), (True, True)]

    events = [event for _, event in layer.sent]
    assert {"type": "message_delivered", "message_ids": [m.id for m in created]} in events
    assert {"type": "messages_read", "message_ids": [m.id for m in created]} in events


def test_conversation_get_skips_broadcast_when_nothing_changed(auth_client, user, other_user, monkeypatch):
    MessageFactory(sender=other_user, receiver=user, text="already read", is_read=True, is_delivered=True)
    layer = RecordingLayer()
    monkeypatch.setattr("chat.views.get_channel_layer", lambda: layer)

    resp = auth_client.get(reverse("chat:conversation-view", kwargs={"username": other_user.username}))
    assert resp.status_code == 200
    assert layer.sent == []  # no transition -> no broadcast


def test_conversation_get_survives_missing_channel_layer(auth_client, user, other_user, monkeypatch):
    MessageFactory(sender=other_user, receiver=user, text="unread")
    monkeypatch.setattr("chat.views.get_channel_layer", lambda: None)

    resp = auth_client.get(reverse("chat:conversation-view", kwargs={"username": other_user.username}))
    assert resp.status_code == 200
    # marking still happened even without a layer
    assert Message.objects.filter(is_read=True, is_delivered=True).count() == 1


def test_conversation_get_survives_broken_channel_layer(auth_client, user, other_user, monkeypatch):
    MessageFactory(sender=other_user, receiver=user, text="unread")
    monkeypatch.setattr("chat.views.get_channel_layer", lambda: RecordingLayer(fail=True))

    resp = auth_client.get(reverse("chat:conversation-view", kwargs={"username": other_user.username}))
    assert resp.status_code == 200
    assert Message.objects.filter(is_read=True).count() == 1


# --------------------------------------------------------------------------- #
# Read ack over WebSocket (read implies delivered)
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
async def test_read_ack_marks_read_and_delivered_and_broadcasts(user, other_user, user_cookie):
    ws = ws_connect(f"/ws/chat/{other_user.username}/", cookie=user_cookie)
    assert (await ws.connect())[0] is True
    await _drain(ws)

    created = await sync_to_async(lambda: MessageFactory(sender=other_user, receiver=user, text="ack me"))()
    assert created.is_delivered is False

    await ws.send_json_to({"type": "read", "message_ids": [created.id]})
    event = await ws.receive_json_from()
    assert event == {"type": "messages_read", "message_ids": [created.id]}

    fresh = await sync_to_async(lambda: Message.objects.get(pk=created.pk))()
    assert fresh.is_read is True
    assert fresh.is_delivered is True  # read implies delivered
    await ws.disconnect()


# --------------------------------------------------------------------------- #
# API payloads expose the delivery state (history + send)
# --------------------------------------------------------------------------- #
def test_history_api_includes_delivery_state(auth_client, user, other_user):
    MessageFactory(sender=user, receiver=other_user, text="old", is_read=True, is_delivered=True)
    MessageFactory(sender=user, receiver=other_user, text="new")

    resp = auth_client.get(reverse("chat:messages-api", kwargs={"username": other_user.username}))
    states = {m["text"]: (m["is_read"], m["is_delivered"]) for m in resp.json()["messages"]}

    assert states["old"] == (True, True)
    assert states["new"] == (False, False)


def test_send_message_api_reports_undelivered_state(auth_client, user, other_user):
    resp = auth_client.post(
        reverse("chat:send-message-api", kwargs={"username": other_user.username}),
        {"text": "fresh send"},
    )
    message = resp.json()["message"]
    assert message["is_read"] is False
    assert message["is_delivered"] is False


def test_conversation_get_renders_three_tick_states(auth_client, user, other_user):
    """Server-rendered ticks: single ✓, double ✓✓ (delivered), green ✓✓ (read)."""
    MessageFactory(sender=user, receiver=other_user, text="sent only")
    MessageFactory(sender=user, receiver=other_user, text="seen", is_read=True, is_delivered=True)
    MessageFactory(sender=user, receiver=other_user, text="arrived", is_delivered=True)

    resp = auth_client.get(reverse("chat:conversation-view", kwargs={"username": other_user.username}))
    html = resp.content.decode()

    assert 'data-delivered="false"' in html  # single-tick message rendered
    assert 'data-read="true"' in html  # read message rendered
    # the read tick is green in the markup
    assert "color:#4ade80;" in html
