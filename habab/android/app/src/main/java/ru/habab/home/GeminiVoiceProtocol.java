package ru.habab.home;

import java.util.*;
import org.json.*;

/** Gemini wire adapter. All turn/item IDs below are explicitly local, not provider IDs. */
final class GeminiVoiceProtocol {
    interface Events { void accept(JSONObject event) throws Exception; }
    private final Events events;
    private final String namespace = "gemini_local_" + UUID.randomUUID().toString().replace("-", "");
    private final Set<Long> inputOffsets = new HashSet<>();
    private final Map<String, String> calls = new HashMap<>();
    private final Set<String> cancelled = new HashSet<>();
    private String response;
    private StringBuilder output = new StringBuilder();
    private int ordinal;
    private boolean audio, canonicalFollowup;
    private List<JSONObject> pendingEvents;

    GeminiVoiceProtocol(Events events) { this.events = events; }

    static byte[] pcm16(short[] samples, int count) {
        byte[] result = new byte[count * 2];
        for (int i = 0; i < count; i++) { result[i * 2] = (byte) samples[i]; result[i * 2 + 1] = (byte) (samples[i] >> 8); }
        return result;
    }

    private void emit(String type, String key, Object value) throws Exception {
        JSONObject event = new JSONObject().put("type", type);
        if (key != null) event.put(key, value);
        publish(event);
    }

    private void startUser(long offset) throws Exception {
        if (!inputOffsets.add(offset)) return;
        if (inputOffsets.size() > 128) throw new IllegalStateException("voice_history_backpressure");
        String user = namespace + "_user_" + ++ordinal;
        canonicalFollowup = false;
        emit("input_audio_buffer.speech_started", "item_id", user);
    }

    private void startResponse() throws Exception {
        if (response != null) return;
        response = namespace + "_turn_" + ++ordinal;
        output = new StringBuilder(); audio = false;
        JSONObject value = new JSONObject().put("id", response);
        if (canonicalFollowup) value.put("metadata", new JSONObject().put("habab_canonical", "true"));
        emit("response.created", "response", value);
    }

    void receive(JSONObject message) throws Exception {
        List<JSONObject> decoded = new ArrayList<>();
        synchronized (this) {
            pendingEvents = decoded;
            try { decode(message); }
            finally { pendingEvents = null; }
        }
        // Never call lifecycle/playback/history code while holding the wire-state lock.
        for (JSONObject event : decoded) events.accept(event);
    }

    private void publish(JSONObject event) { pendingEvents.add(event); }

