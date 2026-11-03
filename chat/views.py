import redis
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.contrib import messages as django_messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from login.models import CustomUser, ProfilePic
from login.utils import is_valid_image_upload

from .models import Message


def _redis_client():
    if not getattr(settings, 'USE_REDIS', False):
        return None
    return redis.Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )


def _build_home_inbox_context(request):
    ProfilePic.objects.get_or_create(user=request.user)

    all_messages = Message.objects.filter(
        Q(sender=request.user) | Q(receiver=request.user)
    ).select_related('sender', 'receiver').order_by('-timestamp')

    user_ids = set()
    latest_message_by_user = {}
    unread_count_by_user = {}

    for msg in all_messages:
        if msg.sender != request.user:
            user_ids.add(msg.sender.id)
            latest_message_by_user[msg.sender.id] = msg
            if not msg.is_read and msg.receiver == request.user:
                unread_count_by_user[msg.sender.id] = unread_count_by_user.get(msg.sender.id, 0) + 1
        if msg.receiver != request.user:
            user_ids.add(msg.receiver.id)
            if msg.receiver.id not in latest_message_by_user or msg.timestamp > latest_message_by_user[msg.receiver.id].timestamp:
                latest_message_by_user[msg.receiver.id] = msg

    chat_users = list(CustomUser.objects.filter(id__in=user_ids))

    for user in chat_users:
        ProfilePic.objects.get_or_create(user=user)
        last_message = latest_message_by_user.get(user.id)
        user.last_message = last_message
        user.last_message_preview = (last_message.text or 'Photo') if last_message else ''
        user.last_message_time = last_message.timestamp if last_message else None
        user.unread_count = unread_count_by_user.get(user.id, 0)

    chat_users.sort(key=lambda user: (user.last_message.timestamp if user.last_message else timezone.datetime.min.replace(tzinfo=timezone.utc)), reverse=True)

    online_users = {}
    last_seen = {}
    r = _redis_client()
    if r is not None:
        try:
            online_users = r.hgetall('online_users')
            last_seen = r.hgetall('last_seen')
        except Exception:
            pass

    for user in chat_users:
        user.is_online = online_users.get(str(user.id)) == 'online' or user.is_online
        user.last_seen_str = last_seen.get(str(user.id)) or user.last_seen

    return {'chat_users': chat_users}


def get_room_group_name(user_id_1, user_id_2):
    return f"chat_{min(user_id_1, user_id_2)}_{max(user_id_1, user_id_2)}"


def get_user_presence(user_id):
    user = CustomUser.objects.filter(id=user_id).first()
    if user:
        return bool(user.is_online), user.last_seen.isoformat() if user.last_seen else None

    r = _redis_client()
    if r is not None:
        try:
            is_online = r.hget('online_users', str(user_id)) == 'online'
            last_seen = r.hget('last_seen', str(user_id))
            return is_online, last_seen
        except Exception:
            pass
    return False, None


def set_user_presence(user_id, is_online, last_seen=None):
    try:
        CustomUser.objects.filter(id=user_id).update(
            is_online=is_online,
            last_seen=last_seen if not is_online else None,
        )
    except Exception:
        pass

    r = _redis_client()
    if r is not None:
        try:
            if is_online:
                r.hset('online_users', str(user_id), 'online')
                r.hdel('last_seen', str(user_id))
            else:
                r.hdel('online_users', str(user_id))
                if last_seen is None:
                    last_seen = timezone.now().isoformat()
                r.hset('last_seen', str(user_id), last_seen)
        except Exception:
            pass


@login_required(login_url='login-view')
def home_view(request):
    context = _build_home_inbox_context(request)
    return render(request, 'chat/home.html', context)


@login_required(login_url='login-view')
@require_http_methods(["GET"])
def home_inbox_api(request):
    context = _build_home_inbox_context(request)
    payload = []
    for user in context['chat_users']:
        payload.append({
            'id': user.id,
            'username': user.username,
            'preview': user.last_message_preview,
            'last_message_time': user.last_message_time.isoformat() if user.last_message_time else None,
            'unread_count': user.unread_count,
            'is_online': user.is_online,
            'profile_pic_url': user.profilepic.profile_pic.url if getattr(user.profilepic, 'profile_pic', None) else None,
            'conversation_url': f"/chat/{user.username}/",
        })
    return JsonResponse({'users': payload})



