// app.js — sender page for remote devices.
// The mic remains idle until the user taps Start broadcast, and only then does
// the browser connect to the hub and stream RAW mono Int16 PCM.

const $ = (id) => document.getElementById(id);
const log = (m) => {
  const el = $('log'); const ts = new Date().toLocaleTimeString();
  el.textContent += `${ts}  ${m}\n`; el.scrollTop = el.scrollHeight;
};

let ctx = null, stream = null, srcNode = null, capNode = null, muteGain = null;
let ws = null, sampleRate = 48000;
let sendChunks = [], sendTimer = null, reconnectTimer = null;
let lastRms = 0, lastPeak = 0;
let openedAt = 0, fastFails = 0;
let lastAudioAt = 0, stallTimer = null, wakeLock = null, stalled = false;
let deviceName = '';
let micId = '';            // '' = let the OS pick the default input
let broadcastActive = false;
let startupInProgress = false;

function defaultName() {
  const p = new URLSearchParams(location.search).get('name');
  if (p) return p;
  const ua = navigator.userAgent;
  let base = 'Device';
  if (/iPhone|iPad/.test(ua)) base = 'iPhone';
  else if (/Android/.test(ua)) base = 'Android';
  else if (/Windows/.test(ua)) base = 'PC';
  else if (/Mac/.test(ua)) base = 'Mac';
  return `${base}-${Math.floor(Math.random() * 900 + 100)}`;
}

function setBroadcastButtonState() {
  const startBtn = $('startBtn');
  const stopBtn = $('stopBtn');
  if (!startBtn || !stopBtn) return;
  startBtn.disabled = broadcastActive || startupInProgress;
  stopBtn.hidden = !broadcastActive;
  startBtn.textContent = broadcastActive ? 'Broadcasting…' : 'Start broadcast';
}

function stopBroadcast({ keepWaiting = false } = {}) {
  broadcastActive = false;
  openedAt = 0; fastFails = 0;
  stopWatchdog();
  releaseWake();
  sendChunks = [];
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  if (sendTimer) { clearInterval(sendTimer); sendTimer = null; }
  if (capNode) capNode.port.postMessage({ type: 'stream', on: false });
  if (ws) {
    const target = ws;
    ws = null;
    if (target.readyState === WebSocket.OPEN || target.readyState === WebSocket.CONNECTING) {
      try { target.close(); } catch {}
    }
  }
  if (!keepWaiting) {
    setStatus('Ready to broadcast', false);
    log('[i] Broadcast stopped.');
  }
  setBroadcastButtonState();
}

// ---- microphone selection ------------------------------------------------
// Browsers only reveal input LABELS once mic permission has been granted, so
// the list is deliberately re-read after getUserMedia succeeds. A Bluetooth
// headset that connects later fires 'devicechange', which refreshes it too.
function micConstraints() {
  const audio = {
    echoCancellation: false,
    noiseSuppression: false,
    autoGainControl: false,
    channelCount: { ideal: 1 },
  };
  if (micId) audio.deviceId = { exact: micId };
  return { audio };
}

function micLabel() {
  const sel = $('micSel');
  if (!sel) return 'default microphone';
  const opt = sel.options[sel.selectedIndex];
  return opt ? opt.textContent : 'default microphone';
}

