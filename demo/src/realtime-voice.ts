import { LitElement, css, html } from "lit";
import { customElement, state } from "lit/decorators";
import { fireEvent } from "../../src/common/dom/fire_event";

const VOICE_AUTH_TYPE = "habab.demo.openai.auth.v1";
const SAMPLE_RATE = 24000;
const LIGHTS = new Set([
  "light.living_room_floor_lamp",
  "light.living_room_spotlights",
  "light.kitchen_spotlights",
  "light.kitchen_worktop",
  "light.entrance_ceiling",
  "light.bedroom_ceiling",
  "light.bedside_lamp",
  "light.office_desk_lamp",
  "light.garden_path",
]);

type SessionTool = { item_id: string; call_id: string; name: string; arguments?: string };
type ResponseItem = {
  type?: string;
  id?: string;
  call_id?: string;
  name?: string;
  arguments?: string;
};
type PlaybackPart = {
  item_id: string;
  content_index: number;
  response_id?: string;
  rendered_duration: number;
  sources: Map<AudioBufferSourceNode, { start_time: number; duration: number }>;
  finished: boolean;
};
type RealtimeEvent = {
  type: string;
  delta?: string;
  item_id?: string;
  response_id?: string;
  content_index?: number;
  call_id?: string;
  name?: string;
  arguments?: string;
  transcript?: string;
  item?: ResponseItem;
  part?: { type?: string };
  response?: { id?: string; status?: string; output?: ResponseItem[] };
};

@customElement("ha-demo-realtime-voice")
class HaDemoRealtimeVoice extends LitElement {
  @state() private _status = "Голосовой помощник выключен";
  @state() private _running = false;
  @state() private _starting = false;
  @state() private _transcript = "";

  private _socket?: WebSocket;
  private _stream?: MediaStream;
  private _context?: AudioContext;
  private _processor?: ScriptProcessorNode;
  private _nextPlaybackTime = 0;
  private _sources = new Set<AudioBufferSourceNode>();
  private _playbackParts = new Map<string, PlaybackPart>();
  private _pendingResponses = new Map<string, Map<string, SessionTool>>();

  static styles = css`
    :host {
      position: fixed;
      z-index: 999;
      left: max(16px, env(safe-area-inset-left));
      inset-block-end: max(16px, env(safe-area-inset-bottom));
      width: min(360px, calc(100vw - 32px));
      color: var(--primary-text-color, #212121);
      font: 14px Roboto, Arial, sans-serif;
    }
    @media (max-width: 720px) {
      :host {
        inset-block-start: max(16px, env(safe-area-inset-top));
        inset-block-end: auto;
      }
    }
    section {
      box-sizing: border-box;
      padding: 16px;
      border-radius: 14px;
      background: var(--card-background-color, #fff);
      box-shadow: 0 4px 24px #0003;
    }
    p { margin: 8px 0; line-height: 1.45; }
    .privacy { color: var(--secondary-text-color, #5f6368); font-size: 12px; }
    .transcript { max-height: 120px; overflow: auto; white-space: pre-wrap; }
    .actions { display: flex; gap: 8px; margin-top: 12px; }
    button {
      min-height: 44px;
      padding: 0 16px;
      border: 0;
      border-radius: 8px;
      color: var(--text-primary-color, #fff);
      background: var(--primary-color, #03a9f4);
      font: inherit;
      font-weight: 600;
      cursor: pointer;
    }
    button[disabled] { opacity: 0.55; cursor: default; }
    button.secondary { color: var(--primary-text-color, #212121); background: var(--secondary-background-color, #eee); }
  `;

  render() {
    return html`<section aria-label="Голосовой помощник демо-квартиры">
      <strong>${this._status}</strong>
      <p class="privacy">Микрофон включится после нажатия. Звук проходит через сервер Habab в OpenAI.</p>
      ${this._transcript ? html`<p class="transcript" aria-live="polite">${this._transcript}</p>` : ""}
      <div class="actions">
        ${this._running
          ? html`<button class="secondary" @click=${this._stopFromClick}>Остановить</button>`
          : html`<button ?disabled=${this._starting} @click=${this._start}>
              ${this._starting ? "Подключаемся…" : "Начать разговор"}
            </button>`}
      </div>
    </section>`;
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this._stop();
  }

