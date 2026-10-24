package ru.habab.home;

import java.util.*;
import org.json.*;
import org.junit.Test;
import static org.junit.Assert.*;

/** Protects Google/OpenAI wire separation, interruption and exact tool correlation. */
public class GeminiVoiceProtocolTest {
    @Test public void ephemeralCredentialOnlyReachesExplicitlySelectedTrustedTransport() {
        String direct = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContentConstrained";
        String habab = "wss://habab-voice.wpmix.net/gemini/ws";
        assertTrue(GeminiVoiceProtocol.allowedEndpoint(direct, "direct"));
        assertTrue(GeminiVoiceProtocol.allowedEndpoint(habab, "habab"));
        assertFalse(GeminiVoiceProtocol.allowedEndpoint(habab, "direct"));
        assertFalse(GeminiVoiceProtocol.allowedEndpoint(direct, "habab"));
        assertFalse(GeminiVoiceProtocol.allowedEndpoint(habab, "unknown"));
        for (String address : Arrays.asList(
                "ws://habab-voice.wpmix.net/gemini/ws",
                "wss://habab-voice.wpmix.net.attacker.example/gemini/ws",
                "wss://habab-voice.wpmix.net@attacker.example/gemini/ws",
                habab + "?key=untrusted", habab + "#fragment", habab + "/")) {
            assertFalse(address, GeminiVoiceProtocol.allowedEndpoint(address, "habab"));
        }
    }

    private final List<JSONObject> events = new ArrayList<>();
    private final GeminiVoiceProtocol protocol = new GeminiVoiceProtocol(events::add);
    private void receive(String json) throws Exception { protocol.receive(new JSONObject(json)); }
    private List<String> types() { List<String> result = new ArrayList<>(); for (JSONObject e : events) result.add(e.optString("type")); return result; }

    @Test public void setupAndRealAudioEventsBecomeLocalCorrelatedTurns() throws Exception {
        receive("{setupComplete:{}}");
        receive("{voiceActivity:{type:'ACTIVITY_START',audioOffset:'1.250s'}}");
        receive("{serverContent:{inputTranscription:{text:'Привет',startOffset:'1.250s'}}}");
        receive("{voiceActivity:{type:'ACTIVITY_END',audioOffset:'2s'}}");
        receive("{serverContent:{modelTurn:{parts:[{inlineData:{mimeType:'audio/pcm;rate=24000',data:'AAA='}}]},outputTranscription:{text:'Привет!'}}}");
        receive("{serverContent:{generationComplete:true}}");
        receive("{serverContent:{turnComplete:true}}");
        assertEquals(Arrays.asList("session.created", "input_audio_buffer.speech_started",
                "habab.input_transcript", "input_audio_buffer.speech_stopped",
                "response.created", "response.output_audio.delta", "response.output_audio_transcript.done",
                "response.output_audio.done", "response.done"), types());
        String id = events.get(4).getJSONObject("response").getString("id");
        assertTrue(id.startsWith("gemini_local_"));
        assertEquals(id, events.get(5).getString("response_id"));
        assertEquals("Привет!", events.get(6).getString("transcript"));
    }

    @Test public void interruptedAudioNeverCompletesAsHeard() throws Exception {
        receive("{voiceActivity:{type:'ACTIVITY_START',audioOffset:'0s'}}");
        receive("{serverContent:{modelTurn:{parts:[{inlineData:{mimeType:'audio/pcm;rate=24000',data:'AAA='}}]}}}");
        receive("{serverContent:{interrupted:true}}");
        receive("{serverContent:{turnComplete:true}}");
        assertTrue(types().contains("habab.output_interrupted"));
        assertFalse(types().contains("response.output_audio.done"));
        assertEquals("cancelled", events.get(events.size() - 1).getJSONObject("response").getString("status"));
    }

