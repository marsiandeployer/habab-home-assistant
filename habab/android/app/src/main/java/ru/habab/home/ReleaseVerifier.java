package ru.habab.home;

import java.security.*;
import java.security.spec.X509EncodedKeySpec;

final class ReleaseVerifier {
    static void verify(byte[] manifest, byte[] signature, byte[] publicKey) throws GeneralSecurityException {
        Signature verifier = Signature.getInstance("SHA256withRSA");
        verifier.initVerify(KeyFactory.getInstance("RSA").generatePublic(new X509EncodedKeySpec(publicKey)));
        verifier.update(manifest);
        if (!verifier.verify(signature)) throw new GeneralSecurityException("Release signature mismatch");
    }
    private ReleaseVerifier() {}
}
