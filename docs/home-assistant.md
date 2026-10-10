# Home Assistant (optional)

OpenScanHub works on its own. You don't need Home Assistant. If you use it, this
optional module adds scan buttons and a "scanner online" sensor to your dashboards.

## 1. Turn the module on

1. Open OpenScanHub, click the gear, and go to **Integrations**.
2. Turn on **Home Assistant** and click **Save**.
3. If Home Assistant runs on another machine, go to **Access** and turn on
   **Allow other devices on my network to use OpenScanHub**, then restart OpenScanHub.
4. In **Access**, click **Show** next to the access token and copy it. Home Assistant
   sends it with every request. With the Docker image you can also run
   `docker exec openscan-hub openscan --show-token`.

While the module is off, the routes below return 404.

## 2. Store the token in Home Assistant

Add the token to `secrets.yaml`:

```yaml
openscan_token: "paste-the-access-token-here"
```

## 3. Scan buttons (`rest_command`)

In `configuration.yaml` (replace `OPENSCAN_IP` with the address of the computer that
runs OpenScanHub):

```yaml
rest_command:
  openscan_scan_feeder:
    url: "http://OPENSCAN_IP:8020/api/ha/scan?source=adf"
    method: POST
    headers:
      X-OpenScan-Token: !secret openscan_token
  openscan_scan_flatbed:
    url: "http://OPENSCAN_IP:8020/api/ha/scan?source=flatbed"
    method: POST
    headers:
      X-OpenScan-Token: !secret openscan_token
```

Restart Home Assistant, then add a button card that calls
`rest_command.openscan_scan_feeder` or `rest_command.openscan_scan_flatbed`.

The scan starts right away and the request returns at once:

| Answer | Meaning |
|--------|---------|
| 202 `{"started": true}` | The scan started. The file appears in your scans folder. |
| 409 `{"error": "Scanner is busy"}` | Another scan is running. Try again shortly. |
| 401 | The token is missing or wrong. |
| 404 | The Home Assistant module is turned off in Settings. |

`GET` works too, and so does `?token=...` in the URL if a header is not possible:
`http://OPENSCAN_IP:8020/api/ha/scan?source=adf&token=YOUR_TOKEN`. Prefer the header,
because URLs can end up in logs.

## 4. "Scanner online" sensor

```yaml
rest:
  - resource: "http://OPENSCAN_IP:8020/api/ha/health"
    headers:
      X-OpenScan-Token: !secret openscan_token
    scan_interval: 60
    binary_sensor:
      - name: "Scanner online"
        device_class: connectivity
        value_template: "{{ value_json.scanner_online }}"
    sensor:
      - name: "Scans waiting to retry"
        value_template: "{{ value_json.queue_pending }}"
```

`/api/ha/health` returns `status`, `version`, `scanner_online`, `scan_in_progress` and
`queue_pending`. The general `/api/health` endpoint (no token needed) returns the same
basics plus `uptime_seconds`, if you only want to know that OpenScanHub is up.

## 5. Home Assistant add-on

If you run Home Assistant OS, you can install OpenScanHub as an add-on instead of
running it on another computer. Add this repository's URL as an add-on repository
(the [`repository.yaml`](../repository.yaml) at the top of the repository is what
Home Assistant looks for). The add-on files are in
[`homeassistant-addon/openscanhub/`](../homeassistant-addon/openscanhub/), and its
[DOCS.md](../homeassistant-addon/openscanhub/DOCS.md) has the steps. The add-on opens the OpenScanHub page inside Home Assistant
(ingress), where Home Assistant handles sign-in, so you don't need the token for the
page itself. The `rest_command` examples above still need the token.

## Changing the token

**Settings, Access, Regenerate** makes a new token. Update `openscan_token` in
`secrets.yaml` and restart Home Assistant afterwards.