    @Test public void inputUsesSixteenKhzAndLittleEndianWithoutResampling() throws Exception {
        assertArrayEquals(new byte[]{0,1,-1,-1}, GeminiVoiceProtocol.pcm16(new short[]{256,-1},2));
        JSONObject wire = protocol.encode(new JSONObject("{type:'input_audio_buffer.append',audio:'AAA='}"));
        assertEquals("audio/pcm;rate=16000", wire.getJSONObject("realtimeInput").getJSONObject("audio").getString("mimeType"));
        assertFalse(wire.has("type"));
    }

    @Test public void toolResultUsesRealProviderCallIdAndSilentStaleScheduling() throws Exception {
        receive("{voiceActivity:{type:'ACTIVITY_START',audioOffset:'0s'}}");
        receive("{toolCall:{functionCalls:[{id:'provider-call-1',name:'ask_home_assistant',args:{text:'Проверь свет'}}]}}");
        JSONObject message = new JSONObject().put("type","conversation.item.create").put("habab_silent",true)
                .put("item", new JSONObject().put("type","function_call_output").put("call_id","provider-call-1").put("output","{answer:'Готово'}"));
        JSONObject response = protocol.encode(message).getJSONObject("toolResponse").getJSONArray("functionResponses").getJSONObject(0);
        assertEquals("provider-call-1", response.getString("id"));
        assertEquals("SILENT", response.getString("scheduling"));
        receive("{toolCallCancellation:{ids:['provider-call-1']}}");
        assertNull(protocol.encode(message));
        assertEquals("habab.tool_cancelled", events.get(events.size()-1).getString("type"));
    }

    @Test public void toolWithoutPrefaceGetsFreshCanonicalResponseAfterToolResult() throws Exception {
        VoiceToolSession session = new VoiceToolSession(1_000);
        session.interrupt(1);
        receive("{voiceActivity:{type:'ACTIVITY_START',audioOffset:'0s'}}");
        receive("{toolCall:{functionCalls:[{id:'provider-call-2',name:'ask_home_assistant',args:{text:'Найди устройство'}}]}} ");
        JSONObject initial = events.stream().filter(e -> "response.created".equals(e.optString("type"))).findFirst().orElseThrow();
        String initialId = initial.getJSONObject("response").getString("id");
        assertNull(initial.getJSONObject("response").optJSONObject("metadata"));
        VoiceResponseGate responseGate = new VoiceResponseGate();
        responseGate.created(initialId);
        assertTrue(session.beginTool("provider-call-2", 1, 2_000));
        receive("{serverContent:{generationComplete:true,turnComplete:true}}");
        boolean initialCompleted = events.stream().anyMatch(e -> "response.done".equals(e.optString("type"))
                && initialId.equals(e.optJSONObject("response").optString("id")));
        assertTrue(initialCompleted);
        responseGate.done(initialId);

        JSONObject result = new JSONObject().put("type", "conversation.item.create").put("item",
                new JSONObject().put("type", "function_call_output").put("call_id", "provider-call-2")
                        .put("output", "{answer:'Нашёл устройство'}"));
        JSONObject[] wire = new JSONObject[1];
        VoiceToolSession.Delivery delivery = session.deliverResult("provider-call-2", 1, 1, false, silent -> {
            try { wire[0] = protocol.encode(result); return wire[0] != null; }
            catch (JSONException error) { return false; }
        });
        assertEquals(VoiceToolSession.Delivery.SENT, delivery);
        JSONObject toolResponse = wire[0];
        assertEquals("WHEN_IDLE", toolResponse.getJSONObject("toolResponse").getJSONArray("functionResponses")
                .getJSONObject(0).getString("scheduling"));

        events.clear();
        receive("{serverContent:{modelTurn:{parts:[{inlineData:{mimeType:'audio/pcm;rate=24000',data:'AAA='}}]},outputTranscription:{text:'Нашёл устройство.'},generationComplete:true}} ");
        JSONObject created = events.stream().filter(e -> "response.created".equals(e.optString("type"))).findFirst().orElse(null);
        assertNotNull("tool response must start a fresh local response", created);
        String canonicalId = created.getJSONObject("response").getString("id");
        assertNotEquals(initialId, canonicalId);
        assertEquals("true", created.getJSONObject("response").getJSONObject("metadata").getString("habab_canonical"));
        boolean oldClosedBeforeCanonical = initialCompleted;
        for (JSONObject event : events) {
            if ("response.done".equals(event.optString("type"))
                    && initialId.equals(event.getJSONObject("response").optString("id"))) {
                responseGate.done(initialId); oldClosedBeforeCanonical = true;
            } else if ("response.created".equals(event.optString("type"))) {
                responseGate.created(event.getJSONObject("response").getString("id"));
            } else if ("response.done".equals(event.optString("type"))) {
                responseGate.done(event.getJSONObject("response").getString("id"));
            }
        }
        assertTrue("the pre-tool response must close before the canonical response", oldClosedBeforeCanonical);
        assertNull(responseGate.done(initialId));
        assertNotNull("the response gate must be free after canonical completion", responseGate.request(() -> {}));
        session.followupStarted(canonicalId);
        session.audioStarted(canonicalId);
        for (JSONObject event : events) {
            if ("response.done".equals(event.optString("type")))
                session.responseDone(event.getJSONObject("response").getString("id"));
        }
        assertEquals(VoiceToolSession.DeadlineAction.DRAIN_RESULT, session.deadlineAction(VoiceToolSession.AUDIO_LIMIT_MS + 1_001));
        session.audioPlayed(canonicalId);
        assertEquals(VoiceToolSession.DeadlineAction.FINISH, session.deadlineAction(VoiceToolSession.AUDIO_LIMIT_MS + 1_002));
    }