async function listMics({ quiet = false } = {}) {
  const sel = $('micSel');
  if (!sel || !navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
  let devs = [];
  try {
    devs = await navigator.mediaDevices.enumerateDevices();
  } catch (e) {
    log('[-] Could not list microphones: ' + (e && e.message ? e.message : e));
    return;
  }
  const ins = devs.filter((d) => d.kind === 'audioinput');
  const keep = sel.value;
  sel.innerHTML = '';
  const def = document.createElement('option');
  def.value = '';
  def.textContent = 'Default microphone';
  sel.appendChild(def);
  ins.forEach((d, i) => {
    if (!d.deviceId || d.deviceId === 'default') return;
    const o = document.createElement('option');
    o.value = d.deviceId;
    o.textContent = d.label || `Microphone ${i + 1}`;
    sel.appendChild(o);
  });
  if (Array.prototype.some.call(sel.options, (o) => o.value === keep)) sel.value = keep;
  else { sel.value = ''; micId = ''; }
  const named = ins.filter((d) => d.label).length;
  if (!quiet) {
    log(`[i] ${ins.length} microphone(s) available`
        + (named ? '.' : ' — names appear after you allow mic access once.'));
  }
}

// Swap the live capture over to a different input without dropping the socket.
async function applyMic() {
  micId = $('micSel').value || '';
  if (!broadcastActive) {
    log(`[i] Microphone set to ${micLabel()} — used on the next broadcast.`);
    return;
  }
  try {
    const next = await navigator.mediaDevices.getUserMedia(micConstraints());
    if (srcNode) { try { srcNode.disconnect(); } catch {} }
    if (stream) stream.getTracks().forEach((t) => { try { t.stop(); } catch {} });
    stream = next;
    srcNode = ctx.createMediaStreamSource(stream);
    srcNode.connect(capNode);
    lastAudioAt = Date.now();
    log(`[+] Switched to ${micLabel()}.`);
  } catch (e) {
    log('[-] Could not switch microphone: ' + (e && e.message ? e.message : e));
    setStatus('mic switch failed', false);
  }
}

async function startBroadcast() {
  if (startupInProgress || broadcastActive) return;
  startupInProgress = true;
  setBroadcastButtonState();
  setStatus('Connecting…', false);
  log('[i] Starting broadcast…');

  try {
    if (!stream) {
      stream = await navigator.mediaDevices.getUserMedia(micConstraints());
      // labels are blank until permission lands, so refresh the list now
      await listMics({ quiet: true });
    }

    if (!ctx) {
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      sampleRate = ctx.sampleRate;
    }

    if (ctx.state === 'suspended') {
      await ctx.resume();
    }

    if (!capNode) {
      await begin();
    }

    broadcastActive = true;
    lastAudioAt = Date.now();
    startWatchdog();
    keepAwake();
    connectHub();
    setBroadcastButtonState();
  } catch (e) {
    stopBroadcast({ keepWaiting: true });
    setStatus('Microphone permission needed', false);
    log('[-] Mic permission required to broadcast: ' + (e && e.message ? e.message : e));
  } finally {
    startupInProgress = false;
    setBroadcastButtonState();
  }
}

async function begin() {
  if (!ctx || !stream) return;
  await ctx.audioWorklet.addModule('worklet.js');
  srcNode = ctx.createMediaStreamSource(stream);
  capNode = new AudioWorkletNode(ctx, 'capture', { numberOfOutputs: 1, outputChannelCount: [1] });
  capNode.port.onmessage = onWorklet;
  muteGain = ctx.createGain(); muteGain.gain.value = 0;
  srcNode.connect(capNode); capNode.connect(muteGain); muteGain.connect(ctx.destination);
  requestAnimationFrame(drawMeter);
  log(`[i] Capturing raw mic @ ${sampleRate} Hz.`);
}

// iOS Safari suspends the AudioContext when the page is backgrounded or the
// screen locks. The socket stays open, so the page keeps claiming it is
// broadcasting while the mic has actually stopped — and it does not always come
// back on its own. Watch for the stall, say so, and try to recover.
async function keepAwake() {
  try {
    if ('wakeLock' in navigator && !wakeLock) {
      wakeLock = await navigator.wakeLock.request('screen');
      wakeLock.addEventListener('release', () => { wakeLock = null; });
    }
  } catch {}
}

function releaseWake() {
  try { if (wakeLock) { wakeLock.release(); wakeLock = null; } } catch {}
}

async function resumeAudio(why) {
  if (!ctx) return;
  try {
    if (ctx.state === 'suspended') {
      await ctx.resume();
      log(`[i] AudioContext resumed (${why}).`);
    }
  } catch (e) {
    log('[-] Could not resume audio: ' + (e && e.message ? e.message : e));
  }
}

function startWatchdog() {
  if (stallTimer) clearInterval(stallTimer);
  stallTimer = setInterval(() => {
    if (!broadcastActive) return;
    const gap = Date.now() - lastAudioAt;
    if (gap > 1500 && !stalled) {
      stalled = true;
      setStatus('microphone stalled', false);
      log(`[-] No microphone audio for ${(gap / 1000).toFixed(1)}s (state: ${ctx ? ctx.state : 'none'}). Trying to resume…`);
    }
    if (stalled) { resumeAudio('watchdog'); keepAwake(); }
  }, 1000);
}

function stopWatchdog() {
  if (stallTimer) { clearInterval(stallTimer); stallTimer = null; }
  stalled = false;
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && broadcastActive) { resumeAudio('page visible'); keepAwake(); }
});