    private void decode(JSONObject message) throws Exception {
        if (message.has("setupComplete")) emit("session.created", null, null);
        JSONObject activity = message.optJSONObject("voiceActivity");
        if (activity != null) {
            if ("ACTIVITY_START".equals(activity.optString("type"))) startUser(duration(activity.optString("audioOffset", "0s")));
            else if ("ACTIVITY_END".equals(activity.optString("type"))) emit("input_audio_buffer.speech_stopped", null, null);
        }
        JSONObject cancellation = message.optJSONObject("toolCallCancellation");
        if (cancellation != null) {
            JSONArray ids = cancellation.optJSONArray("ids");
            if (ids != null) for (int i = 0; i < ids.length(); i++) {
                String id = ids.getString(i); cancelled.add(id);
                emit("habab.tool_cancelled", "call_id", id);
            }
        }
        JSONObject content = message.optJSONObject("serverContent");
        if (content != null) {
            JSONObject input = content.optJSONObject("inputTranscription");
            if (input != null && !input.optString("text").isEmpty()) {
                String text = input.getString("text");
                if (text.length() > 4000) throw new IllegalStateException("voice_history_backpressure");
                // No upstream turn ID or required offsets: preserve the observed fragment
                // independently, without inventing a link to a newer VAD/user turn.
                publish(new JSONObject().put("type", "habab.input_transcript")
                        .put("event_id", namespace + "_input_" + ++ordinal).put("text", text));
            }
            if (content.optBoolean("interrupted")) {
                emit("habab.output_interrupted", null, null);
                finishResponse(false);
            } else {
                JSONObject transcription = content.optJSONObject("outputTranscription");
                if (transcription != null && !transcription.optString("text").isEmpty()) {
                    startResponse(); output.append(transcription.getString("text"));
                    if (output.length() > 12000) throw new IllegalStateException("voice_history_backpressure");
                }
                JSONObject model = content.optJSONObject("modelTurn");
                JSONArray parts = model == null ? null : model.optJSONArray("parts");
                if (parts != null) for (int i = 0; i < parts.length(); i++) {
                    JSONObject blob = parts.getJSONObject(i).optJSONObject("inlineData");
                    if (blob == null) continue;
                    if (!"audio/pcm;rate=24000".equals(blob.optString("mimeType"))) throw new IllegalStateException("invalid_voice_audio_format");
                    startResponse(); audio = true;
                    publish(new JSONObject().put("type", "response.output_audio.delta")
                            .put("response_id", response).put("item_id", response + "_audio").put("delta", blob.getString("data")));
                }
                if (content.optBoolean("generationComplete") || content.optBoolean("turnComplete")) finishResponse(true);
            }
        }
        JSONObject tool = message.optJSONObject("toolCall");
        if (tool != null) {
            JSONArray functions = tool.getJSONArray("functionCalls");
            for (int i = 0; i < functions.length(); i++) {
                JSONObject function = functions.getJSONObject(i);
                String id = function.getString("id"), name = function.getString("name");
                if (calls.size() >= 32 || calls.containsKey(id)) throw new IllegalStateException("invalid_voice_tool");
                calls.put(id, name); startResponse();
                publish(new JSONObject().put("type", "response.function_call_arguments.done")
                        .put("response_id", response).put("call_id", id).put("name", name)
                        .put("arguments", function.getJSONObject("args").toString()));
            }
        }
    }

    private void finishResponse(boolean completed) throws Exception {
        if (response == null) return;
        String id = response; response = null;
        if (completed && output.length() > 0) publish(new JSONObject().put("type", "response.output_audio_transcript.done")
                .put("response_id", id).put("transcript", output.toString()));
        if (completed && audio) publish(new JSONObject().put("type", "response.output_audio.done")
                .put("response_id", id).put("item_id", id + "_audio"));
        emit("response.done", "response", new JSONObject().put("id", id).put("status", completed ? "completed" : "cancelled"));
    }

    synchronized JSONObject encode(JSONObject event) throws JSONException {
        String type = event.optString("type");
        if (type.equals("input_audio_buffer.append")) return new JSONObject().put("realtimeInput", new JSONObject()
                .put("audio", new JSONObject().put("mimeType", "audio/pcm;rate=16000").put("data", event.getString("audio"))));
        if (type.equals("conversation.item.create")) {
            JSONObject item = event.getJSONObject("item");
            if (!"function_call_output".equals(item.optString("type"))) return null;
            String id = item.getString("call_id"), name = calls.get(id);
            if (name == null || cancelled.contains(id)) return null;
            boolean silent = event.optBoolean("habab_silent");
            canonicalFollowup = !silent;
            return new JSONObject().put("toolResponse", new JSONObject().put("functionResponses", new JSONArray().put(
                    new JSONObject().put("id", id).put("name", name).put("response", new JSONObject(item.getString("output")))
                            .put("scheduling", silent ? "SILENT" : "WHEN_IDLE"))));
        }
        // Gemini interrupts at server VAD and resumes from toolResponse. Its setup is immutable.
        // Sending OpenAI's truncate/cancel/context/response.create here would be invalid or trigger speech.
        return null;
    }

    private static long duration(String value) {
        if (!value.endsWith("s")) throw new IllegalArgumentException("invalid_audio_offset");
        return new java.math.BigDecimal(value.substring(0, value.length() - 1)).movePointRight(6).longValueExact();
    }
}
