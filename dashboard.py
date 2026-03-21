#!/usr/bin/env python3
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, os, urllib.request, urllib.error, concurrent.futures, subprocess, yaml, secrets, base64 as _b64
from datetime import datetime, timezone, timedelta

AUTH_USER = os.environ.get("DASHBOARD_USER", "")
AUTH_PASS = os.environ.get("DASHBOARD_PASS", "")
if not AUTH_USER or not AUTH_PASS:
    raise RuntimeError("DASHBOARD_USER and DASHBOARD_PASS environment variables must be set")
SESSION_TOKEN = secrets.token_hex(32)  # 每次重啟產生新 token

def get_config():
    r = subprocess.run(
        ["sudo", "kubectl", "get", "configmap", "litellm-config", "-n", "litellm",
         "-o", "jsonpath={.data.config\\.yaml}"],
        capture_output=True, text=True
    )
    return yaml.safe_load(r.stdout) or {}

def load_keys():
    config = get_config()
    keys = []
    for entry in config.get("model_list", []):
        params = entry.get("litellm_params", {})
        key = params.get("api_key", "")
        model = entry.get("model_name", "")
        gemini_model = params.get("model", "").replace("gemini/", "")
        if key:
            keys.append({"key": key, "model": model, "gemini_model": gemini_model})
    return keys

def get_model_status():
    config = get_config()
    groups = {}
    for entry in config.get("model_list", []):
        name = entry.get("model_name", "")
        gm = entry.get("litellm_params", {}).get("model", "").replace("gemini/", "")
        if name not in groups:
            groups[name] = gm
    return groups

def ping_key(entry):
    key = entry["key"]
    kid = key[-7:]
    model = entry["model"]
    gemini_model = entry.get("gemini_model") or ("gemini-2.5-flash" if "simple" in model else "gemini-3-pro-preview")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent?key={key}"
    body = json.dumps({"contents": [{"parts": [{"text": "hi"}]}], "generationConfig": {"maxOutputTokens": 1}}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=8)
        return {"key_id": kid, "model": model, "gemini_model": gemini_model, "status": "ok"}
    except urllib.error.HTTPError as e:
        body_str = e.read().decode(errors="ignore")
        if e.code == 429:
            status = "out_of_quota" if "GenerateRequestsPerDay" in body_str or "per_day" in body_str.lower() else "rate_limit"
            return {"key_id": kid, "model": model, "gemini_model": gemini_model, "status": status}
        elif e.code in (400, 403):
            return {"key_id": kid, "model": model, "gemini_model": gemini_model, "status": "invalid"}
        else:
            return {"key_id": kid, "model": model, "gemini_model": gemini_model, "status": "error", "detail": str(e.code)}
    except Exception as e:
        return {"key_id": kid, "model": model, "gemini_model": gemini_model, "status": "error", "detail": str(e)[:50]}

