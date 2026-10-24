// Native Assist WebSocket streaming for speech input and optional TTS-only output.
export class AssistVoice {
  constructor(hass, pipeline, onEvent) {
    this.hass = hass;
    this.pipeline = pipeline;
    this.onEvent = onEvent;
    this.pending = [];
  }

  async start() {
    if (!this.pipeline || typeof this.pipeline.id !== "string" || !this.pipeline.id || !this.pipeline.stt_engine) {
      throw { code: "speechRequired" };
    }
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      throw { code: "secure_context" };
    }
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
      if (this.closed || this.recordingEnded) { this.stopMicrophone(); return; }
      this.context = new AudioContext({ sampleRate: 16000 });
      await this.context.audioWorklet.addModule("/habab-home/recorder-worklet.js");
      if (this.closed || this.recordingEnded) { this.stopMicrophone(); return; }
      this.source = this.context.createMediaStreamSource(this.stream);
      this.recorder = new AudioWorkletNode(this.context, "habab-recorder");
      this.recorder.port.onmessage = ({ data }) => {
        if (this.closed || this.recordingEnded) return;
        if (this.ready) this.send(data);
        else if (this.pending.length < 400) this.pending.push(data);
      };
      this.source.connect(this.recorder);
      await this.context.resume();
      if (this.closed || this.recordingEnded) { this.stopMicrophone(); return; }
      this.unsubscribe = await this.hass.connection.subscribeMessage((event) => {
        if (this.closed) return;
        if (event.type === "run-start") {
          if (this.recordingEnded) return;
          this.handler = event.data.runner_data.stt_binary_handler_id;
        } else if (event.type === "stt-start") {
          if (this.recordingEnded) return;
          this.ready = true;
          this.pending.forEach((chunk) => this.send(chunk));
          this.pending = [];
        } else if (event.type === "stt-vad-end" || event.type === "stt-end") {
          this.handler = undefined;
          this.stopMicrophone();
        }
        this.onEvent(event);
        if (event.type === "run-end" || event.type === "error") this.close(false);
      }, {
        type: "assist_pipeline/run", pipeline: this.pipeline.id,
        start_stage: "stt", end_stage: "stt",
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
      const code = error?.name === "NotAllowedError" ? "microphone_denied"
        : ["secure_context", "speechRequired", "microphone_denied"].includes(error?.code) ? error.code
          : "microphone_failed";
      throw { code };
    }
  }

  async speak(text) {
    if (!this.pipeline || typeof this.pipeline.id !== "string" || !this.pipeline.id || !this.pipeline.tts_engine || typeof text !== "string" || !text.trim()) {
      throw { code: "tts_unavailable" };
    }
    this.closed = false;
    let resolveSpeech;
    let rejectSpeech;
    this.speakingPromise = new Promise((resolve, reject) => {
      resolveSpeech = resolve;
      rejectSpeech = reject;
    });
    this.resolveSpeech = resolveSpeech;
    this.rejectSpeech = rejectSpeech;
    this.finishSpeech = (value, error) => {
      if (!this.resolveSpeech && !this.rejectSpeech) return;
      const resolve = this.resolveSpeech;
      const reject = this.rejectSpeech;
      this.resolveSpeech = undefined;
      this.rejectSpeech = undefined;
      if (error) reject?.(error);
      else resolve?.(value);
    };
    try {
      this.unsubscribe = await this.hass.connection.subscribeMessage((event) => {
        if (this.closed) return;
        if (event.type === "tts-end") {
          const url = event.data?.tts_output?.url;
          if (typeof url !== "string" || !url.startsWith("/api/")) {
            this.onEvent({ type: "error", data: { code: "tts_unavailable" } });
          } else {
            this.audio = new Audio(this.hass.hassUrl(url));
            this.audio.play().catch(() => this.onEvent({ type: "playback-blocked" }));
            this.audio.addEventListener("ended", () => this.onEvent({ type: "playback-ended" }), {once: true});
            this.audio.addEventListener("error", () => this.onEvent({ type: "error", data: {code: "tts_unavailable"} }), {once: true});
          }
        }
        this.onEvent(event);
        if (event.type === "run-end") {
          this.finishSpeech?.(true);
          this.close(false);
        } else if (event.type === "error") {
          this.finishSpeech?.(undefined, {code: "tts_unavailable"});
          this.close(false);
        }
      }, {
        type: "assist_pipeline/run", pipeline: this.pipeline.id,
        start_stage: "tts", end_stage: "tts", input: { text },
      });
      if (this.closed) this.releaseSubscription();
      else this.runTimer = setTimeout(() => {
        this.finishSpeech?.(undefined, {code: "timeout"});
        this.onEvent({type: "error", data: {code: "timeout"}});
        this.close();
      }, 120000);
    } catch (_) {
      this.close();
      throw { code: "tts_unavailable" };
    }
    return this.speakingPromise;
  }

  send(chunk) {
    if (this.closed || this.recordingEnded || this.handler == null || this.hass.connection.socket?.readyState !== WebSocket.OPEN) return;
    const data = new Uint8Array(1 + chunk.byteLength);
    data[0] = this.handler;
    data.set(new Uint8Array(chunk.buffer, chunk.byteOffset, chunk.byteLength), 1);
    this.hass.connection.socket.send(data);
  }

  finishRecording() {
    if (this.closed || this.recordingEnded) return;
    const hadHandler = this.handler != null;
    if (this.handler != null) this.send(new Int16Array());
    this.stopMicrophone();
    this.onEvent({ type: "recording-ended" });
    // With no runner yet, there is no recording to finish. Cancel the pending
    // subscription so a late runner cannot reopen or upload buffered audio.
    if (!hadHandler) this.close();
  }

  stopMicrophone() {
    this.recordingEnded = true;
    this.ready = false;
    this.handler = undefined;
    this.pending = [];
    clearTimeout(this.micTimer);
    this.micTimer = undefined;
    const stream = this.stream;
    this.stream = undefined;
    stream?.getTracks().forEach((track) => track.stop());
    if (this.recorder) this.recorder.port.onmessage = null;
    this.recorder = undefined;
    try { this.source?.disconnect(); } catch (_) { /* Already disconnected while closing a finished run. */ }
    this.source = undefined;
    const context = this.context;
    this.context = undefined;
    context?.close().catch(() => {});
  }

  close(stopPlayback = true) {
    this.closed = true;
    this.finishSpeech?.(false);
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
