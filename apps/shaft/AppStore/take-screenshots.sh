#!/bin/bash
# Takes the App Store screenshots from the iOS app's built-in demo into AppStore/screenshots/<device class>/.
# It uses simulators of its own (named "SHAFT Shots …", created on first use), never one another session has booted.
#
#   AppStore/take-screenshots.sh [iphone-6.9] [iphone-6.3] [ipad-13] [watch]     (no argument: all four)
#
# iPhone and iPad: the SHAFT-Screenshots UI test (ScreenshotTests/StoreScreenshots.swift) drives the demo.
# Watch: the iPhone simulator paired with it opens the demo at launch (SHAFT_START_DEMO, Debug only) and the watch
# app opens the first instance (SHAFT_WATCH_OPEN); simctl takes the watch's screen.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
app_dir=$(cd "$here/.." && pwd)
out_root="$here/screenshots"
dd="$app_dir/build/shots-dd"
runtime_ios=$(xcrun simctl list runtimes -j | python3 -c 'import json,sys; r=[x for x in json.load(sys.stdin)["runtimes"] if x["platform"]=="iOS" and x["isAvailable"]]; print(r[-1]["identifier"])')
runtime_watch=$(xcrun simctl list runtimes -j | python3 -c 'import json,sys; r=[x for x in json.load(sys.stdin)["runtimes"] if x["platform"]=="watchOS" and x["isAvailable"]]; print(r[-1]["identifier"])')

classes=("$@")
[ ${#classes[@]} -gt 0 ] || classes=(iphone-6.9 iphone-6.3 ipad-13 watch)

device_type() {
  case "$1" in
    iphone-6.9) echo com.apple.CoreSimulator.SimDeviceType.iPhone-18-Pro-Max ;;
    iphone-6.3) echo com.apple.CoreSimulator.SimDeviceType.iPhone-18-Pro ;;
    ipad-13) echo com.apple.CoreSimulator.SimDeviceType.iPad-Pro-13-inch-M5-12GB ;;
    watch) echo com.apple.CoreSimulator.SimDeviceType.Apple-Watch-Ultra-4-49mm ;;
    *) echo "error: unknown device class $1 (iphone-6.9, iphone-6.3, ipad-13, watch)" >&2; exit 2 ;;
  esac
}

# The udid of "SHAFT Shots <class>", created on first use.
device() {
  local name="SHAFT Shots $1" runtime=$runtime_ios udid
  [ "$1" = watch ] && runtime=$runtime_watch
  udid=$(xcrun simctl list devices -j | python3 -c "import json,sys; d=[x['udid'] for v in json.load(sys.stdin)['devices'].values() for x in v if x['name']=='$name']; print(d[0] if d else '')")
  [ -n "$udid" ] || udid=$(xcrun simctl create "$name" "$(device_type "$1")" "$runtime")
  echo "$udid"
}

boot() {
  xcrun simctl boot "$1" 2>/dev/null || true
  xcrun simctl bootstatus "$1" -b >/dev/null
}

# A clean phone or tablet: no app data, no pairing, a status bar at the time of the shots, full signal and battery.
prepare() {
  boot "$1"
  xcrun simctl uninstall "$1" "$bundle_id" 2>/dev/null || true
  xcrun simctl keychain "$1" reset
  # simctl refuses some clock strings (seen: "16:00"); the next minute is as good.
  xcrun simctl status_bar "$1" override --time "$(date +%H:%M)" 2>/dev/null ||
    xcrun simctl status_bar "$1" override --time "$(date -v+1M +%H:%M)"
  xcrun simctl status_bar "$1" override --dataNetwork wifi --wifiMode active --wifiBars 3 \
    --cellularMode active --cellularBars 4 --batteryState charged --batteryLevel 100
}

# App Store Connect refuses an image with an alpha channel or of another size: check each one.
check() {
  local f w h alpha
  for f in "$1"/*.png; do
    w=$(sips -g pixelWidth "$f" | awk '/pixelWidth/ {print $2}')
    h=$(sips -g pixelHeight "$f" | awk '/pixelHeight/ {print $2}')
    alpha=$(sips -g hasAlpha "$f" | awk '/hasAlpha/ {print $2}')
    [ "$alpha" = no ] || { echo "error: $f has an alpha channel" >&2; exit 1; }
    echo "$(basename "$f") ${w}x${h}"
  done
}

echo "==> Build the app and the screenshot test"
(cd "$app_dir" && xcodegen generate >/dev/null)
xcodebuild -project "$app_dir/SHAFT.xcodeproj" -scheme SHAFT-Screenshots -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath "$dd" CODE_SIGN_IDENTITY=- CODE_SIGN_STYLE=Manual build-for-testing -quiet
app="$dd/Build/Products/Debug-iphonesimulator/SHAFT.app"
bundle_id=$(/usr/libexec/PlistBuddy -c 'Print CFBundleIdentifier' "$app/Info.plist")

for class in "${classes[@]}"; do
  out="$out_root/$class"
  rm -rf "$out"
  mkdir -p "$out"
  if [ "$class" = watch ]; then
    echo "==> watch"
    phone=$(device iphone-6.9)
    watch=$(device watch)
    xcrun simctl list pairs | grep -q "$watch" || xcrun simctl pair "$watch" "$phone" >/dev/null
    prepare "$phone"
    boot "$watch"
    xcrun simctl install "$phone" "$app"
    xcrun simctl install "$watch" "$app/Watch/SHAFT.app"
    watch_id=$(/usr/libexec/PlistBuddy -c 'Print CFBundleIdentifier' "$app/Watch/SHAFT.app/Info.plist")
    SIMCTL_CHILD_SHAFT_START_DEMO=1 xcrun simctl launch "$phone" "$bundle_id" >/dev/null
    sleep 5
    xcrun simctl terminate "$watch" "$watch_id" 2>/dev/null || true
    SIMCTL_CHILD_SHAFT_WATCH_OPEN=1 xcrun simctl launch "$watch" "$watch_id" >/dev/null
    sleep 10
    xcrun simctl io "$watch" screenshot "$out/01-talk.png" >/dev/null
    # simctl's capture keeps an alpha channel: flatten it (JPEG has none) and back to PNG.
    sips -s format jpeg -s formatOptions best "$out/01-talk.png" --out "$out/01-talk.jpg" >/dev/null
    sips -s format png "$out/01-talk.jpg" --out "$out/01-talk.png" >/dev/null
    rm "$out/01-talk.jpg"
  else
    echo "==> $class"
    udid=$(device "$class")
    prepare "$udid"
    TEST_RUNNER_SHAFT_SHOTS="$out" xcodebuild -project "$app_dir/SHAFT.xcodeproj" -scheme SHAFT-Screenshots \
      -destination "id=$udid" -derivedDataPath "$dd" test-without-building -quiet
  fi
  rm -f "$out"/*.txt      # the test's accessibility dumps, written only when a step fails
  check "$out"
done
