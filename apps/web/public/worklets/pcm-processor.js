/**
 * Runs on the audio render thread (must be loaded via audioWorklet.addModule
 * from a URL — cannot be inlined). Buffers mono float samples at the
 * AudioContext's own sample rate (the context is created at 16000Hz in
 * interview-session.ts, so no resampling happens here), converts each full
 * chunk to 16-bit PCM, and transfers it to the main thread for sending over
 * the Deepgram WebSocket. A "clear" command drops any partial buffer so
 * stale candidate audio isn't flushed after the floor changes (e.g. the AI
 * takes the floor mid-utterance).
 */
class PcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._chunkSize = 2048; // ~128ms at 16kHz
    this._buffer = new Float32Array(this._chunkSize);
    this._offset = 0;
    this.port.onmessage = (event) => {
      if (event.data === "clear") {
        this._offset = 0;
      }
    };
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;

    for (let i = 0; i < channel.length; i++) {
      this._buffer[this._offset++] = channel[i];
      if (this._offset === this._chunkSize) {
        this._flush();
      }
    }
    return true;
  }

  _flush() {
    const pcm16 = new Int16Array(this._offset);
    for (let i = 0; i < this._offset; i++) {
      const s = Math.max(-1, Math.min(1, this._buffer[i]));
      pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    this.port.postMessage(pcm16.buffer, [pcm16.buffer]);
    this._offset = 0;
  }
}

registerProcessor("pcm-processor", PcmProcessor);
