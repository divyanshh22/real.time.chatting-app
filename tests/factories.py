"""factory-boy factories for all models in the app.

Rules:
- No hardcoded IDs anywhere; every object is created through a factory.
- ``CustomUser`` creation fires the ``post_save`` signal in
  ``login.signals`` which auto-creates a ``ProfilePic``, exactly like
  production code paths do.
"""

import factory

from chat.models import Message
from login.models import CustomUser


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = CustomUser

    username = factory.Sequence(lambda n: f"user{n}")
    email = factory.LazyAttribute(lambda user: f"{user.username}@example.com")
    first_name = factory.Faker("first_name")
    last_name = factory.Faker("last_name")
    # Stored hashed (set_password), never as plaintext.
    password = factory.PostGenerationMethodCall("set_password", "Str0ng!Passw0rd")
    is_active = True
    is_online = False

    @classmethod
    def _after_postgeneration(cls, instance, create, results=None):
        # Save explicitly so ``set_password()`` (a post-generation hook) is
        # persisted — without factory-boy's deprecated auto-save warning.
        if create and results:
            instance.save()


class MessageFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Message

    sender = factory.SubFactory(UserFactory)
    receiver = factory.SubFactory(UserFactory)
    text = factory.Sequence(lambda n: f"message text {n}")
    is_read = False
    # photo: left empty by default (blank=True, null=True on the model)
    # timestamp: auto_now_add, set by the model itself
