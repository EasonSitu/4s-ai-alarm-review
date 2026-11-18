"""A model that sends keep-alives must not block the review queue forever."""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.review import RemoteError, post_json


class Model(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.path == "/normal":
            self.wfile.write(json.dumps({"answer": 1}).encode())
            return
        try:
            for _ in range(30):
                self.wfile.write(b" ")
                self.wfile.flush()
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
server.daemon_threads = True
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{server.server_port}"
try:
    assert post_json(url + "/normal", "key", {}, 1, True) == {"answer": 1}
    start = time.monotonic()
    try:
        post_json(url + "/slow", "key", {}, 0.3, True)
        raise AssertionError("keep-alives must not prevent the total timeout")
    except RemoteError:
        pass
    assert time.monotonic() - start < 1.2
    print("PASS normal response and bounded model request with keep-alives")
finally:
    server.shutdown()
    server.server_close()
