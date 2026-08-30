// worker/recorder.ts — server-side recording from the Durable Object to
// Supabase Storage. This is what makes a recording survive with no browser
// open: the DO is receiving the sender's audio anyway, so it writes it out.
//
// Audio is written as a series of complete WAV *segments* rather than one
// growing file. Two reasons, both practical:
//   1. A Durable Object has a bounded memory budget (~128 MB). Mono 48 kHz
//      Int16 is ~96 KB/s, so a single buffered file would cap out around 20
//      minutes. Segments make recording length unbounded.
//   2. Every segment is independently valid and playable, so an eviction or
//      crash costs at most the current partial segment instead of the session.

export interface RecorderEnv {
  SUPABASE_URL?: string;
  SUPABASE_SERVICE_ROLE_KEY?: string;
  SUPABASE_RECORDINGS_BUCKET?: string;
}

const SEGMENT_BYTES = 8 * 1024 * 1024; // ~85 s of 48 kHz mono Int16

// Minimal RIFF/WAVE header for 16-bit PCM.
function wavHeader(dataBytes: number, sampleRate: number, channels = 1): Uint8Array {
  const buf = new ArrayBuffer(44);
  const v = new DataView(buf);
  const bytesPerSample = 2;
  const blockAlign = channels * bytesPerSample;
  const ascii = (off: number, s: string) => {
    for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i));
  };
  ascii(0, "RIFF");
  v.setUint32(4, 36 + dataBytes, true);
  ascii(8, "WAVE");
  ascii(12, "fmt ");
  v.setUint32(16, 16, true);          // PCM fmt chunk size
  v.setUint16(20, 1, true);           // format = PCM
  v.setUint16(22, channels, true);
  v.setUint32(24, sampleRate, true);
  v.setUint32(28, sampleRate * blockAlign, true);
  v.setUint16(32, blockAlign, true);
  v.setUint16(34, 16, true);          // bits per sample
  ascii(36, "data");
  v.setUint32(40, dataBytes, true);
  return new Uint8Array(buf);
}

export class Recorder {
  sid: number;
  name: string;
  sampleRate: number;
  private env: RecorderEnv;
  private chunks: Uint8Array[] = [];
  private bytes = 0;
  private segment = 0;
  private startedAt = new Date().toISOString();

  constructor(sid: number, name: string, sampleRate: number, env: RecorderEnv) {
    this.sid = sid;
    this.name = name;
    this.sampleRate = sampleRate || 48000;
    this.env = env;
  }

  get configured(): boolean {
    return Boolean(this.env.SUPABASE_URL && this.env.SUPABASE_SERVICE_ROLE_KEY);
  }

  // Returns a promise to await only when a segment is due, so the hot path
  // stays synchronous for every other frame.
  push(data: ArrayBuffer): Promise<void> | null {
    this.chunks.push(new Uint8Array(data));
    this.bytes += data.byteLength;
    return this.bytes >= SEGMENT_BYTES ? this.flush() : null;
  }

  async flush(): Promise<void> {
    if (this.bytes === 0 || !this.configured) {
      this.chunks = [];
      this.bytes = 0;
      return;
    }

    const dataBytes = this.bytes;
    const header = wavHeader(dataBytes, this.sampleRate);
    const body = new Uint8Array(header.length + dataBytes);
    body.set(header, 0);
    let off = header.length;
    for (const c of this.chunks) {
      body.set(c, off);
      off += c.byteLength;
    }
    // Reset immediately so audio arriving during the upload starts the next
    // segment rather than being lost or double-written.
    this.chunks = [];
    this.bytes = 0;
    const seg = this.segment++;

    const safeName = this.name.replace(/[^A-Za-z0-9._-]+/g, "_").slice(0, 48) || "device";
    const stamp = this.startedAt.replace(/[:.]/g, "-");
    const key = `${safeName}/${stamp}_sid${this.sid}_part${String(seg).padStart(4, "0")}.wav`;
    const bucket = this.env.SUPABASE_RECORDINGS_BUCKET || "recordings";
    const base = (this.env.SUPABASE_URL || "").replace(/\/+$/, "");
    const auth = `Bearer ${this.env.SUPABASE_SERVICE_ROLE_KEY}`;

    try {
      const up = await fetch(`${base}/storage/v1/object/${bucket}/${key}`, {
        method: "POST",
        headers: {
          Authorization: auth,
          "Content-Type": "audio/wav",
          "x-upsert": "true",
        },
        body,
      });
      if (!up.ok) {
        console.log(`[rec] upload failed sid=${this.sid} seg=${seg}: ${up.status} ${await up.text()}`);
        return;
      }

      // Index the segment so recordings are queryable, not just files.
      const row = await fetch(`${base}/rest/v1/sessions`, {
        method: "POST",
        headers: {
          Authorization: auth,
          apikey: this.env.SUPABASE_SERVICE_ROLE_KEY as string,
          "Content-Type": "application/json",
          Prefer: "return=minimal",
        },
        body: JSON.stringify({
          device_name: this.name,
          started_at: this.startedAt,
          ended_at: new Date().toISOString(),
          storage_key: key,
          bytes: body.byteLength,
          sample_rate: this.sampleRate,
        }),
      });
      if (!row.ok) {
        console.log(`[rec] session row failed sid=${this.sid}: ${row.status} ${await row.text()}`);
      } else {
        console.log(`[rec] wrote ${key} (${body.byteLength} bytes)`);
      }
    } catch (e) {
      console.log(`[rec] flush error sid=${this.sid}: ${e}`);
    }
  }
}