@login_required(login_url='login-view')
def conversation_view(request, username):
    other_user = get_object_or_404(CustomUser, username=username)
    ProfilePic.objects.get_or_create(user=other_user)

    # dono ke beech ke messages
    messages = Message.objects.filter(
        Q(sender=request.user, receiver=other_user) |
        Q(sender=other_user, receiver=request.user)
    ).order_by('timestamp')

    # messages read mark karo
    Message.objects.filter(
        sender=other_user,
        receiver=request.user,
        is_read=False
    ).update(is_read=True)

    # POST - naya message save karo
    if request.method == 'POST':
        # NUL bytes would crash PostgreSQL inserts (psycopg2 ValueError)
        text = request.POST.get('text', '').replace('\x00', '').strip()
        photo = request.FILES.get('photo')

        if photo and not is_valid_image_upload(photo):
            # NB: aliased import — this function has a local `messages` QuerySet
            django_messages.error(request, "Please upload a valid image (JPG, PNG, WEBP) under 5 MB.")
            return redirect('chat:conversation-view', username=username)

        if text or photo:
            message = Message.objects.create(
                sender=request.user,
                receiver=other_user,
                text=text,
                photo=photo
            )

            # Broadcast over WebSocket so the other user gets it in real time
            try:
                channel_layer = get_channel_layer()
                async_to_sync(channel_layer.group_send)(get_room_group_name(request.user.id, other_user.id), {
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
                })
            except Exception:
                pass
        return redirect('chat:conversation-view', username=username)

    # Get online status from DB first, with Redis as a fallback
    other_user.is_online, other_user.last_seen_str = get_user_presence(other_user.id)

    return render(request, 'chat/conversation.html', {
        'other_user': other_user,
        'messages': messages,
    })


@login_required(login_url='login-view')
def search_view(request):
    query = request.GET.get('q', '').strip()
    results = []

    if query:
        results = CustomUser.objects.filter(
            username__icontains=query
        ).exclude(id=request.user.id)  # khud ko exclude karo

    return render(request, 'chat/search.html', {
        'results': results,
        'query': query,
    })


@login_required(login_url='login-view')
@require_http_methods(["POST"])
def send_message_api(request, username):
    other_user = get_object_or_404(CustomUser, username=username)
    # NUL bytes would crash PostgreSQL inserts (psycopg2 ValueError)
    text = request.POST.get('text', '').replace('\x00', '').strip()
    photo = request.FILES.get('photo')

    if photo and not is_valid_image_upload(photo):
        return JsonResponse({'error': 'Invalid image file'}, status=400)

    if not text and not photo:
        return JsonResponse({'error': 'Message cannot be empty'}, status=400)

    message = Message.objects.create(
        sender=request.user,
        receiver=other_user,
        text=text,
        photo=photo,
    )

    room_group_name = get_room_group_name(request.user.id, other_user.id)
    payload = {
        'type': 'message',
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

    try:
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(room_group_name, {
            'type': 'chat_message',
            'message': payload['message'],
        })
    except Exception:
        pass

    return JsonResponse(payload)


@login_required(login_url='login-view')
@require_http_methods(["GET"])
def get_messages_api(request, username):
    """API endpoint to fetch messages for WebSocket initialization"""
    other_user = get_object_or_404(CustomUser, username=username)

    messages = Message.objects.filter(
        Q(sender=request.user, receiver=other_user) |
        Q(sender=other_user, receiver=request.user)
    ).order_by('timestamp')

    messages_data = []
    for msg in messages:
        messages_data.append({
            'id': msg.id,
            'sender_id': msg.sender.id,
            'sender_username': msg.sender.username,
            'text': msg.text,
            'photo_url': msg.photo.url if msg.photo else None,
            'timestamp': msg.timestamp.isoformat(),
            'is_read': msg.is_read
        })

    return JsonResponse({'messages': messages_data})


@login_required(login_url='login-view')
@require_http_methods(["GET"])
def get_user_status_api(request, user_id):
    """API endpoint to get user online status"""
    try:
        user = CustomUser.objects.get(id=user_id)
        is_online = False
        last_seen = None
        is_online, last_seen = get_user_presence(user_id)

        return JsonResponse({
            'user_id': user.id,
            'username': user.username,
            'is_online': is_online,
            'last_seen': last_seen
        })
    except CustomUser.DoesNotExist:
        return JsonResponse({'error': 'User not found'}, status=404)
