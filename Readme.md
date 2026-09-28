backend
cd D:\WORK\manager\rmm-project\rmm-project\backend
daphne -b 0.0.0.0 -p 8000 config.asgi:application


Windows agent (from agents/python-agent/, in a separate terminal):

cd D:\WORK\manager\rmm-project\rmm-project\agents\python-agent

python agent.py --server ws://localhost:8000 --device test-device-1

---

# Full walkthrough: how the whole app fits together

## The pieces

- **Backend** (`backend/`) — Django + Channels, run via `daphne`. It's a
  relay: agents and admin dashboards connect over WebSocket and it passes
  messages between whichever pair shares a `device_id`. It also keeps a
  small `Device` registry (SQLite, `core/models.py`) so the dashboard can
  list every device that's ever connected, search it, and paginate it.
- **Windows agent** (`agents/python-agent/agent.py`) — connects as a
  device, executes PowerShell commands, streams the screen, and (once
  "isolated session" is enabled) gives the admin a second, fully private
  Win32 desktop to work in that the user never sees.
- **Android agent** (`agents/android-agent/`) — a Kotlin app that connects
  the same way and streams the phone's screen (via `MediaProjection`).
  Remote command execution and tap/gesture control aren't built for
  Android yet — screen viewing only, for now.
- **Dashboard** (`backend/core/templates/core/`) — the browser UI. `/`
  (no `?device=` param) shows the device list; `/?device=<id>` opens that
  device's live view.

## Running it for local testing

1. **Backend**: `cd backend`, `pip install -r requirements.txt`,
   `python manage.py migrate`, then
   `daphne -b 0.0.0.0 -p 8000 config.asgi:application`.
2. **Windows agent**: `cd agents\python-agent`,
   `python agent.py --server ws://localhost:8000 --device my-pc`.
3. **Android agent**: build/install the app (see below), open it, type
   the server address (`ws://<this-pc's-LAN-IP>:8000`) and a device id,
   and it connects automatically on launch.
4. Open `http://localhost:8000/` (or `http://<LAN-IP>:8000/` from another
   device) — you'll land on the device list. Click a device to open its
   live dashboard.

## Changing the server address later

This is the part that's easy to get wrong, because two different things
get configured in two different ways:

- **`rmm-agent.exe` itself has no baked-in server.** It reads `--server`
  from the command line every time it's launched (default
  `ws://localhost:8000`). You only need to rebuild this if `agent.py`'s
  *code* changed:
  ```
  cd agents\python-agent
  pyinstaller --onefile --noconsole --name rmm-agent agent.py
  ```
- **The installer bakes the server address in** at the moment you
  *compile* it, via the `/DServerURL` flag — that's what actually needs
  rebuilding when only the server address changes (no need to touch
  `agent.py` or re-run PyInstaller):
  ```
  cd agents\python-agent\installer
  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DServerURL=ws://YOUR_SERVER:8000 rmm-agent.iss
  ```
  Add `/DAgentToken=your-secret` too if the server has
  `RMM_SHARED_SECRET` set (see Auth below). Output lands in
  `agents\python-agent\installer\output\rmm-agent-setup.exe`.
- **The Android app's server address is just a text field**, typed into
  the app and saved locally — no rebuild needed to point it somewhere
  else, just retype it.

Note: what Inno Setup produces is a `.exe` **installer**, not a true
`.msi`. It installs/uninstalls the same way, but if you ever specifically
need `.msi` format (e.g. Group Policy/SCCM deployment), that needs a
different toolchain (WiX) - not set up here yet.

## What the installer actually does

`rmm-agent-setup.exe` copies `rmm-agent.exe` into `Program Files` and
registers it as a **SYSTEM-level Scheduled Task** that starts at boot -
running before any user logs in, and surviving reboots/logoffs, the way a
real RMM agent needs to. Each install gets a unique `device_id`
automatically (the machine's own hostname via `%COMPUTERNAME%`), so you
can hand the same installer to many machines without configuring each one
individually. Uninstalling stops the task and removes everything cleanly.

## Auth (off by default)

By default, any agent or admin that knows a `device_id` can connect - no
password, nothing. That's deliberate for local testing. If you ever host
this somewhere reachable beyond a trusted private network, set
`RMM_SHARED_SECRET` on the server before starting `daphne`, and pass the
same value as `--token` to agents / `?token=` on the dashboard URL /
the Android app's token field. Leave it unset and nothing changes.

## Feature summary (as of this point)

- PowerShell/shell execution, invisible to the user (no console flash).
- Live screen viewing - the user's real screen, streamed as PNG frames.
- **Isolated desktop session**: a second, private Win32 desktop the admin
  can open apps/files in, fully invisible to the user and independent of
  what they're doing. Supports full mouse/keyboard control (including
  double-click and right-click), not just viewing.
  - "Exit isolated session" stops watching but leaves it running in the
    background (resume later, exactly as left).
  - "Clear session" actually kills everything in it and starts fresh.
