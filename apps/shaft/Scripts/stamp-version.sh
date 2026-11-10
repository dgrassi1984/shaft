#!/bin/bash
set -euo pipefail

fail() {
  echo "error: $*" >&2
  exit 1
}
trap 'fail "Could not stamp the app version (line $LINENO)."' ERR

command -v git >/dev/null 2>&1 || fail "git is required to stamp the app version."
git -C "$SRCROOT" rev-parse --verify HEAD >/dev/null 2>&1 || fail "A git checkout with HEAD is required to stamp the app version."
tag=$(git -C "$SRCROOT" describe --tags --abbrev=0 --match 'v[0-9]*' HEAD 2>/dev/null) || fail "No reachable v[0-9]* git tag; fetch release tags before building."
version=${tag#v}
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "Git tag $tag must have the form vX.Y.Z for Apple's version field."
count=$(git -C "$SRCROOT" rev-list --count HEAD)
build=$((count + 100))
label=$(git -C "$SRCROOT" describe --tags --always HEAD)
changes=$(git -C "$SRCROOT" diff --name-only HEAD --)
if [ -n "$changes" ]; then
  label="${label}+changes"
fi

plist="$TARGET_BUILD_DIR/$INFOPLIST_PATH"
[ -f "$plist" ] || fail "Processed Info.plist is missing: $plist"
set_key() {
  /usr/libexec/PlistBuddy -c "Set :$1 $2" "$plist" 2>/dev/null ||
    /usr/libexec/PlistBuddy -c "Add :$1 string $2" "$plist"
}
set_key CFBundleShortVersionString "$version"
set_key CFBundleVersion "$build"
set_key SHAFTVersion "$label"
touch "$DERIVED_FILE_DIR/stamp-version.stamp"
