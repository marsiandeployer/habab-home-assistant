package ru.habab.home;

import static org.junit.Assert.*;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.After;
import org.json.JSONObject;
import org.junit.Test;

public class WakeVerifierTest {
    private final List<WakeVerifier> workers = new ArrayList<>();
    private WakeVerifier create(WakeVerifier.Decoder decoder, WakeVerifier.Clock clock) {
        WakeVerifier verifier = new WakeVerifier(decoder, clock);
        workers.add(verifier);
        return verifier;
    }
    @After public void closeWorkers() { for (WakeVerifier verifier : workers) verifier.close(); }
    private static WakeVerifier.Context context(long attempt) {
        return new WakeVerifier.Context(attempt, 100, WakeMatchGate.MatchSource.VAD_END_FINAL, 90, 0, 900);
    }
    private static WakeVerifier.Result await(WakeVerifier verifier) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(3);
        WakeVerifier.Result result;
        while ((result = verifier.poll()) == null && System.nanoTime() < deadline) Thread.sleep(1);
        assertNotNull("bounded worker did not complete", result);
        return result;
    }
    private static WakeVerifier.Result verify(WakeVerifier verifier, JSONObject candidate) throws Exception {
        WakeVerifier.Result immediate = verifier.submit(candidate, context(1));
        return immediate == null ? await(verifier) : immediate;
    }
    private static JSONObject candidate(double start, double end) throws Exception {
        double middle = (start + end) / 2;
        return new JSONObject("{\"result\":[{\"start\":" + start + ",\"end\":" + middle
                + "},{\"start\":" + middle + ",\"end\":" + end + "}]}");
    }
    private static short[] samples(int count, int offset) {
        short[] samples = new short[count];
        for (int i = 0; i < count; i++) samples[i] = (short) (offset + i);
        return samples;
    }
    private static String response(String text) {
        return "{\"alternatives\":[{\"text\":\"" + text + "\"}]}";
    }

    @Test public void ringRetainsOnlyCapacityAndUsesAbsoluteIndexesAfterWrapAndClear() {
        WakeVerifier.AudioWindow ring = new WakeVerifier.AudioWindow(5);
        ring.append(samples(4, 0), 4);
        ring.append(samples(9, 4), 9);
        assertEquals(8, ring.start()); assertEquals(13, ring.end());
        assertArrayEquals(new short[]{8, 9, 10, 11, 12}, ring.crop(8, 13));
        assertNull(ring.crop(7, 10)); assertNull(ring.crop(10, 14)); assertNull(ring.crop(10, 10));
        ring.clear();
        assertEquals(13, ring.start()); assertEquals(13, ring.end());
        assertNull(ring.crop(8, 13));
        ring.append(samples(3, 90), 3);
        assertArrayEquals(new short[]{90, 91, 92}, ring.crop(13, 16));
    }

    @Test public void verificationUsesBoundedContextsAndFullVocabularyAlternatives() throws Exception {
        List<short[]> decoded = new ArrayList<>();
        WakeVerifier verifier = create(audio -> {
            decoded.add(audio);
            return decoded.size() == 4
                    ? "{\"alternatives\":[{\"text\":\"другая фраза\"},{\"text\":\"эй дом\"}]}"
                    : response("другая фраза");
        }, () -> 0);
        verifier.append(samples(48000, 0), 48000);
        WakeVerifier.Result result = verify(verifier, candidate(1, 1.5));
        assertTrue(result.accepted()); assertEquals(4, decoded.size());
        int[] margins = {2400, 0, 5600, 9600};
        long total = 0;
        for (int i = 0; i < margins.length; i++) {
            assertEquals(8000 + 2 * margins[i], decoded.get(i).length);
            assertEquals((short) (16000 - margins[i]), decoded.get(i)[0]);
            total += decoded.get(i).length;
        }
        assertEquals(total, result.decodedSamples);
    }

    @Test public void oldTimestampsAreRejectedAfterWrapAndSessionClearKeepsCumulativeTime() throws Exception {
        List<short[]> decoded = new ArrayList<>();
        WakeVerifier verifier = create(audio -> { decoded.add(audio); return response("эй дом"); }, () -> 0);
        for (int second = 0; second < 30; second++) verifier.append(samples(16000, second * 16000), 16000);
        assertEquals(WakeVerifier.Outcome.INVALID_AUDIO, verify(verifier, candidate(1, 2)).outcome);
        assertTrue(decoded.isEmpty());
        assertTrue(verify(verifier, candidate(28, 29)).accepted());
        assertEquals((short) (28 * 16000 - 2400), decoded.get(0)[0]);
        verifier.clearAudio();
        verifier.append(samples(32000, 123), 32000);
        assertEquals(WakeVerifier.Outcome.INVALID_AUDIO, verify(verifier, candidate(0.2, 0.5)).outcome);
        assertTrue(verify(verifier, candidate(30.5, 31)).accepted());
        assertEquals((short) (123 + 8000 - 2400), decoded.get(1)[0]);
    }

    @Test public void cumulativeFedTimeIncludesRepeatedPreRollAndOmitsUnfedSilence() throws Exception {
        List<short[]> decoded = new ArrayList<>();
        WakeVerifier verifier = create(audio -> { decoded.add(audio); return response("эй дом"); }, () -> 0);
        // First utterance, then endpoint/reset or getFinalResult/reset: neither changes Vosk's clock.
        verifier.append(samples(32000, 0), 32000);
        // No append for the VAD's idle silence. Pre-roll is fed again when speech restarts.
        verifier.append(samples(4800, 100), 4800);
        verifier.append(samples(16000, 200), 16000);
        assertTrue(verify(verifier, candidate(2.3, 2.8)).accepted());
        assertEquals((short) (100 + 4800 - 2400), decoded.get(0)[0]);
        assertEquals((short) 200, decoded.get(0)[2400]);
    }

    @Test public void invalidAudioNeverRunsDecoder() throws Exception {
        final int[] calls = {0};
        WakeVerifier verifier = create(audio -> { calls[0]++; return response("эй дом"); }, () -> 0);
        verifier.append(new short[32000], 32000);
        assertEquals(WakeVerifier.Outcome.INVALID_AUDIO, verify(verifier, new JSONObject()).outcome);
        assertEquals(WakeVerifier.Outcome.INVALID_AUDIO, verify(verifier, candidate(-1, 1)).outcome);
        assertEquals(WakeVerifier.Outcome.INVALID_AUDIO, verify(verifier, candidate(1, 3)).outcome);
        assertEquals(WakeVerifier.Outcome.INVALID_AUDIO, verify(verifier, candidate(1, 1)).outcome);
        assertEquals(WakeVerifier.Outcome.INVALID_AUDIO, verify(verifier, new JSONObject(
                "{\"result\":[{\"start\":1,\"end\":1.5},{\"start\":1.2,\"end\":1.7}]}" )).outcome);
        assertEquals(0, calls[0]);
    }

    @Test public void repeatedAndTooFrequentCandidatesDoNotDecodeAgain() throws Exception {
        final int[] calls = {0};
        WakeVerifier verifier = create(audio -> { calls[0]++; return response("эй дом"); }, () -> 0);
        verifier.append(new short[32000], 32000);
        assertTrue(verify(verifier, candidate(1, 1.5)).accepted());
        assertEquals(WakeVerifier.Outcome.RATE_LIMIT, verify(verifier, candidate(1, 1.5)).outcome);
        assertEquals(WakeVerifier.Outcome.RATE_LIMIT, verify(verifier, candidate(1.6, 1.9)).outcome);
        assertEquals(1, calls[0]);
        verifier.append(new short[16000], 16000);
        assertEquals(WakeVerifier.Outcome.RATE_LIMIT, verify(verifier, candidate(1, 1.5)).outcome);
        assertTrue(verify(verifier, candidate(2, 2.5)).accepted());
        assertEquals(2, calls[0]);
    }

    @Test public void decodeFailuresAndInvalidResponsesFailClosed() throws Exception {
        WakeVerifier.Decoder[] failures = {
            audio -> { throw new IllegalStateException("private diagnostic must not escape"); },
            audio -> { throw new UnsatisfiedLinkError("private native diagnostic must not escape"); },
            audio -> "invalid", audio -> "{}", audio -> "{\"alternatives\":[]}",
            audio -> "{\"alternatives\":[{}]}"
        };
        for (WakeVerifier.Decoder failure : failures) {
            WakeVerifier verifier = create(failure, () -> 0);
            verifier.append(new short[32000], 32000);
            WakeVerifier.Result result = verify(verifier, candidate(1, 1.5));
            assertFalse(result.accepted()); assertEquals(WakeVerifier.Outcome.ERROR, result.outcome);
        }
    }

    @Test public void budgetExhaustionRejectsEvenMatchingDecodeAndStopsRemainingContexts() throws Exception {
        final long[] time = {0}; final int[] calls = {0};
        WakeVerifier verifier = create(audio -> {
            calls[0]++; time[0] += WakeVerifier.BUDGET_MS * 1000000;
            return response("эй дом");
        }, () -> time[0]);
        verifier.append(new short[32000], 32000);
        WakeVerifier.Result result = verify(verifier, candidate(1, 1.5));
        assertEquals(WakeVerifier.Outcome.TIME_LIMIT, result.outcome);
        assertEquals(WakeVerifier.BUDGET_MS, result.elapsedMs); assertEquals(1, calls[0]);
    }

    @Test public void differentFullVocabularyTextRejectsAfterAtMostFourDecodes() throws Exception {
        final int[] calls = {0};
        WakeVerifier verifier = create(audio -> { calls[0]++; return response("эй домик"); }, () -> 0);
        verifier.append(new short[32000], 32000);
        WakeVerifier.Result result = verify(verifier, candidate(1, 1.5));
        assertEquals(WakeVerifier.Outcome.REJECTED, result.outcome);
        assertNull(result.acceptedAudio);
        assertEquals(4, calls[0]);
    }

    @Test public void pendingUsesImmutableAudioAndKeepsOriginalAttemptAndSource() throws Exception {
        CountDownLatch decoding = new CountDownLatch(1), release = new CountDownLatch(1);
        List<short[]> decoded = new ArrayList<>();
        WakeVerifier verifier = create(audio -> {
            decoded.add(audio); decoding.countDown();
            if (!release.await(2, TimeUnit.SECONDS)) throw new IllegalStateException("test_release");
            return response("эй дом");
        }, System::nanoTime);
        assertFalse(verifier.hasPending());
        verifier.append(samples(32000, 0), 32000);
        assertNull(verifier.submit(candidate(1, 1.5), context(77)));
        assertTrue(decoding.await(2, TimeUnit.SECONDS));
        assertTrue(verifier.hasPending());
        verifier.append(samples(256000, 120), 256000); // overwrite the entire mutable ring
        release.countDown();
        WakeVerifier.Result result = await(verifier);
        assertFalse(verifier.hasPending());
        assertTrue(result.accepted());
        assertNotNull(result.acceptedAudio);
        assertTrue(result.acceptedAudio.length <= WakeVerifier.BUFFER_SAMPLES);
        assertEquals((short) (16000 - 9600), result.acceptedAudio[0]);
        assertEquals(77, result.context.attempt);
        assertEquals(WakeMatchGate.MatchSource.VAD_END_FINAL, result.context.source);
        assertEquals((short) (16000 - 2400), decoded.get(0)[0]);
    }

    @Test public void busyWorkerKeepsOnlyLatestQueuedCandidate() throws Exception {
        CountDownLatch decoding = new CountDownLatch(1), release = new CountDownLatch(1);
        AtomicInteger calls = new AtomicInteger();
        WakeVerifier verifier = create(audio -> {
            if (calls.incrementAndGet() == 1) {
                decoding.countDown();
                if (!release.await(2, TimeUnit.SECONDS)) throw new IllegalStateException("test_release");
            }
            return response("эй дом");
        }, System::nanoTime);
        verifier.append(new short[32000], 32000);
        assertNull(verifier.submit(candidate(1, 1.5), context(1)));
        assertTrue(decoding.await(2, TimeUnit.SECONDS));
        verifier.append(new short[16000], 16000);
        assertNull(verifier.submit(candidate(2, 2.5), context(2)));
        verifier.append(new short[16000], 16000);
        assertNull(verifier.submit(candidate(3, 3.5), context(3)));
        release.countDown();
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(2);
        while (calls.get() < 2 && System.nanoTime() < deadline) Thread.sleep(1);
        assertEquals(2, calls.get()); // attempt 2 never reaches the decoder
        assertEquals(1, await(verifier).context.attempt); // first accepted result is preserved
    }

    @Test public void resetDropsRunningAndQueuedResultsAndClosesOnlyAfterNativeWork() throws Exception {
        CountDownLatch decoding = new CountDownLatch(1), release = new CountDownLatch(1), closed = new CountDownLatch(1);
        AtomicInteger calls = new AtomicInteger();
        WakeVerifier verifier = create(new WakeVerifier.Decoder() {
            @Override public String decode(short[] audio) throws Exception {
                calls.incrementAndGet(); decoding.countDown();
                if (!release.await(2, TimeUnit.SECONDS)) throw new IllegalStateException("test_release");
                return response("эй дом");
            }
            @Override public void close() { closed.countDown(); }
        }, System::nanoTime);
        verifier.append(new short[32000], 32000);
        assertNull(verifier.submit(candidate(1, 1.5), context(1)));
        assertTrue(decoding.await(2, TimeUnit.SECONDS));
        verifier.append(new short[16000], 16000);
        assertNull(verifier.submit(candidate(2, 2.5), context(2)));
        verifier.clearAudio();
        verifier.close();
        assertFalse(closed.await(20, TimeUnit.MILLISECONDS));
        release.countDown();
        assertTrue(closed.await(2, TimeUnit.SECONDS));
        assertNull(verifier.poll());
        assertEquals(1, calls.get());
    }

    @Test public void resetInvalidatesRunningAcceptanceWhileNextGenerationStillWorks() throws Exception {
        CountDownLatch decoding = new CountDownLatch(1), release = new CountDownLatch(1);
        AtomicInteger calls = new AtomicInteger();
        WakeVerifier verifier = create(audio -> {
            if (calls.incrementAndGet() == 1) {
                decoding.countDown();
                if (!release.await(2, TimeUnit.SECONDS)) throw new IllegalStateException("test_release");
            }
            return response("эй дом");
        }, System::nanoTime);
        verifier.append(new short[32000], 32000);
        assertNull(verifier.submit(candidate(1, 1.5), context(1)));
        assertTrue(decoding.await(2, TimeUnit.SECONDS));
        verifier.clearAudio();
        verifier.append(new short[32000], 32000);
        assertNull(verifier.submit(candidate(3, 3.5), context(2)));
        release.countDown();
        WakeVerifier.Result result = await(verifier);
        assertTrue(result.accepted()); assertEquals(2, result.context.attempt);
        assertEquals(2, calls.get());
    }
}
