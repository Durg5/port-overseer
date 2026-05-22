import os
import sys
import time
import threading
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, request, render_template_string, Response
import requests
from bs4 import BeautifulSoup

try:
    import yaml
    _has_yaml = True
except ImportError:
    _has_yaml = False

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ──────────────────────────────────────────────
#  Config
# ──────────────────────────────────────────────

DEFAULTS = {
    'title': 'PORT OVERSEER',
    'host': None,
    'port': 8765,
    'scan_interval': 30,
    'max_workers': 20,
    'show_api_services': False,
    'accent_color': '#f59e0b',
    'excluded_ports': [22, 25, 53, 80, 110, 143, 443, 3306, 5432, 6379, 27017, 5672, 4369],
    'port_range': {'min': 1025, 'max': 65534},
}


def _env(key, cast=None, default=None):
    v = os.environ.get(key)
    if v is None:
        return default
    return cast(v) if cast else v


def load_config():
    cfg = dict(DEFAULTS)
    cfg['port_range'] = dict(DEFAULTS['port_range'])
    cfg['excluded_ports'] = list(DEFAULTS['excluded_ports'])

    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yml')
    if os.path.exists(config_path):
        if _has_yaml:
            with open(config_path) as f:
                user = yaml.safe_load(f) or {}
            for k, v in user.items():
                if k in cfg and v is not None:
                    cfg[k] = v
        else:
            print('[port-overseer] WARNING: PyYAML not installed — config.yml ignored. '
                  'Install with: pip install pyyaml', file=sys.stderr)

    # Environment variable overrides (useful for Docker / systemd EnvironmentFile)
    cfg['title'] = _env('OVERSEER_TITLE') or cfg['title']
    cfg['host'] = _env('OVERSEER_HOST') or cfg['host']
    cfg['port'] = int(_env('OVERSEER_PORT', int) or cfg['port'])
    cfg['accent_color'] = _env('OVERSEER_ACCENT') or cfg['accent_color']
    cfg['scan_interval'] = int(_env('OVERSEER_SCAN_INTERVAL', int) or cfg['scan_interval'])
    cfg['show_api_services'] = _env('OVERSEER_SHOW_API', lambda v: v.lower() in ('1', 'true', 'yes'),
                                    default=cfg['show_api_services'])

    # CLI --port override
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg in ('--port', '-p') and i < len(sys.argv):
            try:
                cfg['port'] = int(sys.argv[i + 1])
            except (IndexError, ValueError):
                pass
        elif arg.startswith('--port='):
            try:
                cfg['port'] = int(arg.split('=', 1)[1])
            except ValueError:
                pass

    return cfg


CONFIG = load_config()

# Always exclude the port Port Overseer itself runs on
_EXCLUDED = set(CONFIG['excluded_ports']) | {CONFIG['port']}
_PORT_MIN = CONFIG['port_range']['min']
_PORT_MAX = CONFIG['port_range']['max']

# ──────────────────────────────────────────────
#  Flask app
# ──────────────────────────────────────────────

app = Flask(__name__)

_cache_lock = threading.Lock()
_services = []
_last_updated = 0
_scan_in_progress = False


# ──────────────────────────────────────────────
#  Port discovery
# ──────────────────────────────────────────────

def _read_tcp_file(path):
    ports = set()
    try:
        with open(path) as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 4 or parts[3] != '0A':
                    continue
                port_hex = parts[1].split(':')[-1]
                ports.add(int(port_hex, 16))
    except Exception:
        pass
    return ports


def get_listening_ports():
    ports = set()
    for path in ('/proc/net/tcp', '/proc/net/tcp6'):
        ports |= _read_tcp_file(path)
    return {p for p in ports if _PORT_MIN <= p <= _PORT_MAX and p not in _EXCLUDED}


# ──────────────────────────────────────────────
#  Service probing
# ──────────────────────────────────────────────

