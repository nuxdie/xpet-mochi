package app.mochi.agent;

import android.app.Notification;
import android.content.ComponentName;
import android.media.MediaMetadata;
import android.media.session.MediaController;
import android.media.session.MediaSessionManager;
import android.media.session.PlaybackState;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.Parcelable;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;
import android.util.Log;

import org.json.JSONObject;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Everything that lands in the notification shade (messages, mail, deliveries, app alerts), and, through the same
 * access, what is playing. Enabled by install.sh (`cmd notification allow_listener`). Each notification becomes a
 * `notif` event; each change of track or play state a `media` event. Both go through the service's outbox.
 */
public class NotifListener extends NotificationListenerService {
    static final String TAG = Store.TAG;
    private Store store;
    private final Map<String, Long> recent = new HashMap<>();  // dedupe: apps re-post the same notification
    private MediaSessionManager msm;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final Map<MediaController, MediaController.Callback> watched = new HashMap<>();
    private String lastMedia = "";

    @Override public void onCreate() {
        super.onCreate();
        store = new Store(this);
    }

    @Override public void onListenerConnected() {
        Log.i(TAG, "notification listener connected");
        try {
            msm = getSystemService(MediaSessionManager.class);
            ComponentName me = new ComponentName(this, NotifListener.class);
            msm.addOnActiveSessionsChangedListener(this::watchSessions, me, main);
            watchSessions(msm.getActiveSessions(me));
        } catch (SecurityException e) {
            Log.w(TAG, "no media session access yet: " + e);
        }
    }

    @Override public void onNotificationPosted(StatusBarNotification sbn) {
        if (sbn == null || getPackageName().equals(sbn.getPackageName())) return;
        Notification n = sbn.getNotification();
        if ((n.flags & Notification.FLAG_GROUP_SUMMARY) != 0) return;
        Bundle x = n.extras;
        String title = str(x.getCharSequence(Notification.EXTRA_TITLE));
        String text = str(x.getCharSequence(Notification.EXTRA_BIG_TEXT));
        if (text.isEmpty()) text = str(x.getCharSequence(Notification.EXTRA_TEXT));
        String conv = str(x.getCharSequence(Notification.EXTRA_CONVERSATION_TITLE));
        // a messaging-style notification carries the messages themselves; the newest one is the news
        Parcelable[] msgs = x.getParcelableArray(Notification.EXTRA_MESSAGES);
        String sender = "";
        if (msgs != null && msgs.length > 0 && msgs[msgs.length - 1] instanceof Bundle) {
            Bundle m = (Bundle) msgs[msgs.length - 1];
            text = str(m.getCharSequence("text"));
            CharSequence s = m.getCharSequence("sender");
            if (s == null && m.getBundle("sender_person") != null) s = m.getBundle("sender_person").getCharSequence("name");
            sender = str(s);
        }
        if (title.isEmpty() && text.isEmpty()) return;
        boolean ongoing = sbn.isOngoing() || (n.flags & Notification.FLAG_FOREGROUND_SERVICE) != 0;
        if (ongoing && !Notification.CATEGORY_CALL.equals(n.category)) return;  // "running in background", downloads, players
        if (Notification.CATEGORY_TRANSPORT.equals(n.category) || Notification.CATEGORY_PROGRESS.equals(n.category)) return;
        String key = sbn.getPackageName() + "|" + title + "|" + text;
        long now = System.currentTimeMillis();
        Long last = recent.get(key);
        if (last != null && now - last < 10 * 60_000) return;
        recent.put(key, now);
        if (recent.size() > 500) recent.clear();
        JSONObject ev = new JSONObject();
        try {
            ev.put("type", "notif").put("t", sbn.getPostTime() / 1000).put("app", sbn.getPackageName())
              .put("label", label(sbn.getPackageName())).put("title", cut(title, 120)).put("text", cut(text, 400));
            if (!conv.isEmpty()) ev.put("conv", cut(conv, 80));
            if (!sender.isEmpty()) ev.put("sender", cut(sender, 80));
            if (n.category != null) ev.put("cat", n.category);
        } catch (Exception ignored) { }
        store.enqueue(ev);
        AgentService.flushSoon(this, 45_000);
    }

    // ---- what's playing -----------------------------------------------------------------------------

    private void watchSessions(List<MediaController> sessions) {
        if (sessions == null) return;
        for (MediaController c : watched.keySet()) if (!sessions.contains(c)) c.unregisterCallback(watched.get(c));
        watched.keySet().retainAll(sessions);
        for (MediaController c : sessions) {
            if (watched.containsKey(c)) continue;
            MediaController.Callback cb = new MediaController.Callback() {
                @Override public void onPlaybackStateChanged(PlaybackState s) { media(c); }
                @Override public void onMetadataChanged(MediaMetadata m) { media(c); }
            };
            c.registerCallback(cb, main);
            watched.put(c, cb);
            media(c);
        }
    }

    private void media(MediaController c) {
        MediaMetadata m = c.getMetadata();
        PlaybackState s = c.getPlaybackState();
        if (m == null || s == null) return;
        String title = str(m.getString(MediaMetadata.METADATA_KEY_TITLE)), artist = str(m.getString(MediaMetadata.METADATA_KEY_ARTIST));
        String album = str(m.getString(MediaMetadata.METADATA_KEY_ALBUM));
        boolean playing = s.getState() == PlaybackState.STATE_PLAYING;
        if (title.isEmpty()) return;
        String sig = c.getPackageName() + "|" + title + "|" + artist + "|" + playing;
        if (sig.equals(lastMedia)) return;
        lastMedia = sig;
        JSONObject ev = new JSONObject();
        try {
            ev.put("type", "media").put("t", System.currentTimeMillis() / 1000).put("app", c.getPackageName())
              .put("label", label(c.getPackageName())).put("title", cut(title, 120)).put("artist", cut(artist, 80))
              .put("album", cut(album, 80)).put("playing", playing)
              .put("length", m.getLong(MediaMetadata.METADATA_KEY_DURATION) / 1000);
        } catch (Exception ignored) { }
        store.enqueue(ev);
        AgentService.flushSoon(this, 120_000);
    }

    private String label(String pkg) {
        try {
            return getPackageManager().getApplicationLabel(getPackageManager().getApplicationInfo(pkg, 0)).toString();
        } catch (Exception e) {
            return pkg;
        }
    }

    static String str(CharSequence c) {
        return c == null ? "" : c.toString().trim();
    }

    static String cut(String s, int n) {
        return s.length() <= n ? s : s.substring(0, n - 1) + "…";
    }
}
