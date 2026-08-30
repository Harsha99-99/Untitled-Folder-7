// worker/index.ts — entrypoint. Serves the static sender/monitor pages and
// routes WebSocket upgrades to the AudioHub Durable Object.
//
// Everything that is not /ws is a static file from webapp/ (served by the
// ASSETS binding). /ws?role=sender|listener is upgraded and forwarded to the
// single global hub instance. Listeners are token-gated when HUB_TOKEN is set;
// leave it unset only for a quick open test, then set it (see wrangler.toml).

import { AudioHub } from "./hub";

interface Env {
  HUB: DurableObjectNamespace;
  ASSETS: Fetcher;
  HUB_TOKEN?: string;
  // Recording target. Set as encrypted secrets, never in wrangler.toml:
  //   npx wrangler secret put SUPABASE_URL
  //   npx wrangler secret put SUPABASE_SERVICE_ROLE_KEY
  // Without these the hub still relays; it just does not record.
  SUPABASE_URL?: string;
  SUPABASE_SERVICE_ROLE_KEY?: string;
  SUPABASE_RECORDINGS_BUCKET?: string;
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);

    if (url.pathname === "/ws") {
      const role = url.searchParams.get("role");

      // Gate listeners on the shared token, exactly like hub.py. The client
      // (monitor.js) expects close code 4003 on a bad token, so reject by
      // opening the socket and closing it with that code.
      if (role === "listener" && env.HUB_TOKEN) {
        if (url.searchParams.get("token") !== env.HUB_TOKEN) {
          const pair = new WebSocketPair();
          pair[1].accept();
          pair[1].close(4003, "bad token");
          return new Response(null, { status: 101, webSocket: pair[0] });
        }
      }

      // One global hub so all senders and listeners share the same instance.
      const id = env.HUB.idFromName("global");
      return env.HUB.get(id).fetch(req);
    }

    // Everything else is a static asset (index.html, monitor.html, JS, CSS).
    return env.ASSETS.fetch(req);
  },
};

export { AudioHub };
