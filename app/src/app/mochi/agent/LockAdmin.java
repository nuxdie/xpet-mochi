package app.mochi.agent;

import android.app.admin.DeviceAdminReceiver;

/**
 * Device admin with the one policy it needs, force-lock: the desk's sleep wall (mochi-bedtime) sends "lock" at
 * night and the phone locks like the other screens. Switched on by install.sh over adb (dpm set-active-admin).
 */
public class LockAdmin extends DeviceAdminReceiver {
}
