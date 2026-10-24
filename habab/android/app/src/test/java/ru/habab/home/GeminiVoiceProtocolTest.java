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
