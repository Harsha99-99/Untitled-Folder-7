// monitor.js — operator dashboard: list live devices, listen to one live.
import { VoiceDSP } from './dsp.js';

// Bump on every playback-affecting change. Shown in the header and the log so
// we can tell at a glance whether a device is running the current code or a
// cached older build.
const BUILD = 'build 2026-09-01c · adaptive-buffer';

const $ = (id) => document.getElementById(id);
const log = (m) => {
  const el = $('log'); const ts = new Date().toLocaleTimeString();
  el.textContent += `${ts}  ${m}\n`; el.scrollTop = el.scrollHeight;
};

// Credential used for the WebSocket and the /api calls. Either a Supabase
// access token from sign-in, or a legacy ?token= shared secret.
let token = new URLSearchParams(location.search).get('token') || '';
let cfg = { supabaseUrl: null, anonKey: null, authRequired: true };
const SESSION_KEY = 'hub.session';

async function loadConfig() {
  try { cfg = await (await fetch('/api/config')).json(); } catch { /* keep defaults */ }
}

function saveSession(s) {
  try { localStorage.setItem(SESSION_KEY, JSON.stringify(s)); } catch {}
}
function readSession() {
  try { return JSON.parse(localStorage.getItem(SESSION_KEY) || 'null'); } catch { return null; }
}
function clearSession() {
  try { localStorage.removeItem(SESSION_KEY); } catch {}
}

async function signIn(email, password) {
  if (!cfg.supabaseUrl || !cfg.anonKey) throw new Error('Sign-in is not configured on this hub.');
  const r = await fetch(`${cfg.supabaseUrl.replace(/\/+$/, '')}/auth/v1/token?grant_type=password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', apikey: cfg.anonKey },
    body: JSON.stringify({ email, password }),
  });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error_description || d.msg || d.error || 'Sign-in failed.');
  saveSession(d);
  token = d.access_token;
  return d;
}

// Resolve a usable credential before connecting. Returns true when we may go on.
async function ensureAuth() {
  await loadConfig();
  if (!cfg.authRequired) return true;      // hub is open
  if (token) return true;                  // ?token= shared secret still works
  const s = readSession();
  if (s && s.access_token) { token = s.access_token; return true; }
  return false;
}

function showLogin(msg) {
  const box = $('login');
  if (box) box.hidden = false;
  if (msg) $('loginErr').textContent = msg;
}
let ws = null;
let devices = [];
let recording = new Set();   // sids the hub is recording server-side
let currentId = null;

let ctx = null, player = null, gain = null, analyser = null;
let playSR = 48000;

// Per-listener voice enhancement. The wire carries RAW audio (the hub is a
// pure relay now), so enhancement happens here at playback — the desktop
// console used to do this server-side.
const dsp = new VoiceDSP(playSR);

// ---- WebSocket to hub -----------------------------------------------------
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws?role=listener&token=${encodeURIComponent(token)}`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => { $('status').textContent = 'connected'; log('[+] Connected to hub.'); };
  ws.onclose = (e) => {
    $('status').textContent = 'disconnected';
    if (e.code === 4003) {
      log('[-] Not authorized.');
      clearSession();
      showLogin('Session expired or invalid — sign in again.');
      return;                    // don't reconnect against a rejected credential
    }
    else { log('[-] Disconnected. Reconnecting in 2s…'); setTimeout(connect, 2000); }
  };
  ws.onerror = () => log('[-] WebSocket error.');
  ws.onmessage = onMessage;
}

function onMessage(ev) {
  if (typeof ev.data === 'string') {
    const m = JSON.parse(ev.data);
    if (m.type === 'devices') { devices = m.list; renderDevices(); }
    else if (m.type === 'recording') {
      const was = recording;
      recording = new Set(m.ids || []);
      renderDevices();
      // A device that just stopped has flushed its tail — pick up the new file.
      if (was.size > recording.size) setTimeout(loadRecordings, 1500);
    }
    else if (m.type === 'subscribed') {
      playSR = m.sr || 48000;
      dsp.setSampleRate(playSR);
      dsp.reset();          // don't carry one device's AGC/gate state into another
      log(`[+] Listening to "${m.name}" @ ${playSR} Hz.`);
      startPlayback(playSR);
    }
  } else {
    // binary PCM (Int16) from the subscribed device
    if (!player) return;
    const i16 = new Int16Array(ev.data);
    const raw = new Float32Array(i16.length);
    for (let i = 0; i < i16.length; i++) raw[i] = i16[i] / 32768;

    const f32 = dsp.process(raw);

    // Meter and waveform show what you actually hear, post-enhancement.
    let peak = 0, sum = 0;
    for (let i = 0; i < f32.length; i++) { const v = f32[i]; const a = Math.abs(v); if (a > peak) peak = a; sum += v * v; }
    lastRms = Math.sqrt(sum / Math.max(1, f32.length)); lastPeak = peak;

    const shown = new Int16Array(f32.length);
    for (let i = 0; i < f32.length; i++) shown[i] = Math.max(-1, Math.min(1, f32[i])) * 32767;
    pushWave(shown);

    const outBuf = new Float32Array(f32);
    player.port.postMessage({ type: 'pcm', data: outBuf }, [outBuf.buffer]);
  }
}

// ---- recordings -----------------------------------------------------------
// The bucket is private, so the page never touches Supabase directly: the
// Worker proxies the listing and mints short-lived signed URLs, reusing the
// same token that gates listening.
const fmtBytes = (b) => (b >= 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.round(b / 1024) + ' KB');

async function loadRecordings() {
  const box = $('recordings');
  box.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const r = await fetch(`/api/recordings?token=${encodeURIComponent(token)}`);
    const data = await r.json();
    if (!r.ok) {
      box.innerHTML = `<div class="empty">${escapeHtml(data.hint || data.error || 'Could not load recordings.')}</div>`;
      return;
    }
    renderRecordings(data.items || []);
  } catch (e) {
    box.innerHTML = `<div class="empty">Could not reach the hub: ${escapeHtml(String(e))}</div>`;
  }
}

