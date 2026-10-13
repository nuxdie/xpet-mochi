package app.mochi.agent;

import java.nio.charset.StandardCharsets;
import java.security.KeyFactory;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.PrivateKey;
import java.security.PublicKey;
import java.security.SecureRandom;
import java.security.spec.PKCS8EncodedKeySpec;
import java.security.spec.X509EncodedKeySpec;
import java.util.Arrays;

import javax.crypto.Cipher;
import javax.crypto.KeyAgreement;
import javax.crypto.Mac;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;

/**
 * The same sealed box as brain/mochi_phone.py: MAGIC | eph_pub | nonce | AES-256-GCM(ct, aad = MAGIC | eph_pub).
 * key = HKDF-SHA256(ikm = dh(eph, recipient) || dh(sender, recipient), salt = eph_pub || sender_pub || recipient_pub,
 * info = "mochi-phone-v1"). Ephemeral per message (forward secrecy); the sender's static key is mixed in, so only
 * the desk can seal something the phone opens, and only the phone can seal something the desk opens.
 */
final class Crypto {
    static final byte[] MAGIC = {'M', '1'};
    static final byte[] INFO = "mochi-phone-v1".getBytes(StandardCharsets.US_ASCII);
    // X25519 keys in the JDK are DER-wrapped; the wrappers are fixed, so raw 32-byte keys are these prefixes + raw.
    private static final byte[] PUB_PREFIX = hex("302a300506032b656e032100");
    private static final byte[] PRIV_PREFIX = hex("302e020100300506032b656e04220420");
    private static final SecureRandom RNG = new SecureRandom();

    static byte[] seal(byte[] plain, byte[] myPrivRaw, byte[] myPubRaw, byte[] peerPubRaw) throws Exception {
        KeyPair eph = KeyPairGenerator.getInstance("XDH").generateKeyPair();
        byte[] ephPub = raw(eph.getPublic().getEncoded());
        PublicKey peer = pub(peerPubRaw);
        byte[] ikm = concat(dh(eph.getPrivate(), peer), dh(priv(myPrivRaw), peer));
        byte[] key = hkdf(ikm, concat(ephPub, myPubRaw, peerPubRaw), INFO, 32);
        byte[] nonce = new byte[12];
        RNG.nextBytes(nonce);
        byte[] aad = concat(MAGIC, ephPub);
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.ENCRYPT_MODE, new SecretKeySpec(key, "AES"), new GCMParameterSpec(128, nonce));
        c.updateAAD(aad);
        return concat(aad, nonce, c.doFinal(plain));
    }

    static byte[] open(byte[] blob, byte[] myPrivRaw, byte[] myPubRaw, byte[] peerPubRaw) throws Exception {
        if (blob.length < 2 + 32 + 12 + 16 || blob[0] != MAGIC[0] || blob[1] != MAGIC[1]) throw new Exception("not a mochi blob");
        byte[] ephPub = Arrays.copyOfRange(blob, 2, 34);
        byte[] nonce = Arrays.copyOfRange(blob, 34, 46);
        byte[] ct = Arrays.copyOfRange(blob, 46, blob.length);
        PrivateKey me = priv(myPrivRaw);
        byte[] ikm = concat(dh(me, pub(ephPub)), dh(me, pub(peerPubRaw)));
        byte[] key = hkdf(ikm, concat(ephPub, peerPubRaw, myPubRaw), INFO, 32);
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.DECRYPT_MODE, new SecretKeySpec(key, "AES"), new GCMParameterSpec(128, nonce));
        c.updateAAD(concat(MAGIC, ephPub));
        return c.doFinal(ct);
    }

    private static byte[] dh(PrivateKey a, PublicKey b) throws Exception {
        KeyAgreement ka = KeyAgreement.getInstance("XDH");
        ka.init(a);
        ka.doPhase(b, true);
        return ka.generateSecret();
    }

    static PublicKey pub(byte[] raw) throws Exception {
        return KeyFactory.getInstance("XDH").generatePublic(new X509EncodedKeySpec(concat(PUB_PREFIX, raw)));
    }

    static PrivateKey priv(byte[] raw) throws Exception {
        return KeyFactory.getInstance("XDH").generatePrivate(new PKCS8EncodedKeySpec(concat(PRIV_PREFIX, raw)));
    }

    private static byte[] raw(byte[] der) {  // the last 32 bytes of a DER-encoded X25519 key are the key
        return Arrays.copyOfRange(der, der.length - 32, der.length);
    }

    static byte[] hkdf(byte[] ikm, byte[] salt, byte[] info, int len) throws Exception {
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(salt, "HmacSHA256"));
        byte[] prk = mac.doFinal(ikm);
        mac.init(new SecretKeySpec(prk, "HmacSHA256"));
        byte[] out = new byte[len];
        byte[] t = new byte[0];
        int n = 0, i = 1;
        while (n < len) {
            mac.update(t);
            mac.update(info);
            mac.update((byte) i++);
            t = mac.doFinal();
            int k = Math.min(t.length, len - n);
            System.arraycopy(t, 0, out, n, k);
            n += k;
        }
        return out;
    }

    static byte[] concat(byte[]... parts) {
        int n = 0;
        for (byte[] p : parts) n += p.length;
        byte[] out = new byte[n];
        int i = 0;
        for (byte[] p : parts) {
            System.arraycopy(p, 0, out, i, p.length);
            i += p.length;
        }
        return out;
    }

    private static byte[] hex(String s) {
        byte[] b = new byte[s.length() / 2];
        for (int i = 0; i < b.length; i++) b[i] = (byte) Integer.parseInt(s.substring(2 * i, 2 * i + 2), 16);
        return b;
    }
}