  private async _start() {
    if (this._running || this._starting) return;
    this._starting = true;
    this._transcript = "";
    this._status = "Запрашиваем доступ к микрофону…";
    try {
      // Both calls begin from the user's click. No microphone access happens at page load.
      const media = await navigator.mediaDevices.getUserMedia({ audio: true });
      this._stream = media;
      this._context = new AudioContext({ sampleRate: SAMPLE_RATE });
      await this._context.resume();
      const grantResponse = await fetch("/api/ha-demo/realtime/bootstrap", {
        method: "POST",
        credentials: "same-origin",
      });
      if (!grantResponse.ok) throw new Error("Голосовой помощник сейчас недоступен.");
      const grant = await grantResponse.json();
      if (typeof grant.value !== "string" || typeof grant.websocket_url !== "string") {
        throw new Error("Не удалось открыть голосовую сессию.");
      }
      const socket = new WebSocket(grant.websocket_url);
      this._socket = socket;
      socket.addEventListener("open", () => {
        if (this._socket !== socket || !this._running) {
          socket.close();
          return;
        }
        socket.send(JSON.stringify({ type: VOICE_AUTH_TYPE, token: grant.value }));
      }, { once: true });
      socket.addEventListener("message", (event) => void this._message(event));
      socket.addEventListener("error", () => {
        this._stop(false, "Не удалось подключиться. Проверьте интернет и попробуйте снова.");
      }, { once: true });
      socket.addEventListener("close", () => {
        if (!this._running) return;
        const status = /Не удалось|не принял|недоступен/.test(this._status)
          ? this._status
          : "Соединение завершено.";
        this._stop(false, status);
      }, { once: true });
      this._running = true;
      this._starting = false;
      this._status = "Подключаемся…";
    } catch (error) {
      const message = error instanceof Error && error.name === "NotAllowedError"
        ? "Разрешите доступ к микрофону и попробуйте снова."
        : "Голосовой помощник сейчас недоступен. Проверьте микрофон и соединение.";
      this._starting = false;
      this._stop(false, message);
    }
  }

  private async _message(message: MessageEvent<string>) {
    if (typeof message.data !== "string") return;
    let event: RealtimeEvent;
    try {
      event = JSON.parse(message.data);
    } catch {
      return;
    }
    if (event.type === "session.updated") {
      this._status = "Слушаю. Говорите по-русски.";
      this._startCapture();
    } else if (event.type === "response.created" && event.response?.id) {
      this._pendingResponses.set(event.response.id, new Map());
    } else if (event.type === "response.output_item.added" && event.response_id
        && event.item?.type === "function_call" && event.item.id
        && event.item.call_id && event.item.name) {
      this._pendingResponses.get(event.response_id)?.set(event.item.id, {
        item_id: event.item.id,
        call_id: event.item.call_id,
        name: event.item.name,
      });
    } else if (event.type === "response.function_call_arguments.done" && event.response_id
        && event.item_id && event.call_id && event.name && event.arguments !== undefined) {
      const call = this._pendingResponses.get(event.response_id)?.get(event.item_id);
      if (call?.call_id === event.call_id && call.name === event.name) {
        call.arguments = event.arguments;
      }
    } else if (event.type === "response.done") {
      await this._completeResponse(event);
    } else if (event.type === "conversation.item.input_audio_transcription.completed") {
      this._transcript = `Вы: ${event.transcript ?? ""}`;
    } else if (event.type === "response.output_audio_transcript.delta") {
      this._transcript += event.delta ?? "";
    } else if (event.type === "response.output_audio.delta" && event.delta) {
      await this._playPcm(event.delta, event.item_id, event.content_index, event.response_id);
    } else if (event.type === "response.output_audio.done"
        || (event.type === "response.content_part.done" && event.part?.type === "audio")) {
      this._finishPlaybackPart(event.item_id, event.content_index);
    } else if (event.type === "input_audio_buffer.speech_started") {
      this._pendingResponses.clear();
      this._interruptPlayback(true);
    } else if (event.type === "error") {
      this._status = "Сервис не принял голосовой запрос. Попробуйте ещё раз.";
    }
  }

