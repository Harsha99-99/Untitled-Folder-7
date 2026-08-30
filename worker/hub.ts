// worker/hub.ts — the audio relay as a Durable Object.
//
// This is the Cloudflare-native replacement for hub.py's WebSocket relay. One
// global instance holds every sender and listener for the hub; binary PCM from
// a sender is fanned out to the listeners subscribed to it. The wire protocol
// is byte-for-byte what webapp/app.js (senders) and webapp/monitor.js
// (listeners) already speak, so neither page needs to change.
//
// Uses the Hibernatable WebSocket API (acceptWebSocket + webSocket* handlers):
// the object can be evicted from memory between messages while sockets stay
// connected, so an idle hub costs nothing. Because memory does not survive
// hibernation, all per-socket state lives in the socket's *attachment* (a small
// serialized blob) and the sender-id counter lives in Durable Object storage.

interface SenderAtt {
  role: "sender";
  sid: number;
  name: string;
  sr: number;
  level: { rms: number; peak: number };
}

interface ListenerAtt {
  role: "listener";
  sub: number | null; // sid this listener is currently hearing, or null
}

type Att = SenderAtt | ListenerAtt;

export class AudioHub {
  state: DurableObjectState;
  env: unknown;

  constructor(state: DurableObjectState, env: unknown) {
    this.state = state;
    this.env = env;
  }

  // The Worker forwards the WebSocket upgrade here with ?role=sender|listener.
  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    const role = url.searchParams.get("role");
    const pair = new WebSocketPair();
    const client = pair[0];
    const server = pair[1];

    if (role === "sender") {
      const sid = await this.nextSid();
      const name = url.searchParams.get("name") || `Device ${sid}`;
      const att: SenderAtt = { role: "sender", sid, name, sr: 48000, level: { rms: 0, peak: 0 } };
      server.serializeAttachment(att);
      this.state.acceptWebSocket(server, ["sender"]);
      this.broadcastDevices();
    } else {
      const att: ListenerAtt = { role: "listener", sub: null };
      server.serializeAttachment(att);
      this.state.acceptWebSocket(server, ["listener"]);
      // hub.py sends the device list immediately on listener connect
      server.send(JSON.stringify({ type: "devices", list: this.devices() }));
    }

    return new Response(null, { status: 101, webSocket: client });
  }

  // Monotonic sender ids, matching hub.py's integer sid. Stored so they survive
  // hibernation and never collide across reconnects.
  async nextSid(): Promise<number> {
    const cur = ((await this.state.storage.get<number>("nextId")) ?? 1) as number;
    await this.state.storage.put("nextId", cur + 1);
    return cur;
  }

  // Current device list, optionally excluding one socket (used on close, where
  // getWebSockets may still include the departing sender).
  devices(exclude?: WebSocket) {
    const out: Array<{ id: number; name: string; sr: number; level: { rms: number; peak: number } }> = [];
    for (const ws of this.state.getWebSockets("sender")) {
      if (ws === exclude) continue;
      const a = ws.deserializeAttachment() as SenderAtt | null;
      if (!a) continue;
      out.push({ id: a.sid, name: a.name, sr: a.sr, level: a.level });
    }
    return out;
  }

  broadcastDevices(exclude?: WebSocket) {
    const msg = JSON.stringify({ type: "devices", list: this.devices(exclude) });
    for (const l of this.state.getWebSockets("listener")) {
      try { l.send(msg); } catch { /* socket going away */ }
    }
  }

  async webSocketMessage(ws: WebSocket, message: ArrayBuffer | string) {
    const att = ws.deserializeAttachment() as Att | null;
    if (!att) return;

    if (att.role === "sender") {
      // Binary = raw Int16 PCM. Fan out to listeners tuned to this sender.
      if (typeof message !== "string") {
        for (const l of this.state.getWebSockets("listener")) {
          const la = l.deserializeAttachment() as ListenerAtt | null;
          if (la && la.sub === att.sid) {
            try { l.send(message); } catch { /* drop for a dead listener */ }
          }
        }
        return;
      }
      // Text control frames from the sender.
      let m: Record<string, unknown>;
      try { m = JSON.parse(message); } catch { return; }
      if (m.type === "hello") {
        att.name = (m.name as string) ?? att.name;
        att.sr = Number(m.sampleRate) || att.sr;
        ws.serializeAttachment(att);
        this.broadcastDevices();
      } else if (m.type === "level") {
        att.level = { rms: Number(m.rms) || 0, peak: Number(m.peak) || 0 };
        ws.serializeAttachment(att);
      }
      return;
    }

    // Listener control: subscribe to one device (id = -1 or unknown → none).
    if (typeof message !== "string") return;
    let m: Record<string, unknown>;
    try { m = JSON.parse(message); } catch { return; }
    if (m.type === "subscribe") {
      const id = Number(m.id);
      let target: SenderAtt | null = null;
      for (const s of this.state.getWebSockets("sender")) {
        const a = s.deserializeAttachment() as SenderAtt | null;
        if (a && a.sid === id) { target = a; break; }
      }
      att.sub = target ? id : null;
      ws.serializeAttachment(att);
      if (target) {
        ws.send(JSON.stringify({ type: "subscribed", id, sr: target.sr, name: target.name }));
      }
    }
  }

  async webSocketClose(ws: WebSocket) {
    const att = ws.deserializeAttachment() as Att | null;
    if (att && att.role === "sender") {
      // Exclude the closing socket so it drops off the list immediately.
      this.broadcastDevices(ws);
    }
  }

  async webSocketError(ws: WebSocket) {
    const att = ws.deserializeAttachment() as Att | null;
    if (att && att.role === "sender") this.broadcastDevices(ws);
  }
}
