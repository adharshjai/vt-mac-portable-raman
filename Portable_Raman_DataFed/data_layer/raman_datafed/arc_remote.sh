#!/bin/bash
# arc_remote.sh - the ARC side of one SSH upload of the Portable Raman data layer (PROTOCOL.md 5.6).
# Sent with every SSH upload, so ARC needs nothing installed beforehand. Run as:
#   bash -c "<this script>" raman_arc_upload MODE BASE FOLDER UPLOAD_ID NAME
#     MODE       upload | upload-copy | check
#     BASE       <ARC folder>, or <ARC folder>/simulated
#     FOLDER     the session folder's name      UPLOAD_ID  u + 12 hex digits
#     NAME       the final folder name: FOLDER, or the name an earlier run placed it under
#   stdin (upload, upload-copy): a tar of the files, the manifest and SHA256SUMS; always read to its end.
#   stdout: only lines starting "RAMAN ". Exit: 0 done, 20 tools missing, 21 a folder cannot be made, written
#   or (check) read, 22 receiving failed, 23 the files do not match their list, 24 the name is taken, 25 the
#   rename failed. A FAIL line comes before the rest of stdin is read, so the sender can stop at once.
# It never deletes anything, and writes files only into this upload's own folder under _incoming.
# The ARC folder, simulated/ and their _incoming/ are shared by the project: a run that makes one makes it
# 3770 (group-writable, setgid, sticky), so every member adds folders and nobody renames or deletes another
# member's. This upload's own folders and files are 0750 and 0640 (umask 027; on ARC its folders also keep
# the setgid bit they inherit, so the files belong to the project group).
set -u
umask 027
mode=$1 base=$2 folder=$3 id=$4 name=$5
stage="$base/_incoming/$folder.$id"
case $base in */simulated) top=${base%/simulated} ;; *) top=$base ;; esac
sums=SHA256SUMS
say() { printf 'RAMAN %s\n' "$*"; }
drain() { if [ "$mode" != check ]; then cat > /dev/null; fi; }
fail() { code=$1; shift; say FAIL "$@"; drain; exit "$code"; }
shared() { if [ ! -d "$1" ]; then mkdir -- "$1" && chmod 3770 "$1"; fi; [ -d "$1" ]; }
readable() { [ -L "$1" ] || [ ! -d "$1" ] || { [ -r "$1" ] && [ -x "$1" ]; } || fail 21 access "$1"; }
# The first folder of a copy (the copy itself, or a folder in it) that this account may not read or enter. find
# leaves out the files of such a folder, so a check names it before it compares the copy with its list.
closed() {
  find "$1" -type d -print0 2> /dev/null | while IFS= read -r -d '' d; do
    { [ -r "$d" ] && [ -x "$d" ]; } || { printf '%s\n' "$d"; break; }
  done
}
open_inside() { shut=$(closed "$1"); [ -z "$shut" ] || fail 21 access "$shut"; }
if command -v sha256sum > /dev/null 2>&1; then sha() { sha256sum "$1"; }
elif command -v shasum > /dev/null 2>&1; then sha() { shasum -a 256 "$1"; }
else fail 20 tools "neither sha256sum nor shasum is installed"; fi
command -v tar > /dev/null 2>&1 || fail 20 tools "tar is not installed"
digests() (
  cd "$1" || exit 1
  find . -type f ! -path "./$sums" -print | LC_ALL=C sort | while IFS= read -r f; do
    sha "$f" | sed 's#^\([0-9a-f]*\)  \./#\1  #'
  done
)
matches() { [ -f "$1/$sums" ] && [ "$(digests "$1" | LC_ALL=C sort)" = "$(LC_ALL=C sort "$1/$sums")" ]; }
report() { say DIGESTS-BEGIN; digests "$1" | sed 's/^/RAMAN DIGEST /'; say DIGESTS-END; }
mine() { [ -f "$1/raman_upload_manifest.json" ] && grep -qF "\"upload_id\": \"$id\"" "$1/raman_upload_manifest.json"; }
placed() {
  if [ -d "$base/$name" ] && [ ! -L "$base/$name" ] && mine "$base/$name"; then printf '%s\n' "$base/$name"; return 0; fi
  n=2
  while [ "$n" -le 99 ]; do
    c="$base/${folder}_copy$n"
    if [ -d "$c" ] && [ ! -L "$c" ] && mine "$c"; then printf '%s\n' "$c"; return 0; fi
    n=$((n + 1))
  done
  return 1
}
for d in "$top" "$base"; do
  if [ -L "$d" ]; then fail 21 prepare "$d is a link"; fi
