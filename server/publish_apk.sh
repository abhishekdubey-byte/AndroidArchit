#!/bin/bash
# Build + publish both editions so installed phones can auto-update. Run after changing the app.
set -e
AC=/home/mrityunjay/AndroidArchit/android-companion
BT=$(ls -d /home/mrityunjay/android-sdk/build-tools/* | sort -V | tail -1)
cd "$AC"
./gradlew -q assembleFullRelease assembleLiteRelease --offline \
  -x lintVitalAnalyzeFullRelease -x lintVitalReportFullRelease -x lintVitalFullRelease \
  -x lintVitalAnalyzeLiteRelease -x lintVitalReportLiteRelease -x lintVitalLiteRelease
for ed in full lite; do
  APK="$AC/app/build/outputs/apk/$ed/release/app-$ed-release.apk"
  VC=$("$BT/aapt2" dump badging "$APK" | grep -oE "versionCode='[0-9]+'" | grep -oE "[0-9]+")
  VN=$("$BT/aapt2" dump badging "$APK" | grep -oE "versionName='[^']+'" | cut -d"'" -f2)
  cp "$APK" /home/mrityunjay/AndroidArchit/OperatorCompanion$([ "$ed" = lite ] && echo -Lite).apk
  /home/mrityunjay/AndroidArchit/server/.venv/bin/python - "$ed" "$APK" "$VC" "$VN" <<PY
import sys; sys.path.insert(0,"/home/mrityunjay/AndroidArchit/server")
from pathlib import Path
from app.services import app_update
print(app_update.publish(sys.argv[1], Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4]))
PY
done
