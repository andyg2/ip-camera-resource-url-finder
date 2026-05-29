#!/usr/bin/env python3
"""Tiny zero-dependency lookup server for cameras.db (StrixCamDB).

Run:  python server.py            (serves http://127.0.0.1:8000)
      python server.py 8080       (custom port)

Serves index.html and a JSON search API. The database is opened read-only;
this process never writes to it.
"""
import json
import os
import sqlite3
import sys
import urllib.parse
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
