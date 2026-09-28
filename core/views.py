from pathlib import Path

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from .models import Device

DEVICES_PER_PAGE = 50

# Anything you drop in this folder becomes downloadable at
# http://<server>/<same filename>, e.g. downloads/window_agent.exe ->
# /window_agent.exe. See download_file() below.
DOWNLOADS_DIR = settings.BASE_DIR / "downloads"


def _send_to_device(device_id, message):
    """Push a message into a device's relay group from plain HTTP view
    code (not a consumer) - the same mechanism AdminConsumer.receive() uses
    internally, just invoked directly via the channel layer. sender_channel
    is set to a value no real consumer's channel_name will ever equal, so
    relay_message delivers it to every consumer in the group (the agent,
    and any admin dashboard tabs open on this device)."""
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"device_{device_id}",
        {"type": "relay_message", "sender_channel": None, "message": message},
    )


@login_required
def dashboard(request):
    device_id = request.GET.get("device")
    if not device_id:
        return device_list(request)
    return render(request, "core/dashboard.html", {"device_id": device_id})


@login_required
def device_list(request):
    query = request.GET.get("q", "").strip()
    devices = Device.objects.all()
    if query:
        devices = devices.filter(
            Q(device_id__icontains=query)
            | Q(hostname__icontains=query)
            | Q(display_name__icontains=query)
        )
    paginator = Paginator(devices, DEVICES_PER_PAGE)
    page = paginator.get_page(request.GET.get("page"))
    token = request.GET.get("token", "")
    return render(request, "core/device_list.html", {"page": page, "query": query, "token": token})


@login_required
@require_POST
def rename_device(request, device_id):
    device = get_object_or_404(Device, device_id=device_id)
    device.display_name = request.POST.get("display_name", "").strip()
    device.save(update_fields=["display_name"])
    return JsonResponse({"ok": True, "name": device.name})


@login_required
@require_POST
def delete_device(request, device_id):
    device = get_object_or_404(Device, device_id=device_id)
    was_online = device.is_online
    if was_online:
        # Tell the live agent to remove its own startup task and exit, so
        # it doesn't silently reconnect a moment later and recreate this
        # row. See agent.py's "uninstall" handling.
        _send_to_device(device_id, {"type": "uninstall", "from": "admin", "payload": {}})
    device.delete()
    return JsonResponse({"ok": True, "was_online": was_online})


def download_file(request, filename):
    # Path(...).name strips any directory components (e.g. "../../secret")
    # down to just the filename, so this can only ever serve something
    # that's directly inside DOWNLOADS_DIR - no path traversal.
    safe_name = Path(filename).name
    file_path = DOWNLOADS_DIR / safe_name
    if not file_path.is_file():
        raise Http404(f"No file named {safe_name!r} in {DOWNLOADS_DIR}")
    return FileResponse(open(file_path, "rb"), as_attachment=True, filename=safe_name)
