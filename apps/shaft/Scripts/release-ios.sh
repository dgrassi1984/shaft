#!/bin/bash
# Archives and exports the iPhone/iPad app, its extensions and Watch app for App Store Connect.
#   Scripts/release-ios.sh [--team TEAMID] [--out DIR] [--upload] [--manual-signing] [--allow-untagged] [--dry-run]
#   Scripts/release-ios.sh [--api-key-path PATH --api-key-id ID --api-issuer ID]
#   Scripts/release-ios.sh --self-test (synthetic plists only; signs and uploads nothing)
# --dry-run checks preconditions and prints the plan without signing or contacting Apple.
# --manual-signing exports with installed manual App Store profiles for every archived app and extension.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
app_dir=$(cd "$here/.." && pwd)
team=""
out="$app_dir/build/release-ios"
allow_untagged=0
dry=0
upload=0
signing_style=automatic
self_test_requested=0
api_key_path=""
api_key_id=""
api_issuer=""
usage() { sed -n '2,7p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --team) team=${2:?--team needs a value}; shift 2 ;;
    --out) out=${2:?--out needs a value}; shift 2 ;;
    --upload) upload=1; shift ;;
    --manual-signing) signing_style=manual; shift ;;
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

# The entitlement check is separate so self-test needs no signing identity.
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
  for bundle in "$app" "$app/PlugIns/ShareExtension.appex" "$app/PlugIns/NotificationService.appex" \
    "$app/Watch/SHAFT.app" "$app/PlugIns/MeetingWidgets.appex"; do
    [ -d "$bundle" ] || die "Missing bundle: $bundle"
    # MeetingWidgets does not ship a privacy manifest.
    if [ "$bundle" != "$app/PlugIns/MeetingWidgets.appex" ]; then
      [ -f "$bundle/PrivacyInfo.xcprivacy" ] || die "Missing privacy manifest: $bundle"
      plutil -lint "$bundle/PrivacyInfo.xcprivacy" >/dev/null || die "Invalid privacy manifest: $bundle"
    fi
    [ "$(plist_value "$bundle/Info.plist" CFBundleShortVersionString)" = "$version" ] || die "Version mismatch: $bundle"
    [ "$(plist_value "$bundle/Info.plist" CFBundleVersion)" = "$build" ] || die "Build mismatch: $bundle"
    rc=0
    plist_value "$bundle/Info.plist" NSAppTransportSecurity.NSAllowsLocalNetworking >/dev/null || rc=$?
    [ "$rc" -eq 3 ] || die "Release must have no NSAllowsLocalNetworking: $bundle (reader status $rc)."
  done
}

pick_profiles() {
  python3 - "$1" "$2" "$3" <<'PYCODE'
from datetime import datetime, timezone
from pathlib import Path
import plistlib
import sys

app, team, decoded = sys.argv[1:]

def archived_bundle_ids(bundle):
    with (bundle / 'Info.plist').open('rb') as stream:
        bundle_id = plistlib.load(stream).get('CFBundleIdentifier')
    if not isinstance(bundle_id, str) or not bundle_id:
        raise ValueError(f'Missing or invalid CFBundleIdentifier in {bundle / "Info.plist"}')
    yield bundle_id
    for pattern in ('PlugIns/*.appex', 'Watch/*.app'):
        for child in sorted(bundle.glob(pattern)):
            if child.is_dir():
                yield from archived_bundle_ids(child)

try:
    bundle_ids = sorted(set(archived_bundle_ids(Path(app))))
except (OSError, ValueError, plistlib.InvalidFileException) as error:
    sys.exit(f'Cannot read archived bundles: {error}')

now = datetime.now(timezone.utc).replace(tzinfo=None)
best = {}
for path in sorted(Path(decoded).glob('*.plist')):
    with path.open('rb') as stream:
        profile = plistlib.load(stream)
    entitlements = profile.get('Entitlements', {})
    identifier = entitlements.get('application-identifier')
    expiry = profile.get('ExpirationDate')
    name = profile.get('Name')
    if ('ProvisionedDevices' in profile
            or profile.get('ProvisionsAllDevices', False) is not False
            or entitlements.get('get-task-allow', False) is not False
            or profile.get('IsXcodeManaged', False) is not False
            or not isinstance(identifier, str)
            or not isinstance(expiry, datetime) or expiry <= now
            or not isinstance(name, str) or not name):
        continue
    if identifier not in best or expiry > best[identifier][0]:
        best[identifier] = (expiry, name)

selected = {}
missing = []
for bundle_id in bundle_ids:
    match = best.get(f'{team}.{bundle_id}')
    if match:
        selected[bundle_id] = match[1]
    else:
        missing.append(bundle_id)
if missing:
    print('Missing manual App Store provisioning profiles for these bundle IDs:', file=sys.stderr)
    print(*missing, sep='\n', file=sys.stderr)
    sys.exit('Register each App ID and create a manually managed App Store profile with the Apple Distribution '
             'certificate in the Apple Developer portal or via the App Store Connect API. '
             'Install the profiles and rerun.')
plistlib.dump(selected, sys.stdout.buffer)
PYCODE
}