function renderRecordings(items) {
  const box = $('recordings');
  if (!items.length) {
    box.innerHTML = '<div class="empty">No recordings yet. Press Record on a device above — the hub keeps writing even if you close this page.</div>';
    return;
  }
  box.innerHTML = '';
  for (const it of items) {
    const row = document.createElement('div');
    row.className = 'device';
    const when = new Date(it.started_at).toLocaleString();
    row.innerHTML = `
      <div class="dinfo">
        <div class="dname">${escapeHtml(it.device_name || 'device')}</div>
        <div class="dmeta mono">${escapeHtml(when)} · ${fmtBytes(it.bytes || 0)} · ${it.sample_rate || '?'} Hz</div>
      </div>`;
    const play = document.createElement('button');
    play.textContent = 'Play';
    play.onclick = () => playRecording(it.storage_key, play);
    row.appendChild(play);
    const dl = document.createElement('button');
    dl.textContent = 'Download';
    dl.className = 'ghost';
    dl.onclick = () => downloadRecording(it.storage_key, dl);
    row.appendChild(dl);
    box.appendChild(row);
  }
}

async function signedUrl(key) {
  const r = await fetch(`/api/recording-url?token=${encodeURIComponent(token)}&key=${encodeURIComponent(key)}`);
  const d = await r.json();
  if (!r.ok || !d.url) throw new Error(d.error || 'could not sign URL');
  return d.url;
}

async function playRecording(key, btn) {
  const prev = btn.textContent;
  btn.textContent = '…';
  try {
    const url = await signedUrl(key);
    const el = $('recPlayer');
    el.src = url;
    el.style.display = 'block';
    await el.play().catch(() => {});   // autoplay may be blocked; controls still work
    log(`[+] Playing ${key}`);
  } catch (e) {
    log('[-] ' + e.message);
  } finally {
    btn.textContent = prev;
  }
}

async function downloadRecording(key, btn) {
  const prev = btn.textContent;
  btn.textContent = '…';
  try {
    const url = await signedUrl(key);
    const a = document.createElement('a');
    a.href = url;
    a.download = key.split('/').pop() || 'recording.wav';
    document.body.appendChild(a);
    a.click();
    a.remove();
  } catch (e) {
    log('[-] ' + e.message);
  } finally {
    btn.textContent = prev;
  }
}

