#!/bin/bash
# Mario Kart World 1.0.0's iPhone app (an unsigned IPA for LiveContainer) from a dump folder, with this fork's switches (switch2ios/env_ios.txt) packed in as dm/env.txt.
#
#   switch2ios/build_ipa.sh <dump folder> <build folder> [more scripts/build_ipa_from_nsp.py options: --platform mac, -l fr, --gpu g18p, -o <file>.ipa, ...]
#
# The dump folder holds base/exefs/ (main, main.npdm, sdk, subsdk*) and base/romfs/ (scripts/pack_dump.py gives the layout). A folder with exefs/ and romfs/ at its top
# is laid out as base/ through links in <build folder>/dump: the dump itself is only read. The build goes into <build folder>/build, outside the dump folder (the script's
# default would be <dump folder>/<title ID>_build): the RomFS image (about 22 GB for Mario Kart World), the shader caches and the IPA (about 24 GB with the game files in it),
# about 50 GB in all. --platform mac builds what the Mac host needs instead, without env_ios.txt, and prints the command switch2ios/mac_run.py takes (--phone adds the switches).
# The bundle ID is the script's default, org.nxnative.0400c3f00006e000, as the fork's earlier IPAs had: LiveContainer replaces the app and keeps its Documents.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd); repo=$(dirname "$here")
if [ $# -lt 2 ]; then sed -n '4p' "$0" | sed 's/^# *//' >&2; exit 2; fi
dump=$(cd "$1" && pwd); mkdir -p "$2"; work=$(cd "$2" && pwd); shift 2
case "$work/" in "$dump"/*) echo "the build folder must be outside the dump folder: $work" >&2; exit 2;; esac
if [ ! -f "$dump/base/exefs/main.npdm" ]; then
  if [ -f "$dump/exefs/main.npdm" ] && [ -d "$dump/romfs" ]; then
    mkdir -p "$work/dump/base"; ln -sfn "$dump/exefs" "$work/dump/base/exefs"; ln -sfn "$dump/romfs" "$work/dump/base/romfs"
    echo "the dump laid out as base/ through links: $work/dump"; dump=$work/dump
  else
    echo "$dump: neither base/exefs/main.npdm nor exefs/main.npdm with romfs/ (scripts/pack_dump.py gives the layout)" >&2; exit 2
  fi
fi
env_file=(--env-file "$here/env_ios.txt")
prev=
for a in "$@"; do
  case "$a" in --platform=mac*) env_file=();; mac|mac-app) [ "$prev" = --platform ] && env_file=();; esac
  prev=$a
done
free=$(df -Pk "$work" | awk 'NR == 2 { print int($4 / 1048576) }')
if [ "${#env_file[@]}" -gt 0 ] && [ "$free" -lt 50 ]; then echo "warning: $free GB free where the build goes ($work); an IPA with the game files in it needs about 50" >&2; fi
cd "$repo"
exec python3 scripts/build_ipa_from_nsp.py "$dump" ${env_file[@]+"${env_file[@]}"} --work-dir "$work/build" --progress lines "$@"