installed_profiles() (
  local decoded profile index=0
  decoded=$(mktemp -d) || die 'Cannot create a directory for decoded provisioning profiles.'
  trap 'rm -rf "$decoded"' EXIT
  shopt -s nullglob
  for profile in "$HOME/Library/Developer/Xcode/UserData/Provisioning Profiles/"*.mobileprovision; do
    index=$((index + 1))
    if ! security cms -D -i "$profile" >"$decoded/$index.plist"; then
      echo "Could not decode provisioning profile: $profile" >&2
      rm "$decoded/$index.plist"
    fi
  done
  pick_profiles "$1" "$team" "$decoded"
)

self_test() {
  local t app bundle key fails=0
  local manifests
  t=$(mktemp -d)
  app="$t/Payload/SHAFT.app"
  manifests=("$app" "$app/PlugIns/ShareExtension.appex" "$app/PlugIns/NotificationService.appex" "$app/Watch/SHAFT.app")
  for bundle in "${manifests[@]}" "$app/PlugIns/MeetingWidgets.appex"; do
    mkdir -p "$bundle"
    cat >"$bundle/Info.plist" <<'PLIST'
<?xml version="1.0"?><plist version="1.0"><dict>
<key>CFBundleShortVersionString</key><string>1.2.3</string>
<key>CFBundleVersion</key><string>123</string>
<key>ITSAppUsesNonExemptEncryption</key><false/>
</dict></plist>
PLIST
  done
  for bundle in "${manifests[@]}"; do
    cp "$bundle/Info.plist" "$bundle/PrivacyInfo.xcprivacy"
  done
  expect_status() {
    local name=$1 expected=$2 rc=0
    shift 2
    ( "$@" ) >"$t/check.log" 2>&1 || rc=$?
    if [ "$rc" -eq "$expected" ]; then echo "ok   $name";
    else echo "FAIL $name: expected status $expected, got $rc"; cat "$t/check.log"; fails=$((fails + 1)); fi
  }
  expect_status 'valid five bundles with four privacy manifests' 0 check_bundles "$app"
  for bundle in "${manifests[@]}"; do
    mv "$bundle/PrivacyInfo.xcprivacy" "$t/manifest"
    expect_status "missing manifest: ${bundle#"$t/"}" 1 check_bundles "$app"
    mv "$t/manifest" "$bundle/PrivacyInfo.xcprivacy"
  done
  bundle="$app/Watch/SHAFT.app"
  mv "$bundle" "$t/watch"
  expect_status 'missing watch bundle' 1 check_bundles "$app"
  mv "$t/watch" "$bundle"
  mv "$app/PlugIns/MeetingWidgets.appex" "$t/widgets"
  expect_status 'missing MeetingWidgets bundle' 1 check_bundles "$app"
  mv "$t/widgets" "$app/PlugIns/MeetingWidgets.appex"
  for key in CFBundleShortVersionString CFBundleVersion; do
    /usr/libexec/PlistBuddy -c "Set :$key 999" "$bundle/Info.plist"
    expect_status "mismatched $key" 1 check_bundles "$app"
    cp "$app/Info.plist" "$bundle/Info.plist"
  done
  for bundle in "${manifests[@]}" "$app/PlugIns/MeetingWidgets.appex"; do
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
  mkdir "$t/profiles"
  python3 - "$t" <<'PYCODE'
from datetime import datetime
from pathlib import Path
import plistlib
import sys

root = Path(sys.argv[1])
archive_app = root / 'archive' / 'SHAFT.app'
bundles = {
    archive_app: 'example.shaft',
    archive_app / 'PlugIns/MeetingWidgets.appex': 'example.shaft.widgets',
    archive_app / 'Watch/SHAFT.app': 'example.shaft.watch',
    archive_app / 'Watch/SHAFT.app/PlugIns/WatchExtension.appex': 'example.shaft.watch.extension',
}
for bundle, bundle_id in bundles.items():
    bundle.mkdir(parents=True)
    (bundle / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': bundle_id}))

def profile(name, year, identifier='TEAM.example.shaft', entitlements=None, **flags):
    data = dict(Entitlements={'application-identifier': identifier, **(entitlements or {})},
                Name=name, ExpirationDate=datetime(year, 1, 1), **flags)
    (root / 'profiles' / f'{name}.plist').write_bytes(plistlib.dumps(data))

profile('Earlier', 2098)
profile('Latest', 2099, entitlements={'get-task-allow': False}, IsXcodeManaged=False)
profile('Older', 2097)
profile('Expired', 2000, 'TEAM.example.shaft.widgets')
profile('Devices', 2100, ProvisionedDevices=['example-device'])
profile('Debuggable', 2100, entitlements={'get-task-allow': True})
profile('Enterprise', 2100, ProvisionsAllDevices=True)
profile('Xcode managed', 2100, IsXcodeManaged=True)
profile('Wildcard', 2100, 'TEAM.example.shaft.*')
profile('Other team', 2100, 'OTHER.example.shaft')
profile('Widgets', 2099, 'TEAM.example.shaft.widgets')
profile('Watch', 2099, 'TEAM.example.shaft.watch')
profile('Watch extension', 2099, 'TEAM.example.shaft.watch.extension')
(root / 'expected.plist').write_bytes(plistlib.dumps(dict(zip(
    bundles.values(), ('Latest', 'Widgets', 'Watch', 'Watch extension')
))))
PYCODE
  expect_status 'pick manual App Store profiles for archived bundles' 0 pick_profiles "$t/archive/SHAFT.app" TEAM "$t/profiles"
  if cmp -s "$t/expected.plist" "$t/check.log"; then
    echo 'ok   selected profile names and latest expiry'
  else
    echo 'FAIL selected profile names and latest expiry'
    cat "$t/check.log"
    fails=$((fails + 1))
  fi
  rm "$t/profiles/Widgets.plist" "$t/profiles/Watch extension.plist"
  expect_status 'fail when manual profiles are missing' 1 pick_profiles "$t/archive/SHAFT.app" TEAM "$t/profiles"
  if grep -Fxq 'example.shaft.widgets' "$t/check.log" &&
    grep -Fxq 'example.shaft.watch.extension' "$t/check.log"; then
    echo 'ok   list every missing bundle ID'
  else
    echo 'FAIL list every missing bundle ID'
    cat "$t/check.log"
    fails=$((fails + 1))
  fi
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
if [ "$signing_style" = manual ]; then
  command -v security >/dev/null 2>&1 || problem 'security is not installed.'
fi
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

profiles=""
if [ "$signing_style" = manual ]; then
  archive_app="$archive/Products/Applications/SHAFT.app"
  if [ "$dry" -eq 1 ] && [ ! -d "$archive_app" ]; then
    echo "[dry-run] would select installed manual App Store profiles for each bundle in $archive_app after archiving."
  else
    profiles=$(installed_profiles "$archive_app") || die 'Could not select manual provisioning profiles.'
  fi
fi
write_export_options() {
  if [ "$dry" -eq 1 ]; then
    echo "[dry-run] would write $export_plist: method app-store-connect, destination $1, signingStyle $signing_style, teamID $team, manageAppVersionAndBuildNumber false"
    [ "$signing_style" = manual ] || return 0
    echo '[dry-run] signingCertificate Apple Distribution'
    [ -n "$profiles" ] || return 0
  fi
  python3 - "$export_plist" "$team" "$1" "$signing_style" "$profiles" "$dry" <<'PYCODE'
import plistlib
import sys
path, team, destination, signing_style, profiles, dry = sys.argv[1:]
options = dict(method='app-store-connect', destination=destination, teamID=team,
               signingStyle=signing_style, manageAppVersionAndBuildNumber=False)
if signing_style == 'manual':
    options.update(signingCertificate='Apple Distribution',
                   provisioningProfiles=plistlib.loads(profiles.encode('utf-8')))
if dry == '1':
    for bundle_id, name in options['provisioningProfiles'].items():
        print(f'[dry-run] provisioningProfiles {bundle_id} -> {name}')
else:
    with open(path, 'wb') as stream:
        plistlib.dump(options, stream)
PYCODE
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
  echo '[dry-run] would unzip the exported IPA and require the app, three extensions and Watch app, matching versions, exempt encryption and no NSAllowsLocalNetworking.'
  echo '[dry-run] would require privacy manifests for the app, ShareExtension, NotificationService and Watch app.'
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
  echo 'Verified five bundles, four privacy manifests, versions, exempt encryption, Release networking and production push.'
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
