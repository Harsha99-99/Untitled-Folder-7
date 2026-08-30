// worklet.js — RAW capture passthrough for the sender.
// No DSP here: the operator console applies enhancement per device. We only
// downmix to mono, post a level for the on-screen meter, and post raw frames
// (post-nothing) for streaming.

class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.streaming = false;
    this._q = 0;
    this._accRms = 0;
    this._accPeak = 0;
    this._accN = 0;
    this.port.onmessage = (e) => {
      if (e.data.type === 'stream') this.streaming = e.data.on;
    };
  }
  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const frames = input[0].length;

    // downmix to mono (raw — no filtering)
    let mono;
    if (input.length > 1) {
      mono = new Float32Array(frames);
      for (let c = 0; c < input.length; c++) {
        const ch = input[c];
        for (let i = 0; i < frames; i++) mono[i] += ch[i];
      }
      for (let i = 0; i < frames; i++) mono[i] /= input.length;
    } else {
      mono = input[0];
    }

    // meter
    let peak = 0, sum = 0;
    for (let i = 0; i < frames; i++) { const a = Math.abs(mono[i]); if (a > peak) peak = a; sum += mono[i] * mono[i]; }
    this._accRms += sum; this._accPeak = Math.max(this._accPeak, peak); this._accN += frames;
    if (++this._q >= 8) {
      this.port.postMessage({ type: 'level', rms: Math.sqrt(this._accRms / Math.max(1, this._accN)), peak: this._accPeak });
      this._q = 0; this._accRms = 0; this._accPeak = 0; this._accN = 0;
    }

    // raw frames for streaming
    if (this.streaming) {
      const copy = mono.slice(0);
      this.port.postMessage({ type: 'audio', data: copy }, [copy.buffer]);
    }
    return true;
  }
}
registerProcessor('capture', Capture);