function connectHub() {
  if (!broadcastActive) return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  if (ws && ws.readyState === WebSocket.OPEN) return;
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }

  const socket = new WebSocket(`${proto}://${location.host}/ws?role=sender&name=${encodeURIComponent(deviceName)}`);
  socket.binaryType = 'arraybuffer';
  ws = socket;

  socket.onopen = () => {
    if (ws !== socket) return;
    openedAt = Date.now();
    socket.send(JSON.stringify({ type: 'hello', name: deviceName, sampleRate }));
    if (capNode) capNode.port.postMessage({ type: 'stream', on: true });
    if (sendTimer) clearInterval(sendTimer);
    // 250 ms batches: 5x fewer messages for +0.2 s latency. Message rate is
    // what a serverless relay bills on, and it's irrelevant for monitoring.
    sendTimer = setInterval(flush, 250);
    setStatus('● Broadcasting', true);
    log('[+] Connected to hub — broadcasting.');
    setBroadcastButtonState();
  };

  socket.onclose = (e) => {
    if (ws !== socket) return;
    ws = null;
    if (capNode) capNode.port.postMessage({ type: 'stream', on: false });
    if (sendTimer) { clearInterval(sendTimer); sendTimer = null; }
    if (broadcastActive) {
      // A tunnel whose origin is down still completes the handshake at the edge
      // and drops us immediately, so "connected" alone means very little. Treat
      // a socket that dies young as a failed attempt: back off instead of
      // hammering, and say what is actually wrong.
      const lived = openedAt ? Date.now() - openedAt : 0;
      openedAt = 0;
      if (lived >= 3000) fastFails = 0; else fastFails++;
      const delay = Math.min(2000 * Math.pow(2, Math.max(0, fastFails - 1)), 15000);
      setStatus('reconnecting…', false);
      log(`[i] Disconnected (code ${e.code}${e.reason ? ' ' + e.reason : ''}) after ${lived} ms.`
          + ` Retrying in ${Math.round(delay / 1000)}s…`);
      if (fastFails === 3) {
        log('[-] The hub keeps dropping us the instant we connect. The tunnel is up '
            + 'but nothing is answering behind it — start the operator console on '
            + 'the PC, then this will reconnect on its own.');
      }
      reconnectTimer = setTimeout(() => {
        if (broadcastActive) connectHub();
      }, delay);
    } else {
      setStatus('Ready to broadcast', false);
    }
  };

  socket.onerror = () => {
    if (ws !== socket) return;
    log('[-] Hub socket error.');
  };
}

