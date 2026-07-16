#!/usr/bin/env python3
"""web_ui.py — browser config UI + rclone mount supervisor for Debrid Mount.

Single stdlib-only file (no pip deps). Runs as the container's foreground
process on :8080 (what the Umbrel app_proxy hits). It:
  - serves the dashboard SPA + JSON API
  - reads/writes debrid.env (shell KEY='VALUE' format, same as before)
  - validates TorBox WebDAV creds via `rclone lsd` before mounting
  - manages the rclone mount as a child subprocess (mount/unmount/restart)

Replaces the old `rclone serve http /status` + exec-mount pattern: the UI
stays up forever (so app_proxy always has a server), and the mount is a
child it controls.
"""
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG_DIR = os.environ.get("DEBRID_CONFIG_DIR", "/config")
STATUS_DIR = os.environ.get("DEBRID_STATUS_DIR", "/status")
MOUNTPOINT = os.environ.get("DEBRID_MOUNTPOINT", "/downloads/.vortexo-source")
HOST_MOUNT_PATH = os.environ.get("DEBRID_HOST_MOUNT_PATH", "/home/Downloads/.vortexo-source")
CONFIG_FILE = os.path.join(CONFIG_DIR, "debrid.env")
RCLONE_CONFIG = os.path.join(CONFIG_DIR, "rclone.conf")
ZURG_CONFIG = os.path.join(CONFIG_DIR, "zurg.yml")
RCLONE_LOG = os.path.join(STATUS_DIR, "rclone.log")
LISTEN_PORT = int(os.environ.get("DEBRID_WEB_PORT", "8080"))

# Config keys we manage, with friendly metadata for the UI.
# (env_key, label, control, default, help)
CONFIG_SCHEMA = [
    ("DEBRID_MODE", "Debrid Mode", "select", "webdav",
     {"options": ["webdav", "zurg"],
      "help": "webdav = TorBox direct (recommended). zurg = Real-Debrid via zurg."}),
    # TorBox WebDAV
    ("DEBRID_WEBDAV_URL", "WebDAV URL", "text", "https://webdav.torbox.app",
     {"show_if": {"DEBRID_MODE": "webdav"}, "help": "TorBox's WebDAV endpoint."}),
    ("DEBRID_WEBDAV_VENDOR", "WebDAV Vendor", "text", "other",
     {"show_if": {"DEBRID_MODE": "webdav"}, "help": "rclone vendor: 'other' for TorBox."}),
    ("DEBRID_WEBDAV_USER", "TorBox WebDAV Username", "text", "",
     {"show_if": {"DEBRID_MODE": "webdav"},
      "help": "From TorBox → Settings → WebDAV. NOT your login or API key."}),
    ("DEBRID_WEBDAV_PASS", "TorBox WebDAV Password", "password", "",
     {"show_if": {"DEBRID_MODE": "webdav"},
      "help": "From TorBox → Settings → WebDAV. Dedicated WebDAV password."}),
    # Real-Debrid via zurg
    ("DEBRID_ZURG_TOKEN", "Real-Debrid API Token", "password", "",
     {"show_if": {"DEBRID_MODE": "zurg"},
      "help": "real-debrid.com → Account → Get my API token."}),
    ("DEBRID_ZURG_PORT", "Zurg Port", "text", "9999",
     {"show_if": {"DEBRID_MODE": "zurg"}}),
    # Rclone tuning
    ("DEBRID_RCLONE_VFS_CACHE_MODE", "VFS Cache Mode", "select", "full",
     {"options": ["off", "minimal", "writes", "full"], "help": "full = best for streaming."}),
    ("DEBRID_RCLONE_VFS_CACHE_MAX_SIZE", "VFS Cache Max Size", "text", "20G",
     {"help": "e.g. 20G. Disk space used for cached chunks."}),
    ("DEBRID_RCLONE_VFS_CACHE_MAX_AGE", "VFS Cache Max Age", "text", "6h",
     {"help": "e.g. 6h. How long cached chunks live."}),
    ("DEBRID_RCLONE_DIR_CACHE_TIME", "Dir Cache Time", "text", "10s",
     {"help": "e.g. 10s. How long directory listings are cached."}),
    ("DEBRID_RCLONE_LOG_LEVEL", "Log Level", "select", "INFO",
     {"options": ["ERROR", "NOTICE", "INFO", "DEBUG"]}),
]

