"""Disposable HTTP origin for cache privacy, freshness and failure tests."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets


class Origin(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = (self.headers.get('Host', '')+' '+self.path+' '+secrets.token_hex(8)).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'private, no-store' if self.path.startswith('/private-response') else 'public, max-age=30')
        if self.path.startswith('/set-cookie'):
            self.send_header('Set-Cookie', 'cache-test=not-shared; HttpOnly')
        if self.path.startswith('/vary-user'):
            self.send_header('Vary', 'X-User')
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


ThreadingHTTPServer(('0.0.0.0', 8000), Origin).serve_forever()
