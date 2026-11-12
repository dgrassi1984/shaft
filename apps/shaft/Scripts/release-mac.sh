#!/bin/bash
# Builds the Mac app as a Developer ID signed, notarized, stapled DMG: build/release/SHAFT-<version>.dmg.
# It never uploads anything. apps/shaft/README.md ("Release a signed build") has the one-time setup.
#
#   Scripts/release-mac.sh --notary-profile NAME [--team TEAMID] [--out DIR] [--allow-untagged] [--dry-run]
#   Scripts/release-mac.sh --self-test      (checks the entitlement helpers on synthetic plists; signs nothing)
#
# --dry-run checks every precondition and prints each command it would run; it signs, uploads and contacts Apple
# for nothing, and exits 1 if a precondition fails.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
app_dir=$(cd "$here/.." && pwd)
scheme=SHAFT-macOS

team=""
notary_profile=""
out="$app_dir/build/release"
allow_untagged=0
dry=0
self_test_requested=0

usage() {
  sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --team) team=${2:?--team needs a value}; shift 2 ;;
    --notary-profile) notary_profile=${2:?--notary-profile needs a value}; shift 2 ;;
    --out) out=${2:?--out needs a value}; shift 2 ;;
    --allow-untagged) allow_untagged=1; shift ;;
    --dry-run) dry=1; shift ;;
    --self-test) self_test_requested=1; shift ;;
    -h|--help) usage ;;
    *) echo "error: unknown argument: $1" >&2; usage ;;
  esac
done

problems=0
problem() {
  echo "PRECONDITION FAILED: $*" >&2
  problems=$((problems + 1))
}
die() {
  echo "error: $*" >&2
  exit 1
}
step() { echo; echo "==> $*"; }

# Runs a command, or only prints it under --dry-run.
run() {
  if [ "$dry" -eq 1 ]; then
    printf '[dry-run] would run:'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

# Signing.local.yml (git-ignored) holds the team and the bundle prefix.
local_setting() {
  sed -n "s/^[[:space:]]*$1:[[:space:]]*//p" "$app_dir/Signing.local.yml" 2>/dev/null | head -n 1 | tr -d '"' | tr -d "'"
}

# ---------------------------------------------------------------- entitlement helpers
# plist_value FILE KEYPATH -> prints the value; exit 0 found, 3 key absent, 2 FILE is not a valid plist (message on
# stderr). Dots inside one key (com.apple.security.app-sandbox) must be escaped in KEYPATH: com\.apple\.…
plist_value() {
  local lint
  lint=$(plutil -lint "$1" 2>&1) || { echo "not a valid plist ($1): $lint" >&2; return 2; }
  plutil -extract "$2" raw -o - "$1" 2>/dev/null || return 3
}

# ent_of BUNDLE KEY -> the signed entitlement's value, "absent" if the key is missing; dies on any other failure.
ent_of() {
  local tmp rc=0 val
  tmp=$(mktemp)
  codesign -d --entitlements :- "$1" >"$tmp" 2>/dev/null || { rm -f "$tmp"; die "codesign cannot read the entitlements of $1."; }
  val=$(plist_value "$tmp" "${2//./\\.}") || rc=$?
  rm -f "$tmp"
  case $rc in 0) echo "$val" ;; 3) echo absent ;; *) die "cannot read entitlement $2 of $1 (see message above)." ;; esac
}

# profile_aps BUNDLE -> the embedded profile's aps-environment, "absent", or dies when the profile cannot be decoded.
profile_aps() {
  local tmp rc=0 val
  tmp=$(mktemp)
  security cms -D -i "$1/Contents/embedded.provisionprofile" >"$tmp" 2>/dev/null || { rm -f "$tmp"; die "cannot decode the provisioning profile of $1."; }
  val=$(plist_value "$tmp" 'Entitlements.com\.apple\.developer\.aps-environment') || rc=$?
  rm -f "$tmp"
  case $rc in 0) echo "$val" ;; 3) echo absent ;; *) die "cannot read the provisioning profile of $1 (see message above)." ;; esac
}

