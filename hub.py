#!/usr/bin/env python3
# hub.py — unified HTTP + WebSocket hub for multi-device live listening.
#
# Serves the capture page (senders) and a token-gated monitor dashboard,
# relays live audio between remote devices and listeners, and exposes an
# in-process tap API so a native app (hub_app.py) can receive device audio
# directly. Everything is on ONE port so it still works through a tunnel.
#
#   python hub.py --port 8000
#
# Roles (WebSocket /ws):
#   role=sender               a remote device streaming its mic (Int16 PCM)
#   role=listener&token=...   a browser dashboard (token required)
#
# State lives on the aiohttp `app` object; HubServer runs it in a background
# thread and adds register_tap()/snapshot_devices() for in-process consumers.

import argparse
import asyncio
import json
import os
import queue
import secrets
import socket
import sys
import threading

from aiohttp import web, WSMsgType

# frozen (PyInstaller) aware paths
if getattr(sys, "frozen", False):
    BASE = os.path.dirname(sys.executable)
    RES = getattr(sys, "_MEIPASS", BASE)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
    RES = BASE

WEBROOT = os.path.join(RES, "webapp")
DEFAULT_CAPTURES = os.path.join(BASE, "data", "captures")
MAX_UPLOAD = 300 * 1024 * 1024
TAP_MAXLEN = 256  # per-tap queued chunks before we drop oldest


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# --------------------------------------------------------------------------
# request helpers (state on app)
# --------------------------------------------------------------------------
def _devices(app):
    return [
        {"id": i, "name": d["name"], "sr": d["sr"], "level": d.get("level", {"rms": 0, "peak": 0})}
        for i, d in app["senders"].items()
    ]


async def _broadcast_devices(app):
    msg = json.dumps({"type": "devices", "list": _devices(app)})
    for lw in list(app["listeners"]):
        try:
            await lw.send_str(msg)
        except Exception:
            app["listeners"].discard(lw)


# ---- static + upload ------------------------------------------------------
NOCACHE = {"Cache-Control": "no-store, no-cache, must-revalidate"}


async def index(request):
    return web.FileResponse(os.path.join(WEBROOT, "index.html"), headers=NOCACHE)


async def monitor(request):
    if request.query.get("token") != request.app["token"]:
        return web.Response(status=403, text="Forbidden: valid ?token= required (see console).")
    return web.FileResponse(os.path.join(WEBROOT, "monitor.html"), headers=NOCACHE)


async def static_handler(request):
    rel = request.match_info.get("path", "")
    safe = os.path.normpath(rel).replace("\\", "/").lstrip("/")
    if safe.startswith("..") or safe == "monitor.html":
        raise web.HTTPNotFound()
    full = os.path.join(WEBROOT, safe)
    if not os.path.isfile(full):
        raise web.HTTPNotFound()
    return web.FileResponse(full, headers=NOCACHE)


async def upload(request):
    name = os.path.basename(request.query.get("name", "recording.wav"))
    name = "".join(c for c in name if c.isalnum() or c in "._-") or "recording"
    if not name.lower().endswith(".wav"):
        name += ".wav"
    try:
        length = int(request.headers.get("Content-Length", 0))
    except ValueError:
        length = 0
    if length <= 0 or length > MAX_UPLOAD:
        return web.json_response({"ok": False, "error": f"bad size {length}"}, status=413)
    cdir = request.app["state"]["captures_dir"]
    os.makedirs(cdir, exist_ok=True)
    dest = os.path.join(cdir, name)
    with open(dest, "wb") as f:
        async for chunk in request.content.iter_chunked(65536):
            f.write(chunk)
    _log(f"    [upload] {dest} ({length} bytes)\n")
    return web.json_response({"ok": True, "path": name, "bytes": length})


