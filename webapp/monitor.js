// monitor.js — operator dashboard: list live devices, listen to one live.
import { VoiceDSP } from './dsp.js';

const $ = (id) => document.getElementById(id);
const log = (m) => {
  const el = $('log'); const ts = new Date().toLocaleTimeString();
  el.textContent += `${ts}  ${m}\n`; el.scrollTop = el.scrollHeight;
};

const token = new URLSearchParams(location.search).get('token') || '';
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
    if (e.code === 4003) log('[-] Rejected: bad/missing token. Open the monitor URL printed by the hub.');
    else { log('[-] Disconnected. Reconnecting in 2s…'); setTimeout(connect, 2000); }
  };
  ws.onerror = () => log('[-] WebSocket error.');
  ws.onmessage = onMessage;
}

function onMessage(ev) {
  if (typeof ev.data === 'string') {
    const m = JSON.parse(ev.data);
    if (m.type === 'devices') { devices = m.list; renderDevices(); }
    else if (m.type === 'recording') { recording = new Set(m.ids || []); renderDevices(); }
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
async function startPlayback(sr) {
  if (ctx && ctx.sampleRate !== sr) { await ctx.close(); ctx = null; }
  if (!ctx) {
    ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: sr });
    await ctx.audioWorklet.addModule('play-worklet.js');
    player = new AudioWorkletNode(ctx, 'player', { numberOfInputs: 0, outputChannelCount: [2] });
    player.port.postMessage({ type: 'config', prebuffer: Math.round(sr * 0.40) }); // covers the sender's 250 ms batches
    gain = ctx.createGain(); gain.gain.value = (+$('vol').value) / 100;
    analyser = ctx.createAnalyser(); analyser.fftSize = 1024;
    player.connect(analyser); analyser.connect(gain); gain.connect(ctx.destination);
    requestAnimationFrame(draw);
  }
  if (ctx.state === 'suspended') await ctx.resume();
  player.port.postMessage({ type: 'flush' });
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
  $('stopBtn').onclick = stopListening;
  renderDevices();
  connect();
  log('Tip: browsers need a user gesture to start audio — click a device’s Listen button.');
});
