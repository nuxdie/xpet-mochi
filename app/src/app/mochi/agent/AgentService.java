package app.mochi.agent;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.ServiceInfo;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.media.AudioManager;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.wifi.WifiInfo;
import android.os.BatteryManager;
import android.os.Build;
import android.os.CancellationSignal;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.os.VibrationEffect;
import android.os.VibratorManager;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * The always-on part. Watches location, battery, network, screen and steps; queues events; delivers them to the
 * relay (coalesced, retried, never lost); and pulls what Mochi sends back (locate, buzz, ask).
 */
public class AgentService extends Service {
    static final String TAG = Store.TAG;
    static final String ACTION_RELOAD = "app.mochi.agent.RELOAD";
    static final String ACTION_FLUSH = "app.mochi.agent.FLUSH";
    static final String ACTION_MAILTEST = "app.mochi.agent.MAILTEST";  // from adb: mail the outbox now, to prove the fallback
    static final String CH_ONGOING = "mochi", CH_ASKS = "mochi-asks";
    static final int NOTE_ONGOING = 1;
    static final String VERSION = "0.3";

    // when to bother the radio
    static final long FLUSH_COALESCE_MS = 20_000;
    static final long HEARTBEAT_MS = 15 * 60_000;
    static final long LOC_RESEND_MS = 10 * 60_000;
    static final float LOC_RESEND_M = 40f;
    static final long POLL_IDLE_MS = 3 * 60_000;

    private Store store;
    private Relay relay;
    private Handler io;        // network + queue work
    private Handler main;
    private LocationManager lm;
    private PowerManager.WakeLock wake;
    private NotificationManager nm;

    private Location lastSent;
    private long lastSentAt;
    private int battery = -1;
    private boolean charging;
    private String wifi = "";
    private String net = "";
    private boolean screenOn = true;
    private float steps = -1;
    private int lastBatterySent = -1;
    private long lastFlushOk;
    private final Set<String> seen = new HashSet<>();
    private String ackPending = "";
    private volatile boolean running;
    private final Object pullLock = new Object();  // the puller sleeps on this; nudgePull() wakes it early
    private PhoneSenses senses;
    private String ringer = "";
    private boolean dnd;
    private List<String> audioOut = new ArrayList<>();
    private long startedAt;
    private long lastMailFallback;
    static final long MAIL_FALLBACK_AFTER_MS = 30 * 60_000, MAIL_FALLBACK_EVERY_MS = 20 * 60_000;
    private static volatile AgentService instance;

    /** For the listener and receivers: get the outbox on its way, from wherever they run. */
    static void flushSoon(Context ctx, long delayMs) {
        AgentService me = instance;
        if (me != null) me.scheduleFlush(delayMs);
        else ctx.startForegroundService(new Intent(ctx, AgentService.class).setAction(ACTION_FLUSH));
    }

    @Override public IBinder onBind(Intent i) { return null; }

