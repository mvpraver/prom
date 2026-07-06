import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

bot_process = None

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("SunRoll bot is running".encode("utf-8"))
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return

def run_health_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"Health server started on port {port}", flush=True)
    server.serve_forever()

def stop(*args):
    global bot_process
    if bot_process and bot_process.poll() is None:
        bot_process.terminate()
    sys.exit(0)

def main():
    global bot_process
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    threading.Thread(target=run_health_server, daemon=True).start()
    bot_process = subprocess.Popen([sys.executable, "main.py"])
    code = bot_process.wait()
    sys.exit(code)

if __name__ == "__main__":
    main()