def _log(msg):
    """Write a diagnostic line, but never let logging kill a connection.

    A PyInstaller --windowed build runs with no console, so the standard
    error stream is None and writing to it raises AttributeError. In
    handle_sender that fired before the try/finally, tearing the socket down
    the instant a sender connected (client sees code 1006) and leaving the
    sender registered forever - the ghost rows in the operator console."""
    try:
        stream = sys.stderr
        if stream is not None:
            stream.write(msg)
            stream.flush()
    except Exception:
        pass


# ---- websocket ------------------------------------------------------------
async def ws_handler(request):
    app = request.app
    # No server heartbeat: senders stream continuously so the link is never
    # idle, and Cloudflare tunnels don't reliably forward WS ping/pong control
    # frames — a heartbeat there causes false pong-timeouts and reconnect loops.
    ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)
    role = request.query.get("role")
    if role == "sender":
        await handle_sender(app, request, ws)
    elif role == "listener":
        if request.query.get("token") != app["token"]:
            await ws.close(code=4003, message=b"bad token")
        else:
            await handle_listener(app, ws)
    else:
        await ws.close()
    return ws


async def handle_sender(app, request, ws):
    sid = app["state"]["next_id"]
    app["state"]["next_id"] += 1
    name = request.query.get("name") or f"Device {sid}"
    app["senders"][sid] = {"name": name, "sr": 48000, "ws": ws,
                           "level": {"rms": 0, "peak": 0}, "listeners": set()}
    try:
        _log(f"    [sender+] #{sid} {name}\n")
        await _broadcast_devices(app)
        while True:
            try:
                # senders stream ~every 50 ms; silence for 25 s means it's gone
                msg = await asyncio.wait_for(ws.receive(), timeout=25)
            except asyncio.TimeoutError:
                _log(f"    [sender  ] #{sid} idle timeout\n")
                break
            if msg.type == WSMsgType.BINARY:
                # relay to browser listeners
                for lw in list(app["senders"][sid]["listeners"]):
                    try:
                        await lw.send_bytes(msg.data)
                    except Exception:
                        app["senders"][sid]["listeners"].discard(lw)
                # relay to in-process taps (native app)
                with app["lock"]:
                    taps = list(app["taps"].get(sid, ()))
                for q in taps:
                    try:
                        q.put_nowait(msg.data)
                    except queue.Full:
                        try:
                            q.get_nowait()
                            q.put_nowait(msg.data)
                        except Exception:
                            pass
            elif msg.type == WSMsgType.TEXT:
                try:
                    m = json.loads(msg.data)
                except Exception:
                    continue
                if m.get("type") == "hello":
                    app["senders"][sid]["name"] = m.get("name", name)
                    app["senders"][sid]["sr"] = int(m.get("sampleRate", 48000))
                    await _broadcast_devices(app)
                elif m.get("type") == "level":
                    app["senders"][sid]["level"] = {"rms": m.get("rms", 0), "peak": m.get("peak", 0)}
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                break
    finally:
        app["senders"].pop(sid, None)
        _log(f"    [sender-] #{sid}\n")
        await _broadcast_devices(app)


async def handle_listener(app, ws):
    app["listeners"].add(ws)
    await ws.send_str(json.dumps({"type": "devices", "list": _devices(app)}))
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    m = json.loads(msg.data)
                except Exception:
                    continue
                if m.get("type") == "subscribe":
                    tid = m.get("id")
                    for d in app["senders"].values():
                        d["listeners"].discard(ws)
                    if tid in app["senders"]:
                        app["senders"][tid]["listeners"].add(ws)
                        await ws.send_str(json.dumps({"type": "subscribed", "id": tid,
                                                      "sr": app["senders"][tid]["sr"],
                                                      "name": app["senders"][tid]["name"]}))
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
    finally:
        app["listeners"].discard(ws)
        for d in app["senders"].values():
            d["listeners"].discard(ws)


async def _periodic(app):
    try:
        while True:
            await asyncio.sleep(0.5)
            if app["listeners"] and app["senders"]:
                await _broadcast_devices(app)
    except asyncio.CancelledError:
        pass


async def _on_start(app):
    app["state"]["task"] = asyncio.create_task(_periodic(app))


