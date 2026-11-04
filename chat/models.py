from django.db import models
from login.models import CustomUser

class Message(models.Model):
    sender = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='sent_messages')
    receiver = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='received_messages')
    text = models.TextField(blank=True)
    photo = models.ImageField(upload_to='chat_images/', blank=True, null=True)  # ← ye fix kar
    timestamp = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)
    # True once the receiver's conversation is open on their client (double
    # tick); is_read=True additionally means they actually saw it.
    is_delivered = models.BooleanField(default=False)

    class Meta:
        ordering = ['timestamp']

    def __str__(self):
        return f"{self.sender} → {self.receiver}"
