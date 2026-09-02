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
  // Safe to hand to the browser (it is RLS-scoped and designed to be public).
  // Needed for email/password sign-in on the monitor.
  SUPABASE_ANON_KEY?: string;
  // Hostname that serves the admin monitor. Every other hostname is the
  // public broadcast surface. Override with a var if the domain changes.
  ADMIN_HOST?: string;
}

// Recordings live in a PRIVATE Supabase bucket, so the browser cannot read
// them directly. Rather than shipping a Supabase key to the page, the Worker
// proxies both calls with the service-role key it already holds, gated by the
// same HUB_TOKEN that gates listening.
// The deployment is split across two hostnames with different trust levels:
//
//   listen.<domain>  public. Anyone may open it and broadcast their mic.
//                    The monitor page, the API and the listener role are all
//                    refused here, so a leaked credential is not enough to
//                    listen from the public surface.
//   hub.<domain>     admin. Sign-in required to listen or read recordings.
//
// Serving both from one Worker keeps a single deployment and one relay.
function isAdminHost(url: URL, env: Env): boolean {
  const admin = (env.ADMIN_HOST || "hub.thesaratech.com").toLowerCase();
  return url.hostname.toLowerCase() === admin;
}

// Authorize a listener/monitor request. Two accepted credentials:
//
//   1. A Supabase access token (JWT) from email/password sign-in — the real
//      mechanism. Verified by asking Supabase who the token belongs to, so a
//      revoked or expired session stops working immediately.
//   2. HUB_TOKEN, the original shared secret. Kept so the move to accounts
//      does not lock anyone out mid-transition; once sign-in works, remove the
//      HUB_TOKEN secret and only real accounts are accepted.
//
// With neither configured the hub is open, matching the previous behaviour.
async function authorize(url: URL, env: Env): Promise<boolean> {
  const presented = url.searchParams.get("token") || "";

  const authConfigured = Boolean(env.HUB_TOKEN) || Boolean(env.SUPABASE_ANON_KEY && env.SUPABASE_URL);
  if (!authConfigured) return true;

  if (env.HUB_TOKEN && presented === env.HUB_TOKEN) return true;
  if (!presented) return false;

  if (env.SUPABASE_URL && env.SUPABASE_ANON_KEY) {
    try {
      const base = env.SUPABASE_URL.replace(/\/+$/, "");
      const r = await fetch(`${base}/auth/v1/user`, {
        headers: {
          Authorization: `Bearer ${presented}`,
          apikey: env.SUPABASE_ANON_KEY,
        },
      });
      return r.ok;
    } catch {
      return false;
    }
  }
  return false;
}

function supaReady(env: Env): boolean {
  return Boolean(env.SUPABASE_URL && env.SUPABASE_SERVICE_ROLE_KEY);
}

const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });

// GET /api/recordings — most recent sessions, newest first.
async function listRecordings(env: Env): Promise<Response> {
  const base = (env.SUPABASE_URL as string).replace(/\/+$/, "");
  const key = env.SUPABASE_SERVICE_ROLE_KEY as string;
  const q = "select=id,device_name,started_at,ended_at,storage_key,bytes,sample_rate"
    + "&order=started_at.desc&limit=200";
  const r = await fetch(`${base}/rest/v1/sessions?${q}`, {
    headers: { Authorization: `Bearer ${key}`, apikey: key },
  });
  if (!r.ok) return json({ error: `supabase ${r.status}`, detail: await r.text() }, 502);
  return json({ items: await r.json() });
}

// GET /api/recording-url?key=… — short-lived signed URL for one object.
async function signRecording(env: Env, storageKey: string): Promise<Response> {
  const base = (env.SUPABASE_URL as string).replace(/\/+$/, "");
  const key = env.SUPABASE_SERVICE_ROLE_KEY as string;
  const bucket = env.SUPABASE_RECORDINGS_BUCKET || "recordings";
  const r = await fetch(`${base}/storage/v1/object/sign/${bucket}/${storageKey}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${key}`,
      apikey: key,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ expiresIn: 3600 }),
  });
  if (!r.ok) return json({ error: `sign failed ${r.status}`, detail: await r.text() }, 502);
  const body = (await r.json()) as { signedURL?: string };
  if (!body.signedURL) return json({ error: "no signedURL in response" }, 502);
  return json({ url: `${base}/storage/v1${body.signedURL}` });
}