done
# A check names a folder it must look into but may not read (FAIL access), so that it never reports a copy it
# cannot see as missing or changed: the nearest folder at or above BASE that exists must let this account in,
# BASE and the copy must be readable, and so must every folder in a copy it compares.
if [ "$mode" = check ]; then
  up=$base
  while [ -n "$up" ] && [ ! -e "$up" ]; do up=${up%/*}; done
  [ -z "$up" ] || [ ! -d "$up" ] || [ -x "$up" ] || fail 21 access "$up"
  readable "$base"; readable "$base/$name"
fi
# 1. Put in place by an earlier run of this upload: only check it (the files sent are not needed).
if done_at=$(placed); then
  if [ "$mode" = check ]; then open_inside "$done_at"; fi
  say STEP verify; drain
  matches "$done_at" || { report "$done_at"; fail 23 verify "the copy in $done_at does not match its list"; }
  report "$done_at"; say DONE already "$done_at"; exit 0
fi
if [ "$mode" = check ]; then
  readable "$base/_incoming"; readable "$stage"
  if [ -d "$stage" ]; then
    open_inside "$stage"
    say STEP verify
    if matches "$stage"; then report "$stage"; say STAGED complete "$stage"; else say STAGED partial "$stage"; fi
  elif [ -e "$base/$name" ]; then say ELSEWHERE "$base/$name"
  else say MISSING "$base/$name"; fi
  exit 0
fi
# 2. Receive into this upload's own folder in _incoming, made first. A shared folder that this account
#    may not write into, or may not make a folder in, is named before anything is received (FAIL access).
say STEP prepare
up=${top%/*}
[ -z "$up" ] || mkdir -p -- "$up" || fail 21 prepare "cannot make $up"
for d in "$top" "$base" "$base/_incoming"; do
  p=${d%/*}
  case $p in "$top"|"$base") [ -d "$d" ] || [ ! -d "$p" ] || [ -w "$p" ] || fail 21 access "$p" ;; esac
  shared "$d" || fail 21 prepare "cannot make $d"
done
if [ -L "$base/_incoming" ]; then fail 21 prepare "$base/_incoming is a link"; fi
for d in "$base/_incoming" "$base"; do
  [ -w "$d" ] || fail 21 access "$d"
done
if [ -L "$stage" ]; then fail 21 prepare "$stage is a link"; fi
if [ -d "$stage" ] && matches "$stage"; then
  say STEP verify; drain
else
  mkdir -p -- "$stage" || fail 21 prepare "cannot make $stage"
  say STEP receive
  tar -x -f - -C "$stage" --no-same-owner || fail 22 receive "the files did not arrive completely"
  cat > /dev/null
  say STEP verify
  matches "$stage" || { report "$stage"; fail 23 verify "the files that arrived do not match their list"; }
fi
report "$stage"
# 3. One rename puts the folder in place, under a free name only: NAME, or for a new copy the first free
#    name of FOLDER, FOLDER_copy2 ... FOLDER_copy99.
say STEP place
mine "$stage" || fail 25 place "the folder in _incoming does not hold this upload's manifest"
target="$base/$name"
if [ "$mode" = upload-copy ]; then
  n=1; target=""
  while [ "$n" -le 99 ]; do
    if [ "$n" -eq 1 ]; then c="$base/$folder"; else c="$base/${folder}_copy$n"; fi
    if [ ! -e "$c" ] && [ ! -L "$c" ]; then target=$c; break; fi
    n=$((n + 1))
  done
  [ -n "$target" ] || fail 24 place "no free name from ${folder}_copy2 to ${folder}_copy99"
elif [ -e "$target" ] || [ -L "$target" ]; then
  say CONFLICT "$target"; exit 24
fi
if mv --version > /dev/null 2>&1; then mv -T -- "$stage" "$target"; else mv -- "$stage" "$target"; fi \
  || fail 25 place "cannot rename $stage to $target"
{ [ -d "$target" ] && [ ! -e "$stage" ] && mine "$target"; } || fail 25 place "the rename did not give $target"
say DONE placed "$target"
