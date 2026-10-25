package ru.habab.home;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashSet;
import java.util.Arrays;
import java.util.Collections;
import java.util.Set;
import java.util.stream.Collectors;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

public final class VoiceStatusFileTest {
    @Rule public final TemporaryFolder temporaryFolder = new TemporaryFolder();

    @Test public void preInterruptedWriterPublishesWholeFileAndPreservesInterrupt() throws Exception {
        Path target = temporaryFolder.getRoot().toPath().resolve("voice-status.json");
        Files.write(target, "{\"phase\":\"listening\",\"active\":true}".getBytes(StandardCharsets.UTF_8));
        byte[] offStatus = "{\"phase\":\"off\",\"active\":false}".getBytes(StandardCharsets.UTF_8);
        AtomicInteger interruptedAfterWrite = new AtomicInteger();
        AtomicInteger failure = new AtomicInteger();
        Thread writer = new Thread(() -> {
            Thread.currentThread().interrupt();
            try {
                VoiceStatusFile.write(target, offStatus);
                interruptedAfterWrite.set(Thread.currentThread().isInterrupted() ? 1 : 0);
            } catch (IOException error) {
                failure.set(1);
            }
        });
        writer.start();
        writer.join(TimeUnit.SECONDS.toMillis(3));
        assertFalse("writer should finish promptly", writer.isAlive());
        assertEquals(0, failure.get());
        assertEquals(1, interruptedAfterWrite.get());
        assertEquals(new String(offStatus, StandardCharsets.UTF_8),
                new String(Files.readAllBytes(target), StandardCharsets.UTF_8));
    }

    @Test public void concurrentWritersNeverExposePartialStatusToReaders() throws Exception {
        Path target = temporaryFolder.getRoot().toPath().resolve("voice-status.json");
        String first = "{\"phase\":\"wake\",\"active\":true}";
        String second = "{\"phase\":\"thinking\",\"active\":false}";
        Files.write(target, first.getBytes(StandardCharsets.UTF_8));
        Set<String> completeSnapshots = new HashSet<>(Arrays.asList(first, second));
        CountDownLatch start = new CountDownLatch(1);
        AtomicInteger finished = new AtomicInteger();
        ExecutorService writers = Executors.newFixedThreadPool(2);
        try {
            Future<?> one = writers.submit(() -> replaceRepeatedly(start, finished, target, first));
            Future<?> two = writers.submit(() -> replaceRepeatedly(start, finished, target, second));
            start.countDown();
            int reads = 0;
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(3);
            while (finished.get() < 2) {
                assertTrue("writers did not finish before deadline", System.nanoTime() < deadline);
                String observed = new String(Files.readAllBytes(target), StandardCharsets.UTF_8);
                assertTrue("reader observed partial status: " + observed, completeSnapshots.contains(observed));
                reads++;
            }
            one.get(3, TimeUnit.SECONDS);
            two.get(3, TimeUnit.SECONDS);
            assertTrue("reader must overlap writes", reads > 0);
            assertTrue(completeSnapshots.contains(new String(Files.readAllBytes(target), StandardCharsets.UTF_8)));
        } finally {
            writers.shutdownNow();
            assertTrue(writers.awaitTermination(3, TimeUnit.SECONDS));
        }
    }

    @Test public void failedAtomicReplacementKeepsExistingTargetAndCleansTemporaryFile() throws Exception {
        Path root = temporaryFolder.getRoot().toPath();
        Path targetDirectory = Files.createDirectory(root.resolve("voice-status.json"));
        Path sentinel = Files.write(targetDirectory.resolve("previous-status.json"),
                "{\"phase\":\"off\",\"active\":false}".getBytes(StandardCharsets.UTF_8));
        try {
            VoiceStatusFile.write(targetDirectory, "replacement".getBytes(StandardCharsets.UTF_8));
            fail("moving a temporary file over a non-empty directory must fail");
        } catch (IOException expected) {
            assertEquals("{\"phase\":\"off\",\"active\":false}",
                    new String(Files.readAllBytes(sentinel), StandardCharsets.UTF_8));
        }
        try (java.util.stream.Stream<Path> children = Files.list(root)) {
            assertEquals(Collections.singleton(targetDirectory), children.collect(Collectors.toSet()));
        }
    }

    private static void replaceRepeatedly(CountDownLatch start, AtomicInteger finished,
                                          Path target, String value) {
        try {
            if (!start.await(3, TimeUnit.SECONDS)) throw new AssertionError("writer start timed out");
            byte[] bytes = value.getBytes(StandardCharsets.UTF_8);
            for (int index = 0; index < 400; index++) VoiceStatusFile.write(target, bytes);
        } catch (Exception error) {
            throw new AssertionError(error);
        } finally {
            finished.incrementAndGet();
        }
    }
}