def _is_api(resp):
    ct = resp.headers.get('Content-Type', '')
    if 'application/json' in ct:
        return True
    non_ui = ('application/xml', 'text/xml', 'text/plain', 'application/octet-stream')
    if any(t in ct for t in non_ui):
        return True
    if 'text/html' not in ct:
        body = resp.text.strip()
        if body and body[0] in ('{', '['):
            try:
                import json; json.loads(body); return True
            except Exception:
                pass
    if 'text/html' in ct:
        body = resp.text.strip()
        if body and body[0] in ('{', '['):
            try:
                import json; json.loads(body); return True
            except Exception:
                pass
    body = resp.text
    if '<html' not in body.lower() and '<title' not in body.lower():
        return True
    return False


_UA = 'port-overseer/1.0'


def probe_service(port):
    for scheme in ('http', 'https'):
        try:
            url = f"{scheme}://127.0.0.1:{port}/"
            resp = requests.get(url, timeout=2, verify=False,
                                allow_redirects=True, headers={'User-Agent': _UA})
            if resp.status_code >= 500:
                continue

            is_api = _is_api(resp)

            if is_api:
                return {'port': port, 'title': f'Service :{port}',
                        'icon_path': None, 'scheme': scheme, 'is_api': True}

            soup = BeautifulSoup(resp.text, 'html.parser')
            title_tag = soup.find('title')
            title = (title_tag.get_text(strip=True) if title_tag else '') or f'Service :{port}'

            icon_path = None
            for rel in (['icon'], ['shortcut icon'], ['apple-touch-icon']):
                tag = soup.find('link', rel=rel)
                if tag and tag.get('href'):
                    href = tag['href']
                    if href.startswith('http://') or href.startswith('https://'):
                        from urllib.parse import urlparse
                        href = urlparse(href).path
                    icon_path = href
                    break

            if not icon_path:
                for try_path in ('/favicon.svg', '/favicon.ico'):
                    try:
                        head = requests.head(f"{scheme}://127.0.0.1:{port}{try_path}",
                                             timeout=2, verify=False,
                                             headers={'User-Agent': _UA})
                        if head.status_code == 200:
                            icon_path = try_path
                            break
                    except Exception:
                        pass

            return {'port': port, 'title': title[:80],
                    'icon_path': icon_path, 'scheme': scheme, 'is_api': False}

        except Exception:
            continue
    return None


# ──────────────────────────────────────────────
#  Background scanner
# ──────────────────────────────────────────────

def run_scan():
    global _services, _last_updated, _scan_in_progress
    with _cache_lock:
        _scan_in_progress = True
    try:
        ports = get_listening_ports()
        results = []
        with ThreadPoolExecutor(max_workers=CONFIG['max_workers']) as ex:
            futures = {ex.submit(probe_service, p): p for p in ports}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    results.append(result)
        results.sort(key=lambda s: s['port'])
        with _cache_lock:
            _services = results
            _last_updated = time.time()
    finally:
        with _cache_lock:
            _scan_in_progress = False


def _background_scanner():
    while True:
        try:
            run_scan()
        except Exception:
            pass
        time.sleep(CONFIG['scan_interval'])


_scanner_thread = threading.Thread(target=_background_scanner, daemon=True)
_scanner_thread.start()


# ──────────────────────────────────────────────
#  Routes
# ──────────────────────────────────────────────

@app.route('/api/services')
def api_services():
    show_api = CONFIG['show_api_services']
    with _cache_lock:
        ui = [s for s in _services if not s.get('is_api')]
        api = [s for s in _services if s.get('is_api')]
        visible = ui + api if show_api else ui
        return jsonify({
            'services': visible,
            'api_count': len(api),
            'api_shown': show_api,
            'last_updated': _last_updated,
            'scanning': _scan_in_progress,
        })


@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify({'status': 'refreshing'})