// POST /api/recording-delete?key=… — remove one recording: delete the stored
// object AND its index row. Same service-role key and token gate as the rest.
// A missing object (404) is treated as already gone so the index row is still
// cleaned up. storage_key values are sanitised at write time (recorder.ts) to
// [A-Za-z0-9._-] plus "/", so they are safe to interpolate into the path.
async function deleteRecording(env: Env, storageKey: string): Promise<Response> {
  const base = (env.SUPABASE_URL as string).replace(/\/+$/, "");
  const key = env.SUPABASE_SERVICE_ROLE_KEY as string;
  const bucket = env.SUPABASE_RECORDINGS_BUCKET || "recordings";

  const obj = await fetch(`${base}/storage/v1/object/${bucket}/${storageKey}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${key}`, apikey: key },
  });
  if (!obj.ok && obj.status !== 404) {
    return json({ error: `storage delete failed ${obj.status}`, detail: await obj.text() }, 502);
  }

  const row = await fetch(
    `${base}/rest/v1/sessions?storage_key=eq.${encodeURIComponent(storageKey)}`,
    {
      method: "DELETE",
      headers: { Authorization: `Bearer ${key}`, apikey: key, Prefer: "return=minimal" },
    },
  );
  if (!row.ok) return json({ error: `index delete failed ${row.status}`, detail: await row.text() }, 502);
  return json({ ok: true });
}

// POST /api/recordings-clear — delete EVERY recording: all stored objects and
// all index rows. Same gate as the rest. Objects are removed in one bulk call
// per 100 keys; then the whole sessions table is emptied.
async function clearRecordings(env: Env): Promise<Response> {
  const base = (env.SUPABASE_URL as string).replace(/\/+$/, "");
  const key = env.SUPABASE_SERVICE_ROLE_KEY as string;
  const bucket = env.SUPABASE_RECORDINGS_BUCKET || "recordings";
  const auth = { Authorization: `Bearer ${key}`, apikey: key };

  const list = await fetch(`${base}/rest/v1/sessions?select=storage_key`, { headers: auth });
  if (!list.ok) return json({ error: `list failed ${list.status}`, detail: await list.text() }, 502);
  const rows = (await list.json()) as Array<{ storage_key?: string }>;
  const keys = rows.map((r) => r.storage_key).filter((k): k is string => Boolean(k));

  for (let i = 0; i < keys.length; i += 100) {
    const del = await fetch(`${base}/storage/v1/object/${bucket}`, {
      method: "DELETE",
      headers: { ...auth, "Content-Type": "application/json" },
      body: JSON.stringify({ prefixes: keys.slice(i, i + 100) }),
    });
    if (!del.ok && del.status !== 404) {
      return json({ error: `storage bulk delete failed ${del.status}`, detail: await del.text() }, 502);
    }
  }

  // PostgREST refuses an unfiltered DELETE; `id=not.is.null` matches every row.
  const rowsDel = await fetch(`${base}/rest/v1/sessions?id=not.is.null`, {
    method: "DELETE",
    headers: { ...auth, Prefer: "return=minimal" },
  });
  if (!rowsDel.ok) return json({ error: `index clear failed ${rowsDel.status}`, detail: await rowsDel.text() }, 502);
  return json({ ok: true, deleted: keys.length });
}

