package ru.habab.home;

import java.util.Arrays;
import javax.crypto.*;
import javax.crypto.spec.*;

final class BackupCrypto {
    static Cipher cipher(int mode, char[] password, byte[] salt, byte[] iv) throws Exception {
        if (salt.length != 16 || iv.length != 12) throw new IllegalArgumentException("Invalid backup salt or nonce");
        PBEKeySpec spec = new PBEKeySpec(password, salt, 210000, 256);
        byte[] bytes;
        try { bytes = SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256").generateSecret(spec).getEncoded(); }
        finally { spec.clearPassword(); Arrays.fill(password, '\0'); }
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        try { cipher.init(mode, new SecretKeySpec(bytes, "AES"), new GCMParameterSpec(128, iv)); }
        finally { Arrays.fill(bytes, (byte) 0); }
        return cipher;
    }
    private BackupCrypto() {}
}
