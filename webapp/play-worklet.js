// play-worklet.js — adaptive, drift-compensating jitter buffer with resampling.
//
// The sender's microphone clock and this device's output clock are never
// identical, and some sinks — a Bluetooth headset (AirPods above all) — deliver
// audio in bursts with high, variable latency. A fixed 1:1 buffer therefore
// drifts and then stutters (silence, then a catch-up burst).
//
// We resample the incoming stream to the output rate with a fractional read
// pointer and continuously nudge that ratio by a fraction of a percent (±0.4%,
// inaudible) to hold the buffered latency near a target, which absorbs the
// clock drift. Two things make it survive a genuinely bursty sink:
//   - On an underrun we rebuild a FULL cushion before resuming (not a tiny one
//     that starves again on the next burst), fading to avoid a click.
//   - Each underrun GROWS the target (×1.5, capped). A steady output stays near
//     the small default; a bursty one converges on the depth it actually needs.
//     So the buffer auto-tunes per device instead of guessing one value.
//
// Main thread posts:
//   {type:'config', inRate, targetMs, maxMs}   configure for a stream
//   {type:'pcm',    data:Float32Array}         mono samples at inRate
//   {type:'flush'}                             reset all state
// It posts back {type:'stats', queuedMs, targetMs, underruns} periodically and
// on every underrun, so the page can show what the buffer is doing.
// All queue depths are counted in INPUT-rate samples (what we queue);
// `sampleRate` (the AudioWorkletGlobalScope global) is the OUTPUT rate.

class Player extends AudioWorkletProcessor {
  constructor() {
    super();
    this.q = [];
    this.queued = 0;
    this.head = null;
    this.headPos = 0;
    this._v = 0;

    this.inRate = sampleRate;
    this.ratioBase = 1;

    this.s0 = 0; this.s1 = 0; this.frac = 0; this.primed = false;

    this.started = false;
    this.targetSamples = Math.round(sampleRate * 0.30);
    this.baseTargetSamples = this.targetSamples;
    this.armSamples = this.targetSamples;
    this.maxSamples = Math.round(sampleRate * 2.0);
    this.qAvg = 0;

    this.gain = 0;
    this.gainStep = 1 / Math.max(1, Math.round(sampleRate * 0.005)); // ~5 ms ramp

    this.underruns = 0;
    this.statFrames = 0;
    this.statEvery = Math.max(1, Math.round(sampleRate * 2)); // report ~every 2 s

    this.port.onmessage = (e) => {
      const m = e.data;
      if (m.type === 'pcm') {
        this.q.push(m.data); this.queued += m.data.length;
      } else if (m.type === 'config') {
        this.inRate = m.inRate || sampleRate;
        this.ratioBase = this.inRate / sampleRate;
        const ms = (v, d) => Math.max(1, Math.round(((v || d) / 1000) * this.inRate));
        this.targetSamples = ms(m.targetMs, 300);
        this.baseTargetSamples = this.targetSamples;
        this.maxSamples = ms(m.maxMs, 2000);
        this.armSamples = this.targetSamples;
      } else if (m.type === 'flush') {
        this.q = []; this.queued = 0; this.head = null; this.headPos = 0;
        this.s0 = this.s1 = 0; this.frac = 0; this.primed = false;
        this.started = false; this.gain = 0; this.qAvg = 0;
        this.targetSamples = this.baseTargetSamples;
        this.armSamples = this.targetSamples;
      }
    };
  }

  _postStats() {
    this.port.postMessage({
      type: 'stats',
      queuedMs: Math.round((this.queued / this.inRate) * 1000),
      targetMs: Math.round((this.targetSamples / this.inRate) * 1000),
      underruns: this.underruns,
    });
  }

  // Pull the next input sample into this._v, advancing across queued chunks.
  // Returns false when the queue is empty (underrun).
  _next() {
    while (!this.head || this.headPos >= this.head.length) {
      this.head = this.q.shift() || null;
      this.headPos = 0;
      if (!this.head) return false;
    }
    this._v = this.head[this.headPos++];
    this.queued--;
    return true;
  }

  process(inputs, outputs) {
    const chans = outputs[0];
    const out = chans[0];
    const n = out.length;

    // Prebuffer: hold silence until a full cushion (armSamples) has queued.
    if (!this.started) {
      if (this.queued < this.armSamples) { for (let c = 0; c < chans.length; c++) chans[c].fill(0); return true; }
      this.started = true;
      this.primed = false;
      this.qAvg = this.queued;
    }

    // Latency cap: a reconnect flush or a stall-then-resume can dump a big
    // burst; drop the oldest excess so we don't play seconds behind.
    if (this.queued > this.maxSamples) {
      let drop = this.queued - this.targetSamples;
      while (drop-- > 0 && this._next()) {}
      this.primed = false;
    }

    // Drift control: pull the (smoothed) queue depth toward target by nudging
    // the consume ratio. The EMA ignores the sender's ~250 ms batch sawtooth so
    // we correct real clock drift, not each batch. Correction is clamped to
    // ±0.4% — inaudible — so the buffer tracks without warbling.
    this.qAvg += (this.queued - this.qAvg) * 0.002;
    const err = (this.qAvg - this.targetSamples) / this.targetSamples;
    const corr = Math.max(-0.004, Math.min(0.004, err * 0.05));
    const ratio = this.ratioBase * (1 + corr);

    let underran = false;
    let i = 0;
    for (; i < n; i++) {
      if (!this.primed) {
        if (!this._next()) { underran = true; break; }
        this.s0 = this._v;
        if (!this._next()) { underran = true; break; }
        this.s1 = this._v;
        this.frac = 0;
        this.primed = true;
      }
      while (this.frac >= 1) {
        this.frac -= 1;
        this.s0 = this.s1;
        if (!this._next()) { underran = true; break; }
        this.s1 = this._v;
      }
      if (underran) break;
      const s = this.s0 + (this.s1 - this.s0) * this.frac;
      if (this.gain < 1) { this.gain += this.gainStep; if (this.gain > 1) this.gain = 1; }
      out[i] = s * this.gain;
      this.frac += ratio;
    }

    if (underran) {
      // Fade the rest of this quantum to silence (declick), grow the target so
      // a chronically bursty sink converges on a depth that holds, and re-arm
      // to that FULL cushion so we don't resume only to starve again.
      for (; i < n; i++) {
        if (this.gain > 0) { this.gain -= this.gainStep; if (this.gain < 0) this.gain = 0; }
        out[i] = 0;
      }
      this.underruns++;
      this.targetSamples = Math.min(this.maxSamples, Math.round(this.targetSamples * 1.5));
      this.started = false;
      this.primed = false;
      this.armSamples = this.targetSamples;
      this._postStats();
    }

    for (let c = 1; c < chans.length; c++) chans[c].set(out);

    this.statFrames += n;
    if (this.statFrames >= this.statEvery) { this.statFrames = 0; this._postStats(); }
    return true;
  }
}
registerProcessor('player', Player);
