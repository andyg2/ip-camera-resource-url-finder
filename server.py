#!/usr/bin/env python3
"""Tiny zero-dependency lookup server for cameras.db (StrixCamDB).

Run:  python server.py            (serves http://127.0.0.1:8000)
      python server.py 8080       (custom port)

Serves index.html and a JSON search API. The database is opened read-only;
this process never writes to it.
"""
import base64
import hashlib
import json
import os
import re
import socket
import sqlite3
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "cameras.db")
INDEX_PATH = os.path.join(HERE, "index.html")

SEARCH_SQL = """
SELECT s.id, b.brand, s.url, s.protocol, s.port,
       (SELECT GROUP_CONCAT(m.model, ', ')
          FROM (SELECT model FROM stream_models
                 WHERE stream_id = s.id LIMIT 8) m) AS models
FROM streams s
JOIN brands b ON b.brand_id = s.brand_id
WHERE b.brand LIKE :q
   OR b.brand_id LIKE :q
   OR s.id IN (SELECT stream_id FROM stream_models WHERE model LIKE :q)
ORDER BY (b.brand LIKE :starts) DESC, b.brand COLLATE NOCASE, s.protocol, s.url
LIMIT :limit
"""


def open_db():
    """Open the database read-only. Falls back to a normal open if the
    read-only URI form is unavailable for any reason."""
    try:
        return sqlite3.connect(
            f"file:{urllib.parse.quote(DB_PATH)}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
    except sqlite3.OperationalError:
        return sqlite3.connect(DB_PATH, check_same_thread=False)


def search(query, limit=300):
    q = query.strip()
    if len(q) < 2:
        return []
    con = open_db()
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            SEARCH_SQL,
            {"q": f"%{q}%", "starts": f"{q}%", "limit": limit},
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# --- Liveness probing -------------------------------------------------------
#
# Given a fully built camera URL, check whether something is reachable at that
# exact path. "Reachable" means we got a real protocol response that is not a
# 404 (e.g. 200, 301/302, 401, 403 all count - the path exists, even if it
# wants credentials). A 404, a refused connection, or a timeout means "not
# reachable". This runs only against hosts the user explicitly searched and
# filled in; the server still binds to localhost only.

PROBE_TIMEOUT = 5  # seconds

DEFAULT_PORTS = {
    "rtsp": 554, "rtsps": 322, "rtp": 554, "rtmp": 1935, "rtmps": 443,
    "http": 80, "https": 443, "mms": 1755, "dvrip": 34567,
}

# Cameras almost always present self-signed certs; this is a reachability
# check, not a security boundary, so certificate verification is disabled.
_TLS = ssl.create_default_context()
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep 301/302 visible as themselves instead of following them."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(
    _NoRedirect, urllib.request.HTTPSHandler(context=_TLS)
)


def _probe_http(url, timeout):
    req = urllib.request.Request(
        url, method="GET",
        headers={"User-Agent": "camera-probe", "Range": "bytes=0-0"},
    )
    try:
        resp = _OPENER.open(req, timeout=timeout)
        code = resp.status
        resp.close()
    except urllib.error.HTTPError as exc:
        code = exc.code  # 301, 401, 403, 404, ... all land here
    return {"alive": code != 404, "status": str(code)}


def _rtsp_read(sock):
    """Read one RTSP response (up to the header terminator)."""
    buf = b""
    while b"\r\n\r\n" not in buf and len(buf) < 8192:
        try:
            chunk = sock.recv(2048)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
    return buf.decode("latin-1", "replace")


def _rtsp_status(text):
    first = text.split("\r\n", 1)[0].split()
    if len(first) >= 2 and first[1].isdigit():
        return int(first[1])
    return None


def _rtsp_auth_challenges(text):
    """Map every WWW-Authenticate scheme offered to its parsed params."""
    offered = {}
    for line in text.split("\r\n"):
        if line.lower().startswith("www-authenticate:"):
            value = line.split(":", 1)[1].strip()
            scheme = value.split(" ", 1)[0].lower()
            params = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', value))
            offered[scheme] = params
    return offered


def _digest_header(user, pw, method, uri, params):
    realm = params.get("realm", "")
    nonce = params.get("nonce", "")
    qop = params.get("qop")
    algorithm = params.get("algorithm", "MD5").upper()
    cnonce = os.urandom(8).hex()
    nc = "00000001"

    def md5(s):
        return hashlib.md5(s.encode("utf-8")).hexdigest()

    ha1 = md5("%s:%s:%s" % (user, realm, pw))
    if algorithm == "MD5-SESS":
        ha1 = md5("%s:%s:%s" % (ha1, nonce, cnonce))
    ha2 = md5("%s:%s" % (method, uri))

    parts = ['username="%s"' % user, 'realm="%s"' % realm,
             'nonce="%s"' % nonce, 'uri="%s"' % uri]
    if qop:
        qop = qop.split(",")[0].strip()
        resp = md5("%s:%s:%s:%s:%s:%s" % (ha1, nonce, nc, cnonce, qop, ha2))
        parts += ['response="%s"' % resp, "qop=%s" % qop,
                  "nc=%s" % nc, 'cnonce="%s"' % cnonce]
    else:
        resp = md5("%s:%s:%s" % (ha1, nonce, ha2))
        parts.append('response="%s"' % resp)
    if "algorithm" in params:
        parts.append("algorithm=%s" % params["algorithm"])
    return "Digest " + ", ".join(parts)


