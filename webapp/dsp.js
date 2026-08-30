// dsp.js — browser port of the desktop console's VoiceDSP (hub_app.py).
//
// Same chain, same constants, same envelope coefficients, so enhanced audio in
// the browser sounds like it did in the Tk console:
//
//   highpass (110 Hz, 2nd-order Butterworth)
//     -> fixed gain (+12 dB)
//     -> AGC (target -18 dBFS, ceiling +34 dB, asymmetric attack/release)
//     -> noise gate (-58 dBFS, asymmetric open/close)
//     -> soft-knee limiter above 0.7 via tanh
//
// State (filter memory, AGC gain, gate envelope) persists across blocks, the
// way scipy's lfilter `zi` did — otherwise every buffer boundary clicks.

const dbToLin = (db) => Math.pow(10, db / 20);

export class VoiceDSP {
  constructor(sampleRate) {
    this.sr = sampleRate;

    this.enabled = true;
    this.gain = dbToLin(12.0);

    this.agc = true;
    this.agcTarget = dbToLin(-18.0);
    this.agcMaxGain = dbToLin(34.0);
    this._agcGain = 1.0;

    this.gate = true;
    this.gateThresh = dbToLin(-58.0);
    this._gateEnv = 0.0;

    this.highpass = true;
    this._buildHP(110.0);
  }

  // A 2nd-order Butterworth highpass is the RBJ biquad at Q = 1/sqrt(2),
  // which is what scipy's butter(2, fc/ny, 'high') produces.
  _buildHP(fc) {
    const ny = this.sr / 2;
    fc = Math.min(fc, ny * 0.9);
    const w0 = (2 * Math.PI * fc) / this.sr;
    const cw = Math.cos(w0);
    const alpha = Math.sin(w0) / (2 * Math.SQRT1_2);

    const b0 = (1 + cw) / 2;
    const b1 = -(1 + cw);
    const b2 = (1 + cw) / 2;
    const a0 = 1 + alpha;
    const a1 = -2 * cw;
    const a2 = 1 - alpha;

    this._b = [b0 / a0, b1 / a0, b2 / a0];
    this._a = [a1 / a0, a2 / a0];
    this._z1 = 0; // transposed direct form II state
    this._z2 = 0;
  }

  setSampleRate(sr) {
    if (sr && sr !== this.sr) {
      this.sr = sr;
      this._buildHP(110.0);
    }
  }

  reset() {
    this._z1 = 0;
    this._z2 = 0;
    this._agcGain = 1.0;
    this._gateEnv = 0.0;
  }

  // x: Float32Array in [-1, 1]. Returns a new Float32Array.
  process(x) {
    if (!this.enabled || x.length === 0) return x;

    const n = x.length;
    const y = new Float32Array(n);

    // ---- highpass (stateful across blocks) ----
    if (this.highpass) {
      const [b0, b1, b2] = this._b;
      const [a1, a2] = this._a;
      let z1 = this._z1;
      let z2 = this._z2;
      for (let i = 0; i < n; i++) {
        const xn = x[i];
        const out = b0 * xn + z1;
        z1 = b1 * xn - a1 * out + z2;
        z2 = b2 * xn - a2 * out;
        y[i] = out;
      }
      this._z1 = z1;
      this._z2 = z2;
    } else {
      y.set(x);
    }

    // ---- fixed gain ----
    for (let i = 0; i < n; i++) y[i] *= this.gain;

    // Gate and AGC both key off the RAW input level, not the post-gain level,
    // matching the desktop implementation.
    let rawSum = 0;
    for (let i = 0; i < n; i++) rawSum += x[i] * x[i];
    const rawRms = Math.sqrt(rawSum / n) + 1e-9;

    // ---- AGC ----
    if (this.agc) {
      let curSum = 0;
      for (let i = 0; i < n; i++) curSum += y[i] * y[i];
      const cur = Math.sqrt(curSum / n) + 1e-9;

      let desired = Math.min(this.agcTarget / cur, this.agcMaxGain);
      // Don't let the AGC wind up while the signal is below the gate — that is
      // what turns room tone into a roar between words.
      const below = this.gate && rawRms <= this.gateThresh;
      if (below && desired > this._agcGain) desired = this._agcGain;

      const coeff = desired < this._agcGain ? 0.25 : 0.12; // fast down, slow up
      this._agcGain = (1 - coeff) * this._agcGain + coeff * desired;
      for (let i = 0; i < n; i++) y[i] *= this._agcGain;
    }

    // ---- noise gate ----
    if (this.gate) {
      const target = rawRms > this.gateThresh ? 1.0 : 0.0;
      const coeff = target >= this._gateEnv ? 0.30 : 0.06; // open fast, close slow
      this._gateEnv = (1 - coeff) * this._gateEnv + coeff * target;
      for (let i = 0; i < n; i++) y[i] *= this._gateEnv;
    }

    // ---- soft-knee limiter ----
    for (let i = 0; i < n; i++) {
      const a = Math.abs(y[i]);
      if (a > 0.7) {
        y[i] = Math.sign(y[i]) * (0.7 + 0.3 * Math.tanh((a - 0.7) / 0.3));
      }
    }

    return y;
  }
}
