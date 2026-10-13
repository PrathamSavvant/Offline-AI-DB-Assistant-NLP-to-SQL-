"""Tiny, temporary localhost loading screen; uses only the bundled Python stdlib."""
import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


def read_status(path, run_id):
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if value.get('run_id') == run_id:
            return value
    except (OSError, ValueError):
        pass
    return {'state': 'starting', 'message': 'Preparing your workspace…', 'steps': {}}


def serve(status_path, address_path, run_id):
    page = (Path(__file__).parent / 'templates' / 'startup.html').read_bytes()
    started = time.monotonic()
    terminal_at = None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/status':
                payload = read_status(status_path, run_id)
                target = urlparse(payload.get('url', ''))
                # Redirects are restricted to this application's local HTTP server.
                if target.scheme != 'http' or target.hostname not in {'127.0.0.1', 'localhost', '::1'}:
                    payload.pop('url', None)
                body, mime = json.dumps(payload).encode(), 'application/json'
            elif self.path == '/':
                body, mime = page, 'text/html; charset=utf-8'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    address = {'url': f'http://127.0.0.1:{server.server_port}', 'pid': os.getpid(), 'run_id': run_id}
    temporary = address_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(address), encoding='utf-8')
    temporary.replace(address_path)

    def expire():
        nonlocal terminal_at
        while time.monotonic() - started < 1800:
            status = read_status(status_path, run_id)
            if status.get('state') in {'ready', 'failed', 'cancelled'}:
                terminal_at = terminal_at or time.monotonic()
                grace = 45 if status['state'] == 'ready' else 300
                if time.monotonic() - terminal_at > grace:
                    break
            time.sleep(1)
        server.shutdown()

    threading.Thread(target=expire, daemon=True).start()
    try:
        server.serve_forever(poll_interval=.25)
    finally:
        server.server_close()
        try:
            if json.loads(address_path.read_text()).get('run_id') == run_id:
                address_path.unlink()
        except (OSError, ValueError):
            pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--status', type=Path, required=True)
    parser.add_argument('--address', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    serve(args.status, args.address, args.run_id)
