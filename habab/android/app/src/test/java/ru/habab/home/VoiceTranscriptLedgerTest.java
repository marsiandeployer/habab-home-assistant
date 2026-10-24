package ru.habab.home;

import org.junit.Test;
import static org.junit.Assert.*;

public class VoiceTranscriptLedgerTest {
    @Test public void independentOutputWaitsForPlaybackAndDoesNotInventUserText() {
        VoiceTranscriptLedger ledger = new VoiceTranscriptLedger(); ledger.independentOutput();
        ledger.created("gemini_local_fixture_turn_1");
        ledger.assistantTranscript("gemini_local_fixture_turn_1", "Привет!");
        ledger.queued("gemini_local_fixture_turn_1", "audio"); ledger.done("gemini_local_fixture_turn_1", true);
        assertTrue(ledger.drain().isEmpty());
        ledger.played("gemini_local_fixture_turn_1", "audio");
        VoiceTranscriptLedger.Turn event = ledger.drain().get(0);
        assertNull(event.userText); assertEquals("Привет!", event.assistantText);
        assertEquals("gemini_local_fixture_turn_1", event.eventId);
    }

    @Test public void independentToolPreambleIsNotJournaledAsDuplicateCanonicalReply() {
        VoiceTranscriptLedger ledger = new VoiceTranscriptLedger(); ledger.independentOutput();
        ledger.created("preamble"); ledger.assistantTranscript("preamble", "Секунду, проверю");
        ledger.queued("preamble", "audio"); ledger.canonicalTool("preamble");
        ledger.played("preamble", "audio"); ledger.done("preamble", true);
        assertTrue(ledger.drain().isEmpty());
    }

    @Test public void delayedAsrJoinsTheActualResponseOnlyAfterPlayback() {
        VoiceTranscriptLedger ledger = new VoiceTranscriptLedger();
        ledger.userStarted("item_user"); ledger.created("resp_one");
        ledger.queued("resp_one", "item_audio"); ledger.assistantTranscript("resp_one", "Меня зовут Habab.");
        ledger.done("resp_one", true); assertTrue(ledger.drain().isEmpty());
        ledger.played("resp_one", "item_audio"); assertTrue(ledger.drain().isEmpty());
        ledger.userTranscript("item_user", "Как тебя зовут?");
        VoiceTranscriptLedger.Turn turn = ledger.drain().get(0);
        assertEquals("resp_one", turn.eventId); assertEquals("Как тебя зовут?", turn.userText);
        assertEquals("Меня зовут Habab.", turn.assistantText); assertTrue(ledger.drain().isEmpty());
    }
    @Test public void canonicalToolPreambleAndFollowupAreNotDuplicated() {
        VoiceTranscriptLedger ledger = new VoiceTranscriptLedger();
        ledger.userStarted("item_user"); ledger.userTranscript("item_user", "Включи свет");
        ledger.created("resp_preamble"); ledger.canonicalTool("resp_preamble");
        ledger.created("resp_result"); ledger.queued("resp_result", "item_audio");
        ledger.assistantTranscript("resp_result", "Свет включён"); ledger.done("resp_result", true);
        ledger.played("resp_result", "item_audio"); assertTrue(ledger.drain().isEmpty());
    }
    @Test public void unheardInterruptedAudioNeverBecomesACompletedChatReply() {
        VoiceTranscriptLedger ledger = new VoiceTranscriptLedger();
        ledger.userStarted("item_user"); ledger.userTranscript("item_user", "Расскажи");
        ledger.created("resp_one"); ledger.assistantTranscript("resp_one", "Длинный ответ");
        ledger.queued("resp_one", "first"); ledger.queued("resp_one", "second");
        ledger.played("resp_one", "first"); ledger.done("resp_one", true);
        assertTrue(ledger.drain().isEmpty()); ledger.interrupted("resp_one");
        ledger.played("resp_one", "second"); assertTrue(ledger.drain().isEmpty());
    }
    @Test public void laterSpeechDoesNotChangeWhichUserUtteranceAsrBelongsTo() {
        VoiceTranscriptLedger ledger = new VoiceTranscriptLedger();
        ledger.userStarted("first"); ledger.created("resp_first"); ledger.queued("resp_first", "audio");
        ledger.userStarted("second"); ledger.userTranscript("second", "Другой вопрос");
        ledger.assistantTranscript("resp_first", "Первый ответ"); ledger.done("resp_first", true);
        ledger.played("resp_first", "audio"); assertTrue(ledger.drain().isEmpty());
        ledger.userTranscript("first", "Первый вопрос"); assertEquals("Первый вопрос", ledger.drain().get(0).userText);
    }
    @Test public void closingRejectsLateAsrAndPlaybackCallbacks() {
        VoiceTranscriptLedger ledger = new VoiceTranscriptLedger();
        ledger.userStarted("user"); ledger.created("resp_one"); ledger.close();
        ledger.userTranscript("user", "Поздний текст"); ledger.assistantTranscript("resp_one", "Поздний ответ");
        ledger.played("resp_one", "audio"); ledger.done("resp_one", true); assertTrue(ledger.drain().isEmpty());
    }
}
