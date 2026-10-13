package app.mochi.agent;

import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;

/** The blind mailbox (relay/mochi_relay.py): push sealed blobs into the desk's box, pull ours. */
final class Relay {
    private final String base;
    private final String token;

    Relay(String base, String token) {
        this.base = base.endsWith("/") ? base.substring(0, base.length() - 1) : base;
        this.token = token;
    }

    private HttpURLConnection open(String path, String method, int timeoutMs) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(base + path).openConnection();
        c.setRequestMethod(method);
        c.setConnectTimeout(15000);
        c.setReadTimeout(timeoutMs);
        c.setRequestProperty("Authorization", "Bearer " + token);
        c.setRequestProperty("User-Agent", "mochi-phone-app/1");
        return c;
    }

    private static byte[] readAll(InputStream in) throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        return out.toByteArray();
    }

    /** @return the relay's id for the blob */
    String push(byte[] blob) throws Exception {
        HttpURLConnection c = open("/v1/push/desk", "POST", 30000);
        c.setDoOutput(true);
        c.setFixedLengthStreamingMode(blob.length);
        c.setRequestProperty("Content-Type", "application/octet-stream");
        try (OutputStream out = c.getOutputStream()) { out.write(blob); }
        int code = c.getResponseCode();
        if (code != 200) throw new Exception("push: http " + code);
        return new JSONObject(new String(readAll(c.getInputStream()), StandardCharsets.UTF_8)).getString("id");
    }

    /** Items waiting for the phone, long-polling up to waitSec; acks the ids given (delivered last time). */
    JSONArray pull(int waitSec, String ack) throws Exception {
        HttpURLConnection c = open("/v1/pull/phone?wait=" + waitSec + "&ack=" + URLEncoder.encode(ack, "UTF-8"), "GET", (waitSec + 20) * 1000);
        int code = c.getResponseCode();
        if (code != 200) throw new Exception("pull: http " + code);
        return new JSONObject(new String(readAll(c.getInputStream()), StandardCharsets.UTF_8)).getJSONArray("items");
    }

    static byte[] b64(String s) {
        return Base64.decode(s, Base64.DEFAULT);
    }
}