# Sample config written on first run.
SAMPLE_CONFIG = """\
# Debrid Mount config
#
# Default mode is direct WebDAV, which works for TorBox:
DEBRID_MODE='webdav'
DEBRID_WEBDAV_URL='https://webdav.torbox.app'
DEBRID_WEBDAV_VENDOR='other'
DEBRID_WEBDAV_USER=''
DEBRID_WEBDAV_PASS=''
#
# Real-Debrid via zurg is also supported:
# DEBRID_MODE='zurg'
# DEBRID_ZURG_TOKEN=''
# DEBRID_ZURG_PORT='9999'
#
# Rclone tuning:
DEBRID_RCLONE_VFS_CACHE_MODE='full'
DEBRID_RCLONE_VFS_CACHE_MAX_SIZE='20G'
DEBRID_RCLONE_VFS_CACHE_MAX_AGE='6h'
DEBRID_RCLONE_DIR_CACHE_TIME='10s'
DEBRID_RCLONE_LOG_LEVEL='INFO'
"""


# --------------------------------------------------------------------------
# debrid.env read/write (shell KEY='VALUE' format)
# --------------------------------------------------------------------------
_lock = threading.Lock()


def write_sample_config():
    """Seed debrid.env on first run so the UI has defaults to show."""
    if os.path.isfile(CONFIG_FILE):
        return
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as fh:
        fh.write(SAMPLE_CONFIG)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass


