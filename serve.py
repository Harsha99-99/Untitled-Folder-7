#!/usr/bin/env python3
# serve.py — serve the webapp/ over HTTPS (self-signed) so getUserMedia works
# on phones/tablets on your LAN. On the same machine, http://localhost also
# works as a secure context, but other devices need HTTPS.
#
#   python serve.py                 # HTTPS on 0.0.0.0:8443 (self-signed)
#   python serve.py --http          # plain HTTP (localhost only)
#   python serve.py --port 9000
#
# Self-signed certs trigger a one-time browser warning ("Advanced -> proceed").
# After you proceed, the origin is a secure context and mic capture works.

import argparse
import http.server
import ipaddress
import json
import os
import socket
import ssl
import sys
from urllib.parse import urlparse, parse_qs

CAPTURES_DIR = None  # set in main() relative to project root
MAX_UPLOAD = 300 * 1024 * 1024  # 300 MB cap

HERE = os.path.dirname(os.path.abspath(__file__))
WEBROOT = os.path.join(HERE, "webapp")
CERT = os.path.join(HERE, "config", "dev_cert.pem")
KEY = os.path.join(HERE, "config", "dev_key.pem")


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def ensure_cert():
    if os.path.exists(CERT) and os.path.exists(KEY):
        return
    print("[*] Generating self-signed certificate…")
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    ip = lan_ip()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ip)])
    san = x509.SubjectAlternativeName([
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address(ip)),
    ])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=825))
        .add_extension(san, critical=False)
        .sign(key, hashes.SHA256())
    )
    os.makedirs(os.path.dirname(CERT), exist_ok=True)
    with open(KEY, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
    with open(CERT, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    print(f"[+] Cert created for localhost / 127.0.0.1 / {ip}")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEBROOT, **kw)

    def end_headers(self):
        # AudioWorklet + secure-context friendliness; no caching during dev
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # Save a recording uploaded by the browser into data/captures/.
        if urlparse(self.path).path != "/upload":
            self._json(404, {"ok": False, "error": "not found"})
            return

        q = parse_qs(urlparse(self.path).query)
        raw = q.get("name", ["recording.wav"])[0]
        # sanitize: basename only, safe chars, force .wav
        name = os.path.basename(raw)
        name = "".join(c for c in name if c.isalnum() or c in "._-") or "recording"
        if not name.lower().endswith(".wav"):
            name += ".wav"

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_UPLOAD:
            self._json(413, {"ok": False, "error": f"bad size {length}"})
            return

        os.makedirs(CAPTURES_DIR, exist_ok=True)
        dest = os.path.join(CAPTURES_DIR, name)
        remaining = length
        try:
            with open(dest, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})
            return

        rel = os.path.relpath(dest, HERE)
        sys.stderr.write(f"    [upload] saved {rel} ({length} bytes)\n")
        self._json(200, {"ok": True, "path": rel.replace(os.sep, "/"), "bytes": length})

    def log_message(self, fmt, *args):
        sys.stderr.write("    " + (fmt % args) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--http", action="store_true", help="plain HTTP (localhost only)")
    args = ap.parse_args()

    global CAPTURES_DIR
    CAPTURES_DIR = os.path.join(HERE, "data", "captures")

    if not os.path.isdir(WEBROOT):
        print(f"[-] webapp/ not found at {WEBROOT}")
        sys.exit(1)

    port = args.port or (8000 if args.http else 8443)
    ip = lan_ip()
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)

    if args.http:
        print(f"[+] HTTP  (localhost only): http://localhost:{port}")
        print("    Note: other devices need HTTPS — run without --http.")
    else:
        ensure_cert()
        sslctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sslctx.load_cert_chain(CERT, KEY)
        httpd.socket = sslctx.wrap_socket(httpd.socket, server_side=True)
        print(f"[+] HTTPS on this machine:  https://localhost:{port}")
        print(f"[+] HTTPS from your phone:  https://{ip}:{port}")
        print("    (Self-signed: accept the one-time browser warning to proceed.)")
    print("[*] Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Stopped.")


if __name__ == "__main__":
    main()