// ---- device list UI -------------------------------------------------------
function renderDevices() {
  const box = $('devices');
  if (!devices.length) { box.innerHTML = '<div class="empty">No devices streaming yet. Open the sender page on a device and turn on “Broadcast”.</div>'; return; }
  box.innerHTML = '';
  for (const d of devices) {
    const row = document.createElement('div');
    row.className = 'device' + (d.id === currentId ? ' active' : '');
    const db = d.level && d.level.rms > 1e-6 ? (20 * Math.log10(d.level.rms)).toFixed(0) : '-∞';
    const pct = Math.max(0, Math.min(100, ((+db) + 60) / 60 * 100)) || 0;
    row.innerHTML = `
      <div class="dinfo">
        <div class="dname">${escapeHtml(d.name)}</div>
        <div class="dmeta mono">#${d.id} · ${d.sr} Hz · ${db} dBFS</div>
        <div class="dbar"><div style="width:${pct}%"></div></div>
      </div>`;
    const btn = document.createElement('button');
    btn.textContent = d.id === currentId ? 'Listening' : 'Listen';
    btn.className = d.id === currentId ? 'primary' : '';
    btn.onclick = () => listen(d.id);
    row.appendChild(btn);

    // Server-side recording: the hub keeps writing even if this page closes.
    const rec = document.createElement('button');
    const on = recording.has(d.id);
    rec.textContent = on ? '● REC' : 'Record';
    rec.className = on ? 'primary' : 'ghost';
    rec.title = on
      ? 'Recording on the hub — continues if you close this page. Click to stop.'
      : 'Record this device on the hub (not in this browser).';
    rec.onclick = () => toggleRecord(d.id);
    row.appendChild(rec);

    box.appendChild(row);
  }
}

function toggleRecord(id) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const on = !recording.has(id);
  ws.send(JSON.stringify({ type: 'record', id, on }));
  log(on ? `[*] Recording device #${id} on the hub…` : `[i] Stopping recording for #${id}.`);
}

function listen(id) {
  currentId = id;
  if (player) player.port.postMessage({ type: 'flush' });
  ws.send(JSON.stringify({ type: 'subscribe', id }));
  $('stopBtn').disabled = false;
  renderDevices();
}

function stopListening() {
  currentId = null;
  ws.send(JSON.stringify({ type: 'subscribe', id: -1 }));
  if (player) player.port.postMessage({ type: 'flush' });
  $('stopBtn').disabled = true;
  $('meterLbl').textContent = '— idle —';
  renderDevices();
}

// ---- playback -------------------------------------------------------------
// The context runs at the OUTPUT device's native rate — never pinned to the
// sender's rate. Pinning forced the whole graph to be resampled against the
// output clock, which a Bluetooth sink (its own drifting clock, bursty
// delivery) fights, glitching the audio. The worklet resamples the incoming
// stream to the context rate and drift-corrects the buffer instead.
let lastUnderruns = 0;
async function startPlayback(sr) {
  if (!ctx) {
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    await ctx.audioWorklet.addModule('play-worklet.js');
    player = new AudioWorkletNode(ctx, 'player', { numberOfInputs: 0, outputChannelCount: [2] });
    // The worklet reports buffer health; surface starvation and the auto-tuned
    // target so a stuttering output is visible instead of a mystery.
    player.port.onmessage = (e) => {
      const m = e.data;
      if (m.type !== 'stats') return;
      if (m.underruns > lastUnderruns) {
        log(`[!] audio underran (${m.underruns} total) — buffer grew to ${m.targetMs} ms to cope.`);
        lastUnderruns = m.underruns;
      }
    };
    gain = ctx.createGain(); gain.gain.value = (+$('vol').value) / 100;
    analyser = ctx.createAnalyser(); analyser.fftSize = 1024;
    player.connect(analyser); analyser.connect(gain); gain.connect(ctx.destination);
    requestAnimationFrame(draw);
  }
  lastUnderruns = 0;
  // Buffer starts at 300 ms and auto-grows on underrun, so a bursty Bluetooth
  // sink (AirPods) converges on a depth that holds. Log the output path so a
  // stutter can be tied to the device/latency rather than guessed at.
  const lat = (ctx.baseLatency ? Math.round(ctx.baseLatency * 1000) : 0)
    + (ctx.outputLatency ? '+' + Math.round(ctx.outputLatency * 1000) : '');
  log(`[i] output @ ${ctx.sampleRate} Hz, latency ${lat} ms; stream ${sr} Hz (${BUILD}).`);
  player.port.postMessage({ type: 'config', inRate: sr, targetMs: 300, maxMs: 2000 });
  player.port.postMessage({ type: 'flush' });
  if (ctx.state === 'suspended') await ctx.resume();
}

