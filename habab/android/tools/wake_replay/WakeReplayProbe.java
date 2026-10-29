package ru.habab.home;

import java.io.*;
import java.util.zip.*;

public final class WakeReplayProbe {
    static final class TracedRecognizer extends org.vosk.Recognizer {
        private final String clip;
        private final org.vosk.Model verifierModel;
        private final ByteArrayOutputStream fed = new ByteArrayOutputStream();
        TracedRecognizer(org.vosk.Model model, String clip) throws Exception {
            super(model, 16000, "[\"эй дом\",\"[unk]\",\"[unk]\",\"[unk]\",\"[unk]\",\"[unk]\",\"[unk]\",\"[unk]\",\"[unk]\",\"[unk]\",\"[unk]\",\"[unk]\",\"[unk]\",\"[unk]\",\"[unk]\",\"[unk]\",\"[unk]\"]");
            this.clip = clip; this.verifierModel = model; setWords(true); setEndpointerMode(org.vosk.Recognizer.EndpointerMode.SHORT);
        }
        @Override public boolean acceptWaveForm(short[] samples, int length) {
            for (int i=0;i<length;i++) { fed.write(samples[i]&255); fed.write((samples[i]>>>8)&255); }
            return super.acceptWaveForm(samples, length);
        }
        private String inspect(String raw) {
            try {
                org.json.JSONObject result = new org.json.JSONObject(raw);
                if ("эй дом".equals(result.optString("text"))) {
                    org.json.JSONArray words = result.optJSONArray("result");
                    org.json.JSONArray numbers = new org.json.JSONArray();
                    if (words != null) for (int i=0;i<words.length();i++) {
                        org.json.JSONObject w=words.getJSONObject(i);
                        numbers.put(new org.json.JSONObject().put("duration_ms",Math.round(1000*(w.getDouble("end")-w.getDouble("start")))).put("confidence",w.optDouble("conf",-1)));
                    }
                    System.out.println("WORDS " + clip + " " + numbers);
                    if (words != null && words.length() == 2) {
                        byte[] bytes=fed.toByteArray();
                        int start=Math.max(0, (int)(words.getJSONObject(0).getDouble("start")*16000)-2400);
                        int end=Math.min(bytes.length/2, (int)(words.getJSONObject(1).getDouble("end")*16000)+2400);
                        boolean verified=false;
                        for (int margin : new int[]{2400, 0, 5600, 9600}) {
                          start=Math.max(0,(int)(words.getJSONObject(0).getDouble("start")*16000)-margin);
                          end=Math.min(bytes.length/2,(int)(words.getJSONObject(1).getDouble("end")*16000)+margin);
                          try (org.vosk.Recognizer verifier=new org.vosk.Recognizer(verifierModel,16000)) {
                            if(end>start) {
                                byte[] crop=java.util.Arrays.copyOfRange(bytes,start*2,end*2);
                                verifier.setMaxAlternatives(8);
                                verifier.acceptWaveForm(crop,crop.length);
                                org.json.JSONArray candidates=new org.json.JSONObject(verifier.getFinalResult()).optJSONArray("alternatives");
                                int rank=-1; double delta=-1;
                                if(candidates!=null) for(int k=0;k<candidates.length();k++) {
                                    org.json.JSONObject candidate=candidates.getJSONObject(k);
                                    if("эй дом".equals(WakeDetector.normalize(candidate.optString("text")))) {
                                        verified=true;rank=k;delta=candidates.getJSONObject(0).optDouble("confidence",0)-candidate.optDouble("confidence",0);break;
                                    }
                                }
                                System.out.println("VERIFY_RANK "+clip+" rank="+rank+" score_gap="+delta);
                            }
                        }
                        if(verified) break;
                        }
                        System.out.println("CROP_VERIFY "+clip+" accepted="+verified+" start="+start+" end="+end+" total="+(bytes.length/2));
                        if(!verified) return new org.json.JSONObject().put("text", "").toString();
                    }
                }
            } catch (Exception ignored) { System.out.println("INVALID_NUMERIC_TRACE " + clip); }
            return raw;
        }
        @Override public String getResult() { return inspect(super.getResult()); }
        @Override public String getFinalResult() { return inspect(super.getFinalResult()); }
    }

    public static void main(String[] args) throws Exception {
        String apk = args[0], nativeDir = args[1];
        File work = new File(args[2]);
        System.setProperty("jna.boot.library.path", nativeDir);
        System.setProperty("jna.library.path", nativeDir);
        System.setProperty("jna.tmpdir", work.getAbsolutePath());
        File models = new File(work, "model"); models.mkdirs();
        try (ZipFile file = new ZipFile(apk);
             ZipInputStream zip = new ZipInputStream(file.getInputStream(file.getEntry("assets/voice/model.zip")))) {
            ZipEntry entry;
            while ((entry = zip.getNextEntry()) != null) {
                File out = new File(models, entry.getName());
                if (!out.getCanonicalPath().startsWith(models.getCanonicalPath() + "/")) throw new IOException("invalid model path");
                if (entry.isDirectory()) { out.mkdirs(); continue; }
                out.getParentFile().mkdirs();
                if (!out.isFile()) try (OutputStream target = new FileOutputStream(out)) {
                    byte[] buffer = new byte[65536]; int n;
                    while ((n = zip.read(buffer)) > 0) target.write(buffer, 0, n);
                }
            }
        }
        String model = new File(models, "vosk-model-small-ru-0.22").getAbsolutePath();
        for (int index = 3; index < args.length; index++) {
            File clip = new File(args[index]);
            final int[] finals = {0}, exactFinals = {0};
            WakeDetector.Diagnostics diagnostic = (event, reason, attempt, at, rms, vad, partial, match, conf, elapsed) -> {
                if (event == VoiceLifecycleTrace.Event.WAKE_FINAL) {
                    finals[0]++;
                    if (match == 1) { exactFinals[0]++; System.out.println("FINAL " + clip.getName() + " " + reason + " confidence=" + conf); }
                }
            };
            boolean detected = false, padded = false;
            long now = 20000;
            try (WakeDetector detector = new WakeDetector(model, "эй дом", diagnostic);
                 InputStream input = new FileInputStream(clip)) {
                java.lang.reflect.Field modelField = WakeDetector.class.getDeclaredField("model");
                java.lang.reflect.Field recognizerField = WakeDetector.class.getDeclaredField("recognizer");
                modelField.setAccessible(true); recognizerField.setAccessible(true);
                org.vosk.Recognizer original = (org.vosk.Recognizer) recognizerField.get(detector);
                TracedRecognizer traced = new TracedRecognizer((org.vosk.Model) modelField.get(detector), clip.getName());
                original.close(); recognizerField.set(detector, traced);
                byte[] header = new byte[44];
                if (input.read(header) != 44 || header[0] != 'R' || header[8] != 'W') throw new IOException("invalid WAV");
                byte[] data = new byte[640]; int n;
                while ((n = input.read(data)) > 0) {
                    short[] samples = new short[n / 2];
                    for (int j = 0; j < samples.length; j++) samples[j] = (short)((data[j*2] & 255) | (data[j*2+1] << 8));
                    if (detector.accept(samples, samples.length, now)) { detected = true; break; }
                    now += 20;
                }
                if (!detected) for (int j = 0; j < 100; j++) {
                    now += 20;
                    if (detector.accept(new short[320], 320, now)) { detected = true; padded = true; break; }
                }
            }
            System.out.println("RESULT " + clip.getName() + " detected=" + detected + " padded=" + padded + " exact_finals=" + exactFinals[0]);
        }
    }
}