// Serve a static asset, but force the page and its code to REVALIDATE. iOS
// Safari (and others) cache the HTML and, worse, the AudioWorklet module very
// hard, so a redeployed fix can sit unused on a listener's device until they
// manually clear website data. `no-cache` doesn't disable caching — ASSETS
// still answers with an ETag, so an unchanged file 304s cheaply — it just
// guarantees the browser checks for a newer version every load. Non-code
// assets keep whatever caching ASSETS assigned.
async function serveAsset(req: Request, env: Env): Promise<Response> {
  const res = await env.ASSETS.fetch(req);
  const p = new URL(req.url).pathname.toLowerCase();
  const revalidate = p === "/" || p.endsWith(".html") || p.endsWith(".js") || p.endsWith(".css");
  if (!revalidate) return res;
  const headers = new Headers(res.headers);
  headers.set("Cache-Control", "no-cache");
  return new Response(res.body, { status: res.status, statusText: res.statusText, headers });
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    const admin = isAdminHost(url, env);

    // ---- public broadcast host: sender surface only --------------------
    if (!admin) {
      const p = url.pathname.toLowerCase();
      if (p.startsWith("/api/") || p.startsWith("/monitor")) {
        return new Response("Not found", { status: 404 });
      }
      if (url.pathname === "/ws") {
        if (url.searchParams.get("role") !== "sender") {
          // Refuse the listener role outright here — not an auth failure, this
          // host simply does not do listening.
          const pair = new WebSocketPair();
          pair[1].accept();
          pair[1].close(4004, "listening is not available on this host");
          return new Response(null, { status: 101, webSocket: pair[0] });
        }
        const id = env.HUB.idFromName("global");
        return env.HUB.get(id).fetch(req);
      }
      return serveAsset(req, env);
    }

    // ---- admin host ----------------------------------------------------
    // Land straight on the monitor rather than the broadcast page.
    if (url.pathname === "/") {
      return Response.redirect(new URL("/monitor.html", url).toString(), 302);
    }

    if (url.pathname === "/api/config") {
      // Public on purpose: the anon key is meant to reach the browser. Tells
      // the page whether sign-in is available and where to send it.
      return json({
        supabaseUrl: env.SUPABASE_URL || null,
        anonKey: env.SUPABASE_ANON_KEY || null,
        authRequired: Boolean(env.HUB_TOKEN) || Boolean(env.SUPABASE_ANON_KEY && env.SUPABASE_URL),
      });
    }

    if (url.pathname.startsWith("/api/")) {
      if (!(await authorize(url, env))) return json({ error: "unauthorized" }, 403);
      if (!supaReady(env)) {
        return json({ error: "recording storage not configured",
                      hint: "set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY as Worker secrets" }, 503);
      }
      if (url.pathname === "/api/recordings") return listRecordings(env);
      if (url.pathname === "/api/recording-url") {
        const k = url.searchParams.get("key");
        if (!k) return json({ error: "missing key" }, 400);
        return signRecording(env, k);
      }
      if (url.pathname === "/api/recording-delete") {
        // Destructive: POST only, so a stray GET or a prefetch can't wipe a file.
        if (req.method !== "POST") return json({ error: "method not allowed" }, 405);
        const k = url.searchParams.get("key");
        if (!k) return json({ error: "missing key" }, 400);
        return deleteRecording(env, k);
      }
      if (url.pathname === "/api/recordings-clear") {
        if (req.method !== "POST") return json({ error: "method not allowed" }, 405);
        return clearRecordings(env);
      }
      return json({ error: "not found" }, 404);
    }

    if (url.pathname === "/ws") {
      const role = url.searchParams.get("role");

      // Gate listeners on the shared token, exactly like hub.py. The client
      // (monitor.js) expects close code 4003 on a bad token, so reject by
      // opening the socket and closing it with that code.
      if (role === "listener") {
        if (!(await authorize(url, env))) {
          const pair = new WebSocketPair();
          pair[1].accept();
          pair[1].close(4003, "unauthorized");
          return new Response(null, { status: 101, webSocket: pair[0] });
        }
      }

      // One global hub so all senders and listeners share the same instance.
      const id = env.HUB.idFromName("global");
      return env.HUB.get(id).fetch(req);
    }

    // Everything else is a static asset (index.html, monitor.html, JS, CSS).
    return serveAsset(req, env);
  },
};

export { AudioHub };
