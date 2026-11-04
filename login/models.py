from django.db import models
from django.contrib.auth.models import AbstractUser


class CustomUser(AbstractUser):
    is_online = models.BooleanField(default=False)
    last_seen = models.DateTimeField(null=True, blank=True)
    # Number of currently-open WebSocket connections (tabs/devices) for this
    # user. The user is online iff connection_count > 0 with a fresh heartbeat
    # — see chat/presence.py. This is what makes status multi-tab safe.
    connection_count = models.PositiveIntegerField(default=0)
    # Last heartbeat received from any of the user's connections. Detects
    # sockets that died without a clean disconnect (crash, network drop).
    presence_heartbeat = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.username


class ProfilePic(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE)
    profile_pic = models.ImageField(upload_to='profile_pics/', blank=True, null=True)
    joined_date = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.user.username