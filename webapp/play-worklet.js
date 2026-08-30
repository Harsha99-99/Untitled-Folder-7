// play-worklet.js — jitter-buffered playback of streamed PCM.
// Main thread posts {type:'pcm', data:Float32Array}; we output continuously.
// A small prebuffer avoids choppiness; underruns emit silence.

class Player extends AudioWorkletProcessor {
  constructor() {
    super();
    this.q = [];         // queued Float32Array chunks
    this.cur = null;
    this.pos = 0;
    this.queued = 0;     // total samples queued
    this.started = false;
    this.prebuffer = 0;  // samples to accumulate before playback starts
    this.port.onmessage = (e) => {
      const m = e.data;
      if (m.type === 'pcm') { this.q.push(m.data); this.queued += m.data.length; }
      else if (m.type === 'config') { this.prebuffer = m.prebuffer | 0; }
      else if (m.type === 'flush') { this.q = []; this.cur = null; this.pos = 0; this.queued = 0; this.started = false; }
    };
  }
  process(inputs, outputs) {
    const chans = outputs[0];
    const out = chans[0];
    const n = out.length;

    if (!this.started) {
      if (this.queued < this.prebuffer) { for (let c = 0; c < chans.length; c++) chans[c].fill(0); return true; }
      this.started = true;
    }

    for (let i = 0; i < n; i++) {
      if (!this.cur || this.pos >= this.cur.length) {
        this.cur = this.q.shift() || null;
        this.pos = 0;
        if (!this.cur) { // underrun
          for (let j = i; j < n; j++) out[j] = 0;
          this.started = this.queued > this.prebuffer; // re-prebuffer if we ran dry
          break;
        }
      }
      out[i] = this.cur[this.pos++];
      this.queued--;
    }
    for (let c = 1; c < chans.length; c++) chans[c].set(out);
    return true;
  }
}
registerProcessor('player', Player);
