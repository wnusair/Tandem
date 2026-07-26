"""A tiny web app hosted by Tandem.

Tandem runs this in the node's sandbox with no network of its own, so instead of
a TCP port it binds the unix socket Tandem hands it in $TANDEM_SERVE_SOCKET. The
node proxies HTTP to that socket. Stdlib only, so it runs with no dependencies to
install.
"""

import http.server
import os
import socketserver

SOCKET = os.environ["TANDEM_SERVE_SOCKET"]
NODE = os.environ.get("TANDEM_NODE_ID", "unknown")


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = (
            f"hello from the tandem web app! served by node {NODE}, "
            f"path={self.path}\n"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class UnixHTTPServer(socketserver.UnixStreamServer):
    # BaseHTTPRequestHandler wants a (host, port) client address; give it a stub.
    def get_request(self):
        connection, _ = self.socket.accept()
        return connection, ("local", 0)


if os.path.exists(SOCKET):
    os.remove(SOCKET)

UnixHTTPServer(SOCKET, Handler).serve_forever()
