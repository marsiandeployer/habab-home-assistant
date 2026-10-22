// Native Assist WebSocket streaming. HA owns VAD, STT, intents and speech output.
export class AssistVoice {
  constructor(hass, pipeline, onEvent) {
    this.hass = hass;
    this.pipeline = pipeline;
    this.onEvent = onEvent;
    this.pending = [];
  }

  async start() {
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      throw { code: "secure_context" };
    }
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
      if (this.closed) { this.stopMicrophone(); return; }
      this.context = new AudioContext({ sampleRate: 16000 });
      await this.context.audioWorklet.addModule("/habab-home/recorder-worklet.js");
      if (this.closed) { this.stopMicrophone(); return; }
      this.source = this.context.createMediaStreamSource(this.stream);
      this.recorder = new AudioWorkletNode(this.context, "habab-recorder");
      this.recorder.port.onmessage = ({ data }) => {
        if (this.closed) return;
        if (this.ready) this.send(data);
        else if (this.pending.length < 400) this.pending.push(data);
      };
      this.source.connect(this.recorder);
      await this.context.resume();
      this.unsubscribe = await this.hass.connection.subscribeMessage((event) => {
        if (this.closed) return;
        if (event.type === "run-start") {
          this.handler = event.data.runner_data.stt_binary_handler_id;
        } else if (event.type === "stt-start") {
          this.ready = true;
          this.pending.forEach((chunk) => this.send(chunk));
          this.pending = [];
        } else if (event.type === "stt-vad-end" || event.type === "stt-end") {
          this.handler = undefined;
          this.stopMicrophone();
        } else if (event.type === "tts-end") {
          const url = event.data.tts_output?.url;
          if (url?.startsWith("/api/")) {
            this.audio = new Audio(url);
            this.audio.play().catch(() => this.onEvent({ type: "playback-blocked" }));
          }
        }
        this.onEvent(event);
        if (event.type === "run-end" || event.type === "error") this.close(false);
      }, {
        type: "assist_pipeline/run", pipeline: this.pipeline.id,
        start_stage: "stt", end_stage: this.pipeline.tts_engine ? "tts" : "intent",
        input: { sample_rate: this.context.sampleRate },
      });
      // Covers a terminal event arriving before the subscription Promise resolves.
      if (this.closed) this.releaseSubscription();
      else {
        this.micTimer = setTimeout(() => this.finishRecording(), 20000);
        this.runTimer = setTimeout(() => {
          this.onEvent({ type: "error", data: { code: "timeout" } });
          this.close();
        }, 120000);
      }
    } catch (error) {
      this.close();
      throw { code: error?.name === "NotAllowedError" ? "microphone_denied" : (error.code || "microphone_failed") };
    }
  }

  send(chunk) {
    if (this.handler == null || this.hass.connection.socket?.readyState !== WebSocket.OPEN) return;
    const data = new Uint8Array(1 + chunk.byteLength);
    data[0] = this.handler;
    data.set(new Uint8Array(chunk.buffer, chunk.byteOffset, chunk.byteLength), 1);
    this.hass.connection.socket.send(data);
  }

  finishRecording() {
    if (this.handler != null) this.send(new Int16Array());
    this.handler = undefined;
    this.stopMicrophone();
    this.onEvent({ type: "recording-ended" });
  }

  stopMicrophone() {
    clearTimeout(this.micTimer);
    this.stream?.getTracks().forEach((track) => track.stop());
    if (this.recorder) this.recorder.port.onmessage = null;
    this.source?.disconnect();
    this.context?.close();
    this.context = undefined;
  }

  close(stopPlayback = true) {
    this.closed = true;
    this.stopMicrophone();
    this.pending = [];
    this.releaseSubscription();
    clearTimeout(this.runTimer);
    if (stopPlayback) this.audio?.pause();
  }

  releaseSubscription() {
    const unsubscribe = this.unsubscribe;
    this.unsubscribe = undefined;
    if (!unsubscribe) return;
    // A finished Assist run may already have removed its subscription.
    // Releasing it is idempotent and must not create an unhandled rejection.
    Promise.resolve(unsubscribe()).catch((error) => {
      if (error.code !== "not_found") this.onEvent({ type: "error", data: { code: error.code || "unknown" } });
    });
  }
}
