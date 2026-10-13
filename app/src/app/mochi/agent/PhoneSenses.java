package app.mochi.agent;

import android.app.usage.UsageStats;
import android.app.usage.UsageStatsManager;
import android.content.Context;
import android.content.SharedPreferences;
import android.database.ContentObserver;
import android.database.Cursor;
import android.media.AudioDeviceInfo;
import android.media.AudioManager;
import android.net.Uri;
import android.os.Handler;
import android.provider.CallLog;
import android.provider.ContactsContract;
import android.provider.Telephony;
import android.util.Log;

import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Calendar;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/** The phone's own records: calls, texts, who a number is, which apps they used, what the audio is going to. */
final class PhoneSenses {
    static final String TAG = Store.TAG;
    private final Context ctx;
    private final Store store;
    private final SharedPreferences cursors;  // where we got to in each log, so a restart doesn't resend
    private final Map<String, String> names = new HashMap<>();

    PhoneSenses(Context ctx, Store store) {
        this.ctx = ctx;
        this.store = store;
        cursors = ctx.getSharedPreferences("cursors", Context.MODE_PRIVATE);
    }

    /** Watch the call log and the SMS store; each change is read since the last cursor. */
    void watch(Handler h) {
        ContentObserver calls = new ContentObserver(h) {
            @Override public void onChange(boolean self, Uri uri) { h.postDelayed(PhoneSenses.this::syncCalls, 3000); }
        };
        ContentObserver sms = new ContentObserver(h) {
            @Override public void onChange(boolean self, Uri uri) { h.postDelayed(PhoneSenses.this::syncSms, 1500); }
        };
        try {
            ctx.getContentResolver().registerContentObserver(CallLog.Calls.CONTENT_URI, true, calls);
            ctx.getContentResolver().registerContentObserver(Telephony.Sms.CONTENT_URI, true, sms);
        } catch (SecurityException e) {
            Log.w(TAG, "calls/sms: no permission: " + e);
        }
        if (!cursors.contains("calls")) cursors.edit().putLong("calls", System.currentTimeMillis() - 86400_000L).apply();  // a day back on the first run
        if (!cursors.contains("sms")) cursors.edit().putLong("sms", System.currentTimeMillis() - 86400_000L).apply();
        h.postDelayed(this::syncCalls, 5000);
        h.postDelayed(this::syncSms, 6000);
    }

    synchronized boolean syncCalls() {
        long since = cursors.getLong("calls", 0);
        long newest = since;
        boolean any = false;
        try (Cursor c = ctx.getContentResolver().query(CallLog.Calls.CONTENT_URI,
                new String[]{CallLog.Calls._ID, CallLog.Calls.NUMBER, CallLog.Calls.CACHED_NAME, CallLog.Calls.TYPE, CallLog.Calls.DATE, CallLog.Calls.DURATION},
                CallLog.Calls.DATE + " > ?", new String[]{String.valueOf(since)}, CallLog.Calls.DATE + " ASC")) {
            while (c != null && c.moveToNext()) {
                long date = c.getLong(4);
                int type = c.getInt(3);
                String number = c.getString(1) == null ? "" : c.getString(1);
                String name = c.getString(2);
                if (name == null || name.isEmpty()) name = lookup(number);
                String dir = type == CallLog.Calls.INCOMING_TYPE ? "in" : type == CallLog.Calls.OUTGOING_TYPE ? "out"
                        : type == CallLog.Calls.MISSED_TYPE ? "missed" : type == CallLog.Calls.REJECTED_TYPE ? "rejected"
                        : type == CallLog.Calls.VOICEMAIL_TYPE ? "voicemail" : type == CallLog.Calls.BLOCKED_TYPE ? "blocked" : "other";
                JSONObject ev = new JSONObject();
                ev.put("type", "call").put("t", date / 1000).put("id", c.getLong(0)).put("dir", dir)
                  .put("number", number).put("name", name).put("duration", c.getLong(5));
                store.enqueue(ev);
                newest = Math.max(newest, date);
                any = true;
            }
        } catch (Exception e) {
            Log.w(TAG, "call log: " + e);
        }
        cursors.edit().putLong("calls", newest).apply();
        return any;
    }