- Both screens (user + isolated) can stream side by side at once.
- Mobile-friendly dashboard controls: on-demand virtual keyboard (doesn't
  pop up on every tap), double-tap for double-click, a "right-click, next
  tap" toggle for touchscreens.
- Device registry: searchable, paginated list of every device that's
  connected, auto-named from the machine's hostname (or the phone's own
  Bluetooth device name on Android), renameable from the dashboard.
- Android agent: connects and streams its screen (via `MediaProjection`,
  which needs a one-time on-screen consent per Android's own security
  model - this can't be skipped or hidden, by design).
- Packaging: PyInstaller → standalone `.exe`, Inno Setup → distributable
  installer with auto-start.

## Known gaps (still true)

- No TLS - everything is plain `ws://`/`http://`. Fine on a private LAN,
  not fine once this crosses a public network without a reverse proxy
  terminating TLS in front of it.
- Android agent has no remote command execution or tap/gesture control
  yet - screen viewing only.
- No true `.msi` packaging (see above).

## macOS agent (added later)

`agents/python-agent/agent.py` is the same file for Windows and macOS -
connect/register/shell/screen-view worked on both from the start; remote
mouse/keyboard control was added via Quartz/`CGEvent` (pyobjc), gated
behind `IS_MACOS`/`HAVE_QUARTZ` the same way the Windows path is gated
behind `IS_WINDOWS`/`HAVE_PYWIN32`. **No isolated-desktop equivalent
exists on macOS** - there's no macOS analog of a background Win32 desktop
object; the closest thing (switching to another OS user account) visibly
locks the current session, so it isn't implemented.

**This has not been tested on an actual Mac yet** - written and
syntax-checked on this Windows dev machine only (PyInstaller also can't
cross-compile a real macOS binary from Windows, which is why there's no
compiled `.app`/onefile executable for macOS the way `rmm-agent.exe`
exists for Windows - the mac side runs via system Python + a venv
instead). To test for real:

1. Get the three files onto a Mac: `agent.py`, `requirements.txt`,
   `installer/install-macos.sh` (or just download
   `http://<server>/RMM-Agent-macOS.zip`, unzip, double-click
   `Install RMM Agent (macOS).command`).
2. Watch for the Gatekeeper "unidentified developer" warning on first
   launch (expected - right-click -> Open once) and the Screen
   Recording / Accessibility prompts the installer opens automatically -
   check "python3" (or add it via the venv path it prints) in both.
3. After granting, restart the agent:
   `launchctl kickstart -k gui/$(id -u)/com.rmm.agent`.
4. Confirm on the dashboard: device shows online, screen view renders,
   remote click/type actually reaches the Mac, `run_shell` returns
   output.

Distribution mirrors Windows: `agents/index.html` is an OS-detecting
download page (auto-picks the right card by `navigator.userAgent`) -
copy it to `backend/downloads/index.html` to serve it at
`http://<server>/index.html`. `DOWNLOAD_BASE` near the top of its
`<script>` controls every link on the page; edit it if the server address
changes. The zip is rebuilt with:
```
cd agents/python-agent/installer
python3 -c "
import zipfile, stat
files = ['Install RMM Agent (macOS).command', 'Uninstall RMM Agent (macOS).command']
with zipfile.ZipFile('output/RMM-Agent-macOS.zip', 'w', zipfile.ZIP_DEFLATED) as zf:
    for name in files:
        info = zipfile.ZipInfo(name)
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        zf.writestr(info, open(name, 'rb').read())
"
```
(the manual `external_attr` bit is what keeps the `.command` files
executable after unzip - a plain zip made by Explorer/most zip tools
loses the Unix executable bit, which would make double-click fail with
"not executable"). Copy the result to `backend/downloads/`, along with a
fresh copy of `agent.py`/`requirements.txt`/`install-macos.sh` any time
those change - the curl-one-liner and the `.command` files both
self-download from `backend/downloads/` when there's no local checkout
next to them.
