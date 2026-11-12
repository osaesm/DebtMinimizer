"""
web_server.py — Minimalist LAN Web Server for Debt Optimizer.

Serves a single-button web interface (50vw x 50vh, centered) that triggers
debt_optimizer.py on click and provides visual execution feedback.
Uses Python's standard library (ThreadingHTTPServer) — zero external web dependencies.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Tuple

# Default Configuration
DEFAULT_HOST = os.environ.get("HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("PORT", "8080"))
DEFAULT_SHEET = os.environ.get("SHEET_NAME", "Bank Balance Script 2")
DEFAULT_CREDS = os.environ.get("CREDS_PATH", "credentials.json")
DEFAULT_TOKEN = os.environ.get("TOKEN_PATH", "token.json")
DEFAULT_SERVICE_ACCOUNT = os.environ.get("SERVICE_ACCOUNT_PATH", None)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_PATH = os.path.join(BASE_DIR, "debt_optimizer.py")

# Mutex to prevent concurrent simulation runs
SIMULATION_LOCK = threading.Lock()

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Debt Optimizer</title>
  <style>
    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }
    body {
      min-height: 100vh;
      min-height: 100dvh;
      display: grid;
      place-items: center;
      background-color: #0f172a;
      overflow: hidden;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }
    .sim-button {
      width: 50vw;
      height: 50vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 1.25rem;
      border: 3px solid rgba(255, 255, 255, 0.15);
      border-radius: 2rem;
      background: linear-gradient(135deg, #1d4ed8, #2563eb, #3b82f6);
      background-size: 200% 200%;
      color: #ffffff;
      font-size: clamp(1.75rem, 4vw, 3.75rem);
      font-weight: 800;
      letter-spacing: -0.02em;
      cursor: pointer;
      user-select: none;
      box-shadow: 0 25px 50px -12px rgba(37, 99, 235, 0.45);
      transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
      outline: none;
      position: relative;
    }
    .sim-button:hover:not(:disabled) {
      transform: translateY(-4px) scale(1.015);
      box-shadow: 0 35px 60px -15px rgba(59, 130, 246, 0.6);
      border-color: rgba(255, 255, 255, 0.3);
    }
    .sim-button:active:not(:disabled) {
      transform: translateY(2px) scale(0.985);
    }
    .sim-button:focus-visible {
      outline: 4px solid #60a5fa;
      outline-offset: 6px;
    }
    .sim-button.running {
      cursor: wait;
      background: linear-gradient(135deg, #334155, #475569, #1e293b);
      box-shadow: 0 20px 40px -10px rgba(0, 0, 0, 0.5);
      animation: pulse 1.6s infinite ease-in-out;
    }
    .sim-button.success {
      background: linear-gradient(135deg, #065f46, #059669, #10b981);
      box-shadow: 0 25px 50px -12px rgba(16, 185, 129, 0.5);
      border-color: rgba(52, 211, 153, 0.4);
    }
    .sim-button.error {
      background: linear-gradient(135deg, #991b1b, #dc2626, #ef4444);
      box-shadow: 0 25px 50px -12px rgba(239, 68, 68, 0.5);
      border-color: rgba(248, 113, 113, 0.4);
    }
    .status-subtext {
      font-size: clamp(0.9rem, 1.6vw, 1.35rem);
      font-weight: 500;
      opacity: 0.92;
      letter-spacing: normal;
      text-align: center;
      padding: 0 1rem;
    }
    @keyframes pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.85; transform: scale(0.99); }
    }
  </style>
</head>
<body>
  <button id="runBtn" class="sim-button" type="button" aria-live="polite">
    <span id="btnText">Run Simulation</span>
    <span id="subText" class="status-subtext" style="display: none;"></span>
  </button>

  <script>
    const btn = document.getElementById('runBtn');
    const btnText = document.getElementById('btnText');
    const subText = document.getElementById('subText');
    let isRunning = false;

    btn.addEventListener('click', async () => {
      if (isRunning) return;
      isRunning = true;
      btn.disabled = true;
      btn.className = 'sim-button running';
      btnText.textContent = 'Running...';
      subText.style.display = 'block';
      subText.textContent = 'Simulating cash flow & updating sheet...';

      try {
        const res = await fetch('/run', { method: 'POST' });
        const data = await res.json();
        
        if (res.ok && data.status === 'success') {
          btn.className = 'sim-button success';
          btnText.textContent = 'Success!';
          subText.textContent = data.message || 'Ledger updated successfully';
        } else {
          btn.className = 'sim-button error';
          btnText.textContent = 'Failed';
          subText.textContent = data.message || 'Check server logs';
        }
      } catch (err) {
        btn.className = 'sim-button error';
        btnText.textContent = 'Network Error';
        subText.textContent = 'Could not reach server';
      } finally {
        setTimeout(() => {
          btn.className = 'sim-button';
          btnText.textContent = 'Run Simulation';
          subText.style.display = 'none';
          btn.disabled = false;
          isRunning = false;
        }, 4000);
      }
    });
  </script>
</body>
</html>
"""