function onWorklet(e) {
  const m = e.data;
  if (m.type === 'level') { lastRms = m.rms; lastPeak = m.peak; }
  else if (m.type === 'audio') {
    lastAudioAt = Date.now();
    if (stalled) { stalled = false; setStatus('● Broadcasting', true); log('[+] Microphone resumed.'); }
    const f = m.data;
    const i16 = new Int16Array(f.length);
    for (let i = 0; i < f.length; i++) { let v = f[i]; v = v < -1 ? -1 : v > 1 ? 1 : v; i16[i] = v * 32767; }
    sendChunks.push(i16);
    // Drive sending from the audio callback, not only the 250 ms timer.
    // Browsers throttle a BACKGROUNDED tab's timers to ~1/s, so a minimized
    // sender would ship audio in one-second bursts and every listener would
    // stutter once a second. The AudioWorklet keeps running in the background,
    // so flushing here once ~250 ms has accumulated keeps the stream smooth
    // even when this tab isn't in front. Same ~4/s message rate as the timer.
    if (broadcastActive && ws && ws.readyState === WebSocket.OPEN
        && queuedSamples() >= sampleRate * 0.25) {
      flush();
    }
  }
}

// Anything we cannot send right now must be DROPPED, not queued. The old code
// returned early while leaving sendChunks intact, so a stalled or reconnecting
// socket accumulated ~96 KB/s indefinitely and then dumped minutes of stale
// audio in one burst — heard as a long silence followed by a garbled catch-up.
const MAX_QUEUE_SAMPLES = 48000;      // ~1 s at 48 kHz
const MAX_BUFFERED_BYTES = 512 * 1024; // socket backlog before we shed audio

function queuedSamples() {
  let n = 0; for (const c of sendChunks) n += c.length;
  return n;
}

function trimQueue() {
  let n = queuedSamples();
  while (n > MAX_QUEUE_SAMPLES && sendChunks.length > 1) n -= sendChunks.shift().length;
}

function flush() {
  if (!sendChunks.length) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) { trimQueue(); return; }
  // Backpressure: if the socket is already behind, shed rather than pile on —
  // otherwise latency grows without bound and never recovers.
  if (ws.bufferedAmount > MAX_BUFFERED_BYTES) {
    if (!flush._warned) { log('[!] Link is congested — dropping audio to stay live.'); flush._warned = true; }
    trimQueue();
    return;
  }
  flush._warned = false;
  const n = queuedSamples();
  const merged = new Int16Array(n);
  let o = 0; for (const c of sendChunks) { merged.set(c, o); o += c.length; }
  sendChunks = [];
  try { ws.send(merged.buffer); } catch {}
}

function drawMeter() {
  const db = lastRms > 1e-6 ? 20 * Math.log10(lastRms) : -120;
  $('meterFill').style.width = Math.max(0, Math.min(100, (db + 60) / 60 * 100)) + '%';
  $('meterLbl').textContent = `${db.toFixed(0)} dBFS`;
  requestAnimationFrame(drawMeter);
}

function setStatus(text, on) {
  $('statusBig').textContent = text;
  $('dot').className = 'dot ' + (on ? 'on' : 'off');
}

window.addEventListener('DOMContentLoaded', () => {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    setStatus('Needs HTTPS (secure context)', false);
    log('[-] getUserMedia unavailable — this page must be opened over HTTPS.');
    return;
  }

  deviceName = defaultName();
  $('devName').value = deviceName;
  $('devName').addEventListener('change', () => {
    deviceName = $('devName').value || deviceName;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'hello', name: deviceName, sampleRate }));
  });

  $('startBtn').addEventListener('click', startBroadcast);
  $('stopBtn').addEventListener('click', () => stopBroadcast());
  $('micSel').addEventListener('change', applyMic);
  $('micRefresh').addEventListener('click', () => listMics());
  listMics({ quiet: true });
  // a Bluetooth headset connecting or dropping shows up here
  if (navigator.mediaDevices.addEventListener) {
    navigator.mediaDevices.addEventListener('devicechange', () => {
      log('[i] Audio devices changed — refreshing microphone list.');
      listMics({ quiet: true });
    });
  }
  setBroadcastButtonState();
  setStatus('Ready to broadcast', false);
  log('[i] Tap “Start broadcast” to stream this device’s microphone to the hub.');
});
