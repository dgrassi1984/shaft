#!/bin/bash
# Builds the Mac app as a Developer ID signed, notarized, stapled DMG: build/release/SHAFT-<version>.dmg.
# It never uploads anything. apps/shaft/README.md ("Release a signed build") has the one-time setup.
#
#   Scripts/release-mac.sh --notary-profile NAME [--team TEAMID] [--out DIR] [--allow-untagged] [--dry-run]
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

usage() {
  sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --team) team=${2:?--team needs a value}; shift 2 ;;
    --notary-profile) notary_profile=${2:?--notary-profile needs a value}; shift 2 ;;
    --out) out=${2:?--out needs a value}; shift 2 ;;
    --allow-untagged) allow_untagged=1; shift ;;
    --dry-run) dry=1; shift ;;
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
  echo "[dry-run] would run: xcodebuild -project $app_dir/SHAFT.xcodeproj -scheme $scheme -configuration Release -destination generic/platform=macOS -archivePath $archive -derivedDataPath $out/dd -allowProvisioningUpdates DEVELOPMENT_TEAM=$team archive"
else
  xcodebuild -project "$app_dir/SHAFT.xcodeproj" -scheme "$scheme" -configuration Release \
    -destination 'generic/platform=macOS' -archivePath "$archive" -derivedDataPath "$out/dd" \
    -allowProvisioningUpdates DEVELOPMENT_TEAM="$team" archive
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
sandbox_of() { # path -> "true" or "absent"
  codesign -d --entitlements :- "$1" 2>/dev/null | plutil -extract com.apple.security.app-sandbox raw -o - - 2>/dev/null || echo absent
}
aps_of() { # path -> profile's aps-environment or "absent"
  [ -f "$1/Contents/embedded.provisionprofile" ] || { echo "no-profile"; return; }
  security cms -D -i "$1/Contents/embedded.provisionprofile" 2>/dev/null |
    plutil -extract Entitlements.com.apple.developer.aps-environment raw -o - - 2>/dev/null || echo absent
}
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
  [ "$(sandbox_of "$app")" != "true" ] || die "The app is sandboxed; it must not be (project.yml SHAFT-macOS entitlements)."
  [ "$(sandbox_of "$appex")" = "true" ] || die "The notification extension is not sandboxed; macOS extensions must be."
  aps=$(codesign -d --entitlements :- "$app" 2>/dev/null | plutil -extract com.apple.developer.aps-environment raw -o - - 2>/dev/null || echo absent)
  [ "$aps" = "production" ] || die "The signed app's aps-environment is '$aps', not production."
  for b in "$app" "$appex"; do
    [ -f "$b/Contents/embedded.provisionprofile" ] ||
      die "$b has no embedded provisioning profile. A Developer ID app using push or a keychain group needs one; see README.md 'Release a signed build'."
    info=$(codesign -dv "$b" 2>&1)
    printf '%s\n' "$info" | grep -q 'Authority=Developer ID Application' || die "$b is not signed with Developer ID Application."
    printf '%s\n' "$info" | grep -q 'flags=.*runtime' || die "$b lacks the hardened runtime."
  done
  [ "$(aps_of "$app")" = "production" ] || die "The app's provisioning profile does not grant aps-environment production (is Push Notifications on for its App ID?)."
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
