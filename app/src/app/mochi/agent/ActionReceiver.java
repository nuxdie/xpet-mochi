package app.mochi.agent;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** A button on one of Mochi's notifications was tapped: hand the answer to the service, which queues it. */
public class ActionReceiver extends BroadcastReceiver {
    static final String ACTION_REPLY = "app.mochi.agent.REPLY";

    @Override public void onReceive(Context ctx, Intent i) {
        Intent svc = new Intent(ctx, AgentService.class).setAction(ACTION_REPLY).putExtras(i);
        ctx.startForegroundService(svc);
    }
}