async def _on_stop(app):
    t = app["state"].get("task")
    if t:
        t.cancel()


def make_app(token, captures_dir):
    app = web.Application()
    app["token"] = token
    app["senders"] = {}
    app["listeners"] = set()
    app["taps"] = {}
    app["lock"] = threading.Lock()
    # values reassigned at runtime live in a mutable holder so we never set
    # keys on the (frozen) Application after startup (deprecated in aiohttp)
    app["state"] = {"next_id": 1, "captures_dir": captures_dir}
    app.router.add_get("/", index)
    app.router.add_get("/monitor.html", monitor)
    app.router.add_post("/upload", upload)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/{path:.*}", static_handler)
    app.on_startup.append(_on_start)
    app.on_cleanup.append(_on_stop)
    return app


# --------------------------------------------------------------------------
# Embeddable server
# --------------------------------------------------------------------------
class HubServer:
    """Runs the aiohttp hub in a background thread; adds in-process tap API."""

    def __init__(self, port=8000, token=None, captures_dir=None):
        self.port = port
        self.token = token or secrets.token_urlsafe(12)
        self.captures_dir = captures_dir or DEFAULT_CAPTURES
        self.app = None
        self._thread = None
        self._loop = None
        self._runner = None
        self._ready = threading.Event()
        self._error = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(8):
            raise RuntimeError(self._error or "hub failed to start")
        if self._error:
            raise RuntimeError(self._error)

    def _run(self):
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self.app = make_app(self.token, self.captures_dir)
            self._runner = web.AppRunner(self.app)
            self._loop.run_until_complete(self._runner.setup())
            site = web.TCPSite(self._runner, "0.0.0.0", self.port)
            self._loop.run_until_complete(site.start())
            self._ready.set()
            self._loop.run_forever()
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"
            self._ready.set()
            return
        finally:
            try:
                if self._runner:
                    self._loop.run_until_complete(self._runner.cleanup())
            except Exception:
                pass

    def stop(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=3)

    def set_captures_dir(self, path):
        self.captures_dir = path
        if self.app is not None:
            self.app["state"]["captures_dir"] = path

    # --- in-process tap API (thread-safe) ---
    def register_tap(self, sid):
        q = queue.Queue(maxsize=TAP_MAXLEN)
        with self.app["lock"]:
            self.app["taps"].setdefault(sid, set()).add(q)
        return q

    def unregister_tap(self, sid, q):
        with self.app["lock"]:
            s = self.app["taps"].get(sid)
            if s:
                s.discard(q)
                if not s:
                    self.app["taps"].pop(sid, None)

    def snapshot_devices(self):
        if self.app is None:
            return []
        out = []
        for sid, d in list(self.app["senders"].items()):
            out.append({"id": sid, "name": d["name"], "sr": d["sr"],
                        "level": dict(d.get("level", {"rms": 0, "peak": 0}))})
        return out


def main():
    # Env vars are the production interface (12-factor / containers); CLI flags
    # override them for local runs. HUB_TOKEN keeps the monitor URL stable
    # across restarts — without it hub.py mints a random token each boot.
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("HUB_PORT", "8000")))
    ap.add_argument("--token", default=os.environ.get("HUB_TOKEN") or None)
    args = ap.parse_args()

    if not os.path.isdir(WEBROOT):
        print(f"[-] webapp/ not found at {WEBROOT}")
        sys.exit(1)

    hub = HubServer(port=args.port, token=args.token)
    hub.start()
    ip = lan_ip()
    print("=" * 64)
    print("  Bluetooth Audio Hub — multi-device live listening")
    print("=" * 64)
    print("  Sender page (each remote device):")
    print(f"     http://localhost:{args.port}/           (or via tunnel)")
    print("  Monitor dashboard (this PC):")
    print(f"     http://localhost:{args.port}/monitor.html?token={hub.token}")
    print(f"  LAN IP: {ip}   |   token: {hub.token}")
    print("=" * 64)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\n[*] Stopping…")
        hub.stop()


if __name__ == "__main__":
    main()