  private _startCapture() {
    if (!this._context || !this._stream || !this._socket) return;
    const source = this._context.createMediaStreamSource(this._stream);
    const processor = this._context.createScriptProcessor(4096, 1, 1);
    const muted = this._context.createGain();
    muted.gain.value = 0;
    processor.onaudioprocess = (event) => {
      if (this._socket?.readyState !== WebSocket.OPEN) return;
      const input = event.inputBuffer.getChannelData(0);
      const pcm = this._resamplePcm(input, this._context!.sampleRate, SAMPLE_RATE);
      if (pcm.length) {
        let binary = "";
        for (const byte of new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength)) {
          binary += String.fromCharCode(byte);
        }
        this._socket.send(JSON.stringify({ type: "input_audio_buffer.append", audio: btoa(binary) }));
      }
    };
    source.connect(processor);
    processor.connect(muted);
    muted.connect(this._context.destination);
    this._processor = processor;
  }

  private _resamplePcm(input: Float32Array, inputRate: number, outputRate: number): Int16Array {
    const ratio = inputRate / outputRate;
    const output = new Int16Array(Math.floor(input.length / ratio));
    for (let i = 0; i < output.length; i++) {
      const start = Math.floor(i * ratio);
      const end = Math.min(input.length, Math.floor((i + 1) * ratio));
      let sum = 0;
      for (let j = start; j < end; j++) sum += input[j];
      const sample = Math.max(-1, Math.min(1, sum / Math.max(1, end - start)));
      output[i] = sample < 0 ? sample * 32768 : sample * 32767;
    }
    return output;
  }

  private async _playPcm(encoded: string, itemId?: string, contentIndex?: number, responseId?: string) {
    if (!this._context || !encoded) return;
    const binary = atob(encoded);
    const pcm = new Int16Array(binary.length / 2);
    for (let i = 0; i < pcm.length; i++) {
      pcm[i] = (binary.charCodeAt(i * 2 + 1) << 8) | binary.charCodeAt(i * 2);
    }
    const buffer = this._context.createBuffer(1, pcm.length, SAMPLE_RATE);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) channel[i] = pcm[i] / 32768;
    const source = this._context.createBufferSource();
    source.buffer = buffer;
    source.connect(this._context.destination);
    const startAt = Math.max(this._context.currentTime + 0.03, this._nextPlaybackTime);
    source.start(startAt);
    const endAt = startAt + buffer.duration;
    this._nextPlaybackTime = endAt;
    const partKey = itemId && Number.isInteger(contentIndex)
      ? `${itemId}:${contentIndex}`
      : undefined;
    let playbackPart: PlaybackPart | undefined;
    if (partKey && itemId && contentIndex !== undefined) {
      playbackPart = this._playbackParts.get(partKey) ?? {
        item_id: itemId,
        content_index: contentIndex,
        response_id: responseId,
        rendered_duration: 0,
        sources: new Map(),
        finished: false,
      };
      playbackPart.response_id ??= responseId;
      playbackPart.sources.set(source, { start_time: startAt, duration: buffer.duration });
      this._playbackParts.set(partKey, playbackPart);
    }
    this._sources.add(source);
    source.addEventListener("ended", () => {
      this._sources.delete(source);
      if (!partKey || !playbackPart) return;
      const playback = playbackPart.sources.get(source);
      if (!playback) return;
      playbackPart.rendered_duration += playback.duration;
      playbackPart.sources.delete(source);
      if (playbackPart.finished && playbackPart.sources.size === 0) {
        this._playbackParts.delete(partKey);
      }
    }, { once: true });
  }

  private _finishPlaybackPart(itemId?: string, contentIndex?: number) {
    if (!itemId || contentIndex === undefined) return;
    const partKey = `${itemId}:${contentIndex}`;
    const part = this._playbackParts.get(partKey);
    if (!part) return;
    part.finished = true;
    if (part.sources.size === 0) this._playbackParts.delete(partKey);
  }

  private _finishPlaybackResponse(responseId: string) {
    for (const [partKey, part] of this._playbackParts) {
      if (part.response_id !== responseId) continue;
      part.finished = true;
      if (part.sources.size === 0) this._playbackParts.delete(partKey);
    }
  }

  private _interruptPlayback(truncate = false) {
    const now = this._context?.currentTime ?? 0;
    if (truncate && this._socket?.readyState === WebSocket.OPEN) {
      for (const part of this._playbackParts.values()) {
        const inFlightDuration = [...part.sources.values()].reduce(
          (total, source) => total + Math.max(0, Math.min(now - source.start_time, source.duration)),
          0
        );
        const playedMs = Math.floor((part.rendered_duration + inFlightDuration) * 1000 + 1e-6);
        this._socket.send(JSON.stringify({
          type: "conversation.item.truncate",
          item_id: part.item_id,
          content_index: part.content_index,
          audio_end_ms: playedMs,
        }));
      }
    }
    for (const source of this._sources) source.stop();
    this._sources.clear();
    this._playbackParts.clear();
    this._nextPlaybackTime = this._context?.currentTime ?? 0;
  }

  private async _completeResponse(event: RealtimeEvent) {
    const responseId = event.response?.id;
    if (!responseId) return;
    this._finishPlaybackResponse(responseId);
    const calls = this._pendingResponses.get(responseId);
    this._pendingResponses.delete(responseId);
    if (!calls || event.response?.status !== "completed") return;

    const completedCalls = new Map(
      (event.response.output ?? [])
        .filter((item) => item.type === "function_call" && item.id)
        .map((item) => [item.id!, item])
    );
    for (const call of calls.values()) {
      const completed = completedCalls.get(call.item_id);
      if (!call.arguments || !completed || completed.call_id !== call.call_id
          || completed.name !== call.name || completed.arguments !== call.arguments) continue;
      await this._runTool(call, call.arguments);
    }
  }

  private async _runTool(call: SessionTool, argumentsJson: string) {
    if (!call || !this._socket || this._socket.readyState !== WebSocket.OPEN) return;
    try {
      const args: unknown = JSON.parse(argumentsJson);
      if (!args || typeof args !== "object" || Array.isArray(args)) throw new Error("invalid_arguments");
      const values = args as Record<string, unknown>;
      let items: { entity_id: string; state: string; brightness?: number; temperature?: number }[];
      if (call.name === "set_demo_light" && typeof values.entity_id === "string"
          && LIGHTS.has(values.entity_id) && ["on", "off"].includes(String(values.state))) {
        items = [{ entity_id: values.entity_id, state: String(values.state) }];
      } else if (call.name === "set_demo_climate" && ["heat", "off", "auto"].includes(String(values.state))
          && (values.temperature === undefined || (typeof values.temperature === "number"
            && values.temperature >= 7 && values.temperature <= 35))) {
        items = [{ entity_id: "climate.living_room", state: String(values.state),
          ...(values.temperature === undefined ? {} : { temperature: values.temperature }) }];
      } else if (call.name === "activate_demo_scene" && typeof values.entity_id === "string"
          && ["scene.evening", "scene.focus"].includes(values.entity_id)) {
        items = values.entity_id === "scene.evening"
          ? [
              { entity_id: "light.living_room_floor_lamp", state: "on", brightness: 180 },
              { entity_id: "light.living_room_spotlights", state: "off" },
            ]
          : [
              { entity_id: "light.office_desk_lamp", state: "on", brightness: 255 },
              { entity_id: "light.living_room_floor_lamp", state: "off" },
            ];
      } else {
        throw new Error("unsupported_action");
      }
      const response = await fetch("/api/ha-demo/state", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ entities: items }),
      });
      if (!response.ok) throw new Error("state_not_saved");
      fireEvent(this, "habab-demo-realtime-action", { entities: items });
      this._socket.send(JSON.stringify({ type: "conversation.item.create", item: {
        type: "function_call_output", call_id: call.call_id, output: JSON.stringify({ saved: true }),
      } }));
    } catch {
      this._socket.send(JSON.stringify({ type: "conversation.item.create", item: {
        type: "function_call_output", call_id: call.call_id, output: JSON.stringify({ saved: false }),
      } }));
    }
    this._socket.send(JSON.stringify({ type: "response.create" }));
  }

  private _stop(closeSocket = true, status = "Голосовой помощник выключен") {
    this._processor?.disconnect();
    this._processor = undefined;
    this._stream?.getTracks().forEach((track) => track.stop());
    this._stream = undefined;
    this._interruptPlayback();
    if (closeSocket && this._socket && this._socket.readyState < WebSocket.CLOSING) {
      this._socket.close();
    }
    this._socket = undefined;
    void this._context?.close();
    this._context = undefined;
    this._pendingResponses.clear();
    this._running = false;
    this._starting = false;
    this._status = status;
  }

  private _stopFromClick() {
    this._stop();
  }
}

customElements.whenDefined("ha-demo").then(() => {
  if (!document.querySelector("ha-demo-realtime-voice")) {
    document.body.append(document.createElement("ha-demo-realtime-voice"));
  }
});

declare global {
  interface HASSDomEvents {
    "habab-demo-realtime-action": {
      entities: { entity_id: string; state: string; brightness?: number; temperature?: number }[];
    };
  }

  interface WindowEventMap {
    "habab-demo-realtime-action": CustomEvent<HASSDomEvents["habab-demo-realtime-action"]>;
  }
}
