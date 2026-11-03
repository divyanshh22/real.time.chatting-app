import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone


class PresenceConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope["user"]

        if not self.user.is_authenticated:
            await self.close()
            return

        await self.channel_layer.group_add("global_presence", self.channel_name)
        await self.set_online(self.user.id)

        await self.channel_layer.group_send(
            "global_presence",
            {
                "type": "presence_update",
                "user_id": self.user.id,
                "username": self.user.username,
                "status": "online"
            }
        )
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "user") and self.user.is_authenticated:
            await self.set_offline(self.user.id)
            await self.channel_layer.group_send(
                "global_presence",
                {
                    "type": "presence_update",
                    "user_id": self.user.id,
                    "username": self.user.username,
                    "status": "offline",
                    "last_seen": timezone.now().isoformat()
                }
            )
            await self.channel_layer.group_discard("global_presence", self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if text_data is None:
            return  # binary frames are ignored

        try:
            data = json.loads(text_data)
        except (ValueError, TypeError):
            return  # malformed JSON is dropped instead of crashing
        if not isinstance(data, dict):
            return

        # Client can ask "who's online right now" when the page loads
        if data.get("type") == "get_status":
            user_ids = data.get("user_ids", [])
            if not isinstance(user_ids, (list, tuple)):
                return
            # keep only real integer ids — strings/None/bools would crash the ORM
            valid_ids = [
                uid for uid in user_ids
                if isinstance(uid, int) and not isinstance(uid, bool)
            ]
            statuses = await self.get_statuses(valid_ids)
            await self.send(text_data=json.dumps({
                "type": "bulk_status",
                "statuses": statuses
            }))

    async def presence_update(self, event):
        await self.send(text_data=json.dumps(event))

    @database_sync_to_async
    def set_online(self, user_id):
        User = get_user_model()
        User.objects.filter(id=user_id).update(is_online=True, last_seen=None)

    @database_sync_to_async
    def set_offline(self, user_id):
        User = get_user_model()
        User.objects.filter(id=user_id).update(is_online=False, last_seen=timezone.now())

    @database_sync_to_async
    def get_statuses(self, user_ids):
        User = get_user_model()
        users = User.objects.filter(id__in=user_ids)
        result = {}
        for user in users:
            result[str(user.id)] = {
                "status": "online" if user.is_online else "offline",
                "last_seen": user.last_seen.isoformat() if user.last_seen else None,
            }
        return result
