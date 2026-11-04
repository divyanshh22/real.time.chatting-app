"""Application-wide presence tests (connection counting, heartbeats, healing).

These cover the root-cause fix for the "online status is wrong" bug:

* online state is a COUNT of open sockets, not a boolean two consumers
  fight over — closing one tab/DM must not mark the user offline,
* a grace period absorbs the socket churn of page navigation/reconnects,
* heartbeats + a stale sweep heal sockets that died without disconnecting,
* every read path self-heals ghosts instead of trusting the cached flag.

See chat/presence.py for the design.
"""

import asyncio
from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone

from chat import presence
from tests.conftest import ws_connect
from tests.factories import UserFactory

USER_MODEL = get_user_model()


def _db_user(user):
    return USER_MODEL.objects.get(pk=user.pk)


# --------------------------------------------------------------------------- #
# Sync primitives
# --------------------------------------------------------------------------- #
def test_open_connection_counts_and_marks_online(db, user):
    assert user.is_online is False

    presence.open_connection(user.id)
    fresh = _db_user(user)
    assert fresh.connection_count == 1
    assert fresh.is_online is True
    assert fresh.last_seen is None
    assert fresh.presence_heartbeat is not None

    # second tab/device
    presence.open_connection(user.id)
    assert _db_user(user).connection_count == 2


def test_close_connection_decrements_and_floors_at_zero(db, user):
    presence.open_connection(user.id)
    presence.open_connection(user.id)

    assert presence.close_connection(user.id) == 1
    assert presence.close_connection(user.id) == 0
    # defensive floor: closing more times than opened never goes negative
    assert presence.close_connection(user.id) == 0
    assert _db_user(user).connection_count == 0


def test_commit_offline_requires_zero_count(db, user):
    presence.open_connection(user.id)

    # still connected -> must not be committed offline
    assert presence.commit_offline(user.id) is None
    assert _db_user(user).is_online is True

    presence.close_connection(user.id)
    result = presence.commit_offline(user.id, when=timezone.now())
    assert result is not None
    username, last_seen_iso = result
    assert username == user.username
    fresh = _db_user(user)
    assert fresh.is_online is False
    assert fresh.last_seen is not None
    assert last_seen_iso == fresh.last_seen.isoformat()


def test_touch_heartbeat_is_throttled_and_requires_connection(db, user):
    old = timezone.now() - timedelta(seconds=120)
    presence.open_connection(user.id)
    type(user).objects.filter(pk=user.pk).update(presence_heartbeat=old)

    # stale heartbeat -> refreshed
    presence.touch_heartbeat(user.id)
    assert _db_user(user).presence_heartbeat > old

    # fresh heartbeat (< 10s old) -> left alone
    current = timezone.now() - timedelta(seconds=3)
    type(user).objects.filter(pk=user.pk).update(presence_heartbeat=current)
    presence.touch_heartbeat(user.id)
    assert abs(_db_user(user).presence_heartbeat - current) < timedelta(seconds=1)

    # no open connections -> not a live socket, no heartbeat
    presence.close_connection(user.id)
    presence.close_connection(user.id)  # floor at 0
    type(user).objects.filter(pk=user.pk).update(presence_heartbeat=old)
    presence.touch_heartbeat(user.id)
    assert _db_user(user).presence_heartbeat == old


def test_sweep_stale_heals_crashed_sockets_only(db, user, other_user):
    """Crashed sockets leave connection_count > 0; the sweep must reset it."""
    now = timezone.now()
    ghost = UserFactory(is_online=True)
    type(ghost).objects.filter(pk=ghost.pk).update(
        connection_count=3,  # sockets that never disconnected
        presence_heartbeat=now - timedelta(seconds=presence.STALE_AFTER.total_seconds() + 60),
    )
    legacy = UserFactory(is_online=True)  # pre-migration row: no heartbeat at all
    live = UserFactory(is_online=True)
    presence.open_connection(live.id)

    healed = presence.sweep_stale()
    healed_ids = {row[0] for row in healed}

    assert ghost.pk in healed_ids
    assert legacy.pk in healed_ids
    assert live.pk not in healed_ids

    ghost_row = _db_user(ghost)
    assert ghost_row.is_online is False
    assert ghost_row.connection_count == 0
    assert ghost_row.last_seen is not None  # stamped with the old heartbeat

    assert _db_user(live).is_online is True
    assert _db_user(live).connection_count == 1


def test_effective_presence_fresh_connection_is_online(db, user):
    presence.open_connection(user.id)
    is_online, last_seen = presence.effective_presence(_db_user(user))
    assert is_online is True
    assert last_seen is None


