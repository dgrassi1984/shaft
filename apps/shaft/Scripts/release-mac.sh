#!/bin/bash
# Builds the Mac app as a Developer ID signed, notarized, stapled DMG: build/release/SHAFT-<version>.dmg.
# It never uploads anything. apps/shaft/README.md ("Release a signed build") has the one-time setup.
#
#   Scripts/release-mac.sh (--api-key-path PATH --api-key-id ID --api-issuer ID | --notary-profile NAME)
#     [--team TEAMID] [--out DIR] [--allow-untagged] [--dry-run] [--feed-url URL] [--download-base URL]
# --feed-url defaults to https://dgrassi1984.github.io/shaft-app/appcast.xml.
# --download-base defaults to https://dgrassi1984.github.io/shaft-app/ when tagged; required when untagged.
#   Scripts/release-mac.sh --self-test      (checks helpers on synthetic data; signs nothing)
#
# --dry-run checks local preconditions and prints the plan without signing or contacting Apple.
# It exits 1 if a precondition fails.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
app_dir=$(cd "$here/.." && pwd)
scheme=SHAFT-macOS

team=""
notary_profile=""
api_key_path=""
api_key_id=""
api_issuer=""
out="$app_dir/build/release"
allow_untagged=0
dry=0
self_test_requested=0
feed_url=https://dgrassi1984.github.io/shaft-app/appcast.xml
download_base=""

usage() {
  sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --feed-url) feed_url=${2?--feed-url needs a value}; shift 2 ;;
    --download-base) download_base=${2?--download-base needs a value}; shift 2 ;;
    --team) team=${2:?--team needs a value}; shift 2 ;;
    --notary-profile) notary_profile=${2:?--notary-profile needs a value}; shift 2 ;;
    --api-key-path) api_key_path=${2:?--api-key-path needs a value}; shift 2 ;;
    --api-key-id) api_key_id=${2:?--api-key-id needs a value}; shift 2 ;;
    --api-issuer) api_issuer=${2:?--api-issuer needs a value}; shift 2 ;;
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
  [ -f "$app_dir/Signing.local.yml" ] || return 0
  sed -n "s/^[[:space:]]*$1:[[:space:]]*//p" "$app_dir/Signing.local.yml" 2>/dev/null | head -n 1 | tr -d '"' | tr -d "'"
}

# ---------------------------------------------------------------- entitlement helpers
# plist_value FILE KEYPATH -> prints a scalar value; exit 0 found, 3 key absent (only plutil's "No value at that key
# path" means that), 2 FILE is not a valid plist, 4 any other failure, e.g. the key holds a dictionary or array, or
# plutil failed otherwise (the real message on stderr). Dots inside one key (com.apple.security.app-sandbox) must be escaped in KEYPATH: com\.apple\.…
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

# ---------------------------------------------------------------- Sparkle helpers
url_allowed() {
  local https='^https://([A-Za-z0-9-]+\.)*[A-Za-z0-9-]+(:[0-9]+)?(/[^[:space:]]*)?$'
  local http='^http://(127\.0\.0\.1|localhost)(:[0-9]+)?(/[^[:space:]]*)?$'
  [[ $1 =~ $https || $1 =~ $http ]]
}

keys_match() { [ -n "$1" ] && [ "$1" = "$2" ]; }

parse_sign_output() {
  local shape='^sparkle:edSignature="([A-Za-z0-9+/=]+)" length="([0-9]+)"$'
  [[ $1 =~ $shape ]] || { echo "invalid sign_update output: '$1'" >&2; return 1; }
  printf '%s %s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
}

xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g' -e "s/'/\&apos;/g"
}

# notes_html BODY
notes_html() {
  xml_escape "$1"$'\n' | awk '
    /^[-*] / {
      printf "%s<li>%s", (in_list ? "</li>" : "<ul>"), substr($0, 3)
      in_list = 1
      next
    }
    in_list && /^[[:space:]]+[^[:space:]]/ {
      sub(/^[[:space:]]+/, "")
      printf " %s", $0
      next
    }
    {
      if (in_list) printf "</li></ul>"
      in_list = 0
      if ($0 !~ /^[[:space:]]*$/) printf "<p>%s</p>", $0
    }
    END { if (in_list) printf "</li></ul>" }
  '
}