    @Test public void prefaceAudioRemainsOutsideCanonicalToolFollowupUntilItsBoundary() throws Exception {
        VoiceToolSession session = new VoiceToolSession(1_000);
        session.interrupt(1);
        receive("{voiceActivity:{type:'ACTIVITY_START',audioOffset:'0s'}}");
        receive("{serverContent:{modelTurn:{parts:[{inlineData:{mimeType:'audio/pcm;rate=24000',data:'AAA='}}]},outputTranscription:{text:'Секунду, проверю.'}}}");
        String prefaceId = events.stream().filter(e -> "response.created".equals(e.optString("type")))
                .findFirst().orElseThrow().getJSONObject("response").getString("id");
        receive("{toolCall:{functionCalls:[{id:'provider-call-preface',name:'ask_home_assistant',args:{text:'Найди устройство'}}]}} ");
        JSONObject call = events.stream().filter(e -> "response.function_call_arguments.done".equals(e.optString("type")))
                .findFirst().orElseThrow();
        assertEquals(prefaceId, call.getString("response_id"));
        assertTrue(session.beginTool("provider-call-preface", 1, 2_000));

        JSONObject result = toolResult("provider-call-preface");
        VoiceToolSession.Delivery delivery = session.deliverResult("provider-call-preface", 1, 1, false, silent -> {
            try { return protocol.encode(result) != null; }
            catch (JSONException error) { return false; }
        });
        assertEquals(VoiceToolSession.Delivery.SENT, delivery);
        events.clear();

        receive("{serverContent:{modelTurn:{parts:[{inlineData:{mimeType:'audio/pcm;rate=24000',data:'AQI='}}]},outputTranscription:{text:'Проверяю'}}}");
        JSONObject morePreface = events.stream().filter(e -> "response.output_audio.delta".equals(e.optString("type")))
                .findFirst().orElseThrow();
        assertEquals(prefaceId, morePreface.getString("response_id"));
        boolean canonicalCreated = false;
        for (JSONObject event : events) {
            if (!"response.created".equals(event.optString("type"))) continue;
            JSONObject metadata = event.getJSONObject("response").optJSONObject("metadata");
            canonicalCreated |= metadata != null && metadata.optString("habab_canonical").equals("true");
        }
        assertFalse(canonicalCreated);
        assertEquals(VoiceToolSession.DeadlineAction.DRAIN_RESULT,
                session.deadlineAction(VoiceToolSession.AUDIO_LIMIT_MS + 1_001));

        receive("{serverContent:{generationComplete:true,turnComplete:true}}");
        session.responseDone(prefaceId);
        assertEquals(VoiceToolSession.DeadlineAction.DRAIN_RESULT,
                session.deadlineAction(VoiceToolSession.AUDIO_LIMIT_MS + 1_002));

        events.clear();
        receive("{serverContent:{modelTurn:{parts:[{inlineData:{mimeType:'audio/pcm;rate=24000',data:'AwQ='}}]},outputTranscription:{text:'Устройство найдено.'},generationComplete:true,turnComplete:true}}");
        JSONObject canonical = events.stream().filter(e -> "response.created".equals(e.optString("type")))
                .findFirst().orElseThrow();
        String canonicalId = canonical.getJSONObject("response").getString("id");
        assertNotEquals(prefaceId, canonicalId);
        assertEquals("true", canonical.getJSONObject("response").getJSONObject("metadata").getString("habab_canonical"));
        session.followupStarted(canonicalId);
        session.audioStarted(canonicalId);
        session.responseDone(canonicalId);
        assertEquals(VoiceToolSession.DeadlineAction.DRAIN_RESULT,
                session.deadlineAction(VoiceToolSession.AUDIO_LIMIT_MS + 1_003));
        session.audioPlayed(canonicalId);
        assertEquals(VoiceToolSession.DeadlineAction.FINISH,
                session.deadlineAction(VoiceToolSession.AUDIO_LIMIT_MS + 1_004));
    }