def test_effective_presence_heals_stale_ghost(db, user):
    old = timezone.now() - timedelta(seconds=presence.STALE_AFTER.total_seconds() + 5)
    type(user).objects.filter(pk=user.pk).update(
        is_online=True,
        connection_count=2,
        presence_heartbeat=old,
    )

    is_online, last_seen = presence.effective_presence(_db_user(user))
    assert is_online is False
    assert last_seen == old.isoformat()

    fresh = _db_user(user)  # the read healed the row
    assert fresh.is_online is False
    assert fresh.connection_count == 0
    assert fresh.last_seen == old


def test_effective_presence_offline_row_returns_last_seen(db, user):
    seen = timezone.now() - timedelta(minutes=5)
    type(user).objects.filter(pk=user.pk).update(is_online=False, last_seen=seen)
    is_online, last_seen = presence.effective_presence(_db_user(user))
    assert is_online is False
    assert last_seen == seen.isoformat()

    type(user).objects.filter(pk=user.pk).update(last_seen=None)
    is_online, last_seen = presence.effective_presence(_db_user(user))
    assert (is_online, last_seen) == (False, None)


def test_effective_presence_handles_deleted_row(db, user):
    type(user).objects.filter(pk=user.pk).update(is_online=True, presence_heartbeat=timezone.now())
    ghost_row = _db_user(user)
    USER_MODEL.objects.filter(pk=user.pk).delete()

    is_online, last_seen = presence.effective_presence(ghost_row)
    assert (is_online, last_seen) == (False, None)


def test_announce_without_layer_is_silent(db, monkeypatch):
    monkeypatch.setattr(presence, "get_channel_layer", lambda: None)
    presence.announce_sync(1, "someone", "online")  # must not raise


def test_announce_swallows_layer_errors(db, monkeypatch):
    class BrokenLayer:
        async def group_send(self, group, event):
            raise RuntimeError("channel layer down")

    monkeypatch.setattr(presence, "get_channel_layer", lambda: BrokenLayer())
    presence.announce_sync(1, "someone", "offline", "2026-01-01T00:00:00+00:00")  # must not raise


def test_announce_sync_sends_expected_payload(db, monkeypatch):
    class Recorder:
        def __init__(self):
            self.sent = []

        async def group_send(self, group, event):
            self.sent.append((group, event))

    recorder = Recorder()
    monkeypatch.setattr(presence, "get_channel_layer", lambda: recorder)

    presence.announce_sync(7, "riya", "offline", "2026-01-01T00:00:00+00:00")

    assert recorder.sent == [
        (
            presence.PRESENCE_GROUP,
            {
                "type": "presence_update",
                "user_id": 7,
                "username": "riya",
                "status": "offline",
                "last_seen": "2026-01-01T00:00:00+00:00",
            },
        )
    ]


# --------------------------------------------------------------------------- #
# Grace period: navigation must not flash "offline"
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
async def test_grace_period_absorbs_reconnect(user, monkeypatch):
    # patch the LIVE settings object (settings_test module is only the
    # template it was built from — consumers read django.conf.settings)
    from django.conf import settings as dj_settings

    monkeypatch.setattr(dj_settings, "PRESENCE_OFFLINE_GRACE_SECONDS", 0.3)

    await sync_to_async(presence.open_connection)(user.id)
    closing = asyncio.create_task(presence.connection_closed(user.id, user.username))
    await asyncio.sleep(0.05)  # inside the grace window...
    await sync_to_async(presence.open_connection)(user.id)  # ...next page opened a socket

    result = await closing
    assert result is None  # never announced offline
    fresh = await sync_to_async(_db_user)(user)
    assert fresh.is_online is True


@pytest.mark.django_db(transaction=True)
async def test_grace_period_expires_and_stamps_last_seen(user, monkeypatch):
    from django.conf import settings as dj_settings

    monkeypatch.setattr(dj_settings, "PRESENCE_OFFLINE_GRACE_SECONDS", 0.05)

    await sync_to_async(presence.open_connection)(user.id)
    departed = timezone.now()
    result = await presence.connection_closed(user.id, user.username)

    assert result is not None  # last_seen ISO string announced
    fresh = await sync_to_async(_db_user)(user)
    assert fresh.is_online is False
    assert fresh.connection_count == 0
    assert fresh.last_seen is not None
    assert fresh.last_seen >= departed - timedelta(seconds=1)


@pytest.mark.django_db(transaction=True)
async def test_connection_closed_handles_reconnect_race(user, monkeypatch):
    """A reconnect landing between the final count check and the write must
    not be clobbered (commit_offline's connection_count=0 guard)."""
    real_commit = presence.commit_offline

    def racing_commit(user_id, when=None):
        presence.open_connection(user_id)  # concurrent reconnect
        return real_commit(user_id, when)

    monkeypatch.setattr(presence, "commit_offline", racing_commit)

    await sync_to_async(presence.open_connection)(user.id)
    result = await presence.connection_closed(user.id, user.username)

    assert result is None
    fresh = await sync_to_async(_db_user)(user)
    assert fresh.is_online is True
    assert fresh.connection_count == 1


