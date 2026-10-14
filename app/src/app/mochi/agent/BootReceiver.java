package app.mochi.agent;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Back on after a reboot or an update, without anyone opening the app. */
public class BootReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context ctx, Intent i) {
        ctx.startForegroundService(new Intent(ctx, AgentService.class));
    }
}