    synchronized boolean syncSms() {
        long since = cursors.getLong("sms", 0);
        long newest = since;
        boolean any = false;
        try (Cursor c = ctx.getContentResolver().query(Telephony.Sms.CONTENT_URI,
                new String[]{Telephony.Sms._ID, Telephony.Sms.ADDRESS, Telephony.Sms.BODY, Telephony.Sms.TYPE, Telephony.Sms.DATE, Telephony.Sms.READ},
                Telephony.Sms.DATE + " > ?", new String[]{String.valueOf(since)}, Telephony.Sms.DATE + " ASC")) {
            while (c != null && c.moveToNext()) {
                long date = c.getLong(4);
                int type = c.getInt(3);
                if (type != Telephony.Sms.MESSAGE_TYPE_INBOX && type != Telephony.Sms.MESSAGE_TYPE_SENT) continue;  // drafts, queued, failed
                String address = c.getString(1) == null ? "" : c.getString(1);
                JSONObject ev = new JSONObject();
                ev.put("type", "sms").put("t", date / 1000).put("id", c.getLong(0))
                  .put("dir", type == Telephony.Sms.MESSAGE_TYPE_INBOX ? "in" : "out")
                  .put("address", address).put("name", lookup(address)).put("text", NotifListener.cut(c.getString(2) == null ? "" : c.getString(2), 600))
                  .put("read", c.getInt(5) == 1);
                store.enqueue(ev);
                newest = Math.max(newest, date);
                any = true;
            }
        } catch (Exception e) {
            Log.w(TAG, "sms: " + e);
        }
        cursors.edit().putLong("sms", newest).apply();
        return any;
    }

    /** The contact a number belongs to, or "" (cached per number for the life of the service). */
    String lookup(String number) {
        if (number == null || number.isEmpty() || !Character.isDigit(number.charAt(number.length() - 1))) return "";
        if (names.containsKey(number)) return names.get(number);
        String name = "";
        try (Cursor c = ctx.getContentResolver().query(
                Uri.withAppendedPath(ContactsContract.PhoneLookup.CONTENT_FILTER_URI, Uri.encode(number)),
                new String[]{ContactsContract.PhoneLookup.DISPLAY_NAME}, null, null, null)) {
            if (c != null && c.moveToFirst() && c.getString(0) != null) name = c.getString(0);
        } catch (Exception e) {
            Log.w(TAG, "contacts: " + e);
        }
        names.put(number, name);
        return name;
    }

    /** Today's foreground time per app (the system's daily bucket), the top ones. */
    JSONObject usageToday() {
        JSONObject out = new JSONObject();
        try {
            UsageStatsManager usm = ctx.getSystemService(UsageStatsManager.class);
            Calendar day = Calendar.getInstance();
            day.set(Calendar.HOUR_OF_DAY, 0); day.set(Calendar.MINUTE, 0); day.set(Calendar.SECOND, 0); day.set(Calendar.MILLISECOND, 0);
            long start = day.getTimeInMillis(), now = System.currentTimeMillis();
            Map<String, Long> agg = new HashMap<>();
            List<UsageStats> stats = usm.queryUsageStats(UsageStatsManager.INTERVAL_DAILY, start, now);
            if (stats != null) for (UsageStats s : stats) {
                if (s.getLastTimeUsed() < start) continue;
                long t = s.getTotalTimeInForeground() / 1000;
                if (t < 60) continue;
                agg.merge(s.getPackageName(), t, Long::sum);
            }
            List<Map.Entry<String, Long>> top = new ArrayList<>(agg.entrySet());
            top.sort((a, b) -> Long.compare(b.getValue(), a.getValue()));
            JSONObject apps = new JSONObject();
            for (Map.Entry<String, Long> e : top.subList(0, Math.min(20, top.size()))) apps.put(label(e.getKey()), e.getValue());
            out.put("type", "usage").put("t", now / 1000).put("day", String.format("%tF", day)).put("apps", apps);
        } catch (Exception e) {
            Log.w(TAG, "usage: " + e);
            return null;
        }
        return out;
    }

    /** Where the sound goes: Bluetooth headphones, a car, a speaker, by product name. */
    List<String> audioOutputs() {
        List<String> out = new ArrayList<>();
        try {
            for (AudioDeviceInfo d : ctx.getSystemService(AudioManager.class).getDevices(AudioManager.GET_DEVICES_OUTPUTS)) {
                int t = d.getType();
                if (t == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP || t == AudioDeviceInfo.TYPE_BLUETOOTH_SCO || t == AudioDeviceInfo.TYPE_WIRED_HEADPHONES
                        || t == AudioDeviceInfo.TYPE_WIRED_HEADSET || t == AudioDeviceInfo.TYPE_USB_HEADSET || t == AudioDeviceInfo.TYPE_BLE_HEADSET
                        || t == AudioDeviceInfo.TYPE_BLE_SPEAKER || t == AudioDeviceInfo.TYPE_HEARING_AID) {
                    String n = d.getProductName() == null ? "" : d.getProductName().toString();
                    if (!n.isEmpty() && !out.contains(n)) out.add(n);
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "audio devices: " + e);
        }
        return out;
    }

    String label(String pkg) {
        try {
            return ctx.getPackageManager().getApplicationLabel(ctx.getPackageManager().getApplicationInfo(pkg, 0)).toString();
        } catch (Exception e) {
            return pkg;
        }
    }
}
