// play-worklet.js — adaptive, drift-compensating jitter buffer with resampling.
//
// The sender's microphone clock and this device's output clock are never
// identical, and some sinks — a Bluetooth headset above all — deliver audio in
// bursts with high, variable latency. A fixed 1:1 buffer therefore drifts:
// it slowly drains or fills, then stutters (silence, then a catch-up burst,
// heard as the pitch wobbling "up and down like an old radio").
//
// Instead we resample the incoming stream to the output rate with a fractional
// read pointer, and continuously nudge that resample ratio by a fraction of a
// percent to hold the buffered latency near a target. A ±0.4% nudge is far
// below the ~1% pitch change an ear can notice, so the drift is absorbed
// inaudibly and playback stays continuous. On a genuine underrun we fade out
// (no click) and re-arm with a small cushion rather than a long hard gap.
//
// Main thread posts:
//   {type:'config', inRate, targetMs, minMs, maxMs}  configure for a stream
//   {type:'pcm',    data:Float32Array}               mono samples at inRate
//   {type:'flush'}                                    reset all state
// All queue depths below are counted in INPUT-rate samples (what we queue);
// `sampleRate` (the AudioWorkletGlobalScope global) is the OUTPUT rate.

class Player extends AudioWorkletProcessor {
  constructor() {
    super();
    this.q = [];            // queued input-rate Float32Array chunks
    this.queued = 0;        // total input samples still queued
    this.head = null;       // chunk currently being read
    this.headPos = 0;       // read index into head
    this._v = 0;            // last sample pulled by _next()

    this.inRate = sampleRate;   // sender rate; overwritten by config
    this.ratioBase = 1;         // input samples consumed per output sample

    this.s0 = 0; this.s1 = 0;   // interpolation window (consecutive inputs)
    this.frac = 0;              // fractional position between s0 and s1
    this.primed = false;        // s0/s1 hold valid samples

    this.started = false;       // past the (re)prebuffer gate
    this.targetSamples = Math.round(sampleRate * 0.30); // desired queue depth
    this.armSamples = this.targetSamples;               // depth needed to start
    this.minSamples = Math.round(sampleRate * 0.14);    // re-arm depth after underrun
    this.maxSamples = Math.round(sampleRate * 0.70);    // hard latency cap
    this.qAvg = 0;              // smoothed queue depth drives drift control

    this.gain = 0;              // declick envelope, 0..1
    this.gainStep = 1 / Math.max(1, Math.round(sampleRate * 0.005)); // ~5 ms ramp

    this.port.onmessage = (e) => {
      const m = e.data;
      if (m.type === 'pcm') {
        this.q.push(m.data); this.queued += m.data.length;
      } else if (m.type === 'config') {
        this.inRate = m.inRate || sampleRate;
        this.ratioBase = this.inRate / sampleRate;
        const ms = (v, d) => Math.max(1, Math.round(((v || d) / 1000) * this.inRate));
        this.targetSamples = ms(m.targetMs, 300);
        this.minSamples = ms(m.minMs, 140);
        this.maxSamples = ms(m.maxMs, 700);
        this.armSamples = this.targetSamples;
      } else if (m.type === 'flush') {
        this.q = []; this.queued = 0; this.head = null; this.headPos = 0;
        this.s0 = this.s1 = 0; this.frac = 0; this.primed = false;
        this.started = false; this.gain = 0; this.qAvg = 0;
        this.armSamples = this.targetSamples;
      }
    };
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

    // Prebuffer: hold silence until a healthy cushion has queued. `armSamples`
    // is the full target for the first fill, a smaller cushion after underruns.
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
      this.primed = false; // rebuild the interpolation window after the skip
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
      // Fade the rest of this quantum to silence (declick), then re-arm so we
      // resume once a small cushion refills — not after a full hard gap.
      for (; i < n; i++) {
        if (this.gain > 0) { this.gain -= this.gainStep; if (this.gain < 0) this.gain = 0; }
        out[i] = 0;
      }
      this.started = false;
      this.primed = false;
      this.armSamples = this.minSamples;
    }

    for (let c = 1; c < chans.length; c++) chans[c].set(out);
    return true;
  }
}
registerProcessor('player', Player);
