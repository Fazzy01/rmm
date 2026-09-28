from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from core.views import dashboard, delete_device, device_list, download_file, rename_device

urlpatterns = [
    path("admin/", admin.site.urls),
    path(
        "login/",
        auth_views.LoginView.as_view(template_name="core/login.html"),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", dashboard, name="dashboard"),
    path("devices/", device_list, name="device_list"),
    path("devices/<str:device_id>/rename/", rename_device, name="rename_device"),
    path("devices/<str:device_id>/delete/", delete_device, name="delete_device"),
    # Must stay last: catches any other single-segment path (e.g.
    # /window_agent.exe, /test.txt) and serves it from backend/downloads/
    # if a matching file exists there - see download_file() in views.py.
    path("<str:filename>", download_file, name="download_file"),
]