@app.route('/api/config')
def api_config():
    return jsonify({
        'title': CONFIG['title'],
        'host': CONFIG['host'],
        'scan_interval': CONFIG['scan_interval'],
        'show_api_services': CONFIG['show_api_services'],
        'accent_color': CONFIG['accent_color'],
        'port_range': CONFIG['port_range'],
        'excluded_ports': sorted(_EXCLUDED),
    })


@app.route('/icon')
def icon_proxy():
    port = request.args.get('port', '')
    path = request.args.get('path', '/favicon.ico')
    scheme = request.args.get('scheme', 'http')
    try:
        port = int(port)
        assert 1 <= port <= 65535
    except Exception:
        return Response('bad port', status=400)
    if not path.startswith('/'):
        path = '/' + path
    try:
        resp = requests.get(f"{scheme}://127.0.0.1:{port}{path}",
                            timeout=3, verify=False, headers={'User-Agent': _UA})
        ct = resp.headers.get('Content-Type', 'image/x-icon')
        return Response(resp.content, content_type=ct)
    except Exception:
        return Response('', status=404)


# ──────────────────────────────────────────────
#  HTML template
# ──────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap');

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg: #0a0a0a;
    --card: #111111;
    --accent: {{ accent }};
    --muted: #666666;
    --border: #1f1f1f;
    --font: "Berkeley Mono", "JetBrains Mono", "Courier New", monospace;
  }

  html, body { background: var(--bg); color: #e0e0e0; font-family: var(--font); min-height: 100vh; }

  header {
    border-bottom: 1px solid var(--border);
    padding: 18px 32px;
    display: flex; align-items: center; justify-content: space-between;
    position: sticky; top: 0; background: var(--bg); z-index: 10;
  }
  .header-left { display: flex; align-items: center; gap: 16px; }

  h1 { font-size: 1.5rem; font-weight: 700; letter-spacing: 0.15em; color: var(--accent); text-transform: uppercase; }

  .status-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--muted); display: inline-block; transition: background 0.3s;
  }
  .status-dot.scanning { background: var(--accent); animation: pulse 1s infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.2; } }

  .status-text { font-size: 0.72rem; color: var(--muted); letter-spacing: 0.05em; }

  .header-right { display: flex; align-items: center; gap: 10px; }

  .icon-btn {
    background: transparent; border: 1px solid var(--border);
    color: var(--muted); font-family: var(--font); font-size: 0.8rem;
    padding: 6px 10px; cursor: pointer; transition: border-color 0.15s, color 0.15s;
    line-height: 1;
  }
  .icon-btn:hover { border-color: var(--accent); color: var(--accent); }

  .rescan-btn {
    background: transparent; border: 1px solid var(--accent);
    color: var(--accent); font-family: var(--font); font-size: 0.75rem;
    letter-spacing: 0.12em; padding: 6px 14px; cursor: pointer;
    text-transform: uppercase; transition: background 0.15s, color 0.15s;
  }
  .rescan-btn:hover { background: var(--accent); color: #000; }
  .rescan-btn:disabled { opacity: 0.4; cursor: not-allowed; }

  main { padding: 32px; }

  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 12px;
  }

  .tile {
    background: var(--card); border: 1px solid var(--border);
    padding: 16px; text-decoration: none; color: inherit;
    display: flex; flex-direction: column; gap: 10px;
    transition: border-color 0.15s, filter 0.15s; min-height: 110px;
    position: relative;
  }
  .tile:hover { border-color: var(--accent); filter: brightness(1.15); }

  .tile-icon { width: 28px; height: 28px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
  .tile-icon img { width: 28px; height: 28px; object-fit: contain; }
  .tile-icon .monogram {
    width: 28px; height: 28px; background: #1a1a1a; border: 1px solid var(--border);
    display: flex; align-items: center; justify-content: center;
    font-size: 0.9rem; font-weight: 700; color: var(--accent); text-transform: uppercase;
  }

  .tile-title { font-size: 0.82rem; font-weight: 700; color: #d0d0d0; line-height: 1.3; word-break: break-word; flex: 1; }
  .tile-port { font-size: 0.72rem; color: var(--accent); letter-spacing: 0.08em; }

  .api-badge {
    position: absolute; top: 8px; right: 8px;
    font-size: 0.58rem; letter-spacing: 0.1em;
    color: var(--muted); border: 1px solid var(--border); padding: 1px 4px;
  }

  .empty-state {
    display: flex; align-items: center; justify-content: center;
    height: 40vh; font-size: 1.4rem; letter-spacing: 0.2em;
    color: var(--accent); animation: pulse 1.2s infinite;
  }

  /* ── Config panel ── */
  .overlay {
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,0.7); z-index: 100; align-items: center; justify-content: center;
  }
  .overlay.open { display: flex; }

  .panel {
    background: #111; border: 1px solid var(--border);
    width: min(520px, 94vw); max-height: 80vh; overflow-y: auto;
    padding: 28px 32px; display: flex; flex-direction: column; gap: 18px;
  }

  .panel h2 { font-size: 1rem; letter-spacing: 0.15em; color: var(--accent); text-transform: uppercase; }

  .cfg-row { display: flex; flex-direction: column; gap: 4px; }
  .cfg-label { font-size: 0.68rem; letter-spacing: 0.1em; color: var(--muted); text-transform: uppercase; }
  .cfg-val { font-size: 0.82rem; color: #d0d0d0; }
  .cfg-val code { color: var(--accent); font-family: var(--font); font-size: 0.8rem; }

  .panel-close {
    align-self: flex-end; background: transparent; border: 1px solid var(--border);
    color: var(--muted); font-family: var(--font); font-size: 0.75rem;
    padding: 5px 12px; cursor: pointer; letter-spacing: 0.1em; text-transform: uppercase;
  }
  .panel-close:hover { border-color: var(--accent); color: var(--accent); }

  .divider { border: none; border-top: 1px solid var(--border); margin: 2px 0; }
</style>
</head>
<body>

<header>
  <div class="header-left">
    <h1>{{ title }}</h1>
    <span class="status-dot" id="dot"></span>
    <span class="status-text" id="statusText">loading...</span>
  </div>
  <div class="header-right">
    <button class="icon-btn" title="Settings" onclick="document.getElementById('cfgOverlay').classList.add('open')">&#9881;</button>
    <button class="rescan-btn" id="rescanBtn" onclick="rescan()">&#x21BB; RESCAN</button>
  </div>
</header>

<main>
  <div id="content"></div>
</main>

<!-- Config panel -->
<div class="overlay" id="cfgOverlay" onclick="if(event.target===this)this.classList.remove('open')">
  <div class="panel">
    <h2>Configuration</h2>
    <hr class="divider">
    <div id="cfgBody"></div>
    <hr class="divider">
    <button class="panel-close" onclick="document.getElementById('cfgOverlay').classList.remove('open')">Close</button>
  </div>
</div>

<script>
const STATIC_HOST = {{ host_js }};

function resolvedHost() {
  return STATIC_HOST || window.location.hostname;
}

function fmt(ts) {
  if (!ts) return 'never';
  return new Date(ts * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
}

function iconSrc(svc) {
  if (!svc.icon_path) return null;
  return `/icon?port=${svc.port}&path=${encodeURIComponent(svc.icon_path)}&scheme=${svc.scheme}`;
}

function escHtml(s) {
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function renderTiles(services) {
  const content = document.getElementById('content');
  if (!services || services.length === 0) {
    content.innerHTML = '<div class="empty-state">SCANNING...</div>';
    return;
  }
  const host = resolvedHost();
  const tiles = services.map((svc, i) => {
    const href = `${svc.scheme}://${host}:${svc.port}`;
    const src = iconSrc(svc);
    const letter = (svc.title || '?')[0].toUpperCase();
    const iconHtml = src
      ? `<img class="svc-icon" src="${src}" alt="" data-letter="${letter}" data-idx="${i}">`
      : `<div class="monogram">${letter}</div>`;
    const apiBadge = svc.is_api ? '<span class="api-badge">API</span>' : '';
    return `<a class="tile" href="${href}" target="_blank" rel="noopener">
      ${apiBadge}
      <div class="tile-icon">${iconHtml}</div>
      <div class="tile-title">${escHtml(svc.title)}</div>
      <div class="tile-port">:${svc.port}</div>
    </a>`;
  }).join('');
  content.innerHTML = `<div class="grid">${tiles}</div>`;
  content.querySelectorAll('img.svc-icon').forEach(img => {
    img.addEventListener('error', function() {
      this.parentElement.innerHTML = '<div class="monogram">' + (this.dataset.letter||'?') + '</div>';
    }, {once: true});
  });
}

async function poll() {
  try {
    const r = await fetch('/api/services');
    const data = await r.json();
    const dot = document.getElementById('dot');
    const statusText = document.getElementById('statusText');
    const btn = document.getElementById('rescanBtn');

    dot.className = 'status-dot' + (data.scanning ? ' scanning' : '');
    const apiNote = (!data.api_shown && data.api_count)
      ? ` · ${data.api_count} api hidden`
      : (data.api_shown && data.api_count ? ` · ${data.api_count} api` : '');
    statusText.textContent = `${data.services.length} service${data.services.length !== 1 ? 's' : ''}${apiNote} — updated ${fmt(data.last_updated)}`;
    btn.disabled = data.scanning;
    renderTiles(data.services);
  } catch(e) {
    document.getElementById('statusText').textContent = 'error fetching data';
  }
}

async function rescan() {
  document.getElementById('rescanBtn').disabled = true;
  await fetch('/api/refresh', {method: 'POST'});
  poll();
}

async function loadConfig() {
  try {
    const r = await fetch('/api/config');
    const cfg = await r.json();
    const rows = [
      ['Title', `<code>${escHtml(cfg.title)}</code>`],
      ['Host override', cfg.host ? `<code>${escHtml(cfg.host)}</code>` : '<span style="color:var(--muted)">auto (from browser)</span>'],
      ['Accent color', `<code>${escHtml(cfg.accent_color)}</code> <span style="display:inline-block;width:10px;height:10px;background:${escHtml(cfg.accent_color)};border:1px solid #333;vertical-align:middle;margin-left:4px"></span>`],
      ['Scan interval', `<code>${cfg.scan_interval}s</code>`],
      ['Show API services', `<code>${cfg.show_api_services}</code>`],
      ['Port range', `<code>${cfg.port_range.min} – ${cfg.port_range.max}</code>`],
      ['Excluded ports', `<code>${cfg.excluded_ports.join(', ')}</code>`],
    ];
    document.getElementById('cfgBody').innerHTML = rows.map(([label, val]) =>
      `<div class="cfg-row"><span class="cfg-label">${escHtml(label)}</span><span class="cfg-val">${val}</span></div>`
    ).join('');
  } catch(e) {}
}

poll();
loadConfig();
setInterval(poll, {{ scan_interval }} * 1000);
</script>
</body>
</html>
"""


@app.route('/')
def index():
    host_js = f'"{CONFIG["host"]}"' if CONFIG.get('host') else 'null'
    return render_template_string(
        HTML,
        title=CONFIG['title'],
        accent=CONFIG['accent_color'],
        host_js=host_js,
        scan_interval=CONFIG['scan_interval'],
    )


# ──────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────

if __name__ == '__main__':
    port = CONFIG['port']
    print(f'[port-overseer] starting on http://0.0.0.0:{port}')
    print(f'[port-overseer] title: {CONFIG["title"]}')
    print(f'[port-overseer] scan interval: {CONFIG["scan_interval"]}s')
    app.run(host='0.0.0.0', port=port, debug=False)
