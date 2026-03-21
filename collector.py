#!/usr/bin/env python3
"""Receives POST from LiteLLM logger.py, writes requests.jsonl.
Also caches key status so dashboard /api/keys is fast."""
import subprocess, time, json, os, threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

LOG_FILE   = "/home/ubuntu/litellm-proxy/requests.jsonl"
STATE_FILE = "/home/ubuntu/litellm-proxy/collector_state.json"
WRITE_LOCK = threading.Lock()
KEY_CACHE  = {"data": [], "ts": 0}  # cached key status
KEY_LOCK   = threading.Lock()

def write_record(record):
    with WRITE_LOCK:
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")

# ── HTTP server ──────────────────────────────────────────────────────────────
class LogHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def do_GET(self):
        if self.path == "/key-cache":
            with KEY_LOCK:
                data = json.dumps(KEY_CACHE).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            record = json.loads(self.rfile.read(length))
        except Exception:
            self.send_response(400); self.end_headers(); return

        if self.path == "/log":
            write_record(record)
            self.send_response(200); self.end_headers()
        elif self.path == "/key-cache":
            with KEY_LOCK:
                KEY_CACHE["data"] = record
                KEY_CACHE["ts"] = time.time()
            self.send_response(200); self.end_headers()
        else:
            self.send_response(404); self.end_headers()

def run_http():
    HTTPServer(("0.0.0.0", 8889), LogHandler).serve_forever()

# ── 舊的 kubectl log 掃描（fallback，抓沒有 key 資訊的舊格式）──────────────
def get_pod_name():
    r = subprocess.run(
        ["sudo", "kubectl", "get", "pods", "-n", "litellm", "-l", "app=litellm",
         "-o", "jsonpath={.items[0].metadata.name}"],
        capture_output=True, text=True
    )
    return r.stdout.strip()

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_pod": "", "last_line": 0}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def collect():
    state = load_state()
    pod = get_pod_name()
    if not pod:
        return

    if pod != state.get("last_pod"):
        state = {"last_pod": pod, "last_line": 0}

    result = subprocess.run(
        ["sudo", "kubectl", "logs", "-n", "litellm", pod],
        capture_output=True, text=True
    )
    lines = result.stdout.splitlines()
    new_lines = lines[state["last_line"]:]

    for line in new_lines:
        if "200 OK" not in line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        model_name = d.get("model", "")
        if "flash" in model_name:
            brain = "simple-brain"
        elif "pro" in model_name:
            brain = "smart-brain"
        else:
            continue
        ts = datetime.now(timezone.utc).isoformat()
        try:
            raw_ts = d.get("timestamp", ts)
            parsed = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            ts = parsed.isoformat()
        except Exception:
            pass
        write_record({"ts": ts, "model": brain})

    state["last_line"] = len(lines)
    save_state(state)

if __name__ == "__main__":
    print("Collector started (HTTP :8889 + kubectl log scanner)")
    threading.Thread(target=run_http, daemon=True).start()
    while True:
        try:
            collect()
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(15)
