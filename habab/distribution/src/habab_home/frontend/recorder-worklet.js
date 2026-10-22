// PCM conversion follows Home Assistant src/util/recorder-worklet.js (Apache-2.0).
class HababRecorder extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    const pcm = new Int16Array(input.length);
    for (let i = 0; i < input.length; i++) {
      const value = Math.max(-1, Math.min(1, input[i]));
      pcm[i] = value < 0 ? value * 0x8000 : value * 0x7fff;
    }
    this.port.postMessage(pcm, [pcm.buffer]);
    return true;
  }
}
registerProcessor("habab-recorder", HababRecorder);
