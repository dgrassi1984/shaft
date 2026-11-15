#!/bin/bash
# Archives and exports the iPhone/iPad app, its extensions and Watch app for App Store Connect.
#   Scripts/release-ios.sh [--team TEAMID] [--out DIR] [--upload] [--allow-untagged] [--dry-run]
#   Scripts/release-ios.sh [--api-key-path PATH --api-key-id ID --api-issuer ID]
#   Scripts/release-ios.sh --self-test (synthetic plists only; signs and uploads nothing)
# --dry-run checks preconditions and prints the plan without signing or contacting Apple.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
app_dir=$(cd "$here/.." && pwd)
team=""
out="$app_dir/build/release-ios"
allow_untagged=0
dry=0
upload=0
self_test_requested=0
api_key_path=""
api_key_id=""
api_issuer=""
usage() { sed -n '2,6p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --team) team=${2:?--team needs a value}; shift 2 ;;
    --out) out=${2:?--out needs a value}; shift 2 ;;
    --upload) upload=1; shift ;;
    --allow-untagged) allow_untagged=1; shift ;;
    --dry-run) dry=1; shift ;;
    --self-test) self_test_requested=1; shift ;;
    --api-key-path) api_key_path=${2:?--api-key-path needs a value}; shift 2 ;;
    --api-key-id) api_key_id=${2:?--api-key-id needs a value}; shift 2 ;;
    --api-issuer) api_issuer=${2:?--api-issuer needs a value}; shift 2 ;;
    -h|--help) usage ;;
    *) echo "error: unknown argument: $1" >&2; usage ;;
  esac
done
problems=0
problem() { echo "PRECONDITION FAILED: $*" >&2; problems=$((problems + 1)); }
die() { echo "error: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }
run() {
  if [ "$dry" -eq 1 ]; then
    printf '[dry-run] would run:'; printf ' %q' "$@"; printf '\n'
  else
    "$@"
  fi
}
local_setting() {
  sed -n "s/^[[:space:]]*$1:[[:space:]]*//p" "$app_dir/Signing.local.yml" 2>/dev/null | head -n 1 | tr -d '"' | tr -d "'"
}

# Scalar plist reader uses the same failure statuses as release-mac.sh.
plist_value() {
  local lint kind out
  lint=$(plutil -lint "$1" 2>&1) || { echo "not a valid plist ($1): $lint" >&2; return 2; }
  kind=$(plutil -type "$2" "$1" 2>&1) || {
    case $kind in
      *"No value at that key path"*) return 3 ;;
      *) echo "plutil could not read $2 in $1: $kind" >&2; return 4 ;;
    esac
  }
  case $kind in
    string|bool|integer|real) ;;
    *) echo "$2 in $1 holds a $kind, not a scalar" >&2; return 4 ;;
  esac
  out=$(plutil -extract "$2" raw -o - "$1" 2>&1) || { echo "plutil could not extract $2 from $1: $out" >&2; return 4; }
  printf '%s\n' "$out"
}

# Check the four exported bundles; the entitlement check is separate so self-test needs no signing identity.
check_production() {
  [ "$(plist_value "$1" aps-environment)" = production ] || die "Signed app aps-environment is not production."
}
check_bundles() {
  local app=$1 bundle version build rc
  version=$(plist_value "$app/Info.plist" CFBundleShortVersionString) || die "Cannot read app version."
  build=$(plist_value "$app/Info.plist" CFBundleVersion) || die "Cannot read app build."
  [ -n "$version" ] && [ -n "$build" ] || die "App version or build is empty."
  [ "$(plutil -type ITSAppUsesNonExemptEncryption "$app/Info.plist")" = bool ] &&
    [ "$(plist_value "$app/Info.plist" ITSAppUsesNonExemptEncryption)" = false ] ||
    die "App must declare ITSAppUsesNonExemptEncryption = false (boolean)."
  for bundle in "$app" "$app/PlugIns/ShareExtension.appex" "$app/PlugIns/NotificationService.appex" "$app/Watch/SHAFT.app"; do
    [ -d "$bundle" ] || die "Missing bundle: $bundle"
    [ -f "$bundle/PrivacyInfo.xcprivacy" ] || die "Missing privacy manifest: $bundle"
    plutil -lint "$bundle/PrivacyInfo.xcprivacy" >/dev/null || die "Invalid privacy manifest: $bundle"
    [ "$(plist_value "$bundle/Info.plist" CFBundleShortVersionString)" = "$version" ] || die "Version mismatch: $bundle"
    [ "$(plist_value "$bundle/Info.plist" CFBundleVersion)" = "$build" ] || die "Build mismatch: $bundle"
    rc=0
    plist_value "$bundle/Info.plist" NSAppTransportSecurity.NSAllowsLocalNetworking >/dev/null || rc=$?
    [ "$rc" -eq 3 ] || die "Release must have no NSAllowsLocalNetworking: $bundle (reader status $rc)."
  done
}

