from django.urls import re_path

from core.consumers import AdminConsumer, AgentConsumer

websocket_urlpatterns = [
    re_path(r"ws/agent/(?P<device_id>[\w-]+)/$", AgentConsumer.as_asgi()),
    re_path(r"ws/admin/(?P<device_id>[\w-]+)/$", AdminConsumer.as_asgi()),
]
