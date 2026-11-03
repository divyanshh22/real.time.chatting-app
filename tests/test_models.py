"""Unit tests for the models: chat.Message, login.CustomUser, login.ProfilePic.

All objects are created through factory-boy factories — no hardcoded IDs.
"""

from datetime import timedelta

import pytest
from django.db import IntegrityError
from django.utils import timezone
from freezegun import freeze_time

from chat.models import Message
from login.models import CustomUser, ProfilePic
from tests.factories import MessageFactory, UserFactory

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# CustomUser
# --------------------------------------------------------------------------- #
def test_custom_user_str_returns_username(user):
    assert str(user) == user.username


def test_custom_user_defaults(user):
    assert user.is_online is False
    assert user.last_seen is None
    assert user.is_active is True


def test_custom_user_username_is_unique(db):
    UserFactory(username="taken_name")
    with pytest.raises(IntegrityError):
        UserFactory(username="taken_name")


def test_profilepic_auto_created_by_signal(user):
    """login.signals creates a ProfilePic for every new user."""
    assert hasattr(user, "profilepic")
    profile = ProfilePic.objects.get(user=user)
    assert str(profile) == user.username


def test_profilepic_joined_date_is_auto_set(user):
    profile = ProfilePic.objects.get(user=user)
    assert profile.joined_date is not None
    assert abs(profile.joined_date - timezone.now()) < timedelta(minutes=1)


def test_profilepic_cascade_delete_with_user(user):
    profile_id = ProfilePic.objects.get(user=user).id
    user.delete()
    assert not ProfilePic.objects.filter(id=profile_id).exists()


# --------------------------------------------------------------------------- #
# Message
# --------------------------------------------------------------------------- #
def test_message_str(user, other_user):
    msg = MessageFactory(sender=user, receiver=other_user)
    assert str(msg) == f"{user} → {other_user}"


def test_message_defaults(user, other_user):
    msg = Message.objects.create(sender=user, receiver=other_user)
    assert msg.text == ""
    assert not msg.photo  # no file attached (ImageFieldFile is falsy when empty)
    assert msg.is_read is False


def test_message_timestamp_auto_set(user, other_user):
    from datetime import datetime, timezone as dt_timezone

    with freeze_time("2024-06-01 12:00:00"):
        msg = MessageFactory(sender=user, receiver=other_user)
    # auto_now_add must capture the (frozen) creation time
    assert msg.timestamp == datetime(2024, 6, 1, 12, 0, tzinfo=dt_timezone.utc)


def test_message_meta_ordering_is_timestamp():
    assert Message._meta.ordering == ["timestamp"]


def test_messages_ordered_by_timestamp_not_creation(user, other_user, third_user):
    """Ordering follows the timestamp field, even if rows were created out of order."""
    now = timezone.now()
    m1 = MessageFactory(sender=user, receiver=other_user, text="oldest")
    m2 = MessageFactory(sender=user, receiver=other_user, text="newest")
    m3 = MessageFactory(sender=user, receiver=other_user, text="middle")

    Message.objects.filter(pk=m1.pk).update(timestamp=now - timedelta(hours=2))
    Message.objects.filter(pk=m3.pk).update(timestamp=now - timedelta(hours=1))
    Message.objects.filter(pk=m2.pk).update(timestamp=now)

    texts = list(Message.objects.filter(sender=user).order_by("timestamp").values_list("text", flat=True))
    assert texts == ["oldest", "middle", "newest"]


def test_message_related_names(user, other_user):
    sent = MessageFactory(sender=user, receiver=other_user, text="outgoing")
    received = MessageFactory(sender=other_user, receiver=user, text="incoming")

    assert list(user.sent_messages.all()) == [sent]
    assert list(user.received_messages.all()) == [received]
    assert list(other_user.sent_messages.all()) == [received]
    assert list(other_user.received_messages.all()) == [sent]


def test_message_sender_cascade_delete(user, other_user):
    msg = MessageFactory(sender=user, receiver=other_user)
    user.delete()
    assert not Message.objects.filter(id=msg.id).exists()


def test_message_receiver_cascade_delete(user, other_user):
    msg = MessageFactory(sender=user, receiver=other_user)
    other_user.delete()
    assert not Message.objects.filter(id=msg.id).exists()


def test_message_requires_sender(db, other_user):
    with pytest.raises(IntegrityError):
        Message.objects.create(receiver=other_user, text="no sender")


def test_message_requires_receiver(db, user):
    with pytest.raises(IntegrityError):
        Message.objects.create(sender=user, text="no receiver")


def test_message_text_field_has_no_max_length():
    """TextField has no max_length — long messages must be storable."""
    field = Message._meta.get_field("text")
    assert field.max_length is None


def test_message_stores_very_long_text(user, other_user):
    long_text = "x" * 100_000
    msg = MessageFactory(sender=user, receiver=other_user, text=long_text)
    msg.refresh_from_db()
    assert len(msg.text) == 100_000


def test_message_upload_paths():
    assert Message._meta.get_field("photo").upload_to == "chat_images/"
    assert ProfilePic._meta.get_field("profile_pic").upload_to == "profile_pics/"


def test_custom_user_model_str_and_meta():
    """CustomUser is the configured AUTH_USER_MODEL."""
    from django.conf import settings

    assert settings.AUTH_USER_MODEL == "login.CustomUser"
    assert CustomUser._meta.pk.name == "id"
