// Shared mono PCM16 recorder. Both Home Assistant Assist and browser realtime use
// the same microphone lifecycle and worklet so stopping one path has one boundary.
export class Pcm16Recorder {
  constructor(onChunk) {
    this.onChunk = onChunk;
  }

  async start() {
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      throw { code: "secure_context" };
    }
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
      if (this.closed) { this.close(); return; }
      this.context = new AudioContext({ sampleRate: 16000 });
      if (this.context.sampleRate !== 16000) throw { code: "invalid_audio_format" };
      await this.context.audioWorklet.addModule("/habab-home/recorder-worklet.js");
      if (this.closed) { this.close(); return; }
      this.source = this.context.createMediaStreamSource(this.stream);
      this.node = new AudioWorkletNode(this.context, "habab-recorder");
      this.node.port.onmessage = ({ data }) => {
        if (!this.closed && data instanceof Int16Array) this.onChunk(data);
      };
      this.source.connect(this.node);
      await this.context.resume();
      if (this.closed) this.close();
    } catch (error) {
      this.close();
      if (["secure_context", "invalid_audio_format"].includes(error?.code)) throw error;
      throw { code: error?.name === "NotAllowedError" ? "microphone_denied" : "microphone_failed" };
    }
  }

  close() {
    this.closed = true;
    const stream = this.stream;
    this.stream = undefined;
    stream?.getTracks().forEach((track) => track.stop());
    if (this.node) this.node.port.onmessage = null;
    this.node = undefined;
    try { this.source?.disconnect(); } catch (_) { /* Already disconnected while closing. */ }
    this.source = undefined;
    const context = this.context;
    this.context = undefined;
    context?.close().catch(() => {});
  }
}

export function playVoiceCue(kind) {
  if (kind !== "start" && kind !== "end") return Promise.reject({ code: "playback" });
  if (typeof AudioContext !== "function") return Promise.reject({ code: "playback" });
  const context = new AudioContext();
  const oscillator = context.createOscillator();
  const gain = context.createGain();
  oscillator.type = "sine";
  oscillator.frequency.value = kind === "start" ? 740 : 520;
  gain.gain.setValueAtTime(0, context.currentTime);
  gain.gain.linearRampToValueAtTime(0.09, context.currentTime + 0.012);
  gain.gain.setValueAtTime(0.09, context.currentTime + 0.075);
  gain.gain.linearRampToValueAtTime(0, context.currentTime + 0.11);
  oscillator.connect(gain);
  gain.connect(context.destination);
  return context.resume().then(() => new Promise((resolve, reject) => {
    oscillator.onended = () => {
      oscillator.disconnect();
      gain.disconnect();
      context.close().catch(() => {}).then(resolve, reject);
    };
    oscillator.start();
    oscillator.stop(context.currentTime + 0.115);
  })).catch((error) => {
    oscillator.disconnect();
    gain.disconnect();
    context.close().catch(() => {});
    throw error?.code ? error : { code: "playback" };
  });
}

export async function bootstrapAfterStartCue({ playCue, createSession, isCurrent, stopSession }) {
  await playCue("start");
  if (!isCurrent()) return undefined;
  const session = await createSession();
  if (!isCurrent()) {
    if (typeof session?.session_id === "string") {
      try { await stopSession(session.session_id); } catch (_) { /* A stale lease is already locally unusable. */ }
    }
    return undefined;
  }
  return session;
}

// Native Assist WebSocket streaming for speech input and optional TTS-only output.
export class AssistVoice {
  constructor(hass, pipeline, onEvent, { runConversation = false, conversationId } = {}) {
    this.hass = hass;
    this.pipeline = pipeline;
    this.onEvent = onEvent;
    this.runConversation = runConversation;
    this.conversationId = conversationId;
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
      this.capture = new Pcm16Recorder((data) => {
        if (this.closed || this.recordingEnded) return;
        if (this.ready) this.send(data);
        else if (this.pending.length < 400) this.pending.push(data);
      });
      await this.capture.start();
      if (this.closed || this.recordingEnded) { this.stopMicrophone(); return; }
      const options = {
        type: "assist_pipeline/run", pipeline: this.pipeline.id,
        start_stage: "stt", end_stage: this.runConversation ? "intent" : "stt",
        input: { sample_rate: 16000 },
      };
      if (this.conversationId) options.conversation_id = this.conversationId;
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
      }, options);
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
    this.capture?.close();
    this.capture = undefined;
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

