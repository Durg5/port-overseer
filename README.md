# Port Overseer

A minimal, self-hosted dashboard that automatically discovers every web service running on your machine and gives you a single-page launch pad to reach them.

If you run a lot of local apps on different ports — media servers, AI tools, dev servers, dashboards, APIs — Port Overseer scans your listening TCP ports, probes each one for an HTTP response, extracts the page title and favicon, and renders a clean clickable tile for each UI it finds.

![Port Overseer screenshot placeholder](https://placehold.co/900x400/0a0a0a/f59e0b?text=PORT+OVERSEER)

---

## Features

- **Zero config required** — works out of the box; auto-detects all listening web services
- **Live scan** — background scanner re-probes every 30 seconds (configurable); manual rescan button
- **Smart filtering** — automatically hides raw API / JSON endpoints from the tile grid (configurable)
- **Favicon fetching** — pulls the real icon from each service so you can identify apps at a glance
- **Monogram fallback** — when no favicon exists, displays the first letter of the service title
- **Settings panel** — click the ⚙ gear button to inspect the active configuration at runtime
- **Fully configurable** — `config.yml` controls title, accent color, hostname, scan interval, port range, excluded ports, and more
- **Environment variable overrides** — every key config value can be set via env vars (great for Docker / systemd)
- **Single Python file** — the entire backend is `app.py`; easy to read, fork, or embed

---

## Requirements

- Python 3.8+
- Linux (uses `/proc/net/tcp` for port discovery — does not work on macOS/Windows without modification)
- Root or `CAP_NET_BIND_SERVICE` if you want to bind to port 80

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/your-username/port-overseer.git
cd port-overseer
```

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure (optional)

Copy the example config and edit it:

```bash
cp config.example.yml config.yml
nano config.yml
```

See [Configuration](#configuration) below for all options.

### 4. Run

```bash
python app.py
```

Port Overseer starts on **port 8765** by default. Open your browser to:

```
http://localhost:8765
```

To use a different port:

```bash
python app.py --port 9000
# or
OVERSEER_PORT=9000 python app.py
```

---

## Run as a System Service (systemd)

To keep Port Overseer running across reboots:

### 1. Copy files to a permanent location

```bash
sudo cp -r . /opt/port-overseer
sudo python3 -m venv /opt/port-overseer/venv
sudo /opt/port-overseer/venv/bin/pip install -r /opt/port-overseer/requirements.txt
```

### 2. Install the service

```bash
sudo cp port-overseer.service /etc/systemd/system/port-overseer.service
# Edit WorkingDirectory and ExecStart if you used a different path:
sudo nano /etc/systemd/system/port-overseer.service
```

### 3. Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now port-overseer
```

### 4. Check status

```bash
sudo systemctl status port-overseer
sudo journalctl -u port-overseer -f
```

---

## Run on Port 80 (without root)

### Option A — `authbind`

```bash
sudo apt install authbind
sudo touch /etc/authbind/byport/80
sudo chmod 500 /etc/authbind/byport/80
sudo chown YOUR_USER /etc/authbind/byport/80

# Then run:
authbind --deep python app.py --port 80
```

### Option B — nginx reverse proxy

Run Port Overseer on 8765 and proxy it from port 80:

```nginx
server {
    listen 80;
    server_name overseer.local;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## Run with Docker

```bash
docker run -d \
  --name port-overseer \
  --network host \
  -v $(pwd)/config.yml:/app/config.yml:ro \
  -e OVERSEER_PORT=8765 \
  your-username/port-overseer
```

> **Note:** `--network host` is required so Port Overseer can read `/proc/net/tcp` and probe `127.0.0.1`.

A minimal `Dockerfile` for building your own image:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py config.example.yml ./
EXPOSE 8765
CMD ["python", "app.py"]
```

---

## Configuration

All settings live in `config.yml` (copy from `config.example.yml`). Every setting also has an environment variable override for Docker/systemd use.

| `config.yml` key | Env var | Default | Description |
|---|---|---|---|
| `title` | `OVERSEER_TITLE` | `PORT OVERSEER` | Text shown in the dashboard header |
| `host` | `OVERSEER_HOST` | `null` | Hostname used in service links. `null` = auto-detect from browser |
| `port` | `OVERSEER_PORT` | `8765` | Port Port Overseer listens on |
| `accent_color` | `OVERSEER_ACCENT` | `#f59e0b` | Header/accent color (any CSS color) |
| `scan_interval` | `OVERSEER_SCAN_INTERVAL` | `30` | Seconds between background rescans |
| `max_workers` | — | `20` | Parallel threads used when probing ports |
| `show_api_services` | `OVERSEER_SHOW_API` | `false` | Show raw API/JSON endpoints as tiles |
| `port_range.min` | — | `1025` | Lowest port number to scan |
| `port_range.max` | — | `65534` | Highest port number to scan |
| `excluded_ports` | — | `[22, 25, 53, ...]` | Ports to never probe (list in config.yml) |

### Example config.yml

```yaml
title: "MY HOMELAB"
host: "homelab.local"        # Fixed hostname for service links
accent_color: "#6366f1"      # Indigo accent
scan_interval: 60            # Rescan every minute
show_api_services: true      # Also show JSON API endpoints
port_range:
  min: 1025
  max: 65534
excluded_ports:
  - 22
  - 3306
  - 5432
  - 6379
```

### The `host` setting

By default (`host: null`) Port Overseer uses `window.location.hostname` — whatever hostname your browser used to reach the dashboard — when building the clickable tile links. This means:

- If you open `http://myserver.local:8765`, tiles link to `http://myserver.local:PORT`
- If you open `http://192.168.1.10:8765`, tiles link to `http://192.168.1.10:PORT`

Set `host` explicitly only if you access the dashboard via a different hostname than your services (e.g., through a reverse proxy or SSH tunnel).

---

## API Endpoints

Port Overseer exposes a small REST API you can query directly:

| Endpoint | Method | Description |
|---|---|---|
| `GET /api/services` | GET | Returns all discovered services as JSON |
| `POST /api/refresh` | POST | Triggers an immediate rescan (non-blocking) |
| `GET /api/config` | GET | Returns the active configuration |
| `GET /icon?port=N&path=/favicon.ico&scheme=http` | GET | Proxies a service favicon through Port Overseer |

### Example `/api/services` response

```json
{
  "services": [
    {
      "port": 3000,
      "title": "Grafana",
      "icon_path": "/public/img/grafana_icon.svg",
      "scheme": "http",
      "is_api": false
    }
  ],
  "api_count": 4,
  "api_shown": false,
  "last_updated": 1716400000.0,
  "scanning": false
}
```

---

## How It Works

1. **Port discovery** — reads `/proc/net/tcp` and `/proc/net/tcp6` to find all TCP ports in state `LISTEN` within the configured range, excluding system/infra ports.
2. **Parallel probing** — each port is probed via HTTP then HTTPS (with a 2-second timeout). The first successful response wins.
3. **UI vs API detection** — responses with `application/json` content type, no `<html>` tag, or a body that parses as valid JSON are classified as API endpoints and hidden from the tile grid (unless `show_api_services: true`).
4. **Title & icon extraction** — BeautifulSoup parses the HTML `<title>` and `<link rel="icon">` tags. Falls back to probing `/favicon.svg` and `/favicon.ico` directly.
5. **Caching** — results are stored in memory and served instantly to the browser. A background thread rescans on the configured interval. The ↻ RESCAN button triggers an immediate out-of-cycle scan.

---

## Limitations

- **Linux only** (port discovery reads `/proc/net/tcp`). macOS/BSD support would require `lsof` or `netstat` integration.
- **Local services only** — Port Overseer probes `127.0.0.1`, so it only discovers services running on the same machine.
- **No authentication** — Port Overseer itself has no login. Run it on a trusted LAN or behind a reverse proxy with auth if needed.
- **No persistence** — the discovered service list is in-memory and rebuilt on each scan/restart.

---

## License

MIT — do whatever you want with it.
