# Lane A needs (changes in files other lanes own)

## build_exe.py: hidden imports for the new modules
Add after the existing `--hidden-import` lines:
```python
        "--hidden-import", "openscan.logging_setup",
        "--hidden-import", "openscan.services",
        "--hidden-import", "openscan.mdns",
        "--hidden-import", "openscan.singleinstance",
        "--hidden-import", "openscan.autostart",
        "--hidden-import", "openscan.integrations",
        "--hidden-import", "openscan.integrations.homeassistant",
        "--hidden-import", "openscan.web.settings_api",
        "--hidden-import", "openscan.retry_queue",
```

## Dockerfile / docker-compose.yml (lane C)
The default bind is now 127.0.0.1, which is unreachable from outside a container. Set:
```dockerfile
ENV OPENSCAN_BIND=0.0.0.0
```
Every page and API call still needs the access token (or a sign-in on /login). Users find it with
`docker logs openscanhub` (startup line "Access token ...") or
`docker exec openscanhub openscan --show-token`. If users reach the container by a plain host name
(not an IP or a .local name), they add it with `OPENSCAN_ALLOWED_HOSTS=nas,nas.lan`.

## homeassistant-addon/ (lane C)
In the add-on's environment:
```
OPENSCAN_BIND=0.0.0.0
OPENSCAN_TRUST_PROXY_IP=172.30.32.2
```
172.30.32.2 is the ingress proxy; requests from it skip the token, Host and Origin checks because
Home Assistant authenticates the user. All UI URLs are relative and redirects honor `X-Ingress-Path`.
docs/home-assistant.md links to `../homeassistant-addon/` and says "see the README there".

## README.md
Replace the "Home Assistant Integration" section (it documents the removed `GET /api/scan/adf`) with:
```markdown
## Home Assistant (optional)

OpenScanHub can add scan buttons and a "scanner online" sensor to Home Assistant. Turn the module on
under Settings, Integrations, then follow [docs/home-assistant.md](docs/home-assistant.md).
```
Also worth saying near the top of the README: "OpenScanHub only accepts connections from this computer
until you turn on Settings, Access, Allow other devices. Other devices then sign in once with the access
token (tray menu: Show access token, or `openscan --show-token`)."

## Service modules (lanes B and C), used by lane A if present
- `openscan.scanner.ftp_receive.ftp_receiver_running() -> bool` and
  `openscan.scanner.folder_watch.folder_watcher_running() -> bool` feed the dashboard status dot for
  FTP and folder setups (missing functions show a gray "unknown" dot).
- `openscan.retry_queue.get_queue().start(config)` / `.stop()` are called by `openscan/services.py`.
- Optional (lane B, monitor): to show automatic feeder scans in the dashboard's Recent scans, call
  `from .web.dashboard_api import add_to_history` with
  `{"filename", "timestamp" (ISO), "size_bytes", "source": "Feeder", "auto": True, "ok", "queued", "saved_paths", "errors"}`
  (the last four from `dashboard_api.delivery_summary(result)`).

## Version
`openscan/__init__.py` still says 0.1.0; the startup log and /api/health report it. Bump to 0.3.0 with
pyproject.toml (I11 owner).
