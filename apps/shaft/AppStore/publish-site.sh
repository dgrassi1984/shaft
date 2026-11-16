#!/bin/bash
# Copies the App Store support and privacy pages (site/) into a checkout of the public site repository, under
# shaft-app/, with the support address filled in. It commits and pushes nothing: review the diff there, then push.
# An optional signed appcast is copied byte-for-byte to shaft-app/appcast.xml.
#
#   AppStore/publish-site.sh --email ADDRESS --to SITE_CHECKOUT [--appcast FILE]
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
email="" to="" appcast=""
while [ $# -gt 0 ]; do
  case "$1" in
    --appcast) appcast=${2:?--appcast needs a value}; shift 2 ;;
    --email) email=${2:?--email needs a value}; shift 2 ;;
    --to) to=${2:?--to needs a value}; shift 2 ;;
    *) sed -n '2,7p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
  esac
done
[[ $email =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || { echo "error: --email must be an address, got '$email'" >&2; exit 1; }
[ -d "$to/.git" ] || { echo "error: --to must be a git checkout of the site repository: $to" >&2; exit 1; }
if [ -n "$appcast" ]; then
  [ -f "$appcast" ] && grep -q '<!-- sparkle-signatures:' "$appcast" || {
    echo "error: not a signed appcast: run release-mac.sh ($appcast)" >&2; exit 1;
  }
fi
dest="$to/shaft-app"
mkdir -p "$dest"
for f in "$here"/site/*; do
  [ "$(basename "$f")" != appcast.xml ] || continue
  sed "s/SUPPORT_EMAIL/$email/g" "$f" > "$dest/$(basename "$f")"
done
! grep -l SUPPORT_EMAIL "$dest"/*.html || { echo "error: a page still holds the placeholder" >&2; exit 1; }
[ -z "$appcast" ] || cp "$appcast" "$dest/appcast.xml"
echo "Wrote $(ls "$dest" | wc -l | tr -d ' ') files to $dest. Review and push that repository; the pages go live at"
echo "https://<owner>.github.io/shaft-app/ once GitHub Pages serves its default branch."
if [ -n "$appcast" ]; then echo "Feed: https://<owner>.github.io/shaft-app/appcast.xml"; fi