function pcm16Base64(samples) {
  const bytes = new Uint8Array(samples.length * 2);
  for (let index = 0; index < samples.length; index++) {
    bytes[index * 2] = samples[index] & 0xff;
    bytes[index * 2 + 1] = (samples[index] >> 8) & 0xff;
  }
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, Math.min(offset + 0x8000, bytes.length)));
  }
  return btoa(binary);
}

const MAX_GEMINI_FRAME_BYTES = 256 * 1024;
const TRANSCRIPT_DRAIN_TIMEOUT_MS = 5000;

function decodeBase64(value) {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

/** Habab's constrained Gemini Live WebSocket adapter for the unified browser chat. */
export class BrowserGeminiVoice {
  constructor(session, { onEvent, onTurn, onTranscript, onStop, playCue }) {
    this.session = session;
    this.onEvent = onEvent;
    this.onTurn = onTurn;
    this.onTranscript = onTranscript;
    this.onStop = onStop;
    this.playCue = playCue;
    this.closed = false;
    this.accepting = false;
    this.callIds = new Set();
    this.cancelledCallIds = new Set();
    this.pendingTools = new Map();
    this.playbackSources = new Set();
    this.responses = new Set();
    this.playbackGeneration = 0;
    this.responseNumber = 0;
    this.transcriptNumber = 0;
    this.transcriptQueue = Promise.resolve();
    this.transcriptDrainTimeoutMs = TRANSCRIPT_DRAIN_TIMEOUT_MS;
    this.transcriptDrainAborted = false;
    this.transcriptWriteFailed = false;
    this.messageQueue = Promise.resolve();
    this.messageGeneration = 0;
    this.canonicalFollowupPending = false;
    this.currentInputLength = 0;
  }

  async start() {
    if (!this.session || this.session.websocket_url !== "wss://habab-voice.wpmix.net/gemini/ws" ||
        this.session.auth?.mode !== "first_text_frame" || this.session.auth?.type !== "habab.voice.auth.v1" ||
        this.session.client !== "browser_v1" || !this.session.value ||
        typeof WebSocket !== "function") {
      throw { code: "browserVoiceFailed" };
    }
    const ttl = this.session.lease_ttl_seconds;
    if (!Number.isFinite(ttl) || ttl <= 0 || ttl > 3600) throw { code: "browserVoiceFailed" };
    const expiresAt = this.session.expires_at;
    if (typeof expiresAt !== "number" || !Number.isSafeInteger(expiresAt) || expiresAt <= 0) {
      throw { code: "browserVoiceFailed" };
    }
    const credentialExpiry = expiresAt * 1000;
    if (!Number.isSafeInteger(credentialExpiry) || credentialExpiry <= Date.now()) {
      throw { code: "browserVoiceFailed" };
    }
    this.socket = new WebSocket(this.session.websocket_url);
    const messageGeneration = ++this.messageGeneration;
    this.socket.binaryType = "arraybuffer";
    this.expiryTimer = setTimeout(() => {
      this.onEvent({ type: "expired" });
      void this.finish();
    // Stop while a small fraction of the server lease is still available for
    // the authenticated stop request to revoke it before server expiry.
    }, ttl * 1000 * 0.9);
    this.credentialTimer = setTimeout(() => {
      if (!this.ready) this.fail({ code: "timeout" });
    }, credentialExpiry - Date.now());
    await new Promise((resolve, reject) => {
      this.startResolve = resolve;
      this.startReject = reject;
      this.socket.onopen = () => {
        if (this.closed) return;
        try {
          // The opaque credential is sent only in the relay's first text frame.
          this.socket.send(JSON.stringify({
            type: this.session.auth.type,
            credential: this.session.value,
            setup: this.session.setup,
          }));
        } catch (_) { this.fail({ code: "browserVoiceFailed" }); }
      };
      this.socket.onmessage = (event) => {
        this.queueSocketMessage(event.data, messageGeneration);
      };
      this.socket.onerror = () => this.fail({ code: "browserVoiceFailed" });
      this.socket.onclose = () => {
        if (!this.closed) this.fail({ code: "browserVoiceFailed" });
      };
    });
  }

  queueSocketMessage(data, generation) {
    if (this.closed || generation !== this.messageGeneration) return;
    this.messageQueue = this.messageQueue.then(async () => {
      if (this.closed || generation !== this.messageGeneration) return;
      const text = await this.decodeSocketMessage(data);
      if (this.closed || generation !== this.messageGeneration) return;
      await this.receive(JSON.parse(text));
    }).catch(() => {
      if (!this.closed && generation === this.messageGeneration) {
        this.fail({ code: "browserVoiceFailed" });
      }
    });
  }

  async decodeSocketMessage(data) {
    let bytes;
    if (typeof data === "string") {
      if (data.length > MAX_GEMINI_FRAME_BYTES) throw new Error("invalid_frame");
      bytes = new TextEncoder().encode(data);
    } else if (data instanceof ArrayBuffer) {
      if (data.byteLength > MAX_GEMINI_FRAME_BYTES) throw new Error("invalid_frame");
      bytes = new Uint8Array(data);
    } else if (ArrayBuffer.isView(data)) {
      if (data.byteLength > MAX_GEMINI_FRAME_BYTES) throw new Error("invalid_frame");
      bytes = new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
    } else if (typeof Blob === "function" && data instanceof Blob) {
      if (data.size > MAX_GEMINI_FRAME_BYTES) throw new Error("invalid_frame");
      bytes = new Uint8Array(await data.arrayBuffer());
      if (bytes.byteLength > MAX_GEMINI_FRAME_BYTES) throw new Error("invalid_frame");
    } else {
      throw new Error("invalid_frame");
    }
    if (bytes.byteLength > MAX_GEMINI_FRAME_BYTES) throw new Error("invalid_frame");
    return new TextDecoder("utf-8", {fatal: true}).decode(bytes);
  }

  async receive(message) {
    if (this.closed || !message || typeof message !== "object") return;
    if (message.setupComplete) {
      if (this.ready) return;
      this.ready = true;
      clearTimeout(this.credentialTimer);
      this.credentialTimer = undefined;
      try {
        this.capture = new Pcm16Recorder((samples) => this.sendAudio(samples));
        await this.capture.start();
        if (this.closed) { this.capture?.close(); return; }
        this.accepting = true;
        this.onEvent({ type: "ready" });
        this.startResolve?.();
        this.startResolve = undefined;
        return;
      } catch (error) {
        this.fail(error);
        return;
      }
    }
    const activity = message.voiceActivity?.type;
    if (activity === "ACTIVITY_START") {
      this.interruptPlayback();
      this.canonicalFollowupPending = false;
      this.currentInputLength = 0;
      for (const pending of this.pendingTools.values()) pending.bargedIn = true;
    } else if (activity === "ACTIVITY_END") {
      this.currentInputLength = 0;
      this.onEvent({ type: "speech-end" });
    }
    if (message.toolCallCancellation?.ids !== undefined && !Array.isArray(message.toolCallCancellation.ids)) {
      this.fail({ code: "browserVoiceFailed" }); return;
    }
    if (Array.isArray(message.toolCallCancellation?.ids)) {
      for (const id of message.toolCallCancellation.ids) this.cancelTool(id);
    }
    const content = message.serverContent;
    if (content?.interrupted) this.interruptPlayback();
    if (typeof content?.inputTranscription?.text === "string" && content.inputTranscription.text) {
      this.currentInputLength += content.inputTranscription.text.length;
      if (content.inputTranscription.text.length > 4000 || this.currentInputLength > 4000) {
        this.fail({ code: "browserVoiceFailed" }); return;
      }
      this.onEvent({ type: "speech-start" });
      this.recordTranscript("user", content.inputTranscription.text);
    }
    if (!content?.interrupted) {
      if (content?.outputTranscription?.text) {
        const response = this.ensureResponse();
        response.text += content.outputTranscription.text;
        if (response.text.length > 12000) { this.fail({ code: "browserVoiceFailed" }); return; }
      }
      const parts = content?.modelTurn?.parts;
      if (Array.isArray(parts)) {
        for (const part of parts) {
          const audio = part?.inlineData;
          if (!audio) continue;
          if (audio.mimeType !== "audio/pcm;rate=24000" || typeof audio.data !== "string") {
            this.fail({ code: "browserVoiceFailed" }); return;
          }
          const response = this.ensureResponse();
          response.audio = true;
          this.queueAudio(audio.data, response.generation);
        }
      }
    }
    if (message.toolCall?.functionCalls !== undefined) {
      if (!Array.isArray(message.toolCall.functionCalls)) { this.fail({ code: "browserVoiceFailed" }); return; }
      for (const call of message.toolCall.functionCalls) {
        if (!call || typeof call.id !== "string" || typeof call.name !== "string") {
          this.fail({ code: "browserVoiceFailed" }); return;
        }
        void this.handleTool(call);
      }
    }
    if (content && !content.interrupted && (content.generationComplete || content.turnComplete)) {
      const response = this.response;
      if (response) {
        response.complete = true;
        if (this.response === response) this.response = undefined;
        this.maybeRecordResponse(response);
      }
    }
  }

  ensureResponse() {
    if (this.response && !this.response.complete) return this.response;
    const response = {
      id: ++this.responseNumber,
      text: "",
      audio: false,
      complete: false,
      canonical: this.canonicalFollowupPending,
      generation: this.playbackGeneration,
      sources: 0,
      heard: false,
      recorded: false,
    };
    this.responses.add(response);
    this.response = response;
    this.canonicalFollowupPending = false;
    return response;
  }

  sendAudio(samples) {
    if (!this.accepting || this.closed || this.socket?.readyState !== WebSocket.OPEN) return;
    this.socket.send(JSON.stringify({ realtimeInput: {
      audio: { mimeType: "audio/pcm;rate=16000", data: pcm16Base64(samples) },
    } }));
  }

  queueAudio(encoded, generation) {
    if (!this.accepting || this.closed) return;
    try {
      if (!this.playbackContext) this.playbackContext = new AudioContext({ sampleRate: 24000 });
      const bytes = decodeBase64(encoded);
      if (bytes.byteLength % 2) throw new Error("invalid_audio");
      const samples = new Int16Array(bytes.byteLength / 2);
      const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      for (let index = 0; index < samples.length; index++) samples[index] = view.getInt16(index * 2, true);
      const context = this.playbackContext;
      const buffer = context.createBuffer(1, samples.length, 24000);
      const channel = buffer.getChannelData(0);
      for (let index = 0; index < samples.length; index++) channel[index] = samples[index] / 32768;
      const source = context.createBufferSource();
      source.buffer = buffer;
      source.connect(context.destination);
      const response = this.response;
      if (!response || response.generation !== generation) return;
      response.sources++;
      const startAt = Math.max(context.currentTime, this.playbackNextAt || context.currentTime);
      this.playbackNextAt = startAt + buffer.duration;
      this.playbackSources.add(source);
      source.onended = () => {
        this.playbackSources.delete(source);
        response.sources--;
        if (this.closed || generation !== this.playbackGeneration) return;
        response.heard = true;
        this.maybeRecordResponse(response);
      };
      source.start(startAt);
    } catch (_) {
      this.onEvent({ type: "error", code: "playback" });
    }
  }

  interruptPlayback() {
    this.playbackGeneration++;
    this.playbackNextAt = undefined;
    for (const source of this.playbackSources) {
      source.onended = null;
      try { source.stop(); } catch (_) { /* Already stopped. */ }
    }
    this.playbackSources.clear();
    if (this.response) this.response = undefined;
    this.responses.clear();
  }

  maybeRecordResponse(response) {
    if (!response.complete || response.sources !== 0 || !response.heard || response.canonical ||
        response.recorded || !response.text.trim() || this.closed) return;
    response.recorded = true;
    this.responses.delete(response);
    if (this.response === response) this.response = undefined;
    this.recordTranscript("assistant", response.text.trim());
  }

  recordTranscript(role, text) {
    if (!this.accepting || this.closed || this.transcriptDrainAborted || !text.trim()) return;
    const random = globalThis.crypto?.randomUUID?.().replaceAll("-", "");
    const eventId = `browser_${random || `${Date.now().toString(36)}_${++this.transcriptNumber}`}`;
    const event = {role, text, event_id: eventId};
    this.transcriptQueue = this.transcriptQueue.then(() => {
      if (this.transcriptDrainAborted) return;
      return this.onTranscript(event);
    }).catch(() => {
      this.transcriptWriteFailed = true;
      if (!this.closed) this.onEvent({ type: "error", code: "browserVoiceFailed" });
    });
  }

  async drainTranscriptQueue() {
    let timer;
    const timeout = new Promise((resolve) => {
      timer = setTimeout(() => resolve(false), this.transcriptDrainTimeoutMs);
    });
    const drained = await Promise.race([
      this.transcriptQueue.then(() => !this.transcriptWriteFailed), timeout,
    ]);
    clearTimeout(timer);
    if (!drained) this.transcriptDrainAborted = true;
    return drained;
  }

  async handleTool(call) {
    if (!this.accepting || this.closed || !call || typeof call.id !== "string" ||
        this.callIds.has(call.id) || this.callIds.size >= 32 || typeof call.name !== "string") return;
    this.callIds.add(call.id);
    const response = this.ensureResponse();
    if (call.name === "end_voice_session") {
      if (!call.args || typeof call.args !== "object" || Object.keys(call.args).length !== 0) {
        this.fail({ code: "browserVoiceFailed" });
        return;
      }
      await this.finish();
      return;
    }
    if (call.name !== "ask_home_assistant" || typeof call.args?.text !== "string" ||
        !call.args.text.trim() || call.args.text.length > 4000) {
      this.fail({ code: "browserVoiceFailed" });
      return;
    }
    const pending = {controller: new AbortController(), bargedIn: false};
    this.pendingTools.set(call.id, pending);
    try {
      const result = await this.onTurn(call.args.text, pending.controller.signal);
      if (this.closed || this.cancelledCallIds.has(call.id) || !this.accepting) return;
      if (typeof result?.text !== "string") throw {code: "invalid_voice_turn"};
      this.canonicalFollowupPending = !pending.bargedIn;
      if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify({
        toolResponse: { functionResponses: [{
          id: call.id,
          name: call.name,
          response: { answer: result.text },
          scheduling: pending.bargedIn ? "SILENT" : "WHEN_IDLE",
        }] },
      }));
    } catch (error) {
      if (!pending.controller.signal.aborted && !this.closed) this.fail({ code: "browserVoiceTurnFailed" });
    } finally {
      this.pendingTools.delete(call.id);
    }
  }

  cancelTool(id) {
    if (typeof id !== "string") return;
    this.cancelledCallIds.add(id);
    this.pendingTools.get(id)?.controller.abort();
  }

  fail(error) {
    if (this.closed) return;
    this.onEvent({ type: "error", code: error?.code || "browserVoiceFailed" });
    const reject = this.startReject;
    this.startReject = undefined;
    reject?.(error);
    void this.finish({ cue: false });
  }

  async finish({ cue = true } = {}) {
    if (this.finishPromise) return this.finishPromise;
    this.closed = true;
    this.accepting = false;
    this.messageGeneration++;
    clearTimeout(this.expiryTimer);
    clearTimeout(this.credentialTimer);
    this.expiryTimer = undefined;
    this.credentialTimer = undefined;
    this.startReject?.({ code: "browserVoiceFailed" });
    this.startReject = undefined;
    this.capture?.close();
    this.capture = undefined;
    this.interruptPlayback();
    for (const pending of this.pendingTools.values()) pending.controller.abort();
    this.pendingTools.clear();
    const socket = this.socket;
    this.socket = undefined;
    try { socket?.close(); } catch (_) { /* Already closed. */ }
    this.playbackContext?.close().catch(() => {});
    this.playbackContext = undefined;
    this.finishPromise = (async () => {
      let stopError;
      const transcriptsDrained = await this.drainTranscriptQueue();
      if (!transcriptsDrained) this.onEvent({ type: "error", code: "browserVoiceFailed" });
      try { await this.onStop(this.session.session_id); }
      catch (_) { stopError = true; }
      if (cue) {
        try { await this.playCue("end"); } catch (_) { /* Stop is already gated; cue failure is non-fatal. */ }
      }
      this.onEvent({ type: "ended", stopError, transcriptError: !transcriptsDrained });
    })();
    return this.finishPromise;
  }

  close() {
    return this.finish({ cue: false });
  }
}