def check_all_keys(keys):
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        result = list(ex.map(ping_key, keys))
    # push to collector cache
    try:
        body = json.dumps(result).encode()
        req = urllib.request.Request("http://127.0.0.1:8889/key-cache", data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass
    return result

def get_cached_keys():
    try:
        r = urllib.request.urlopen("http://127.0.0.1:8889/key-cache", timeout=2)
        cache = json.loads(r.read())
        if cache.get("ts", 0) > 0:
            return cache["data"]
    except Exception:
        pass
    return None

LOG_FILE = "/home/ubuntu/litellm-proxy/requests.jsonl"

def load_records():
    if not os.path.exists(LOG_FILE):
        return []
    records = []
    with open(LOG_FILE) as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except Exception:
                pass
    return records

def count_fallbacks(records, hours=24):
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    count = 0
    for r in records:
        if r.get("type") != "fallback":
            continue
        try:
            ts = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
            if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                count += 1
        except Exception:
            pass
    return count

def bucket(records, now, span_hours, bucket_minutes):
    start = now - timedelta(hours=span_hours)
    n = int(span_hours * 60 / bucket_minutes)
    buckets = [{"t": (start + timedelta(minutes=i*bucket_minutes)).isoformat(), "simple": 0, "smart": 0} for i in range(n)]
    for r in records:
        try:
            ts = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
            if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
            if ts < start or ts > now: continue
            idx = int((ts - start).total_seconds() / 60 / bucket_minutes)
            if 0 <= idx < n:
                key = "simple" if r["model"] == "simple-brain" else "smart"
                buckets[idx][key] += 1
        except Exception:
            pass
    return buckets

RANGES = [
    ("1hr",    1,      1),
    ("1day",   24,     60),
    ("2weeks", 24*14,  60*24),
    ("1month", 24*30,  60*24),
    ("1year",  24*365, 60*24*30),
]

HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>LiteLLM Dashboard</title>
<script src="/chart.js"></script>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d0d0f;color:#e2e8f0;min-height:100vh;padding:2rem}
  h1{font-size:1.25rem;font-weight:600;color:#f1f5f9;margin-bottom:0.25rem;display:flex;align-items:center;gap:0.5rem}
  .subtitle{font-size:0.8rem;color:#64748b;margin-bottom:2rem}
  .cards{display:flex;gap:1rem;margin-bottom:1.5rem;flex-wrap:wrap}
  .card{background:#161618;border:1px solid #1e1e24;border-radius:12px;padding:1.25rem 1.5rem;min-width:160px;flex:1}
  .card-label{font-size:0.72rem;color:#64748b;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:0.5rem}
  .card-value{font-size:2rem;font-weight:700;line-height:1}
  .card-value.simple{color:#34d399}
  .card-value.smart{color:#818cf8}
  .card-sub{font-size:0.72rem;color:#475569;margin-top:0.4rem}
  .section{background:#161618;border:1px solid #1e1e24;border-radius:12px;padding:1.25rem 1.5rem;margin-bottom:1rem}
  .section-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem}
  .section-title{font-size:0.72rem;color:#64748b;text-transform:uppercase;letter-spacing:0.05em}
  .section-time{font-size:0.7rem;color:#334155}
  /* brain rows */
  .brain-row{display:flex;align-items:center;gap:1rem;padding:0.6rem 0;border-bottom:1px solid #1e1e24}
  .brain-row:last-child{border-bottom:none}
  .brain-name{font-size:0.82rem;font-weight:600;min-width:100px}
  .brain-name.simple{color:#34d399}
  .brain-name.smart{color:#818cf8}
  select.model-select{background:#0d0d0f;color:#64748b;border:1px solid #1e1e24;border-radius:6px;padding:0.2rem 0.4rem;font-size:0.72rem;cursor:pointer;max-width:180px}
  .key-pills{display:flex;flex-wrap:wrap;gap:0.4rem;flex:2}
  .key-pill{display:flex;align-items:center;gap:5px;background:#0d0d0f;border:1px solid #1e1e24;border-radius:7px;padding:4px 9px;font-size:0.75rem}
  .status-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
  .status-ok{background:#34d399}
  .status-rate_limit{background:#f59e0b}
  .status-out_of_quota{background:#ef4444}
  .status-invalid,.status-error{background:#f87171}
  .status-checking{background:#475569;animation:pulse 1s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
  .key-badge{font-family:monospace;font-size:0.75rem;color:#a5b4fc}
  .status-label{color:#475569;font-size:0.7rem}
  /* chart */
  .chart-card{background:#161618;border:1px solid #1e1e24;border-radius:12px;padding:1.5rem;margin-bottom:1rem}
  .chart-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:1.25rem;flex-wrap:wrap;gap:0.75rem}
  .tabs{display:flex;gap:4px;background:#0d0d0f;border-radius:8px;padding:3px}
  .tab{padding:5px 14px;border-radius:6px;cursor:pointer;font-size:0.78rem;color:#64748b;border:none;background:transparent;transition:all 0.15s}
  .tab.active{background:#1e1e24;color:#e2e8f0;font-weight:500}
  .legend{display:flex;gap:1rem;align-items:center;flex-wrap:wrap}
  .legend-item{display:flex;align-items:center;gap:6px;font-size:0.75rem;color:#64748b}
  .dot{width:8px;height:8px;border-radius:50%}
  canvas{max-height:260px}
  .updated{font-size:0.7rem;color:#334155;margin-top:1rem;text-align:right}
  .spinner{width:12px;height:12px;border:2px solid #334155;border-top-color:#f59e0b;border-radius:50%;animation:spin 0.6s linear infinite;display:inline-block}
  @keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<h1>🦞 LiteLLM</h1>
<p class="subtitle">Request monitor · auto-refresh 30s</p>

<div class="cards">
  <div class="card">
    <div class="card-label">Simple Brain</div>
    <div class="card-value simple" id="total-simple">—</div>
    <div class="card-sub" style="display:flex;align-items:center;gap:0.4rem;margin-top:0.5rem">
      <select class="model-select" id="select-simple" onchange="switchModel('simple-brain',this.value)"></select>
      <span id="switch-status-simple" style="font-size:0.75rem"></span>
    </div>
  </div>
  <div class="card">
    <div class="card-label">Smart Brain</div>
    <div class="card-value smart" id="total-smart">—</div>
    <div class="card-sub" style="display:flex;align-items:center;gap:0.4rem;margin-top:0.5rem">
      <select class="model-select" id="select-smart" onchange="switchModel('smart-brain',this.value)"></select>
      <span id="switch-status-smart" style="font-size:0.75rem"></span>
    </div>
  </div>
  <div class="card">
    <div class="card-label">Total</div>
    <div class="card-value" id="total-all" style="color:#f1f5f9">—</div>
    <div class="card-sub">all time</div>
  </div>
  <div class="card">
    <div class="card-label">Fallbacks (24h)</div>
    <div class="card-value" id="fallbacks-24h" style="color:#f59e0b">—</div>
    <div class="card-sub">simple→fallback triggers</div>
  </div>
  <div class="card">
    <div class="card-label">Quota Reset</div>
    <div class="card-value" id="quota-reset" style="color:#64748b;font-size:1.2rem">—</div>
    <div class="card-sub">UTC 00:00 daily</div>
  </div>
</div>

<div class="section">
  <div class="section-header">
    <span class="section-title">Key Status</span>
    <span class="section-time" id="key-status-time"></span>
  </div>
  <div id="brain-rows"><span style="color:#475569;font-size:0.78rem">loading...</span></div>
</div>

<div class="chart-card">
  <div class="chart-header">
    <div class="legend">
      <div class="legend-item"><div class="dot" style="background:#34d399"></div>simple-brain</div>
      <div class="legend-item"><div class="dot" style="background:#818cf8"></div>smart-brain</div>
    </div>
    <div class="tabs">
      <button class="tab active" onclick="setRange('1hr')">1h</button>
      <button class="tab" onclick="setRange('1day')">1d</button>
      <button class="tab" onclick="setRange('2weeks')">2w</button>
      <button class="tab" onclick="setRange('1month')">1mo</button>
      <button class="tab" onclick="setRange('1year')">1y</button>
    </div>
  </div>
  <canvas id="chart"></canvas>
</div>

<div class="updated" id="updated"></div>

<script>
let chart, allData, currentRange = '1hr';
let keyData = [];   // from /api/keys
let modelStatus = {}; // from /api/model-status

const SWITCH_MODELS = {
  'simple-brain': ['gemini-2.5-flash', 'gemini-3-flash-preview', 'gemini-2.0-flash'],
  'smart-brain':  ['gemini-3-pro-preview', 'gemini-2.5-pro'],
};

const LABEL_FMT = {
  '1hr':    b => new Date(b).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}),
  '1day':   b => new Date(b).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}),
  '2weeks': b => new Date(b).toLocaleDateString([],{month:'short',day:'numeric'}),
  '1month': b => new Date(b).toLocaleDateString([],{month:'short',day:'numeric'}),
  '1year':  b => new Date(b).toLocaleDateString([],{month:'short',year:'2-digit'}),
};

const STATUS_LABEL = {ok:'✓ ok', rate_limit:'rate limit', out_of_quota:'out of quota', invalid:'invalid', error:'error'};

function renderBrainRows() {
  const brains = ['simple-brain', 'smart-brain'];
  const container = document.getElementById('brain-rows');
  container.innerHTML = brains.map(brain => {
    const cls = brain.includes('simple') ? 'simple' : 'smart';
    const brainKeys = keyData.filter(k => k.model === brain);
    const pillsHtml = brainKeys.length
      ? brainKeys.map(k => `<div class="key-pill">
          <div class="status-dot status-${k.status}"></div>
          <span class="key-badge">${k.key_id}</span>
          <span class="status-label">${STATUS_LABEL[k.status] || k.status}</span>
        </div>`).join('')
      : '<span style="color:#334155;font-size:0.75rem">no keys</span>';
    return `<div class="brain-row">
      <span class="brain-name ${cls}">${brain}</span>
      <div class="key-pills">${pillsHtml}</div>
    </div>`;
  }).join('');
}

async function loadModelStatus() {
  try {
    const r = await apiFetch('/api/model-status');
    modelStatus = await r.json();
    ['simple-brain', 'smart-brain'].forEach(brain => {
      const id = brain.includes('simple') ? 'simple' : 'smart';
      const sel = document.getElementById('select-' + id);
      if (!sel) return;
      const current = modelStatus[brain] || '';
      const opts = (SWITCH_MODELS[brain] || [current]);
      // add current if not in list
      if (current && !opts.includes(current)) opts.unshift(current);
      sel.innerHTML = opts.map(m => `<option value="${m}" ${m===current?'selected':''}>${m}</option>`).join('');
    });
    renderBrainRows();
  } catch(e) {}
}

async function switchModel(group, model) {
  const id = group.includes('simple') ? 'simple' : 'smart';
  const statusEl = document.getElementById('switch-status-' + id);
  statusEl.innerHTML = '<div class="spinner"></div>';
  const r = await apiFetch('/api/switch-model', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({group, gemini_model: model})
  });
  if (r.ok) {
    statusEl.textContent = '✓';
    statusEl.style.color = '#34d399';
    setTimeout(() => { statusEl.textContent = ''; statusEl.style.color = ''; }, 4000);
    setTimeout(() => { loadModelStatus(); loadKeyStatus(); }, 8000);
  } else {
    statusEl.textContent = '✗';
    statusEl.style.color = '#f87171';
    setTimeout(() => { statusEl.textContent = ''; statusEl.style.color = ''; }, 4000);
  }
}

async function loadKeyStatus() {
  try {
    const r = await apiFetch('/api/keys');
    keyData = await r.json();
    document.getElementById('key-status-time').textContent = 'checked ' + new Date().toLocaleTimeString();
    renderBrainRows();
  } catch(e) {}
}

async function apiFetch(url, opts={}) {
  const headers = {'X-Session': '__SESSION_TOKEN__', ...(opts.headers||{})};
  return fetch(url, {...opts, headers});
}

async function load() {
  const r = await apiFetch('/api');
  const j = await r.json();
  allData = j;
  document.getElementById('total-simple').textContent = j.total_simple;
  document.getElementById('total-smart').textContent = j.total_smart;
  document.getElementById('total-all').textContent = j.total_simple + j.total_smart;
  document.getElementById('fallbacks-24h').textContent = j.fallbacks_24h ?? '—';
  // quota reset countdown
  if (j.quota_reset_ts) {
    const resetMs = new Date(j.quota_reset_ts).getTime();
    const updateCountdown = () => {
      const diff = Math.max(0, resetMs - Date.now());
      const h = Math.floor(diff / 3600000);
      const m = Math.floor((diff % 3600000) / 60000);
      document.getElementById('quota-reset').textContent = `${h}h ${m}m`;
    };
    updateCountdown();
    clearInterval(window._countdownTimer);
    window._countdownTimer = setInterval(updateCountdown, 60000);
  }
  document.getElementById('updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
  render(currentRange);
}

function setRange(range) {
  currentRange = range;
  document.querySelectorAll('.tab').forEach(t => {
    const map = {'1h':'1hr','1d':'1day','2w':'2weeks','1mo':'1month','1y':'1year'};
    t.classList.toggle('active', map[t.textContent] === range);
  });
  render(range);
}

function render(range) {
  if (!allData) return;
  const d = allData.ranges[range];
  const fmt = LABEL_FMT[range];
  const labels = d.map(b => fmt(b.t));
  const simple = d.map(b => b.simple);
  const smart  = d.map(b => b.smart);
  const ctx = document.getElementById('chart');
  if (chart) chart.destroy();
  chart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        {label:'simple-brain', data:simple, backgroundColor:'rgba(52,211,153,0.7)', borderColor:'rgba(52,211,153,0.9)', borderWidth:0, borderRadius:3, stack:'s'},
        {label:'smart-brain',  data:smart,  backgroundColor:'rgba(129,140,248,0.7)', borderColor:'rgba(129,140,248,0.9)', borderWidth:0, borderRadius:3, stack:'s'},
      ]
    },
    options: {
      responsive: true,
      animation: {duration: 300},
      plugins: {
        legend: {display: false},
        tooltip: {
          backgroundColor:'#1e1e24', titleColor:'#94a3b8', bodyColor:'#e2e8f0',
          borderColor:'#2d2d35', borderWidth:1, padding:10,
          callbacks: {
            title: items => items[0].label,
            label: item => ` ${item.dataset.label}: ${item.raw}`
          }
        }
      },
      scales: {
        x: {ticks:{color:'#475569',maxTicksLimit:10,font:{size:11}},grid:{color:'rgba(255,255,255,0.03)'},border:{color:'#1e1e24'}},
        y: {ticks:{color:'#475569',font:{size:11}},grid:{color:'rgba(255,255,255,0.05)'},border:{color:'#1e1e24'},beginAtZero:true}
      }
    }
  });
}

load();
loadModelStatus();
loadKeyStatus();
setInterval(load, 30000);
setInterval(loadKeyStatus, 60000);
setInterval(loadModelStatus, 60000);
</script>
</body></html>
"""

LOGIN_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Login</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d0f;display:flex;align-items:center;justify-content:center;min-height:100vh;font-family:-apple-system,sans-serif}
.box{background:#161618;border:1px solid #1e1e24;border-radius:12px;padding:2rem;width:320px}
h2{color:#f1f5f9;font-size:1rem;margin-bottom:1.5rem}
input{width:100%;background:#0d0d0f;border:1px solid #1e1e24;border-radius:8px;padding:0.6rem 0.75rem;color:#e2e8f0;font-size:0.875rem;margin-bottom:0.75rem;outline:none}
input:focus{border-color:#334155}
button{width:100%;background:#6366f1;color:#fff;border:none;border-radius:8px;padding:0.65rem;font-size:0.875rem;cursor:pointer;font-weight:500}
.err{color:#f87171;font-size:0.78rem;margin-top:0.75rem;text-align:center}
</style></head>
<body><div class="box">
<h2>🦞 LiteLLM</h2>
<form method="POST" action="/login">
  <input type="text" name="u" placeholder="Email" autocomplete="username">
  <input type="password" name="p" placeholder="Password" autocomplete="current-password">
  <button type="submit">Login</button>
  {error}
</form>
</div></body></html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def _send(self, body, content_type="application/json", status=200):
        if isinstance(body, str): body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self):
        # cookie auth
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sid" and v == SESSION_TOKEN:
                return True
        # JS fetch header auth
        if self.headers.get("X-Session", "") == SESSION_TOKEN:
            return True
        self.send_response(302)
        self.send_header("Location", "/login")
        self.end_headers()
        return False

    def do_POST(self):
        if self.path == "/login":
            length = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(length).decode()
            from urllib.parse import parse_qs
            params = parse_qs(data)
            u = params.get("u", [""])[0]
            p = params.get("p", [""])[0]
            if u == AUTH_USER and p == AUTH_PASS:
                self.send_response(302)
                self.send_header("Set-Cookie", f"sid={SESSION_TOKEN}; HttpOnly; Path=/; Max-Age=2592000")
                self.send_header("Location", "/")
                self.end_headers()
            else:
                page = LOGIN_HTML.replace("{error}", '<p class="err">Invalid credentials</p>')
                self._send(page.encode(), "text/html; charset=utf-8")
            return
        if not self._check_auth(): return
        if self.path == "/api/switch-model":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            group = body.get("group")
            new_model = body.get("gemini_model")
            if not new_model or not group:
                self.send_response(400); self.end_headers(); return
            config = get_config()
            for entry in config.get("model_list", []):
                if entry.get("model_name") == group:
                    entry["litellm_params"]["model"] = f"gemini/{new_model}"
            new_yaml = yaml.dump(config, allow_unicode=True)
            proc = subprocess.run(
                ["sudo", "kubectl", "create", "configmap", "litellm-config", "-n", "litellm",
                 "--from-literal=config.yaml=" + new_yaml, "--dry-run=client", "-o", "yaml"],
                capture_output=True, text=True
            )
            subprocess.run(["sudo", "kubectl", "apply", "-f", "-"], input=proc.stdout, capture_output=True, text=True)
            subprocess.run(["sudo", "kubectl", "rollout", "restart", "deployment/litellm", "-n", "litellm"], capture_output=True)
            self._send(json.dumps({"ok": True}))
            return
        self.send_response(404); self.end_headers()

    def do_GET(self):
        if self.path == "/login":
            self._send(LOGIN_HTML.replace("{error}", "").encode(), "text/html; charset=utf-8")
            return
        if self.path == "/chart.js":
            with open("/home/ubuntu/litellm-proxy/chart.min.js", "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return
        if not self._check_auth(): return
        if self.path == "/api/keys":
            keys = [k for k in load_keys() if k["model"] in ("simple-brain", "smart-brain")]
            cached = get_cached_keys()
            if cached is not None:
                self._send(json.dumps(cached))
            else:
                self._send(json.dumps(check_all_keys(keys)))
            return
        if self.path == "/api/model-status":
            groups = get_model_status()
            self._send(json.dumps({k: v for k, v in groups.items() if k in ("simple-brain", "smart-brain")}))
            return
        if self.path == "/api":
            records = load_records()
            now = datetime.now(timezone.utc)
            data = {"ranges": {}}
            for name, hours, bmin in RANGES:
                data["ranges"][name] = bucket(records, now, hours, bmin)
            success = [r for r in records if r.get("type", "success") == "success"]
            data["total_simple"] = sum(1 for r in success if r.get("model") == "simple-brain")
            data["total_smart"]  = sum(1 for r in success if r.get("model") == "smart-brain")
            data["fallbacks_24h"] = count_fallbacks(records, hours=24)
            # quota reset: next UTC 00:00
            from datetime import timedelta
            next_reset = (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
            data["quota_reset_ts"] = next_reset.isoformat()
            self._send(json.dumps(data))
            return
        self._send(HTML.replace("__SESSION_TOKEN__", SESSION_TOKEN).encode(), "text/html; charset=utf-8")

def _bg_key_refresh():
    """Background thread: ping all keys every 5 minutes and push to collector cache."""
    import time
    while True:
        try:
            keys = [k for k in load_keys() if k["model"] in ("simple-brain", "smart-brain")]
            check_all_keys(keys)  # also pushes to cache
        except Exception:
            pass
        time.sleep(300)

if __name__ == "__main__":
    import threading
    threading.Thread(target=_bg_key_refresh, daemon=True).start()
    print("Dashboard running on http://0.0.0.0:8888")
    HTTPServer(("0.0.0.0", 8888), Handler).serve_forever()
