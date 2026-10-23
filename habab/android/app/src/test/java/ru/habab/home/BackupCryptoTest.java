package ru.habab.home;

import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import javax.crypto.*;
import org.junit.Test;
import static org.junit.Assert.*;

public class BackupCryptoTest {
    @Test public void roundtripAndPasswordErasure() throws Exception {
        byte[] salt = new byte[16], iv = new byte[12], plain = "private configuration".getBytes(StandardCharsets.UTF_8);
        char[] password = "nonfunctional test password".toCharArray();
        byte[] encrypted = BackupCrypto.cipher(Cipher.ENCRYPT_MODE, password, salt, iv).doFinal(plain);
        for (char value : password) assertEquals('\0', value);
        assertFalse(Arrays.equals(plain, encrypted));
        assertArrayEquals(plain, BackupCrypto.cipher(Cipher.DECRYPT_MODE, "nonfunctional test password".toCharArray(), salt, iv).doFinal(encrypted));
    }

    @Test public void alteredCiphertextAndWrongPasswordAreRejected() throws Exception {
        byte[] salt = new byte[16], iv = new byte[12];
        byte[] encrypted = BackupCrypto.cipher(Cipher.ENCRYPT_MODE, "nonfunctional test password".toCharArray(), salt, iv).doFinal(new byte[128]);
        assertThrows(AEADBadTagException.class, () -> BackupCrypto.cipher(Cipher.DECRYPT_MODE, "different test password".toCharArray(), salt, iv).doFinal(encrypted));
        encrypted[0] ^= 1;
        assertThrows(AEADBadTagException.class, () -> BackupCrypto.cipher(Cipher.DECRYPT_MODE, "nonfunctional test password".toCharArray(), salt, iv).doFinal(encrypted));
    }
}
