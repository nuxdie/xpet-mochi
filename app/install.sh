#!/bin/sh
# Builds, installs and pairs Mochi on the phone connected over adb, granting it everything it asks for.
#   app/install.sh            build + install/update + grant + pair + start
#   app/install.sh --no-pair  keep the phone's current pairing (just an update)
set -eu
cd "$(dirname "$0")"
PKG=app.mochi.agent
adb get-state >/dev/null 2>&1 || { echo "no phone on adb (adb connect IP:PORT)"; exit 1; }
sh build.sh
adb install -r -g build/mochi.apk
# runtime permissions -g doesn't cover, and the background allowances that keep a location service alive
for p in ACCESS_BACKGROUND_LOCATION ACCESS_FINE_LOCATION ACCESS_COARSE_LOCATION POST_NOTIFICATIONS ACTIVITY_RECOGNITION NEARBY_WIFI_DEVICES \
         READ_CALL_LOG READ_PHONE_STATE READ_CONTACTS READ_SMS RECEIVE_SMS BLUETOOTH_CONNECT CAMERA RECORD_AUDIO; do
    adb shell pm grant $PKG android.permission.$p 2>/dev/null || true
done
for op in RUN_IN_BACKGROUND RUN_ANY_IN_BACKGROUND COARSE_LOCATION FINE_LOCATION MONITOR_LOCATION MONITOR_HIGH_POWER_LOCATION ACTIVITY_RECOGNITION \
          GET_USAGE_STATS READ_CALL_LOG READ_SMS READ_CONTACTS CAMERA RECORD_AUDIO; do
    adb shell cmd appops set $PKG $op allow 2>/dev/null || true
done
adb shell dumpsys deviceidle whitelist +$PKG >/dev/null
adb shell cmd notification allow_listener $PKG/.NotifListener >/dev/null   # the shade, and what's playing
adb shell cmd appops set $PKG SYSTEM_ALERT_WINDOW allow 2>/dev/null || true
adb shell am start -W -n $PKG/.MainActivity >/dev/null   # a launch takes the app out of the "stopped" state, so boot works
if [ "${1:-}" != "--no-pair" ]; then
    CFG=$(mochi-phone pair)
    B64=$(printf '%s' "$CFG" | base64 -w0)
    adb shell "run-as $PKG sh -c 'echo $B64 | base64 -d > files/config.json && chmod 600 files/config.json'"
    echo "paired"
fi
adb shell am start -W -n $PKG/.MainActivity -a app.mochi.agent.RELOAD >/dev/null
sleep 3
adb shell "run-as $PKG ls -l files/"
adb logcat -d -s Mochi:V | tail -n 8
