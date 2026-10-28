package ru.habab.home;

import java.io.File;
import java.io.FileOutputStream;
import java.io.BufferedOutputStream;
import java.io.OutputStream;
import java.io.IOException;
import java.util.Arrays;

/** In-memory rolling audio; only an accepted wake is persisted. */
final class WakeClipBuffer {
    static final int SAMPLE_RATE = 16000;
    private final short[] samples;
    private int next, length;

    WakeClipBuffer(int seconds) { samples = new short[seconds * SAMPLE_RATE]; }

    void append(short[] input, int count) {
        for (int i = 0; i < count; i++) {
            samples[next] = input[i]; next = (next + 1) % samples.length;
            if (length < samples.length) length++;
        }
    }

    short[] snapshot() {
        short[] result = new short[length];
        int start = (next - length + samples.length) % samples.length;
        for (int i = 0; i < length; i++) result[i] = samples[(start + i) % samples.length];
        return result;
    }

    void clear() { Arrays.fill(samples, (short) 0); next = 0; length = 0; }

    static synchronized void save(File directory, short[] data, long timestamp, long attempt) throws IOException {
        if (!directory.isDirectory() && !directory.mkdirs()) throw new IOException("wake_clip_directory");
        File destination = new File(directory, "wake-" + timestamp + "-" + attempt + ".wav");
        File temporary = new File(directory, destination.getName() + ".tmp");
        try (OutputStream out = new BufferedOutputStream(new FileOutputStream(temporary))) {
            int bytes = data.length * 2;
            out.write(new byte[]{'R','I','F','F'}); le(out, 36 + bytes, 4);
            out.write(new byte[]{'W','A','V','E','f','m','t',' '}); le(out, 16, 4);
            le(out, 1, 2); le(out, 1, 2); le(out, SAMPLE_RATE, 4); le(out, SAMPLE_RATE * 2, 4);
            le(out, 2, 2); le(out, 16, 2); out.write(new byte[]{'d','a','t','a'}); le(out, bytes, 4);
            for (short sample : data) le(out, sample, 2);
        } catch (IOException error) { temporary.delete(); throw error; }
        if (!temporary.renameTo(destination)) { temporary.delete(); throw new IOException("wake_clip_rename"); }
        File[] files = directory.listFiles((dir, name) -> name.startsWith("wake-") && name.endsWith(".wav"));
        if (files != null && files.length > 50) {
            Arrays.sort(files, (a,b) -> a.getName().compareTo(b.getName()));
            for (int i = 0; i < files.length - 50; i++) {
                if (files[i].delete()) new File(directory, files[i].getName() + ".review").delete();
            }
        }
    }

    static synchronized void markForReview(File directory, long timestamp, long attempt) throws IOException {
        File clip = new File(directory, "wake-" + timestamp + "-" + attempt + ".wav");
        if (!clip.isFile()) throw new IOException("wake_clip_missing");
        File marker = new File(directory, clip.getName() + ".review");
        if (!marker.exists() && !marker.createNewFile()) throw new IOException("wake_clip_marker");
    }

    private static void le(OutputStream out, int value, int size) throws IOException {
        for (int i = 0; i < size; i++) out.write((value >> (8 * i)) & 0xff);
    }
}