def _rtsp_describe(scheme, host, port, uri, timeout, auth=None):
    extra = ("Authorization: %s\r\n" % auth) if auth else ""
    msg = (
        "DESCRIBE %s RTSP/1.0\r\n"
        "CSeq: 1\r\n"
        "Accept: application/sdp\r\n"
        "User-Agent: camera-probe\r\n"
        "%s\r\n"
    ) % (uri, extra)
    raw = socket.create_connection((host, port), timeout)
    try:
        raw.settimeout(timeout)
        sock = _TLS.wrap_socket(raw, server_hostname=host) if scheme == "rtsps" else raw
        sock.sendall(msg.encode("ascii", "ignore"))
        return _rtsp_read(sock)
    finally:
        raw.close()


def _probe_rtsp(scheme, host, port, path, user, pw, timeout):
    # DESCRIBE confirms the exact path: 200 = path exists, 404 = wrong path.
    # Cameras answer DESCRIBE with 401 first, so when we have credentials we
    # answer the auth challenge and re-ask to get a real 200/404 instead of a
    # bare "reachable" 401. Try Digest first (what cameras almost always use),
    # then fall back to Basic if Digest is still rejected.
    uri = "%s://%s:%d%s" % (scheme, host, port, path or "/")
    text = _rtsp_describe(scheme, host, port, uri, timeout)
    code = _rtsp_status(text)

    if code == 401 and (user or pw):
        offered = _rtsp_auth_challenges(text)
        attempts = []
        if "digest" in offered:
            attempts.append(_digest_header(user, pw, "DESCRIBE", uri, offered["digest"]))
        # Basic as a fallback whenever Digest fails or is not offered.
        token = base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()
        attempts.append("Basic " + token)
        for auth in attempts:
            text = _rtsp_describe(scheme, host, port, uri, timeout, auth)
            code = _rtsp_status(text)
            if code != 401:
                break

    if code is None:
        return {"alive": False, "status": "no rtsp reply"}
    if code == 404:
        return {"alive": False, "status": "404"}
    if code == 401:
        # Reachable RTSP server, but the path could not be confirmed (no
        # credentials given, or they were rejected).
        return {"alive": True, "status": "401 auth"}
    return {"alive": True, "status": str(code)}


def _probe_tcp(host, port, timeout):
    # rtmp / dvrip / mms / other: no cheap path-level check, so a successful
    # TCP connect to the right port is the best signal we have.
    socket.create_connection((host, port), timeout).close()
    return {"alive": True, "status": "tcp open"}


def probe(url, timeout=PROBE_TIMEOUT):
    parsed = urllib.parse.urlsplit(url)
    scheme = (parsed.scheme or "").lower()
    host = parsed.hostname
    if not host or "<" in url:
        return {"alive": False, "status": "no host"}
    port = parsed.port or DEFAULT_PORTS.get(scheme, 0)
    try:
        if scheme in ("http", "https"):
            return _probe_http(url, timeout)
        if scheme in ("rtsp", "rtsps", "rtp"):
            path = parsed.path + (("?" + parsed.query) if parsed.query else "")
            rtsp_scheme = "rtsp" if scheme == "rtp" else scheme
            user = urllib.parse.unquote(parsed.username or "")
            pw = urllib.parse.unquote(parsed.password or "")
            return _probe_rtsp(rtsp_scheme, host, port or 554, path, user, pw, timeout)
        if not port:
            return {"alive": False, "status": "no port"}
        return _probe_tcp(host, port, timeout)
    except (socket.timeout, TimeoutError):
        return {"alive": False, "status": "timeout"}
    except (OSError, ssl.SSLError):
        return {"alive": False, "status": "no connection"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # quiet

    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            try:
                with open(INDEX_PATH, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "index.html not found", "text/plain")
            return
        if parsed.path == "/api/search":
            params = urllib.parse.parse_qs(parsed.query)
            q = (params.get("q") or [""])[0]
            try:
                limit = min(int((params.get("limit") or ["300"])[0]), 1000)
            except ValueError:
                limit = 300
            try:
                results = search(q, limit)
            except Exception as exc:  # surface DB errors to the client
                self._send(500, json.dumps({"error": str(exc)}),
                           "application/json")
                return
            self._send(200, json.dumps({"count": len(results),
                                        "results": results}),
                       "application/json")
            return
        if parsed.path == "/api/probe":
            params = urllib.parse.parse_qs(parsed.query)
            url = (params.get("url") or [""])[0]
            if not url:
                self._send(400, json.dumps({"error": "missing url"}),
                           "application/json")
                return
            try:
                result = probe(url)
            except Exception as exc:  # never let a probe crash the request
                result = {"alive": False, "status": "error", "error": str(exc)}
            self._send(200, json.dumps(result), "application/json")
            return
        self._send(404, "not found", "text/plain")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    if not os.path.exists(DB_PATH):
        sys.exit(f"cameras.db not found at {DB_PATH}")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Camera stream lookup running at http://127.0.0.1:{port}")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