    @Override public void onCreate() {
        super.onCreate();
        store = new Store(this);
        nm = getSystemService(NotificationManager.class);
        channels();
        startForeground(NOTE_ONGOING, ongoing("starting"), ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION);
        main = new Handler(Looper.getMainLooper());
        HandlerThread t = new HandlerThread("mochi-io");
        t.start();
        io = new Handler(t.getLooper());
        wake = getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "mochi:io");
        lm = getSystemService(LocationManager.class);
        running = true;
        startedAt = System.currentTimeMillis();
        instance = this;
        senses = new PhoneSenses(this, store);
        setup();
        senses();
        senses.watch(io);
        Thread puller = new Thread(() -> { sleep(3000); pullLoop(); }, "mochi-pull");  // a moment for the network callback
        puller.setDaemon(true);
        puller.start();
        io.postDelayed(this::heartbeat, HEARTBEAT_MS);
    }

    private void setup() {
        store.reload();
        if (!store.paired()) {
            note("not paired yet (install.sh)");
            relay = null;
            return;
        }
        relay = new Relay(store.config.optString("relay"), store.config.optString("token"));
        JSONObject hello = ev("hello");
        try { hello.put("version", VERSION).put("android", Build.VERSION.RELEASE).put("model", Build.MODEL); } catch (Exception ignored) { }
        store.enqueue(hello);
        scheduleFlush(0);
        note("with you");
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        String a = intent == null ? null : intent.getAction();
        if (ACTION_RELOAD.equals(a)) io.post(this::setup);
        else if (ACTION_FLUSH.equals(a)) scheduleFlush(0);
        else if (ACTION_MAILTEST.equals(a)) io.post(() -> {
            JSONObject hello = ev("hello");
            try { hello.put("version", VERSION).put("via", "mailtest"); } catch (Exception ignored) { }
            store.enqueue(hello);  // something to send, in case the outbox is empty
            lastMailFallback = 0; startedAt = 0; lastFlushOk = 0;
            mailFallback();
        });
        else if (ActionReceiver.ACTION_REPLY.equals(a)) reply(intent);
        return START_STICKY;
    }

    @Override public void onDestroy() {
        running = false;
        instance = null;
        super.onDestroy();
    }

    // ---- senses --------------------------------------------------------------------------------------

    private void senses() {
        // location: the fused provider (Google's, on a Pixel), falling back to GPS + network
        String provider = lm.getAllProviders().contains(LocationManager.FUSED_PROVIDER) ? LocationManager.FUSED_PROVIDER : LocationManager.GPS_PROVIDER;
        try {
            lm.requestLocationUpdates(provider, 30_000, 15f, locListener, Looper.getMainLooper());
            if (!provider.equals(LocationManager.FUSED_PROVIDER) && lm.getAllProviders().contains(LocationManager.NETWORK_PROVIDER))
                lm.requestLocationUpdates(LocationManager.NETWORK_PROVIDER, 60_000, 50f, locListener, Looper.getMainLooper());
            Location last = lm.getLastKnownLocation(provider);
            if (last != null) onFix(last, false);
        } catch (SecurityException e) {
            Log.e(TAG, "no location permission", e);
            note("needs location permission");
        }
        // battery (sticky: the registration itself delivers the current state)
        registerReceiver(batteryReceiver, new IntentFilter(Intent.ACTION_BATTERY_CHANGED), Context.RECEIVER_EXPORTED);
        IntentFilter screen = new IntentFilter(Intent.ACTION_SCREEN_ON);
        screen.addAction(Intent.ACTION_SCREEN_OFF);
        registerReceiver(screenReceiver, screen, Context.RECEIVER_EXPORTED);
        screenOn = getSystemService(PowerManager.class).isInteractive();
        // ringer, do-not-disturb, and where the sound goes (headphones, the car)
        IntentFilter modes = new IntentFilter(AudioManager.RINGER_MODE_CHANGED_ACTION);
        modes.addAction(NotificationManager.ACTION_INTERRUPTION_FILTER_CHANGED);
        modes.addAction("android.bluetooth.device.action.ACL_CONNECTED");
        modes.addAction("android.bluetooth.device.action.ACL_DISCONNECTED");
        modes.addAction(AudioManager.ACTION_HEADSET_PLUG);
        registerReceiver(modeReceiver, modes, Context.RECEIVER_EXPORTED);
        readModes();
        // network: which transport, and the Wi-Fi name (needs location permission, hence the flag)
        ConnectivityManager cm = getSystemService(ConnectivityManager.class);
        cm.registerDefaultNetworkCallback(new ConnectivityManager.NetworkCallback(ConnectivityManager.NetworkCallback.FLAG_INCLUDE_LOCATION_INFO) {
            @Override public void onCapabilitiesChanged(Network n, NetworkCapabilities nc) {
                String newNet = nc.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) ? "wifi"
                        : nc.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) ? "mobile"
                        : nc.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) ? "ethernet" : "other";
                String newWifi = "";
                if (nc.getTransportInfo() instanceof WifiInfo) {
                    String s = ((WifiInfo) nc.getTransportInfo()).getSSID();
                    if (s != null && !s.equals(WifiManagerCompat.UNKNOWN)) newWifi = s.replace("\"", "");
                }
                boolean changed = !newNet.equals(net) || !newWifi.equals(wifi);
                net = newNet;
                wifi = newWifi;
                if (changed) { stateEvent(); scheduleFlush(FLUSH_COALESCE_MS); nudgePull(); }
            }
            @Override public void onLost(Network n) {
                net = "none";
                wifi = "";
                stateEvent();
            }
        });
        // steps: the cumulative counter since boot; the desk works out the deltas
        SensorManager sm = getSystemService(SensorManager.class);
        Sensor stepSensor = sm.getDefaultSensor(Sensor.TYPE_STEP_COUNTER);
        if (stepSensor != null) sm.registerListener(new SensorEventListener() {
            @Override public void onSensorChanged(SensorEvent e) { steps = e.values[0]; }
            @Override public void onAccuracyChanged(Sensor s, int a) { }
        }, stepSensor, SensorManager.SENSOR_DELAY_NORMAL);
    }

    private final LocationListener locListener = new LocationListener() {
        @Override public void onLocationChanged(Location l) { onFix(l, false); }
        @Override public void onProviderEnabled(String p) { }
        @Override public void onProviderDisabled(String p) { }
    };

    private void onFix(Location l, boolean force) {
        boolean worth = force || lastSent == null
                || l.distanceTo(lastSent) >= LOC_RESEND_M
                || System.currentTimeMillis() - lastSentAt >= LOC_RESEND_MS
                || (l.hasAccuracy() && lastSent.hasAccuracy() && l.getAccuracy() < lastSent.getAccuracy() / 3 && lastSent.getAccuracy() > 100);
        if (!worth) return;
        JSONObject ev = ev("loc");
        try {
            ev.put("t", l.getTime() / 1000).put("lat", round(l.getLatitude(), 6)).put("lon", round(l.getLongitude(), 6))
              .put("acc", l.hasAccuracy() ? Math.round(l.getAccuracy()) : JSONObject.NULL)
              .put("alt", l.hasAltitude() ? Math.round(l.getAltitude()) : JSONObject.NULL)
              .put("speed", l.hasSpeed() ? round(l.getSpeed(), 1) : JSONObject.NULL)
              .put("provider", l.getProvider());
        } catch (Exception ignored) { }
        store.enqueue(ev);
        lastSent = l;
        lastSentAt = System.currentTimeMillis();
        boolean moving = l.hasSpeed() && l.getSpeed() > 1.5f;
        scheduleFlush(force || !moving ? 0 : FLUSH_COALESCE_MS);
    }

    private final BroadcastReceiver batteryReceiver = new BroadcastReceiver() {
        @Override public void onReceive(Context c, Intent i) {
            int level = i.getIntExtra(BatteryManager.EXTRA_LEVEL, -1), scale = i.getIntExtra(BatteryManager.EXTRA_SCALE, 100);
            int status = i.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
            boolean nowCharging = status == BatteryManager.BATTERY_STATUS_CHARGING || status == BatteryManager.BATTERY_STATUS_FULL;
            int pct = scale > 0 ? level * 100 / scale : level;
            boolean flip = nowCharging != charging;
            battery = pct;
            charging = nowCharging;
            if (flip) nudgePull();
            if (flip || lastBatterySent < 0 || Math.abs(pct - lastBatterySent) >= 5 || pct <= 15) {
                stateEvent();
                scheduleFlush(flip ? 0 : FLUSH_COALESCE_MS);
            }
        }
    };

    private final BroadcastReceiver screenReceiver = new BroadcastReceiver() {
        @Override public void onReceive(Context c, Intent i) {
            screenOn = Intent.ACTION_SCREEN_ON.equals(i.getAction());
            stateEvent();
            scheduleFlush(60_000);  // the screen flips often; let it ride with the next flush
        }
    };

    private final BroadcastReceiver modeReceiver = new BroadcastReceiver() {
        @Override public void onReceive(Context c, Intent i) {
            io.postDelayed(() -> {  // a Bluetooth device takes a moment to become an audio route
                if (readModes()) { stateEvent(); scheduleFlush(FLUSH_COALESCE_MS); }
            }, 2500);
        }
    };

    /** @return whether anything changed */
    private boolean readModes() {
        int rm = getSystemService(AudioManager.class).getRingerMode();
        String newRinger = rm == AudioManager.RINGER_MODE_SILENT ? "silent" : rm == AudioManager.RINGER_MODE_VIBRATE ? "vibrate" : "normal";
        boolean newDnd = nm.getCurrentInterruptionFilter() != NotificationManager.INTERRUPTION_FILTER_ALL
                && nm.getCurrentInterruptionFilter() != NotificationManager.INTERRUPTION_FILTER_UNKNOWN;
        List<String> newOut = senses.audioOutputs();
        boolean changed = !newRinger.equals(ringer) || newDnd != dnd || !newOut.equals(audioOut);
        ringer = newRinger;
        dnd = newDnd;
        audioOut = newOut;
        return changed;
    }

    private void stateEvent() {
        if (battery < 0) return;  // the battery broadcast hasn't arrived yet; the first real state follows in a moment
        JSONObject ev = ev("state");
        try {
            ev.put("battery", battery).put("charging", charging).put("wifi", wifi).put("net", net).put("screen", screenOn)
              .put("ringer", ringer).put("dnd", dnd).put("audio", new JSONArray(audioOut));
            if (steps >= 0) ev.put("steps", Math.round(steps));
        } catch (Exception ignored) { }
        lastBatterySent = battery;
        store.enqueue(ev);
    }

    private void heartbeat() {
        if (!running) return;
        readModes();
        stateEvent();
        JSONObject usage = senses.usageToday();
        if (usage != null) store.enqueue(usage);
        senses.syncCalls();
        senses.syncSms();
        if (lastSent != null) onFix(lastSent, true);  // the last fix again, so the desk knows the phone is alive even if still
        scheduleFlush(0);
        io.postDelayed(this::heartbeat, HEARTBEAT_MS);
    }

    // ---- outbound ------------------------------------------------------------------------------------

    private boolean flushScheduled;

    private void scheduleFlush(long delayMs) {
        io.removeCallbacks(flushRunnable);
        io.postDelayed(flushRunnable, delayMs);
    }

    private final Runnable flushRunnable = this::flush;
    private int flushFailures;

    private void flush() {
        if (relay == null) return;
        List<JSONObject> pending = store.pending();
        if (pending.isEmpty()) return;
        wake.acquire(60_000);
        try {
            // many events may have piled up offline: send them in chunks the relay accepts
            int n = Math.min(pending.size(), 400);
            JSONObject msg = new JSONObject();
            msg.put("id", randomId()).put("t", System.currentTimeMillis() / 1000).put("events", Store.toArray(pending.subList(0, n)));
            byte[] blob = Crypto.seal(msg.toString().getBytes(StandardCharsets.UTF_8), store.key("phone_priv"), store.key("phone_pub"), store.key("desk_pub"));
            relay.push(blob);
            store.delivered(n);
            flushFailures = 0;
            lastFlushOk = System.currentTimeMillis();
            note("with you");
            if (pending.size() > n) scheduleFlush(1000);
            nudgePull();  // the radio is up anyway: see whether the desk left anything
            boolean cheap = charging || "wifi".equals(net);
            if (!cheap && hasLoc(pending.subList(0, n))) {  // the desk answers a fix in seconds (an errand nearby): look again
                io.postDelayed(this::nudgePull, 25_000);
                io.postDelayed(this::nudgePull, 60_000);
            }
        } catch (Exception e) {
            flushFailures++;
            long backoff = Math.min(30_000L * (1L << Math.min(flushFailures, 5)), 15 * 60_000L);
            Log.w(TAG, "flush failed (" + flushFailures + "): " + e + "; retry in " + backoff / 1000 + "s");
            note("waiting for a connection (" + store.outboxSize() + " queued)");
            scheduleFlush(backoff);
            mailFallback();
        } finally {
            if (wake.isHeld()) wake.release();
        }
    }

    /** The relay has been out of reach for a while but the network is there: mail the same sealed blob instead. */
    private void mailFallback() {
        JSONObject smtp = store.config.optJSONObject("smtp");
        long now = System.currentTimeMillis();
        if (smtp == null || "none".equals(net) || net.isEmpty()) return;
        if (now - Math.max(lastFlushOk, startedAt) < MAIL_FALLBACK_AFTER_MS || now - lastMailFallback < MAIL_FALLBACK_EVERY_MS) return;
        lastMailFallback = now;
        List<JSONObject> pending = store.pending();
        if (pending.isEmpty()) return;
        try {
            int n = Math.min(pending.size(), 400);
            String id = randomId();
            JSONObject msg = new JSONObject();
            msg.put("id", id).put("t", now / 1000).put("via", "mail").put("events", Store.toArray(pending.subList(0, n)));
            byte[] blob = Crypto.seal(msg.toString().getBytes(StandardCharsets.UTF_8), store.key("phone_priv"), store.key("phone_pub"), store.key("desk_pub"));
            Mailer.send(smtp.optString("host", "smtp.gmail.com"), smtp.optInt("port", 465), smtp.optString("user"), smtp.optString("pass"),
                    smtp.optString("to"), "mochi blob " + id, blob);
            store.delivered(n);
            Log.i(TAG, "mailed " + n + " events as fallback");
            note("relay unreachable; mailed " + n + " events");
        } catch (Exception e) {
            Log.w(TAG, "mail fallback failed: " + e);
        }
    }

    // ---- inbound -------------------------------------------------------------------------------------

    private void pullLoop() {
        int failures = 0;
        while (running) {
            if (relay == null) { sleep(10_000); continue; }
            // a long-poll holds the radio up: do that only when it's cheap (charging or on Wi-Fi); otherwise look now and then
            boolean cheap = charging || "wifi".equals(net);
            try {
                JSONArray items = relay.pull(cheap ? 50 : 0, ackPending);
                ackPending = "";
                failures = 0;
                StringBuilder ack = new StringBuilder();
                for (int i = 0; i < items.length(); i++) {
                    JSONObject it = items.getJSONObject(i);
                    if (ack.length() > 0) ack.append(',');
                    ack.append(it.getString("id"));
                    try {
                        byte[] plain = Crypto.open(Relay.b64(it.getString("b64")), store.key("phone_priv"), store.key("phone_pub"), store.key("desk_pub"));
                        JSONObject msg = new JSONObject(new String(plain, StandardCharsets.UTF_8));
                        String id = msg.optString("id");
                        long age = Math.abs(System.currentTimeMillis() / 1000 - msg.optLong("t"));
                        if (seen.contains(id) || age > 2 * 86400) continue;
                        seen.add(id);
                        if (seen.size() > 1000) seen.clear();
                        main.post(() -> handle(msg));
                    } catch (Exception e) {
                        Log.w(TAG, "unreadable item " + it.optString("id") + ": " + e);
                    }
                }
                ackPending = ack.toString();
                if (items.length() > 0) continue;  // ack right away, there may be more
                if (!cheap) idle(POLL_IDLE_MS);
            } catch (Exception e) {
                failures++;
                Log.w(TAG, "pull failed: " + e);
                idle(Math.min(15_000L * (1L << Math.min(failures, 6)), 15 * 60_000L));
            }
        }
    }

    private void idle(long ms) {
        synchronized (pullLock) {
            try { pullLock.wait(ms); } catch (InterruptedException ignored) { }
        }
    }

    private static boolean hasLoc(List<JSONObject> evs) {
        for (JSONObject e : evs) if ("loc".equals(e.optString("type"))) return true;
        return false;
    }

    private void nudgePull() {
        synchronized (pullLock) { pullLock.notifyAll(); }
    }

    private void handle(JSONObject msg) {
        String type = msg.optString("type");
        Log.i(TAG, "from the desk: " + type);
        switch (type) {
            case "locate":
                locateNow();
                break;
            case "buzz":
                buzz(msg.optString("text", "mochi"));
                break;
            case "ask":
                ask(msg.optString("ask_id"), msg.optString("text"), msg.optJSONArray("options"),
                    msg.optString("link", ""), msg.optBoolean("buzz", false));
                break;
            case "close":
                nm.cancel(1000 + (msg.optString("ask_id").hashCode() & 0xffff));
                break;
            case "photo":
                io.post(() -> capture("photo", msg));
                break;
            case "audio":
                io.post(() -> capture("audio", msg));
                break;
            default:
                Log.w(TAG, "unknown message type " + type);
        }
    }

    /** A still or a clip, on the desk's request. The service takes the camera/microphone foreground types for the
     *  duration (Android shows its green dot meanwhile, as it should) and gives them back after. */
    private void capture(String what, JSONObject msg) {
        String req = msg.optString("id");
        JSONObject ev = ev(what);
        wake.acquire(150_000);
        try {
            int types = ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
                    | (what.equals("photo") ? ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA : ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE);
            startForeground(NOTE_ONGOING, ongoing(what.equals("photo") ? "taking a picture for Mochi" : "listening for Mochi"), types);
            byte[] data;
            if (what.equals("photo")) {
                boolean front = "front".equals(msg.optString("cam"));
                data = Capture.photo(this, front, io);
                ev.put("cam", front ? "front" : "back");
            } else {
                int secs = Math.max(1, Math.min(msg.optInt("seconds", 10), 120));
                data = Capture.audio(this, secs);
                ev.put("seconds", secs);
            }
            ev.put("req", req).put("b64", android.util.Base64.encodeToString(data, android.util.Base64.NO_WRAP)).put("bytes", data.length);
            Log.i(TAG, what + ": " + data.length + " bytes");
        } catch (Exception e) {
            Log.w(TAG, what + " failed: " + e);
            try { ev.put("req", req).put("error", String.valueOf(e)); } catch (Exception ignored) { }
        } finally {
            try { startForeground(NOTE_ONGOING, ongoing("with you"), ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION); } catch (Exception ignored) { }
            if (wake.isHeld()) wake.release();
        }
        store.enqueue(ev);
        scheduleFlush(0);
    }

    private void locateNow() {
        try {
            String provider = lm.getAllProviders().contains(LocationManager.FUSED_PROVIDER) ? LocationManager.FUSED_PROVIDER : LocationManager.GPS_PROVIDER;
            lm.getCurrentLocation(provider, new CancellationSignal(), getMainExecutor(), l -> {
                if (l != null) onFix(l, true);
                else if (lastSent != null) onFix(lastSent, true);
            });
        } catch (SecurityException e) {
            Log.e(TAG, "locate: no permission", e);
        }
    }

    private void buzz(String text) {
        getSystemService(VibratorManager.class).getDefaultVibrator()
                .vibrate(VibrationEffect.createWaveform(new long[]{0, 120, 80, 120, 80, 240}, -1));
        Notification n = new Notification.Builder(this, CH_ASKS)
                .setSmallIcon(R.mipmap.ic_launcher).setContentTitle("Mochi").setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text)).setAutoCancel(true).build();
        nm.notify((int) (System.currentTimeMillis() & 0xffff), n);
    }

    private void ask(String askId, String text, JSONArray options, String link, boolean strong) {
        int nid = 1000 + (askId.hashCode() & 0xffff);
        Notification.Builder b = new Notification.Builder(this, CH_ASKS)
                .setSmallIcon(R.mipmap.ic_launcher).setContentTitle(strong ? "Mochi" : "Mochi asks").setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text)).setAutoCancel(false);
        if (link.startsWith("geo:") || link.startsWith("https://")) {  // a tap on the body: the place, in Maps
            Intent open = new Intent(Intent.ACTION_VIEW, android.net.Uri.parse(link)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            b.setContentIntent(PendingIntent.getActivity(this, nid * 8 + 7, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT));
        }
        List<String> opts = new ArrayList<>();
        if (options != null) for (int i = 0; i < Math.min(3, options.length()); i++) opts.add(options.optString(i));
        if (opts.isEmpty()) { opts.add("Yes"); opts.add("Not now"); }
        int req = nid * 8;
        for (String o : opts) {
            Intent i = new Intent(this, ActionReceiver.class).setAction(ActionReceiver.ACTION_REPLY)
                    .putExtra("ask_id", askId).putExtra("option", o).putExtra("nid", nid);
            PendingIntent pi = PendingIntent.getBroadcast(this, req++, i, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
            b.addAction(new Notification.Action.Builder(null, o, pi).build());
        }
        nm.notify(nid, b.build());
        getSystemService(VibratorManager.class).getDefaultVibrator().vibrate(strong
                ? VibrationEffect.createWaveform(new long[]{0, 120, 80, 120, 80, 240}, -1)  // the buzz: meant to be felt in a pocket
                : VibrationEffect.createOneShot(150, VibrationEffect.DEFAULT_AMPLITUDE));
    }

    private void reply(Intent i) {
        JSONObject ev = ev("reply");
        try { ev.put("ask_id", i.getStringExtra("ask_id")).put("option", i.getStringExtra("option")); } catch (Exception ignored) { }
        store.enqueue(ev);
        nm.cancel(i.getIntExtra("nid", 0));
        scheduleFlush(0);
    }

    // ---- bits ----------------------------------------------------------------------------------------

    private void channels() {
        NotificationChannel on = new NotificationChannel(CH_ONGOING, "Mochi is with you", NotificationManager.IMPORTANCE_MIN);
        on.setShowBadge(false);
        nm.createNotificationChannel(on);
        nm.createNotificationChannel(new NotificationChannel(CH_ASKS, "Mochi asks", NotificationManager.IMPORTANCE_HIGH));
    }

    private Notification ongoing(String text) {
        Intent open = new Intent(this, MainActivity.class);
        return new Notification.Builder(this, CH_ONGOING)
                .setSmallIcon(R.mipmap.ic_launcher).setContentTitle("Mochi").setContentText(text).setOngoing(true)
                .setContentIntent(PendingIntent.getActivity(this, 0, open, PendingIntent.FLAG_IMMUTABLE))
                .build();
    }

    private void note(String text) {
        main.post(() -> nm.notify(NOTE_ONGOING, ongoing(text)));
    }

    private static JSONObject ev(String type) {
        JSONObject o = new JSONObject();
        try { o.put("type", type).put("t", System.currentTimeMillis() / 1000); } catch (Exception ignored) { }
        return o;
    }

    private static double round(double v, int places) {
        double m = Math.pow(10, places);
        return Math.round(v * m) / m;
    }

    private static String randomId() {
        byte[] b = new byte[6];
        new SecureRandom().nextBytes(b);
        StringBuilder s = new StringBuilder();
        for (byte x : b) s.append(String.format("%02x", x));
        return s.toString();
    }

    private static void sleep(long ms) {
        try { Thread.sleep(ms); } catch (InterruptedException ignored) { }
    }

    /** WifiManager.UNKNOWN_SSID without dragging WifiManager in. */
    static final class WifiManagerCompat {
        static final String UNKNOWN = "<unknown ssid>";
    }
}
