import json
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from .models import Message
from login.models import CustomUser


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.other_username = self.scope['url_route']['kwargs']['username']
        self.me = self.scope['user']

        if not self.me or not self.me.is_authenticated:
            await self.close()
            return

        try:
            self.other_user = await database_sync_to_async(CustomUser.objects.get)(username=self.other_username)
        except ObjectDoesNotExist:
            await self.close()
            return

        self.room_group_name = self._get_room_group_name(self.me.id, self.other_user.id)
        try:
            await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        except Exception:
            pass

        await database_sync_to_async(self._set_online_state)(self.me.id, True)
        await self.accept()
        await self._announce_status('online')

    async def disconnect(self, close_code):
        if not hasattr(self, 'room_group_name'):
            # connect() never completed (no prior connect, anonymous user, or
            # unknown chat partner) — there is nothing to clean up.
            return

        try:
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)
        except Exception:
            pass

        await database_sync_to_async(self._set_online_state)(self.me.id, False)
        await self._announce_status('offline')

    async def receive(self, text_data=None, bytes_data=None):
        if text_data is None:
            return  # binary frames are ignored

        try:
            payload = json.loads(text_data)
        except (ValueError, TypeError):
            return  # malformed JSON is dropped instead of crashing the consumer
        if not isinstance(payload, dict):
            return

        message_type = payload.get('type')

        if message_type == 'message':
            await self._handle_message(payload)
        elif message_type == 'typing':
            await self._broadcast_typing(payload.get('is_typing', False))
        elif message_type == 'read':
            await self._mark_messages_read(payload.get('message_ids', []))

    async def chat_message(self, event):
        await self.send(text_data=json.dumps({
            'type': 'message',
            'message': event['message'],
        }))

    async def user_status(self, event):
        await self.send(text_data=json.dumps({
            'type': 'user_status',
            'status': event['status'],
            'last_seen': event.get('last_seen'),
            'username': event.get('username'),
        }))

    async def typing_status(self, event):
        await self.send(text_data=json.dumps({
            'type': 'typing',
            'username': event['username'],
            'is_typing': event['is_typing'],
        }))

    async def messages_read(self, event):
        await self.send(text_data=json.dumps({
            'type': 'messages_read',
            'message_ids': event['message_ids'],
        }))

    @staticmethod
    def _get_room_group_name(user_id_1, user_id_2):
        return f"chat_{min(user_id_1, user_id_2)}_{max(user_id_1, user_id_2)}"

    async def _announce_status(self, status):
        now = timezone.now().isoformat()
        try:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'user_status',
                    'status': status,
                    'last_seen': now if status == 'offline' else None,
                    'username': self.me.username,
                }
            )
        except Exception:
            pass

    async def _broadcast_typing(self, is_typing):
        try:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'typing_status',
                    'username': self.me.username,
                    'is_typing': is_typing,
                }
            )
        except Exception:
            pass

    async def _handle_message(self, payload):
        text = payload.get('text')
        if not isinstance(text, str):
            return  # non-string text (int, list, dict, ...) is dropped
        # NUL bytes would crash PostgreSQL inserts (psycopg2 ValueError)
        text = text.replace('\x00', '').strip()
        if not text:
            return

        message = await database_sync_to_async(self._create_message)(text=text)
        try:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'chat_message',
                    'message': {
                        'id': message.id,
                        'sender_id': message.sender.id,
                        'sender_username': message.sender.username,
                        'text': message.text,
                        'photo_url': message.photo.url if message.photo else None,
                        'timestamp': message.timestamp.isoformat(),
                        'is_read': message.is_read,
                    },
                }
            )
        except Exception:
            pass

    def _create_message(self, text):
        return Message.objects.create(
            sender=self.me,
            receiver=self.other_user,
            text=text,
        )

    def _set_online_state(self, user_id, is_online):
        CustomUser.objects.filter(id=user_id).update(
            is_online=is_online,
            last_seen=None if is_online else timezone.now(),
        )

    async def _mark_messages_read(self, message_ids):
        if not isinstance(message_ids, (list, tuple)):
            return
        # keep only real integer ids — strings/None/bools would crash the ORM
        ids = [mid for mid in message_ids if isinstance(mid, int) and not isinstance(mid, bool)]
        if not ids:
            return

        updated = await database_sync_to_async(self._mark_ids_as_read)(ids)
        if not updated:
            return

        try:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'messages_read',
                    'message_ids': ids,
                }
            )
        except Exception:
            pass

    def _mark_ids_as_read(self, message_ids):
        return Message.objects.filter(id__in=message_ids, receiver=self.me).update(is_read=True)
