package app.mochi.agent;

import android.content.Context;
import android.util.Base64;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.List;

/** The app's files: config.json (pairing, written by app/install.sh over adb) and the outbox of events not yet delivered. */
final class Store {
    static final String TAG = "Mochi";
    private final File dir;
    private final File outbox;
    JSONObject config;  // null until paired

    Store(Context ctx) {
        dir = ctx.getFilesDir();
        outbox = new File(dir, "outbox.jsonl");
        reload();
    }

    void reload() {
        try {
            File f = new File(dir, "config.json");
            config = f.exists() ? new JSONObject(new String(Files.readAllBytes(f.toPath()), StandardCharsets.UTF_8)) : null;
        } catch (Exception e) {
            Log.e(TAG, "config unreadable", e);
            config = null;
        }
    }

    boolean paired() {
        return config != null && config.has("relay") && config.has("token") && config.has("phone_priv") && config.has("desk_pub");
    }

    byte[] key(String name) {
        return Base64.decode(config.optString(name), Base64.DEFAULT);
    }

    /** Events wait on disk until the relay takes them, so nothing is lost offline or across a restart. */
    synchronized void enqueue(JSONObject ev) {
        try (FileOutputStream out = new FileOutputStream(outbox, true)) {
            out.write((ev.toString() + "\n").getBytes(StandardCharsets.UTF_8));
        } catch (IOException e) {
            Log.e(TAG, "outbox write failed", e);
        }
    }

    synchronized List<JSONObject> pending() {
        List<JSONObject> out = new ArrayList<>();
        if (!outbox.exists()) return out;
        try {
            for (String line : Files.readAllLines(outbox.toPath(), StandardCharsets.UTF_8)) {
                if (line.isEmpty()) continue;
                try { out.add(new JSONObject(line)); } catch (Exception ignored) { }
            }
        } catch (IOException e) {
            Log.e(TAG, "outbox read failed", e);
        }
        return out;
    }

    /** Drop the first n lines (the ones just delivered); anything enqueued meanwhile stays. */
    synchronized void delivered(int n) {
        try {
            List<String> lines = outbox.exists() ? Files.readAllLines(outbox.toPath(), StandardCharsets.UTF_8) : new ArrayList<>();
            List<String> keep = new ArrayList<>();
            int seen = 0;
            for (String l : lines) {
                if (l.isEmpty()) continue;
                if (seen++ < n) continue;
                keep.add(l);
            }
            File tmp = new File(dir, "outbox.tmp");
            Files.write(tmp.toPath(), (String.join("\n", keep) + (keep.isEmpty() ? "" : "\n")).getBytes(StandardCharsets.UTF_8));
            if (!tmp.renameTo(outbox)) Log.e(TAG, "outbox rename failed");
        } catch (IOException e) {
            Log.e(TAG, "outbox trim failed", e);
        }
    }

    int outboxSize() {
        return pending().size();
    }

    static JSONArray toArray(List<JSONObject> l) {
        JSONArray a = new JSONArray();
        for (JSONObject o : l) a.put(o);
        return a;
    }
}