# appcast_xml VERSION BUILD MINOS ENCLOSURE_URL LENGTH SIGNATURE NOTES_URL NOTES_HTML PUBDATE
appcast_xml() {
  local version build minos enclosure length signature pubdate link_label link description="$8"
  [[ $7 != *']]>'* ]] || { echo "release notes URL contains ]]>: $7" >&2; return 1; }
  if [ -n "$7" ]; then
    link_label='What changed'
    [ -z "$description" ] || link_label='Full release notes'
    link=$(printf '<p><a href="%s">%s</a></p>' "$(xml_escape "$7")" "$link_label")
    description="$description$link"
  fi
  [[ $description != *']]>'* ]] || { echo 'release notes description contains ]]>' >&2; return 1; }
  version=$(xml_escape "$1"); build=$(xml_escape "$2"); minos=$(xml_escape "$3")
  enclosure=$(xml_escape "$4"); length=$(xml_escape "$5"); signature=$(xml_escape "$6")
  pubdate=$(xml_escape "$9")
  cat <<XML
<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">
  <channel>
    <title>SHAFT</title>
    <item>
      <title>SHAFT $version</title>
      <pubDate>$pubdate</pubDate>
      <sparkle:version>$build</sparkle:version>
      <sparkle:shortVersionString>$version</sparkle:shortVersionString>
      <sparkle:minimumSystemVersion>$minos</sparkle:minimumSystemVersion>
XML
  if [ -n "$description" ]; then
    printf '      <description><![CDATA[%s]]></description>\n' "$description"
  fi
  cat <<XML
      <enclosure url="$enclosure" length="$length" type="application/octet-stream" sparkle:edSignature="$signature"/>
    </item>
  </channel>
</rss>
XML
}