def execute_simulation(sheet_name: str, creds_path: str, token_path: str, service_account: str = None) -> Tuple[bool, str]:
    """Runs debt_optimizer.py as a subprocess, ensuring isolated execution and stdout capture."""
    cmd = [
        sys.executable,
        SCRIPT_PATH,
        "--sheet", sheet_name,
        "--creds", creds_path,
        "--token", token_path
    ]
    if service_account:
        cmd.extend(["--service-account", service_account])

    print(f"[web_server] Executing: {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=BASE_DIR
        )
        output = proc.stdout + "\n" + proc.stderr
        print(f"[web_server] Simulation finished with return code {proc.returncode}")
        if proc.returncode == 0:
            # Extract last success line if available
            summary = "Simulation complete and written to sheet."
            for line in reversed(proc.stdout.splitlines()):
                if "Success!" in line or "written to Google Sheet" in line:
                    summary = line.strip()
                    break
            return True, summary
        else:
            err_line = "Execution failed."
            for line in reversed(output.splitlines()):
                if line.strip():
                    err_line = line.strip()
                    break
            return False, err_line
    except subprocess.TimeoutExpired:
        print("[web_server] Error: Simulation timed out after 180s.")
        return False, "Simulation timed out after 180 seconds."
    except Exception as e:
        print(f"[web_server] Error: {e}")
        return False, str(e)


def create_handler_class(config: dict):
    class RequestHandler(BaseHTTPRequestHandler):
        def _send_response(self, code: int, content_type: str, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/index"):
                body = HTML_PAGE.encode("utf-8")
                self._send_response(200, "text/html; charset=utf-8", body)
            elif self.path == "/health":
                body = json.dumps({"status": "healthy"}).encode("utf-8")
                self._send_response(200, "application/json", body)
            else:
                body = json.dumps({"error": "Not Found"}).encode("utf-8")
                self._send_response(404, "application/json", body)

        def do_POST(self):
            if self.path == "/run":
                # Acquire mutex to avoid concurrent execution runs
                acquired = SIMULATION_LOCK.acquire(blocking=False)
                if not acquired:
                    resp = {
                        "status": "busy",
                        "message": "Simulation is already in progress. Please wait."
                    }
                    self._send_response(429, "application/json", json.dumps(resp).encode("utf-8"))
                    return

                try:
                    success, message = execute_simulation(
                        sheet_name=config["sheet"],
                        creds_path=config["creds"],
                        token_path=config["token"],
                        service_account=config.get("service_account")
                    )
                    status_str = "success" if success else "error"
                    code = 200 if success else 500
                    resp = {
                        "status": status_str,
                        "message": message
                    }
                    self._send_response(code, "application/json", json.dumps(resp).encode("utf-8"))
                finally:
                    SIMULATION_LOCK.release()
            else:
                body = json.dumps({"error": "Not Found"}).encode("utf-8")
                self._send_response(404, "application/json", body)

        def log_message(self, format, *args):
            # Formatted log message
            print(f"[web_server] {self.address_string()} - {format % args}")

    return RequestHandler


def run_server(host: str, port: int, config: dict):
    handler_class = create_handler_class(config)
    server = ThreadingHTTPServer((host, port), handler_class)
    print(f"==================================================")
    print(f" Debt Optimizer Web Service Running")
    print(f" URL: http://{host}:{port}/")
    print(f" Target Sheet: {config['sheet']}")
    print(f" Press Ctrl+C to terminate.")
    print(f"==================================================")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server gracefully...")
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="Web server for Debt Optimizer LAN trigger.")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Host interface to bind (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to bind (default: {DEFAULT_PORT})")
    parser.add_argument("--sheet", default=DEFAULT_SHEET, help=f"Google Sheet name (default: '{DEFAULT_SHEET}')")
    parser.add_argument("--creds", default=DEFAULT_CREDS, help="Path to credentials.json")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="Path to token.json")
    parser.add_argument("--service-account", default=DEFAULT_SERVICE_ACCOUNT, help="Optional service_account.json path")
    args = parser.parse_args()

    config = {
        "sheet": args.sheet,
        "creds": args.creds,
        "token": args.token,
        "service_account": args.service_account
    }
    run_server(args.host, args.port, config)


if __name__ == "__main__":
    main()
