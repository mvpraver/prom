import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


bot_process = None
stopping = False


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"SunRoll bot is running")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def run_web_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"Health server started on port {port}", flush=True)
    server.serve_forever()


def stop_all(*args):
    global stopping, bot_process
    stopping = True

    if bot_process and bot_process.poll() is None:
        print("Stopping bot process...", flush=True)
        bot_process.terminate()
        try:
            bot_process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            bot_process.kill()

    sys.exit(0)


def main():
    global bot_process, stopping

    signal.signal(signal.SIGTERM, stop_all)
    signal.signal(signal.SIGINT, stop_all)

    threading.Thread(target=run_web_server, daemon=True).start()

    while not stopping:
        print("Starting SunRoll bot...", flush=True)

        bot_process = subprocess.Popen([sys.executable, "main.py"])
        code = bot_process.wait()

        if stopping:
            break

        print(f"Bot stopped with exit code {code}. Restarting in 10 seconds...", flush=True)
        time.sleep(10)


if __name__ == "__main__":
    main()