self_test() {
  local t app bundle fails=0
  t=$(mktemp -d)
  app="$t/Payload/SHAFT.app"
  for bundle in "$app" "$app/PlugIns/ShareExtension.appex" "$app/PlugIns/NotificationService.appex" "$app/Watch/SHAFT.app"; do
    mkdir -p "$bundle"
    cat >"$bundle/Info.plist" <<'PLIST'
<?xml version="1.0"?><plist version="1.0"><dict>
<key>CFBundleShortVersionString</key><string>1.2.3</string>
<key>CFBundleVersion</key><string>123</string>
<key>ITSAppUsesNonExemptEncryption</key><false/>
</dict></plist>
PLIST
    cp "$bundle/Info.plist" "$bundle/PrivacyInfo.xcprivacy"
  done
  expect_status() {
    local name=$1 expected=$2 rc=0
    shift 2
    ( "$@" ) >"$t/check.log" 2>&1 || rc=$?
    if [ "$rc" -eq "$expected" ]; then echo "ok   $name";
    else echo "FAIL $name: expected status $expected, got $rc"; cat "$t/check.log"; fails=$((fails + 1)); fi
  }
  expect_status 'valid four bundles' 0 check_bundles "$app"
  for bundle in "$app" "$app/PlugIns/ShareExtension.appex" "$app/PlugIns/NotificationService.appex" "$app/Watch/SHAFT.app"; do
    mv "$bundle/PrivacyInfo.xcprivacy" "$t/manifest"
    expect_status "missing manifest: ${bundle#"$t/"}" 1 check_bundles "$app"
    mv "$t/manifest" "$bundle/PrivacyInfo.xcprivacy"
  done
  bundle="$app/Watch/SHAFT.app"
  mv "$bundle" "$t/watch"
  expect_status 'missing watch bundle' 1 check_bundles "$app"
  mv "$t/watch" "$bundle"
  for key in CFBundleShortVersionString CFBundleVersion; do
    /usr/libexec/PlistBuddy -c "Set :$key 999" "$bundle/Info.plist"
    expect_status "mismatched $key" 1 check_bundles "$app"
    cp "$app/Info.plist" "$bundle/Info.plist"
  done
  for bundle in "$app" "$app/PlugIns/ShareExtension.appex" "$app/PlugIns/NotificationService.appex" "$app/Watch/SHAFT.app"; do
    /usr/libexec/PlistBuddy -c 'Add :NSAppTransportSecurity dict' -c 'Add :NSAppTransportSecurity:NSAllowsLocalNetworking bool false' "$bundle/Info.plist"
    expect_status "local networking key even when false: ${bundle#"$t/"}" 1 check_bundles "$app"
    /usr/libexec/PlistBuddy -c 'Delete :NSAppTransportSecurity' "$bundle/Info.plist"
  done
  /usr/libexec/PlistBuddy -c 'Set :ITSAppUsesNonExemptEncryption true' "$app/Info.plist"
  expect_status 'nonexempt encryption' 1 check_bundles "$app"
  /usr/libexec/PlistBuddy -c 'Delete :ITSAppUsesNonExemptEncryption' -c 'Add :ITSAppUsesNonExemptEncryption string false' "$app/Info.plist"
  expect_status 'encryption must be boolean' 1 check_bundles "$app"
  echo 'not a plist' >"$app/Info.plist"
  expect_status 'malformed plist' 1 check_bundles "$app"
  cat >"$t/entitlements.plist" <<'PLIST'
<?xml version="1.0"?><plist version="1.0"><dict><key>aps-environment</key><string>production</string></dict></plist>
PLIST
  expect_status 'production entitlement' 0 check_production "$t/entitlements.plist"
  /usr/libexec/PlistBuddy -c 'Set :aps-environment development' "$t/entitlements.plist"
  expect_status 'development entitlement' 1 check_production "$t/entitlements.plist"
  /usr/libexec/PlistBuddy -c 'Delete :aps-environment' "$t/entitlements.plist"
  expect_status 'missing entitlement' 1 check_production "$t/entitlements.plist"
  rm -rf "$t"
  [ "$fails" -eq 0 ] || die "$fails self-test(s) failed."
  echo 'self-test passed.'
}
if [ "$self_test_requested" -eq 1 ]; then self_test; exit 0; fi

step Preconditions
[ -f "$app_dir/Signing.local.yml" ] || problem 'Signing.local.yml is missing (see README.md).'
[ -n "$team" ] || team=$(local_setting DEVELOPMENT_TEAM)
[[ $team =~ ^[A-Z0-9]+$ ]] || problem 'Set DEVELOPMENT_TEAM in Signing.local.yml or pass --team TEAMID.'
[ -n "$(local_setting SHAFT_BUNDLE_PREFIX)" ] || problem 'SHAFT_BUNDLE_PREFIX is missing from Signing.local.yml.'
if [ "$allow_untagged" -eq 0 ]; then
  dirty=$(git -C "$app_dir" status --porcelain --untracked-files=normal)
  [ -z "$dirty" ] || problem "Uncommitted changes; use a clean tree or --allow-untagged for a trial build:
$dirty"
fi
tag=$(git -C "$app_dir" describe --exact-match --tags --match 'v[0-9]*' HEAD 2>/dev/null || true)
[ -n "$tag" ] || [ "$allow_untagged" -eq 1 ] || problem 'HEAD is not on a v* tag; use --allow-untagged for a trial build.'
echo "git: ${tag:-untagged HEAD $(git -C "$app_dir" rev-parse --short HEAD)}"
for tool in xcodegen xcodebuild codesign plutil ditto python3; do
  command -v "$tool" >/dev/null 2>&1 || problem "$tool is not installed."
done
auth=()
if [ -n "$api_key_path$api_key_id$api_issuer" ]; then
  if [ -z "$api_key_path" ] || [ -z "$api_key_id" ] || [ -z "$api_issuer" ]; then
    problem 'Pass --api-key-path, --api-key-id and --api-issuer together.'
  else
    [ -f "$api_key_path" ] || problem 'API key path is not a file.'
    auth=(-authenticationKeyPath "$api_key_path" -authenticationKeyID "$api_key_id" -authenticationKeyIssuerID "$api_issuer")
  fi
fi
if [ "$problems" -gt 0 ]; then
  echo "$problems precondition(s) failed." >&2
  [ "$dry" -eq 1 ] || exit 1
  echo '(dry run: printing the plan anyway)' >&2
fi
archive="$out/SHAFT.xcarchive"
export_dir="$out/export"
export_plist="$out/ExportOptions.plist"

# Keep existing releases intact; Xcode can reuse the derived data on a retry.
run mkdir -p "$out"
step 'Generate the project'
if [ "$dry" -eq 1 ]; then
  echo "[dry-run] would run: (cd $app_dir && xcodegen generate)"
else
  (cd "$app_dir" && xcodegen generate)
fi
step 'Archive (Release, production push)'
run xcodebuild -project "$app_dir/SHAFT.xcodeproj" -scheme SHAFT-iOS -configuration Release \
  -destination 'generic/platform=iOS' -archivePath "$archive" -derivedDataPath "$out/dd" \
  -allowProvisioningUpdates ${auth[@]+"${auth[@]}"} DEVELOPMENT_TEAM="$team" CODE_SIGN_STYLE=Automatic SHAFT_APS_ENVIRONMENT=production archive

write_export_options() {
  if [ "$dry" -eq 1 ]; then
    echo "[dry-run] would write $export_plist: method app-store-connect, destination $1, signingStyle automatic, teamID $team, manageAppVersionAndBuildNumber false"
  else
    python3 - "$export_plist" "$team" "$1" <<'PYCODE'
import plistlib
import sys
with open(sys.argv[1], 'wb') as stream:
    plistlib.dump(dict(method='app-store-connect', destination=sys.argv[3], teamID=sys.argv[2],
                       signingStyle='automatic', manageAppVersionAndBuildNumber=False), stream)
PYCODE
  fi
}
# $1: where xcodebuild writes (the local IPA, or the upload's logs: never the IPA just verified).
export_archive() {
  run xcodebuild -exportArchive -archivePath "$archive" -exportOptionsPlist "$export_plist" \
    -exportPath "$1" -allowProvisioningUpdates ${auth[@]+"${auth[@]}"}
}
step 'Export a local IPA for verification'
write_export_options export
# Refuse stale IPAs so the verification always checks this export.
if [ "$dry" -eq 0 ]; then
  [ ! -d "$export_dir" ] || die "Export directory already exists: $export_dir (choose a fresh --out)."
fi
export_archive "$export_dir"
step 'Verify the exported IPA'
if [ "$dry" -eq 1 ]; then
  echo '[dry-run] would unzip the exported IPA and require app, both extensions and Watch app, four privacy manifests, matching versions, exempt encryption and no NSAllowsLocalNetworking.'
  echo '[dry-run] would verify the app signature and require signed aps-environment = production.'
  ipa="$export_dir/SHAFT.ipa"
else
  shopt -s nullglob
  ipas=("$export_dir/"*.ipa)
  [ "${#ipas[@]}" -eq 1 ] || die "Expected exactly one IPA in $export_dir."
  ipa=${ipas[0]}
  unpack=$(mktemp -d)
  trap 'rm -rf "$unpack"' EXIT
  ditto -x -k "$ipa" "$unpack"
  app="$unpack/Payload/SHAFT.app"
  check_bundles "$app"
  codesign --verify --deep --strict --verbose=2 "$app"
  codesign -d --entitlements :- "$app" >"$unpack/entitlements.plist"
  check_production "$unpack/entitlements.plist"
  echo 'Verified four bundles: manifests, versions, exempt encryption, Release networking and production push.'
fi
if [ "$upload" -eq 1 ]; then
  step 'Upload the verified archive to App Store Connect'
  write_export_options upload
  export_archive "$out/upload"
fi
step Result
if [ "$dry" -eq 1 ]; then
  echo "[dry-run] would print: $ipa"
  [ "$problems" -eq 0 ] || exit 1
else
  echo "$ipa"
fi