self_test() {
  local t fails=0 d notes
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
  cat >"$t/nested.plist" <<'P'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>com.apple.security.app-sandbox</key><dict><key>x</key><true/></dict>
<key>list</key><array><string>q</string></array>
</dict></plist>
P
  expect "app without sandbox: key absent (status 3)" "3" "$(plist_value "$t/app.plist" 'com\.apple\.security\.app-sandbox' >/dev/null 2>&1; echo $?)"
  expect "app: aps-environment read" "production" "$(plist_value "$t/app.plist" 'com\.apple\.developer\.aps-environment')"
  expect "extension with sandbox: true" "true" "$(plist_value "$t/ext.plist" 'com\.apple\.security\.app-sandbox')"
  expect "unescaped dots are not the key (the old bug)" "3" "$(plist_value "$t/ext.plist" 'com.apple.security.app-sandbox' >/dev/null 2>&1; echo $?)"
  expect "the escaping used by ent_of" 'com\.apple\.security\.app-sandbox' "$(k=com.apple.security.app-sandbox; echo "${k//./\\.}")"
  expect "profile: nested Entitlements key" "production" "$(plist_value "$t/profile.plist" 'Entitlements.com\.apple\.developer\.aps-environment')"
  expect "malformed plist: status 2, not 'absent'" "2" "$(plist_value "$t/bad.plist" 'com\.apple\.security\.app-sandbox' >/dev/null 2>&1; echo $?)"
  expect "key holding a dictionary: status 4, not 'absent'" "4" "$(plist_value "$t/nested.plist" 'com\.apple\.security\.app-sandbox' >/dev/null 2>&1; echo $?)"
  expect "key holding an array: status 4" "4" "$(plist_value "$t/nested.plist" 'list' >/dev/null 2>&1; echo $?)"
  m=$(plist_value "$t/nested.plist" 'com\.apple\.security\.app-sandbox' 2>&1 || true)
  if [[ $m == *"holds a dictionary"* ]]; then ok=yes; else ok=no; fi
  expect "dictionary failure carries the real message" "yes" "$ok"
  m=$(plist_value "$t/bad.plist" x 2>&1 || true)
  if [[ $m == *"not a valid plist"* ]]; then ok=yes; else ok=no; fi
  expect "malformed plist: message says so" "yes" "$ok"
  for url in https://example.com/a http://127.0.0.1:8765/a http://localhost/a; do
    expect "URL allowed: $url" 0 "$(url_allowed "$url"; echo $?)"
  done
  for url in http://example.com http://127.0.0.1.evil.com/ http://localhost@evil.com/ ftp://example.com/ '' https:///; do
    expect "URL refused: $url" 1 "$(url_allowed "$url"; echo $?)"
  done
  expect "parse signature" 'Ab+/= 123' "$(parse_sign_output 'sparkle:edSignature="Ab+/=" length="123"')"
  expect "bad signature length" 1 "$(parse_sign_output 'sparkle:edSignature="Ab==" length="abc"' >/dev/null 2>&1; echo $?)"
  expect "empty signature output" 1 "$(parse_sign_output '' >/dev/null 2>&1; echo $?)"
  expect "matching keys" 0 "$(keys_match abc abc; echo $?)"
  expect "different keys" 1 "$(keys_match abc def; echo $?)"
  expect "empty keys" 1 "$(keys_match '' ''; echo $?)"
  notes=$(notes_html $'- First\n* Second')
  expect "notes group consecutive bullets" '<ul><li>First</li><li>Second</li></ul>' "$notes"
  expect "notes escape HTML" '<ul><li>&lt;b&gt;&amp; &quot;text&quot;</li></ul>' "$(notes_html '- <b>& "text"')"
  expect "notes render paragraph after list" '<ul><li>First</li></ul><p>A paragraph.</p>' "$(notes_html $'- First\nA paragraph.')"
  expect "notes join indented continuation" '<ul><li>First continued</li></ul>' "$(notes_html $'- First\n  continued')"
  expect "empty notes body produces empty HTML" '' "$(notes_html '')"
  appcast_xml '1.2.3' 123 26.0 'https://example.com/a?x=1&y=2' 456 'Ab+/=' 'https://example.com/notes?a=1&b=2' "$notes" 'Mon, 16 Nov 2026 12:00:00 +0000' >"$t/appcast.xml"
  if command -v xmllint >/dev/null 2>&1; then
    expect "appcast is well-formed XML" 0 "$(xmllint --noout - <"$t/appcast.xml"; echo $?)"
  else
    echo "skip appcast XML well-formedness: xmllint is not installed."
  fi
  expect "exact enclosure attributes and escaped URL" 0 "$(grep -Fq '<enclosure url="https://example.com/a?x=1&amp;y=2" length="456" type="application/octet-stream" sparkle:edSignature="Ab+/="/>' "$t/appcast.xml"; echo $?)"
  expect "description includes inline notes and full release notes link" 0 "$(grep -Fq '<description><![CDATA[<ul><li>First</li><li>Second</li></ul><p><a href="https://example.com/notes?a=1&amp;b=2">Full release notes</a></p>]]></description>' "$t/appcast.xml"; echo $?)"
  expect "URL alone keeps What changed link" '      <description><![CDATA[<p><a href="https://example.com/notes">What changed</a></p>]]></description>' "$(appcast_xml 1 2 26 url 3 abc https://example.com/notes '' date | grep '<description>')"
  expect "inline notes alone appear in description" "      <description><![CDATA[$notes]]></description>" "$(appcast_xml 1 2 26 url 3 abc '' "$notes" date | grep '<description>')"
  expect "empty notes omit description" 1 "$(appcast_xml 1 2 26 https://example.com/a 3 abc '' '' date | grep -q '<description>'; echo $?)"
  expect "notes URL refuses CDATA terminator" 1 "$(appcast_xml 1 2 26 url 3 abc 'https://example.com/]]>' '' date >/dev/null 2>&1; echo $?)"
  expect "inline notes refuse CDATA terminator" 1 "$(appcast_xml 1 2 26 url 3 abc '' '<p>]]></p>' date >/dev/null 2>&1; echo $?)"
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
notary_auth=()
if [ -n "$api_key_path$api_key_id$api_issuer" ]; then
  if [ -z "$api_key_path" ] || [ -z "$api_key_id" ] || [ -z "$api_issuer" ]; then
    problem 'Pass --api-key-path, --api-key-id and --api-issuer together.'
  elif [ -n "$notary_profile" ]; then
    problem 'pass either --notary-profile or the API key, not both'
  elif [ ! -f "$api_key_path" ]; then
    problem 'API key path is not a file.'
  else
    notary_auth=(--key "$api_key_path" --key-id "$api_key_id" --issuer "$api_issuer")
  fi
elif [ -n "$notary_profile" ]; then
  notary_auth=(--keychain-profile "$notary_profile")
else
  problem 'Pass --notary-profile NAME or --api-key-path PATH --api-key-id ID --api-issuer ID.'
fi

dirty=$(git -C "$app_dir" diff --name-only HEAD -- 2>/dev/null || true)
if [ -n "$dirty" ]; then
  problem "Tracked files have uncommitted changes (a release is built from a clean tree):
$dirty"
fi

tag=$(git -C "$app_dir" describe --exact-match --tags --match 'v[0-9]*' HEAD 2>/dev/null || true)
if [ -z "$tag" ] && [ "$allow_untagged" -eq 0 ]; then
  problem "HEAD is not on a v* tag. Tag the release first, or pass --allow-untagged for a trial build."
fi
if [ -z "$download_base" ]; then
  if [ -n "$tag" ]; then
    download_base="https://dgrassi1984.github.io/shaft-app/"
  else
    problem "an untagged trial build requires --download-base"
  fi
fi
url_allowed "$feed_url" || problem "--feed-url is not an allowed URL: '$feed_url'"
url_allowed "$download_base" || problem "--download-base is not an allowed URL: '$download_base'"
[[ $download_base == */ ]] || download_base="$download_base/"
# Only whether the item exists: no -w/-g, so the private key is never read here (sign_update reads it later).
security find-generic-password -s https://sparkle-project.org -a ed25519 >/dev/null 2>&1 ||
  problem "No Sparkle EdDSA key in the login keychain. See README.md 'Release a signed build' for one-time key setup."
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

if [ -n "${notary_auth[0]:-}" ]; then
  if [ "$dry" -eq 1 ]; then
    run xcrun notarytool history "${notary_auth[@]}"
  elif ! nout=$(xcrun notarytool history "${notary_auth[@]}" 2>&1); then
    if [ -n "$notary_profile" ]; then
      problem "notarytool cannot use keychain profile '$notary_profile': $nout
A profile stored by notarytool store-credentials is unreadable while this Mac's screen is locked. notarytool then reports it as missing.
For unattended releases, pass the App Store Connect API key with --api-key-path, --api-key-id and --api-issuer."
    else
      problem "notarytool cannot use App Store Connect API key '$api_key_id': $nout"
    fi
  fi
fi
[ "$dry" -eq 1 ] && echo '(dry run: Apple is not contacted)'

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
  echo "[dry-run] would run: xcodebuild -project $app_dir/SHAFT.xcodeproj -scheme $scheme -configuration Release -destination generic/platform=macOS -archivePath $archive -derivedDataPath $out/dd -allowProvisioningUpdates DEVELOPMENT_TEAM=$team SHAFT_APS_ENVIRONMENT=production SHAFT_UPDATE_FEED=$feed_url archive"
else
  xcodebuild -project "$app_dir/SHAFT.xcodeproj" -scheme "$scheme" -configuration Release \
    -destination 'generic/platform=macOS' -archivePath "$archive" -derivedDataPath "$out/dd" \
    -allowProvisioningUpdates DEVELOPMENT_TEAM="$team" SHAFT_APS_ENVIRONMENT=production SHAFT_UPDATE_FEED="$feed_url" archive
fi

sparkle_bin="$out/dd/SourcePackages/artifacts/sparkle/Sparkle/bin"
archive_info="$archive/Products/Applications/SHAFT.app/Contents/Info.plist"
if [ "$dry" -eq 1 ]; then
  echo "[dry-run] would require executable: $sparkle_bin/sign_update and $sparkle_bin/generate_keys"
  echo "[dry-run] would run: $sparkle_bin/generate_keys -p | tr -d '[:space:]'"
  echo "[dry-run] would run: plist_value $archive_info SUPublicEDKey"
  echo "[dry-run] would require: keys_match signing and archived public keys; shipping a mismatch would strand every installed copy"
else
  for tool in sign_update generate_keys; do
    [ -x "$sparkle_bin/$tool" ] || die "Sparkle tool is not executable: $sparkle_bin/$tool"
  done
  public_key=$("$sparkle_bin/generate_keys" -p | tr -d '[:space:]')
  archived_key=$(plist_value "$archive_info" SUPublicEDKey) || die "Cannot read SUPublicEDKey in $archive_info."
  keys_match "$public_key" "$archived_key" ||
    die "Sparkle signing public key '$public_key' differs from archived SUPublicEDKey '$archived_key'; shipping it would strand every installed copy."
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
[dry-run] would require: codesign -dvv shows 'Authority=Developer ID Application' and flags runtime (hardened) on both
[dry-run] would require: plist_value $app/Contents/Info.plist SUFeedURL equals $feed_url, SUPublicEDKey non-empty, SUVerifyUpdateBeforeExtraction and SURequireSignedFeed true
[dry-run] would require framework: $app/Contents/Frameworks/Sparkle.framework
[dry-run] would run: codesign -dvv $app/Contents/Frameworks/Sparkle.framework
[dry-run] would run: codesign -dvv $app/Contents/Frameworks/Sparkle.framework/Versions/B/Autoupdate
[dry-run] would run: codesign -dvv $app/Contents/Frameworks/Sparkle.framework/Versions/B/Updater.app
[dry-run] would require: Developer ID Application and flags runtime on all three Sparkle bundles
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
    # -dvv: plain -dv prints no Authority= lines, so the check below failed every correctly signed build.
    info=$(codesign -dvv "$b" 2>&1)
    printf '%s\n' "$info" | grep -q 'Authority=Developer ID Application' || die "$b is not signed with Developer ID Application."
    printf '%s\n' "$info" | grep -q 'flags=.*runtime' || die "$b lacks the hardened runtime."
  done
  [ "$(profile_aps "$app")" = "production" ] || die "The app's provisioning profile does not grant aps-environment production (is Push Notifications on for its App ID?)."
  app_info="$app/Contents/Info.plist"
  actual_feed=$(plist_value "$app_info" SUFeedURL) || die "Cannot read SUFeedURL in $app_info."
  [ "$actual_feed" = "$feed_url" ] || die "SUFeedURL is '$actual_feed', expected '$feed_url'."
  exported_key=$(plist_value "$app_info" SUPublicEDKey) || die "Cannot read SUPublicEDKey in $app_info."
  [ -n "$exported_key" ] || die "SUPublicEDKey in $app_info is empty."
  for key in SUVerifyUpdateBeforeExtraction SURequireSignedFeed; do
    value=$(plist_value "$app_info" "$key") || die "Cannot read $key in $app_info."
    [ "$value" = true ] || die "$key in $app_info is '$value', expected true."
  done
  framework="$app/Contents/Frameworks/Sparkle.framework"
  [ -d "$framework" ] || die "Sparkle framework is missing: $framework"
  for b in "$framework" "$framework/Versions/B/Autoupdate" "$framework/Versions/B/Updater.app"; do
    info=$(codesign -dvv "$b" 2>&1) || die "Cannot read signature of $b: $info"
    printf '%s\n' "$info" | grep -q 'Authority=Developer ID Application' || die "$b is not signed with Developer ID Application: $info"
    printf '%s\n' "$info" | grep -q 'flags=.*runtime' || die "$b lacks the hardened runtime: $info"
  done
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
  if [ -n "${notary_auth[0]:-}" ]; then
    run xcrun notarytool submit "$dmg" "${notary_auth[@]}" --wait --output-format json
    printf '[dry-run] on failure: xcrun notarytool log <submission id>'
    printf ' %q' "${notary_auth[@]}"
    printf '\n'
  fi
else
  result="$out/notarytool-submit.json"
  if ! xcrun notarytool submit "$dmg" "${notary_auth[@]}" --wait --output-format json >"$result"; then
    cat "$result" >&2 || true
    sub=$(plutil -extract id raw -o - "$result" 2>/dev/null || true)
    [ -z "$sub" ] || xcrun notarytool log "$sub" "${notary_auth[@]}" >&2 || true
    die "notarytool submit failed (output above)."
  fi
  sub=$(plutil -extract id raw -o - "$result")
  status=$(plutil -extract status raw -o - "$result")
  echo "notarization submission id: $sub, status: $status"
  if [ "$status" != "Accepted" ]; then
    xcrun notarytool log "$sub" "${notary_auth[@]}" >&2 || true
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

step "Sparkle appcast"
notes_url=""
release_notes_html=""
if [ -n "$tag" ]; then
  notes_url="https://github.com/dgrassi1984/shaft/releases/tag/$tag"
  notes_body=$(git -C "$app_dir" tag -l --format='%(contents:body)' "$tag")
  release_notes_html=$(notes_html "$notes_body")
fi
enclosure_url="${download_base}SHAFT-$version.dmg"
appcast="$out/appcast.xml"
if [ "$dry" -eq 1 ]; then
  run "$sparkle_bin/sign_update" "$dmg"
  echo "[dry-run] would parse_sign_output of sign_update into signature and length"
  run "$sparkle_bin/sign_update" --verify "$dmg" '<signature>'
  for key in CFBundleVersion CFBundleShortVersionString LSMinimumSystemVersion; do
    echo "[dry-run] would run: plist_value $app/Contents/Info.plist $key (required non-empty)"
  done
  echo "[dry-run] would run: LC_ALL=C date -u '+%a, %d %b %Y %H:%M:%S +0000'"
  printf '[dry-run] would write: appcast_xml %q <build> <minimum macOS> %q <length> <signature> %q %q <pubdate> > %q\n' \
    "$version" "$enclosure_url" "$notes_url" "$release_notes_html" "$appcast"
else
  sig_line=$("$sparkle_bin/sign_update" "$dmg")
  parsed=$(parse_sign_output "$sig_line") || die "Cannot parse DMG signature (see message above)."
  signature=${parsed% *}
  length=${parsed##* }
  "$sparkle_bin/sign_update" --verify "$dmg" "$signature"
  build=$(plist_value "$app/Contents/Info.plist" CFBundleVersion) || die "Cannot read CFBundleVersion in $app/Contents/Info.plist."
  version=$(plist_value "$app/Contents/Info.plist" CFBundleShortVersionString) || die "Cannot read CFBundleShortVersionString in $app/Contents/Info.plist."
  minos=$(plist_value "$app/Contents/Info.plist" LSMinimumSystemVersion) || die "LSMinimumSystemVersion is absent or unreadable in $app/Contents/Info.plist."
  [ -n "$build" ] && [ -n "$version" ] && [ -n "$minos" ] || die "Empty app metadata: build='$build', version='$version', minimum macOS='$minos'."
  pubdate=$(LC_ALL=C date -u '+%a, %d %b %Y %H:%M:%S +0000')
  appcast_xml "$version" "$build" "$minos" "$enclosure_url" "$length" "$signature" "$notes_url" "$release_notes_html" "$pubdate" >"$appcast" || die "Cannot write $appcast."
fi
run "$sparkle_bin/sign_update" "$appcast"
run "$sparkle_bin/sign_update" --verify "$appcast"

step "Result"
if [ "$dry" -eq 1 ]; then
  echo "[dry-run] would print the path and SHA-256 of $dmg"
else
  echo "$dmg"
  shasum -a 256 "$dmg"
fi
echo "$appcast"
if [ -n "$tag" ]; then
  echo "Copy the DMG and signed appcast.xml into shaft-app/ in the dgrassi1984/dgrassi1984.github.io Pages checkout, then review and push the site repository."
  echo "The DMG must be downloadable at: $enclosure_url"
  echo "Then publish the GitHub release $tag with this DMG and the tag's notes, marked latest (docs/deploy.md, Cutting a release):"
  echo "  gh release create $tag \"$dmg\" --title $tag --notes \"\$(git tag -l --format='%(contents:body)' $tag)\" --latest"
else
  echo "Serve the trial DMG at $enclosure_url and the signed appcast.xml at $feed_url."
fi
echo "Nothing was uploaded."
[ "$problems" -eq 0 ] || exit 1
