package ru.habab.home;

import static org.junit.Assert.*;
import java.nio.file.*;
import java.util.stream.Stream;
import org.junit.Test;

public class WakeClipBufferTest {
    @Test public void wrapsAndSavesValidPcmWithoutPersistingBeforeWake() throws Exception {
        WakeClipBuffer ring = new WakeClipBuffer(1);
        short[] input = new short[17000];
        for (int i = 0; i < input.length; i++) input[i] = (short) i;
        Path dir = Files.createTempDirectory("wake-clips-test");
        try {
            ring.append(input, input.length);
            try (Stream<Path> files = Files.list(dir)) { assertEquals(0, files.count()); }
            short[] clip = ring.snapshot();
            assertEquals(16000, clip.length); assertEquals(1000, clip[0]); assertEquals(16999, clip[15999]);
            WakeClipBuffer.save(dir.toFile(), clip, 1234567890123L, 1);
            byte[] wav = Files.readAllBytes(dir.resolve("wake-1234567890123-1.wav"));
            assertEquals(32044, wav.length); assertEquals('R', wav[0]); assertEquals('W', wav[8]);
            assertEquals(0xe8, wav[44] & 0xff); assertEquals(0x03, wav[45] & 0xff);
            WakeClipBuffer.markForReview(dir.toFile(), 1234567890123L, 1);
            WakeClipBuffer.markForReview(dir.toFile(), 1234567890123L, 1);
            assertTrue(Files.exists(dir.resolve("wake-1234567890123-1.wav.review")));
            try { WakeClipBuffer.markForReview(dir.toFile(), 1234567890123L, 2); fail("missing clip"); }
            catch (java.io.IOException expected) { assertEquals("wake_clip_missing", expected.getMessage()); }
            ring.clear(); assertEquals(0, ring.snapshot().length);
            for (int i = 0; i < 55; i++) WakeClipBuffer.save(dir.toFile(), new short[]{1}, 1234567891000L + i, i);
            try (Stream<Path> files = Files.list(dir)) { assertEquals(50, files.count()); }
            assertFalse(Files.exists(dir.resolve("wake-1234567890123-1.wav.review")));
        } finally {
            try (Stream<Path> files = Files.list(dir)) { files.forEach(p -> p.toFile().delete()); }
            Files.deleteIfExists(dir);
        }
    }
}