    @Test public void sameFrameToolCallAndGenerationCompleteCloseTheToolResponse() throws Exception {
        receive("{toolCall:{functionCalls:[{id:'provider-call-frame',name:'ask_home_assistant',args:{text:'Найди устройство'}}]},serverContent:{generationComplete:true,turnComplete:true}}");
        JSONObject call = events.stream().filter(e -> "response.function_call_arguments.done".equals(e.optString("type")))
                .findFirst().orElseThrow();
        String responseId = call.getString("response_id");
        assertTrue(events.stream().anyMatch(e -> "response.done".equals(e.optString("type"))
                && responseId.equals(e.optJSONObject("response").optString("id"))));
    }

    private JSONObject toolResult(String callId) throws JSONException {
        return new JSONObject().put("type", "conversation.item.create").put("item",
                new JSONObject().put("type", "function_call_output").put("call_id", callId)
                        .put("output", "{answer:'Нашёл устройство'}"));
    }

    @Test public void clockCancelAndResponseCreateNeverTriggerGoogleGeneration() throws Exception {
        for (String type : new String[]{"response.cancel","response.create","conversation.item.truncate"})
            assertNull(protocol.encode(new JSONObject().put("type",type)));
        assertNull(protocol.encode(new JSONObject("{type:'conversation.item.create',item:{type:'message',role:'system',content:[]}}")));
    }

    @Test public void lateAsrFragmentsHaveIndependentLocalIdsAndNeverClaimUserTurnCorrelation() throws Exception {
        receive("{voiceActivity:{type:'ACTIVITY_START',audioOffset:'0.12s'}}");
        receive("{serverContent:{inputTranscription:{text:'Первая часть'}}}");
        String first = events.get(1).getString("event_id");
        receive("{voiceActivity:{type:'ACTIVITY_START',audioOffset:'1.4s'}}");
        receive("{serverContent:{inputTranscription:{text:'Поздняя часть'}}}");
        JSONObject late = events.get(events.size()-1);
        assertEquals("habab.input_transcript", late.getString("type"));
        assertEquals("Поздняя часть", late.getString("text"));
        assertFalse(late.has("item_id"));
        assertFalse(late.has("response_id"));
        assertNotEquals(first, late.getString("event_id"));
    }
}
