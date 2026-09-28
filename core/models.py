from django.db import models


class Device(models.Model):
    """
    Registry of every device that has ever connected as an agent, so the
    dashboard can list/search/paginate them instead of requiring the admin
    to already know a device_id. Written to by the consumers on
    connect/disconnect/register (see core/consumers.py), not by any admin
    UI directly.
    """

    device_id = models.CharField(max_length=64, unique=True)
    hostname = models.CharField(max_length=255, blank=True)  # auto-reported by the agent
    display_name = models.CharField(max_length=255, blank=True)  # admin override, wins if set
    os = models.CharField(max_length=32, blank=True)
    is_online = models.BooleanField(default=False)
    last_seen = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-last_seen"]

    @property
    def name(self):
        return self.display_name or self.hostname or self.device_id

    def __str__(self):
        return f"{self.name} ({self.os})"