# --------------------------------------------------------------------------- #
# Over real WebSockets (/ws/presence/ + /ws/chat/)
# --------------------------------------------------------------------------- #
async def _drain(ws, max_msgs=8, idle=0.15):
    got = []
    for _ in range(max_msgs):
        if await ws.receive_nothing(timeout=idle):
            break
        got.append(await ws.receive_json_from())
    return got


@pytest.mark.django_db(transaction=True)
async def test_presence_multi_tab_stays_online_until_last_socket_closes(user, user_cookie):
    first = ws_connect("/ws/presence/", cookie=user_cookie)
    second = ws_connect("/ws/presence/", cookie=user_cookie)
    assert (await first.connect())[0] is True
    assert (await second.connect())[0] is True
    await _drain(first), await _drain(second)

    await first.disconnect()
    # one tab closed: NO offline broadcast to the surviving tab, still online
    assert await second.receive_nothing(timeout=0.3) is True
    fresh = await sync_to_async(_db_user)(user)
    assert fresh.is_online is True
    assert fresh.connection_count == 1

    await second.disconnect()  # last tab closed
    fresh = await sync_to_async(_db_user)(user)
    assert fresh.is_online is False
    assert fresh.last_seen is not None


@pytest.mark.django_db(transaction=True)
async def test_closing_chat_socket_does_not_mark_user_offline(user, other_user, user_cookie):
    """Regression for 'switching DMs makes the other user appear offline':
    the presence socket (open on every page) keeps the user online while a
    per-conversation socket closes."""
    presence_ws = ws_connect("/ws/presence/", cookie=user_cookie)
    chat_ws = ws_connect(f"/ws/chat/{other_user.username}/", cookie=user_cookie)
    assert (await presence_ws.connect())[0] is True
    assert (await chat_ws.connect())[0] is True
    await _drain(presence_ws), await _drain(chat_ws)

    await chat_ws.disconnect()
    fresh = await sync_to_async(_db_user)(user)
    assert fresh.is_online is True  # the bug: this used to flip to False

    await presence_ws.disconnect()
    fresh = await sync_to_async(_db_user)(user)
    assert fresh.is_online is False


@pytest.mark.django_db(transaction=True)
async def test_presence_ping_refreshes_heartbeat(user, user_cookie):
    ws = ws_connect("/ws/presence/", cookie=user_cookie)
    assert (await ws.connect())[0] is True
    await _drain(ws)

    stale = timezone.now() - timedelta(seconds=60)
    await sync_to_async(lambda: type(user).objects.filter(pk=user.pk).update(presence_heartbeat=stale))()

    await ws.send_json_to({"type": "ping"})
    # the ping handler runs to completion before the next request is served
    await ws.send_json_to({"type": "get_status", "user_ids": [user.id]})
    reply = await ws.receive_json_from()
    assert reply["type"] == "bulk_status"

    fresh = await sync_to_async(_db_user)(user)
    assert fresh.presence_heartbeat > stale
    assert reply["statuses"][str(user.id)]["status"] == "online"
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_sweep_on_connect_announces_stale_users(user, user_cookie):
    ghost = await sync_to_async(UserFactory)(is_online=True)
    await sync_to_async(
        lambda: (
            type(ghost)
            .objects.filter(pk=ghost.pk)
            .update(
                connection_count=2,
                presence_heartbeat=timezone.now() - timedelta(seconds=presence.STALE_AFTER.total_seconds() + 60),
            )
        )
    )()

    ws = ws_connect("/ws/presence/", cookie=user_cookie)
    assert (await ws.connect())[0] is True
    events = await _drain(ws)

    offline_events = [
        e
        for e in events
        if e["type"] == "presence_update"
        and e["user_id"] == ghost.pk
        and e["status"] == "offline"
        and e.get("last_seen")
    ]
    assert offline_events, f"expected stale offline announce, got: {events}"

    healed = await sync_to_async(_db_user)(ghost)
    assert healed.is_online is False
    assert healed.connection_count == 0
    await ws.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_get_status_reports_effective_presence_and_heals(user, other_user, user_cookie):
    # other_user is a ghost: flagged online, but heartbeat long gone
    await sync_to_async(
        lambda: (
            type(other_user)
            .objects.filter(pk=other_user.pk)
            .update(
                is_online=True,
                connection_count=1,
                presence_heartbeat=timezone.now() - timedelta(seconds=presence.STALE_AFTER.total_seconds() + 60),
            )
        )
    )()

    ws = ws_connect("/ws/presence/", cookie=user_cookie)
    assert (await ws.connect())[0] is True
    await _drain(ws)

    await ws.send_json_to({"type": "get_status", "user_ids": [user.id, other_user.id]})
    reply = await ws.receive_json_from()

    assert reply["statuses"][str(user.id)]["status"] == "online"
    assert reply["statuses"][str(other_user.id)]["status"] == "offline"

    healed = await sync_to_async(_db_user)(other_user)
    assert healed.is_online is False
    await ws.disconnect()