self_test() {
  local t fails=0 d
  t=$(mktemp -d)
  expect() { # name, expected, actual
    if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1: expected '$2', got '$3'"; fails=$((fails + 1)); fi
  }
  cat >"$t/app.plist" <<'P'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>com.apple.developer.aps-environment</key><string>production</string>
<key>com.apple.security.device.audio-input</key><true/>
</dict></plist>
P
  cat >"$t/ext.plist" <<'P'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>com.apple.security.app-sandbox</key><true/>
</dict></plist>
P
  cat >"$t/profile.plist" <<'P'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Entitlements</key><dict>
<key>com.apple.developer.aps-environment</key><string>production</string>
</dict></dict></plist>
P
  echo 'this is not a plist' >"$t/bad.plist"
  expect "app without sandbox: key absent (status 3)" "3" "$(plist_value "$t/app.plist" 'com\.apple\.security\.app-sandbox' >/dev/null 2>&1; echo $?)"
  expect "app: aps-environment read" "production" "$(plist_value "$t/app.plist" 'com\.apple\.developer\.aps-environment')"
  expect "extension with sandbox: true" "true" "$(plist_value "$t/ext.plist" 'com\.apple\.security\.app-sandbox')"
  expect "unescaped dots are not the key (the old bug)" "3" "$(plist_value "$t/ext.plist" 'com.apple.security.app-sandbox' >/dev/null 2>&1; echo $?)"
  expect "the escaping used by ent_of" 'com\.apple\.security\.app-sandbox' "$(k=com.apple.security.app-sandbox; echo "${k//./\\.}")"
  expect "profile: nested Entitlements key" "production" "$(plist_value "$t/profile.plist" 'Entitlements.com\.apple\.developer\.aps-environment')"
  expect "malformed plist: status 2, not 'absent'" "2" "$(plist_value "$t/bad.plist" 'com\.apple\.security\.app-sandbox' >/dev/null 2>&1; echo $?)"
  m=$(plist_value "$t/bad.plist" x 2>&1 || true)
  if [[ $m == *"not a valid plist"* ]]; then ok=yes; else ok=no; fi
  expect "malformed plist: message says so" "yes" "$ok"
  d=$t; rm -rf "$d"
  if [ "$fails" -ne 0 ]; then echo "$fails self-test(s) failed." >&2; exit 1; fi
  echo "self-test passed."
  exit 0
}
[ "$self_test_requested" -eq 1 ] && self_test

# ---------------------------------------------------------------- preconditions
step "Preconditions"

[ -f "$app_dir/Signing.local.yml" ] || problem "apps/shaft/Signing.local.yml is missing (README.md, 'Sign and install on your own devices')."
[ -n "$team" ] || team=$(local_setting DEVELOPMENT_TEAM)
[ -n "$team" ] || problem "No team: pass --team TEAMID or set DEVELOPMENT_TEAM in Signing.local.yml."
prefix=$(local_setting SHAFT_BUNDLE_PREFIX)
[ -n "$prefix" ] || problem "SHAFT_BUNDLE_PREFIX is not set in Signing.local.yml."
[ -n "$notary_profile" ] || problem "--notary-profile NAME is required (the 'xcrun notarytool store-credentials' profile; README.md)."

dirty=$(git -C "$app_dir" diff --name-only HEAD -- 2>/dev/null || true)
if [ -n "$dirty" ]; then
  problem "Tracked files have uncommitted changes (a release is built from a clean tree):
$dirty"
fi

tag=$(git -C "$app_dir" describe --exact-match --tags --match 'v[0-9]*' HEAD 2>/dev/null || true)
if [ -z "$tag" ] && [ "$allow_untagged" -eq 0 ]; then
  problem "HEAD is not on a v* tag. Tag the release first, or pass --allow-untagged for a trial build."
fi
echo "git: ${tag:-untagged HEAD $(git -C "$app_dir" rev-parse --short HEAD)}"

command -v xcodegen >/dev/null 2>&1 || problem "xcodegen is not installed (brew install xcodegen)."
command -v xcodebuild >/dev/null 2>&1 || problem "xcodebuild is not installed (Xcode)."

identity=""
identities=$(security find-identity -v -p codesigning 2>&1) || problem "security find-identity failed: $identities"
if [ -n "$team" ]; then
  identity=$(printf '%s\n' "$identities" | grep "Developer ID Application" | grep "($team)" | head -n 1 | awk '{print $2}' || true)
fi
if [ -z "$identity" ]; then
  problem "No valid 'Developer ID Application' identity for team ${team:-?} in the keychain. security find-identity -v -p codesigning says:
$identities
Create one in Xcode (Settings > Accounts > Manage Certificates > + > Developer ID Application)."
else
  echo "signing identity: $identity (Developer ID Application, team $team)"
fi

# Developer ID apps that use push (aps-environment) or a team keychain group need a provisioning profile.
# Automatic signing at export time creates and embeds Developer ID profiles for both bundle ids with the account
# signed in to Xcode (-allowProvisioningUpdates); the exported bundles are checked below, which fails the release if
# either profile is missing or not production push.
profile_dirs=("$HOME/Library/Developer/Xcode/UserData/Provisioning Profiles" "$HOME/Library/MobileDevice/Provisioning Profiles")
found_profiles=0
for d in "${profile_dirs[@]}"; do
  [ -d "$d" ] && found_profiles=$((found_profiles + $(find "$d" -name '*.provisionprofile' 2>/dev/null | wc -l)))
done
echo "local macOS provisioning profiles: $found_profiles (Xcode adds the Developer ID ones at export; checked after export)"

if [ "$dry" -eq 0 ] && [ -n "$notary_profile" ]; then
  nout=$(xcrun notarytool history --keychain-profile "$notary_profile" 2>&1) ||
    problem "notarytool cannot use keychain profile '$notary_profile': $nout"
fi
[ "$dry" -eq 1 ] && echo "(dry run: the notary profile '${notary_profile:-?}' is not contacted)"

if [ "$problems" -gt 0 ]; then
  echo >&2
  echo "$problems precondition(s) failed." >&2
  if [ "$dry" -eq 0 ]; then
    exit 1
  fi
  echo "(dry run: printing the plan anyway)" >&2
fi

# ---------------------------------------------------------------- plan
archive="$out/SHAFT.xcarchive"
export_dir="$out/export"
export_plist="$out/ExportOptions.plist"
app="$export_dir/SHAFT.app"
appex="$app/Contents/PlugIns/NotificationService.appex"
stage="$out/dmg-stage"
team=${team:-TEAMID}
identity=${identity:-DEVELOPER-ID-IDENTITY}
notary_profile=${notary_profile:-PROFILE}

step "Clean output"
run rm -rf "$out"
run mkdir -p "$out"

step "Generate the project"
if [ "$dry" -eq 1 ]; then
  echo "[dry-run] would run: (cd $app_dir && xcodegen generate)"
else
  (cd "$app_dir" && xcodegen generate)
fi

step "Archive (Release)"
if [ "$dry" -eq 1 ]; then
  echo "[dry-run] would run: xcodebuild -project $app_dir/SHAFT.xcodeproj -scheme $scheme -configuration Release -destination generic/platform=macOS -archivePath $archive -derivedDataPath $out/dd -allowProvisioningUpdates DEVELOPMENT_TEAM=$team SHAFT_APS_ENVIRONMENT=production archive"
else
  xcodebuild -project "$app_dir/SHAFT.xcodeproj" -scheme "$scheme" -configuration Release \
    -destination 'generic/platform=macOS' -archivePath "$archive" -derivedDataPath "$out/dd" \
    -allowProvisioningUpdates DEVELOPMENT_TEAM="$team" SHAFT_APS_ENVIRONMENT=production archive
fi

step "Export with Developer ID signing"
if [ "$dry" -eq 1 ]; then
  echo "[dry-run] would write $export_plist: method developer-id, teamID $team, signingStyle automatic, signingCertificate 'Developer ID Application'"
else
  cat >"$export_plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>method</key><string>developer-id</string>
  <key>teamID</key><string>$team</string>
  <key>signingStyle</key><string>automatic</string>
  <key>signingCertificate</key><string>Developer ID Application</string>
</dict>
</plist>
PLIST
fi
run xcodebuild -exportArchive -archivePath "$archive" -exportOptionsPlist "$export_plist" \
  -exportPath "$export_dir" -allowProvisioningUpdates

step "Verify the signed app"
if [ "$dry" -eq 1 ]; then
  cat <<EOT
[dry-run] would run: codesign --verify --deep --strict --verbose=2 $app
[dry-run] would require: codesign -d --entitlements :- $app  has NO com.apple.security.app-sandbox
[dry-run] would require: codesign -d --entitlements :- $appex  has com.apple.security.app-sandbox = true
[dry-run] would require: app signed entitlement com.apple.developer.aps-environment = production
[dry-run] would require: embedded.provisionprofile in the app and the extension, app profile with aps-environment production
[dry-run] would require: codesign -dv shows 'Authority=Developer ID Application' and flags runtime (hardened) on both
EOT
else
  codesign --verify --deep --strict --verbose=2 "$app"
  sb=$(ent_of "$app" com.apple.security.app-sandbox)
  [ "$sb" != "true" ] || die "The app is sandboxed; it must not be (project.yml SHAFT-macOS entitlements)."
  sbx=$(ent_of "$appex" com.apple.security.app-sandbox)
  [ "$sbx" = "true" ] || die "The notification extension is not sandboxed (app-sandbox is '$sbx'); macOS extensions must be."
  aps=$(ent_of "$app" com.apple.developer.aps-environment)
  [ "$aps" = "production" ] || die "The signed app's aps-environment is '$aps', not production."
  for b in "$app" "$appex"; do
    [ -f "$b/Contents/embedded.provisionprofile" ] ||
      die "$b has no embedded provisioning profile. A Developer ID app using push or a keychain group needs one; see README.md 'Release a signed build'."
    info=$(codesign -dv "$b" 2>&1)
    printf '%s\n' "$info" | grep -q 'Authority=Developer ID Application' || die "$b is not signed with Developer ID Application."
    printf '%s\n' "$info" | grep -q 'flags=.*runtime' || die "$b lacks the hardened runtime."
  done
  [ "$(profile_aps "$app")" = "production" ] || die "The app's provisioning profile does not grant aps-environment production (is Push Notifications on for its App ID?)."
  echo "app: not sandboxed, hardened, aps-environment production; extension: sandboxed."
fi

version="X.Y.Z"
if [ "$dry" -eq 0 ]; then
  version=$(plutil -extract CFBundleShortVersionString raw -o - "$app/Contents/Info.plist")
fi
dmg="$out/SHAFT-$version.dmg"

step "Create the DMG"
run mkdir -p "$stage"
run ditto "$app" "$stage/SHAFT.app"
run ln -s /Applications "$stage/Applications"
run hdiutil create -volname SHAFT -srcfolder "$stage" -ov -format UDZO "$dmg"

step "Sign the DMG"
run codesign --sign "$identity" --timestamp "$dmg"
run codesign --verify --verbose=2 "$dmg"

step "Notarize"
if [ "$dry" -eq 1 ]; then
  echo "[dry-run] would run: xcrun notarytool submit $dmg --keychain-profile $notary_profile --wait --output-format json"
  echo "[dry-run] on failure: xcrun notarytool log <submission id> --keychain-profile $notary_profile"
else
  result="$out/notarytool-submit.json"
  if ! xcrun notarytool submit "$dmg" --keychain-profile "$notary_profile" --wait --output-format json >"$result"; then
    cat "$result" >&2 || true
    sub=$(plutil -extract id raw -o - "$result" 2>/dev/null || true)
    [ -z "$sub" ] || xcrun notarytool log "$sub" --keychain-profile "$notary_profile" >&2 || true
    die "notarytool submit failed (output above)."
  fi
  sub=$(plutil -extract id raw -o - "$result")
  status=$(plutil -extract status raw -o - "$result")
  echo "notarization submission id: $sub, status: $status"
  if [ "$status" != "Accepted" ]; then
    xcrun notarytool log "$sub" --keychain-profile "$notary_profile" >&2 || true
    die "Notarization status is '$status' (log above)."
  fi
fi

step "Staple and assess"
run xcrun stapler staple "$dmg"
run xcrun stapler validate "$dmg"
if [ "$dry" -eq 1 ]; then
  echo "[dry-run] would run: spctl -a -vv -t install $dmg   (must say 'accepted')"
else
  assess=$(spctl -a -vv -t install "$dmg" 2>&1) || die "spctl rejected the DMG: $assess"
  printf '%s\n' "$assess"
  printf '%s\n' "$assess" | grep -q 'accepted' || die "spctl did not say accepted: $assess"
fi

step "Result"
if [ "$dry" -eq 1 ]; then
  echo "[dry-run] would print the path and SHA-256 of $dmg"
  [ "$problems" -eq 0 ] || exit 1
else
  echo "$dmg"
  shasum -a 256 "$dmg"
  echo "Nothing was uploaded. Attach the DMG to the GitHub release yourself."
fi
