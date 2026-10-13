#!/bin/sh
# Builds app/build/mochi.apk with nothing but the Android SDK (no Gradle, no dependencies).
#   SDK=~/android-sdk (default), build-tools 35.0.0, platform android-35, a JDK with javac.
set -eu
cd "$(dirname "$0")"
SDK=${ANDROID_SDK_ROOT:-$HOME/android-sdk}
BT=$SDK/build-tools/35.0.0
JAR=$SDK/platforms/android-35/android.jar
KS=${MOCHI_APP_KEYSTORE:-$HOME/.config/mochi/phone-app.jks}
OUT=build
rm -rf "$OUT"; mkdir -p "$OUT/gen" "$OUT/classes" res/mipmap
# launcher icon: the pig's portrait from the repo
python3 - <<'EOF'
from PIL import Image
im = Image.open("../dist/mochi-avatar.png").convert("RGBA")
for size, name in ((192, "res/mipmap/ic_launcher.png"),):
    im.resize((size, size), Image.LANCZOS).save(name)
EOF
"$BT/aapt2" compile --dir res -o "$OUT/res.zip"
"$BT/aapt2" link -o "$OUT/base.apk" -I "$JAR" --manifest AndroidManifest.xml -R "$OUT/res.zip" --java "$OUT/gen" --auto-add-overlay
"${JAVAC:-$SDK/jdk/bin/javac}" --release 17 -Xlint:-options -cp "$JAR" -d "$OUT/classes" $(find src "$OUT/gen" -name '*.java')
"$BT/d8" --release --lib "$JAR" --min-api 33 --output "$OUT" $(find "$OUT/classes" -name '*.class')
(cd "$OUT" && zip -q base.apk classes.dex)
"$BT/zipalign" -f -p 4 "$OUT/base.apk" "$OUT/aligned.apk"
if [ ! -f "$KS" ]; then  # the signing key: updates must be signed with the same one, so it lives outside the repo
    mkdir -p "$(dirname "$KS")"
    "${KEYTOOL:-$SDK/jdk/bin/keytool}" -genkeypair -keystore "$KS" -storepass mochi-phone -keypass mochi-phone -alias mochi -keyalg EC -groupname secp256r1 \
        -validity 10000 -dname "CN=Mochi" >/dev/null 2>&1
    chmod 600 "$KS"
fi
"$BT/apksigner" sign --ks "$KS" --ks-pass pass:mochi-phone --ks-key-alias mochi --out "$OUT/mochi.apk" "$OUT/aligned.apk"
ls -l "$OUT/mochi.apk"