def read_config():
    """Parse debrid.env into a {KEY: value} dict. Strips surrounding quotes."""
    cfg = {}
    if not os.path.isfile(CONFIG_FILE):
        return cfg
    with open(CONFIG_FILE, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            # Strip surrounding single or double quotes.
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            cfg[key] = val
    return cfg


def write_config(updates):
    """Merge updates into debrid.env, preserving the shell KEY='VALUE' format."""
    with _lock:
        cfg = read_config()
        cfg.update(updates)
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as fh:
            fh.write("# Debrid Mount config (managed by the Web UI)\n")
            for key, label, control, default, meta in CONFIG_SCHEMA:
                val = cfg.get(key, default)
                fh.write("{}='{}'\n".format(key, val))
        os.replace(tmp, CONFIG_FILE)
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except OSError:
            pass


def config_for_ui():
    """Return the schema + current values, grouped for the UI."""
    cfg = read_config()
    fields = []
    for key, label, control, default, meta in CONFIG_SCHEMA:
        fields.append({
            "key": key, "label": label, "control": control,
            "value": cfg.get(key, default),
            "options": meta.get("options"),
            "show_if": meta.get("show_if"),
            "help": meta.get("help"),
        })
    return {"fields": fields}


# --------------------------------------------------------------------------
# rclone config generation (moved here from mount.sh)
# --------------------------------------------------------------------------
def write_rclone_config():
    """Generate rclone.conf from debrid.env. Returns True on success."""
    cfg = read_config()
    mode = cfg.get("DEBRID_MODE", "webdav").lower()
    if mode == "zurg":
        port = cfg.get("DEBRID_ZURG_PORT", "9999")
        body = "[debrid]\ntype = webdav\nurl = http://127.0.0.1:{}/dav\nvendor = other\n".format(port)
    else:
        user = cfg.get("DEBRID_WEBDAV_USER", "")
        password = cfg.get("DEBRID_WEBDAV_PASS", "")
        if not user or not password:
            return False
        # rclone stores passwords obscured (not encrypted — rclone's own scheme).
        obscured = _rclone_obscure(password)
        url = cfg.get("DEBRID_WEBDAV_URL", "https://webdav.torbox.app")
        vendor = cfg.get("DEBRID_WEBDAV_VENDOR", "other")
        body = ("[debrid]\ntype = webdav\nurl = {}\nvendor = {}\n"
                "user = {}\npass = {}\n".format(url, vendor, user, obscured))
    with _lock:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(RCLONE_CONFIG, "w") as fh:
            fh.write(body)
        try:
            os.chmod(RCLONE_CONFIG, 0o600)
        except OSError:
            pass
    return True


def _rclone_obscure(password):
    """Run `rclone obscure <password>` to get rclone's obfuscated form."""
    try:
        out = subprocess.run(["rclone", "obscure", password],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return password  # fallback: store plaintext (rclone accepts it with a warning)


def write_zurg_config():
    """Generate zurg.yml for Real-Debrid mode."""
    cfg = read_config()
    token = cfg.get("DEBRID_ZURG_TOKEN", "")
    if not token:
        return False
    port = cfg.get("DEBRID_ZURG_PORT", "9999")
    body = (
        "zurg: v1\n"
        "token: {}\n"
        "host: \"0.0.0.0\"\n"
        "port: {}\n"
        "check_for_changes_every_secs: 10\n"
        "enable_repair: true\n"
        "auto_delete_rar_torrents: true\n"
        "directories:\n"
        "  shows:\n"
        "    group_order: 20\n"
        "    group: media\n"
        "    filters:\n"
        "      - has_episodes: true\n"
        "  movies:\n"
        "    group_order: 30\n"
        "    group: media\n"
        "    only_show_the_biggest_file: true\n"
        "    filters:\n"
        "      - regex: /.*/\n"
    ).format(token, port)
    with _lock:
        with open(ZURG_CONFIG, "w") as fh:
            fh.write(body)
        try:
            os.chmod(ZURG_CONFIG, 0o600)
        except OSError:
            pass
    return True


# --------------------------------------------------------------------------
# rclone mount subprocess supervisor
# --------------------------------------------------------------------------
class Mount:
    def __init__(self):
        self.proc = None
        self.zurg_proc = None
        self._lock = threading.Lock()

    @property
    def is_mounted(self):
        # Check /proc/self/mountinfo for the mountpoint.
        try:
            with open("/proc/self/mountinfo") as fh:
                return (" " + MOUNTPOINT + " ") in fh.read()
        except OSError:
            return False

    def status(self):
        return {"mounted": self.is_mounted, "mode": read_config().get("DEBRID_MODE", "webdav")}

    def _rclone_args(self):
        cfg = read_config()
        return [
            "rclone", "mount", "debrid:", MOUNTPOINT,
            "--config", RCLONE_CONFIG,
            "--allow-other",
            "--allow-non-empty",
            "--read-only",
            "--dir-cache-time", cfg.get("DEBRID_RCLONE_DIR_CACHE_TIME", "10s"),
            "--vfs-cache-mode", cfg.get("DEBRID_RCLONE_VFS_CACHE_MODE", "full"),
            "--vfs-cache-max-size", cfg.get("DEBRID_RCLONE_VFS_CACHE_MAX_SIZE", "20G"),
            "--vfs-cache-max-age", cfg.get("DEBRID_RCLONE_VFS_CACHE_MAX_AGE", "6h"),
            "--cache-dir", os.path.join(CONFIG_DIR, "rclone-cache"),
            "--poll-interval", "0",
            "--umask", "002",
            "--uid", "1000",
            "--gid", "1000",
            "--log-level", cfg.get("DEBRID_RCLONE_LOG_LEVEL", "INFO"),
        ]

    def _prepare(self):
        """FUSE prep (moved from mount.sh): allow_other, clean stale mount, mkdir."""
        # enable user_allow_other in fuse.conf
        fuse_conf = "/etc/fuse.conf"
        if os.path.isfile(fuse_conf) and os.access(fuse_conf, os.W_OK):
            with open(fuse_conf) as fh:
                existing = fh.read()
            if "user_allow_other" not in existing:
                with open(fuse_conf, "a") as fh:
                    fh.write("user_allow_other\n")
        # unmount stale
        if self.is_mounted:
            subprocess.run(["fusermount3", "-uz", MOUNTPOINT], timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["fusermount", "-uz", MOUNTPOINT], timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.makedirs(MOUNTPOINT, exist_ok=True)
        os.makedirs(os.path.join(CONFIG_DIR, "rclone-cache"), exist_ok=True)

    def mount(self):
        with self._lock:
            if self.is_mounted:
                return False, "already mounted"
            cfg = read_config()
            mode = cfg.get("DEBRID_MODE", "webdav").lower()
            # Generate configs.
            if mode == "zurg":
                if not write_zurg_config():
                    return False, "missing Real-Debrid token"
            else:
                if not write_rclone_config():
                    return False, "missing TorBox WebDAV username/password"
            self._prepare()
            # Start zurg first if needed.
            if mode == "zurg":
                self.zurg_proc = subprocess.Popen(
                    ["zurg", "-c", ZURG_CONFIG],
                    stdout=open(RCLONE_LOG, "ab"), stderr=subprocess.STDOUT)
                # wait for zurg to be ready
                if not _wait_for_zurg(cfg.get("DEBRID_ZURG_PORT", "9999")):
                    return False, "zurg did not become ready"
                write_rclone_config()  # now writes the localhost:dav url
            log_fh = open(RCLONE_LOG, "ab")
            self.proc = subprocess.Popen(self._rclone_args(), stdout=log_fh,
                                         stderr=subprocess.STDOUT)
            # Give it a moment to either mount or crash.
            time.sleep(3)
            if self.proc.poll() is not None:
                return False, "rclone exited immediately — check creds / log"
            return True, "mounting"

    def unmount(self):
        with self._lock:
            msg_parts = []
            if self.is_mounted:
                subprocess.run(["fusermount3", "-uz", MOUNTPOINT], timeout=10,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["fusermount", "-uz", MOUNTPOINT], timeout=10,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                msg_parts.append("unmounted")
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                msg_parts.append("rclone stopped")
            self.proc = None
            if self.zurg_proc and self.zurg_proc.poll() is None:
                self.zurg_proc.terminate()
                self.zurg_proc = None
                msg_parts.append("zurg stopped")
            return True, ", ".join(msg_parts) if msg_parts else "not running"

    def restart(self):
        self.unmount()
        time.sleep(1)
        return self.mount()


def _wait_for_zurg(port, timeout=60):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen("http://127.0.0.1:{}/dav/version.txt".format(port), timeout=2)
            return True
        except Exception:
            time.sleep(1)
    return False


mount = Mount()


# --------------------------------------------------------------------------
# Credential test (rclone lsd)
# --------------------------------------------------------------------------
def test_webdav(user, password, url=None, vendor="other"):
    """Validate WebDAV creds by running `rclone lsd` against a temp config."""
    if not user or not password:
        return {"valid": False, "error": "username and password required"}
    obscured = _rclone_obscure(password)
    url = url or "https://webdav.torbox.app"
    tmp_conf = os.path.join(CONFIG_DIR, "test-rclone.conf")
    with open(tmp_conf, "w") as fh:
        fh.write("[debrid]\ntype = webdav\nurl = {}\nvendor = {}\nuser = {}\npass = {}\n".format(
            url, vendor, user, obscured))
    try:
        result = subprocess.run(
            ["rclone", "lsd", "debrid:/", "--config", tmp_conf, "--timeout", "30s"],
            capture_output=True, text=True, timeout=45)
        if result.returncode == 0:
            dirs = [l for l in result.stdout.strip().split("\n") if l.strip()]
            return {"valid": True, "dirs": len(dirs), "sample": dirs[:5]}
        return {"valid": False, "error": _clean_rclone_error(result.stderr)}
    except subprocess.TimeoutExpired:
        return {"valid": False, "error": "timed out connecting to TorBox WebDAV"}
    except Exception as e:
        return {"valid": False, "error": str(e)}
    finally:
        try:
            os.remove(tmp_conf)
        except OSError:
            pass


def _clean_rclone_error(stderr):
    """Extract the useful line from rclone's verbose error output."""
    for line in stderr.split("\n"):
        line = line.strip()
        if line and ("error" in line.lower() or "failed" in line.lower()
                     or "401" in line or "403" in line or "denied" in line.lower()):
            return line[:200]
    return (stderr.strip().split("\n")[-1] if stderr.strip() else "unknown error")[:200]


def tail(path, lines=100):
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as fh:
            return b"".join(fh.readlines()[-lines:]).decode("utf-8", errors="replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------
PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Debrid Mount</title>
<style>
:root{--bg:#020617;--card:rgba(15,23,42,.72);--border:rgba(255,255,255,.16);--text:#eef2ff;--muted:#94a3b8;--accent:#0ea5e9;--good:#84cc16;--bad:#ef4444}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;font:15px/1.5 ui-sans-serif,system-ui,sans-serif;color:var(--text);background:radial-gradient(circle at top left,#164e63,#020617 52%,#111827);padding-bottom:90px}
header{display:flex;align-items:center;justify-content:space-between;max-width:760px;margin:0 auto;padding:28px 22px 0}
.brand h1{margin:0;font-size:26px;letter-spacing:-.03em}.brand .sub{color:var(--muted);font-size:13px}
main{max-width:760px;margin:0 auto;padding:22px;display:grid;gap:18px}
.card{border:1px solid var(--border);border-radius:18px;background:var(--card);padding:22px;box-shadow:0 18px 50px rgba(0,0,0,.28)}
.card h2{margin:0 0 10px;font-size:17px}.muted{color:var(--muted)}.small{font-size:12px}
label{display:flex;flex-direction:column;gap:6px;margin-bottom:14px}
input,select{background:rgba(2,6,23,.6);border:1px solid var(--border);color:var(--text);padding:10px 12px;border-radius:10px;font:inherit;width:100%}
input:focus,select:focus{outline:none;border-color:var(--accent)}
.btn{background:rgba(14,165,233,.16);color:var(--text);border:1px solid var(--border);padding:10px 18px;border-radius:10px;font:inherit;cursor:pointer}
.btn:hover{background:rgba(14,165,233,.3)}
.btn--good{background:rgba(132,204,22,.18);border-color:rgba(132,204,22,.32)}.btn--good:hover{background:rgba(132,204,22,.32)}
.btn--bad{background:rgba(239,68,68,.16);border-color:rgba(239,68,68,.32)}
.btn--ghost{background:transparent}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.pill{display:inline-block;padding:6px 14px;border-radius:999px;font-size:13px;font-weight:600;border:1px solid var(--border)}
.pill--good{color:#d9f99d;background:rgba(132,204,22,.16)}.pill--bad{color:#fecaca;background:rgba(239,68,68,.16)}.pill--unknown{color:var(--muted)}
.savebar{position:fixed;bottom:0;left:0;right:0;display:flex;align-items:center;justify-content:center;gap:14px;padding:14px 22px;background:rgba(2,6,23,.92);border-top:1px solid var(--border);backdrop-filter:blur(8px);z-index:50}
.savebar.hidden{display:none}
.log{background:rgba(2,6,23,.7);border:1px solid var(--border);border-radius:10px;padding:14px;font:12px/1.5 ui-monospace,monospace;max-height:240px;overflow:auto;white-space:pre-wrap;color:#cbd5e1}
.ok{color:var(--good)}.err{color:var(--bad)}
</style></head><body>
<header><div class="brand"><h1>Debrid Mount</h1><span class="sub">TorBox / Real-Debrid filesystem</span></div>
<span id="pill" class="pill pill--unknown">checking…</span></header>
<main>
<section class="card"><h2>Status</h2>
<p class="muted" id="state">—</p>
<div class="row"><button class="btn btn--good" onclick="act('mount')">Mount</button>
<button class="btn btn--bad" onclick="act('unmount')">Unmount</button>
<button class="btn" onclick="act('restart')">Restart</button>
<button class="btn btn--ghost" onclick="loadLog()">Refresh log</button></div>
<pre class="log" id="log">loading…</pre></section>
<section class="card"><h2>Configuration</h2><div id="fields"></div></section>
</main>
<div class="savebar hidden" id="savebar"><span class="muted">Unsaved changes</span>
<button class="btn" onclick="save(false)">Save</button>
<button class="btn btn--good" onclick="save(true)">Save &amp; Mount</button></div>
<script>
const api=(p,o)=>fetch(p,o).then(r=>r.json());
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let schema=[],vals={},dirty=false;
function markDirty(){dirty=true;document.getElementById('savebar').classList.remove('hidden')}
function shouldShow(f){if(!f.show_if)return true;const[k,v]=Object.entries(f.show_if)[0];return vals[k]===v}
function render(){document.getElementById('fields').innerHTML=schema.filter(shouldShow).map(f=>{
  const v=esc(vals[f.key]??'');
  if(f.control==='select'){const o=(f.options||[]).map(x=>`<option value="${x}" ${vals[f.key]===x?'selected':''}>${x}</option>`).join('');return `<label>${esc(f.label)}<select data-key="${f.key}">${o}</select>${f.help?`<span class="muted small">${esc(f.help)}</span>`:''}</label>`}
  const t=f.control==='password'?'password':'text';
  const test=f.control==='password'&&f.key==='DEBRID_WEBDAV_PASS'?`<button type="button" class="btn btn--ghost" onclick="testCreds()">Test</button><span id="test-result"></span>`:'';
  return `<label>${esc(f.label)}<div class="row"><input type="${t}" data-key="${f.key}" value="${v}">${test}</div>${f.help?`<span class="muted small">${esc(f.help)}</span>`:''}</label>`}).join('');
  bind();}
function bind(){document.querySelectorAll('[data-key]').forEach(el=>el.addEventListener('input',()=>{snapshot();markDirty();if(el.tagName==='SELECT')render()}))}
function snapshot(){document.querySelectorAll('[data-key]').forEach(el=>vals[el.dataset.key]=el.value)}
async function load(){const c=await api('/api/config');schema=c.fields;c.fields.forEach(f=>vals[f.key]=f.value);render();refresh();}
async function save(andMount){snapshot();const r=await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(vals)});if(r.ok){document.getElementById('savebar').classList.add('hidden');dirty=false;if(andMount){const m=await api('/api/mount/restart',{method:'POST'});alert(m.message||'mounting')}refresh();}else alert('Save failed')}
async function act(a){const r=await api('/api/mount/'+a,{method:'POST'});refresh();}
async function testCreds(){snapshot();const r=await api('/api/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user:vals.DEBRID_WEBDAV_USER,password:vals.DEBRID_WEBDAV_PASS,url:vals.DEBRID_WEBDAV_URL,vendor:vals.DEBRID_WEBDAV_VENDOR})});const el=document.getElementById('test-result');if(r.valid)el.innerHTML=`<span class="ok">✓ ${r.dirs} folders found</span>`;else el.innerHTML=`<span class="err">✗ ${esc(r.error||'failed')}</span>`}
async function refresh(){try{const s=await api('/api/status');const p=document.getElementById('pill'),st=document.getElementById('state');if(s.mounted){p.className='pill pill--good';p.textContent='mounted';st.textContent=`mounted (${s.mode})`}else{p.className='pill pill--bad';p.textContent='not mounted';st.textContent='not mounted'}loadLog()}catch(e){}}
async function loadLog(){try{const r=await api('/api/log');document.getElementById('log').textContent=r.lines||'(empty)'}catch(e){}}
load();setInterval(refresh,5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "debrid-mount-ui/1.0"
    def log_message(self, *a): pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n: return {}
        try: return json.loads(self.rfile.read(n).decode())
        except: return None

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            body = PAGE_HTML.encode()
            self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path == "/api/health":
            return self._json({"ok": True})
        if path == "/api/config":
            return self._json(config_for_ui())
        if path == "/api/status":
            return self._json({**mount.status(), "configured": _is_configured()})
        if path == "/api/log":
            return self._json({"lines": tail(RCLONE_LOG, 100)})
        return self._json({"error":"not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/config":
            body = self._read_json()
            if body is None: return self._json({"error":"invalid json"}, 400)
            write_config(body); return self._json({"ok": True})
        if path == "/api/test":
            body = self._read_json() or {}
            return self._json(test_webdav(body.get("user",""), body.get("password",""),
                                          body.get("url"), body.get("vendor","other")))
        if path == "/api/mount/mount":
            ok, msg = mount.mount(); return self._json({"ok": ok, "message": msg, **mount.status()})
        if path == "/api/mount/unmount":
            ok, msg = mount.unmount(); return self._json({"ok": ok, "message": msg, **mount.status()})
        if path == "/api/mount/restart":
            ok, msg = mount.restart(); return self._json({"ok": ok, "message": msg, **mount.status()})
        return self._json({"error":"not found"}, 404)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


def _is_configured():
    cfg = read_config()
    if cfg.get("DEBRID_MODE", "webdav").lower() == "zurg":
        return bool(cfg.get("DEBRID_ZURG_TOKEN"))
    return bool(cfg.get("DEBRID_WEBDAV_USER") and cfg.get("DEBRID_WEBDAV_PASS"))


def main():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(STATUS_DIR, exist_ok=True)
    write_sample_config()
    # Auto-mount on boot if already configured.
    if _is_configured():
        try:
            mount.mount()
        except Exception as e:
            print("[web_ui] auto-mount failed: {}".format(e), file=sys.stderr)
    srv = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print("[web_ui] listening on :{}".format(LISTEN_PORT), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        mount.unmount()
        srv.server_close()


if __name__ == "__main__":
    main()