// ---- meter + waveform -----------------------------------------------------
let lastRms = 0, lastPeak = 0;
const waveBuf = new Float32Array(900);
let waveW = 0;
function pushWave(i16) {
  const step = Math.max(1, Math.floor(i16.length / 60));
  for (let i = 0; i < i16.length; i += step) {
    waveBuf[waveW] = i16[i] / 32768; waveW = (waveW + 1) % waveBuf.length;
  }
}
function draw() {
  const cv = $('wave'), g = cv.getContext('2d'); const w = cv.width, h = cv.height;
  g.clearRect(0, 0, w, h);
  g.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue('--accent') || '#4fd1c5';
  g.beginPath();
  for (let i = 0; i < w; i++) {
    const idx = (waveW + i) % waveBuf.length;
    const y = (0.5 - waveBuf[idx] * 0.9) * h;
    i ? g.lineTo(i, y) : g.moveTo(i, y);
  }
  g.stroke();
  if (currentId != null) {
    const db = lastRms > 1e-6 ? 20 * Math.log10(lastRms) : -120;
    $('meterFill').style.width = Math.max(0, Math.min(100, (db + 60) / 60 * 100)) + '%';
    $('meterLbl').textContent = `${db.toFixed(1)} dBFS  peak ${lastPeak.toFixed(2)}`;
  }
  requestAnimationFrame(draw);
}

function escapeHtml(s) { return (s || '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

// ---- wiring ---------------------------------------------------------------
window.addEventListener('DOMContentLoaded', () => {
  const b = $('build'); if (b) b.textContent = BUILD;
  log(`[i] ${BUILD}`);
  if (!token) { log('[-] No token in URL. Open the monitor link printed by the hub (…/monitor.html?token=…).'); }
  $('vol').oninput = (e) => { if (gain) gain.gain.value = (+e.target.value) / 100; };

  const bindDsp = (id, key) => {
    const el = $(id);
    if (!el) return;
    el.onchange = () => { dsp[key] = el.checked; dsp.reset(); };
    dsp[key] = el.checked;
  };
  bindDsp('dspOn', 'enabled');
  bindDsp('dspHp', 'highpass');
  bindDsp('dspAgc', 'agc');
  bindDsp('dspGate', 'gate');

  $('recRefresh').onclick = loadRecordings;

  $('loginForm').onsubmit = async (e) => {
    e.preventDefault();
    const btn = $('loginBtn');
    btn.disabled = true; $('loginErr').textContent = '';
    try {
      await signIn($('email').value.trim(), $('password').value);
      $('login').hidden = true;
      $('signOut').hidden = false;
      start();
    } catch (err) {
      $('loginErr').textContent = err.message;
    } finally {
      btn.disabled = false;
    }
  };

  $('signOut').onclick = () => {
    clearSession();
    location.reload();
  };

  $('stopBtn').onclick = stopListening;
  renderDevices();

  // Nothing connects until we hold a credential — otherwise the socket just
  // gets closed with 4003 and the page looks broken instead of asking to sign in.
  ensureAuth().then((ok) => {
    if (ok) {
      if (readSession()) $('signOut').hidden = false;
      start();
    } else {
      showLogin('');
    }
  });
});

function start() {
  connect();
  loadRecordings();
  log('Tip: browsers need a user gesture to start audio — click a device’s Listen button.');
}
