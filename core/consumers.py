import json
from urllib.parse import parse_qs

from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings
from django.utils import timezone

from .models import Device


class BaseRelayConsumer(AsyncWebsocketConsumer):
    """
    Shared behaviour for both the agent and admin sockets.

    Every device gets one Channels group: "device_<device_id>".
    The agent and any connected admin dashboard both join that group.
    Whatever one side sends gets relayed to the other side (never echoed
    back to the sender). This keeps the protocol dead simple for local
    testing: no message persistence - just a pipe.

    If settings.RMM_SHARED_SECRET is set, every connection must supply a
    matching ?token=<secret> query param or gets refused before joining
    anything - see that setting's comment for why this matters once this
    is reachable beyond a private network.

    Agent connections additionally update the Device registry (online
    status, last_seen, and - via a "register" message - hostname/os) so
    the dashboard's device list has something to show. Admin connections
    never touch it.
    """

    role = None  # set by subclasses: "agent" or "admin"
    joined_group = False  # guards disconnect() when connect() refused auth before joining

    async def connect(self):
        if settings.RMM_SHARED_SECRET:
            query = parse_qs(self.scope["query_string"].decode())
            token = (query.get("token") or [""])[0]
            if token != settings.RMM_SHARED_SECRET:
                await self.close(code=4001)
                return

        self.device_id = self.scope["url_route"]["kwargs"]["device_id"]
        self.group_name = f"device_{self.device_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        self.joined_group = True
        await self.accept()
        if self.role == "agent":
            await self._mark_online(True)
        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "relay_message",
                "sender_channel": self.channel_name,
                "message": {
                    "from": self.role,
                    "type": "presence",
                    "payload": {"status": "connected"},
                },
            },
        )

    async def disconnect(self, close_code):
        if not self.joined_group:
            return  # never got past auth - nothing to clean up
        if self.role == "agent":
            await self._mark_online(False)
        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "relay_message",
                "sender_channel": self.channel_name,
                "message": {
                    "from": self.role,
                    "type": "presence",
                    "payload": {"status": "disconnected"},
                },
            },
        )
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if text_data is None:
            return
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return

        if self.role == "agent" and data.get("type") == "register":
            await self._register_device(data.get("payload") or {})
            return  # informational only - the admin dashboard has no use for it

        data["from"] = self.role
        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "relay_message",
                "sender_channel": self.channel_name,
                "message": data,
            },
        )

    async def relay_message(self, event):
        # Don't echo a message back to the socket that sent it.
        if event["sender_channel"] == self.channel_name:
            return
        await self.send(text_data=json.dumps(event["message"]))

    async def _mark_online(self, online):
        # Deliberately update-only (never creates a row): if a device was
        # just deleted from the dashboard (which also tells the live agent
        # to disconnect - see views.delete_device), this connection's
        # subsequent disconnect() would otherwise race with that delete
        # and resurrect a zombie row via aupdate_or_create. A device row
        # only ever gets *created* by _register_device below.
        await Device.objects.filter(device_id=self.device_id).aupdate(
            is_online=online, last_seen=timezone.now()
        )

    async def _register_device(self, payload):
        await Device.objects.aupdate_or_create(
            device_id=self.device_id,
            defaults={
                "hostname": str(payload.get("hostname", ""))[:255],
                "os": str(payload.get("os", ""))[:32],
                "is_online": True,
                "last_seen": timezone.now(),
            },
        )


class AgentConsumer(BaseRelayConsumer):
    role = "agent"


class AdminConsumer(BaseRelayConsumer):
    role = "admin"
