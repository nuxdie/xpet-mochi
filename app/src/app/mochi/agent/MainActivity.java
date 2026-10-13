package app.mochi.agent;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Bundle;
import android.os.PowerManager;
import android.provider.Settings;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

/** A plain screen: what's set up, and buttons for the bits adb can't grant. Most of the time nobody opens this. */
public class MainActivity extends Activity {
    private TextView status;

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (24 * getResources().getDisplayMetrics().density);
        root.setPadding(pad, pad, pad, pad);
        status = new TextView(this);
        status.setTextSize(16);
        root.addView(status);
        root.addView(button("Grant permissions", v -> requestPermissions(new String[]{
                Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION,
                Manifest.permission.POST_NOTIFICATIONS, Manifest.permission.ACTIVITY_RECOGNITION,
                Manifest.permission.NEARBY_WIFI_DEVICES}, 1)));
        root.addView(button("Allow location all the time", v ->
                requestPermissions(new String[]{Manifest.permission.ACCESS_BACKGROUND_LOCATION}, 2)));
        root.addView(button("Ignore battery optimisation", v ->
                startActivity(new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:" + getPackageName())))));
        root.addView(button("Start / reload", v -> {
            startForegroundService(new Intent(this, AgentService.class).setAction(AgentService.ACTION_RELOAD));
            refresh();
        }));
        setContentView(root);
        startForegroundService(new Intent(this, AgentService.class));
        onNewIntent(getIntent());
    }

    /** `am start -n app.mochi.agent/.MainActivity -a app.mochi.agent.RELOAD` (install.sh): re-read the pairing. */
    @Override protected void onNewIntent(Intent i) {
        super.onNewIntent(i);
        if (i != null && (AgentService.ACTION_RELOAD.equals(i.getAction()) || AgentService.ACTION_MAILTEST.equals(i.getAction())))
            startForegroundService(new Intent(this, AgentService.class).setAction(i.getAction()));
    }

    @Override protected void onResume() {
        super.onResume();
        refresh();
    }

    private void refresh() {
        Store s = new Store(this);
        boolean fine = granted(Manifest.permission.ACCESS_FINE_LOCATION), bg = granted(Manifest.permission.ACCESS_BACKGROUND_LOCATION);
        boolean notif = granted(Manifest.permission.POST_NOTIFICATIONS);
        boolean batt = getSystemService(PowerManager.class).isIgnoringBatteryOptimizations(getPackageName());
        status.setText("Mochi " + AgentService.VERSION + "\n\n"
                + (s.paired() ? "paired with " + s.config.optString("relay") : "NOT PAIRED (run app/install.sh from the desk)") + "\n"
                + "location: " + (fine ? "yes" : "NO") + ", in background: " + (bg ? "yes" : "NO") + "\n"
                + "notifications: " + (notif ? "yes" : "NO") + "\n"
                + "battery optimisation ignored: " + (batt ? "yes" : "NO") + "\n"
                + "queued events: " + s.outboxSize() + "\n");
    }

    private boolean granted(String p) {
        return checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED;
    }

    private Button button(String text, android.view.View.OnClickListener l) {
        Button b = new Button(this);
        b.setText(text);
        b.setOnClickListener(l);
        return b;
    }
}
