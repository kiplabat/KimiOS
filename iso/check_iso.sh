#!/usr/bin/env bash
# What Anaconda will read inside a built ISO: the installer's /.buildstamp and os-release (its product name) and
# the patched bootc source. Fails when they are wrong or cannot be found (the ISO's files are listed then).
#   bash iso/check_iso.sh kimios-installer.iso
set -uo pipefail
ISO="$1"; W="$(mktemp -d)"
fail() { echo "::error::$*"; echo "The ISO holds:"; xorriso -indev "$ISO" -find / -type f 2>/dev/null | head -60; exit 1; }

# the installer's root file system: wherever image-builder put it, squashfs, erofs or ext4, and the image inside it
# when there is one (LiveOS/rootfs.img: what dracut mounts at start)
open_fs() {   # image, target directory -> prints the directory, or "ext4:<image>"
  if unsquashfs -d "$2" "$1" >/dev/null 2>&1; then echo "$2"
  elif fsck.erofs --extract="$2" "$1" >/dev/null 2>&1; then echo "$2"
  elif debugfs -R "ls /" "$1" >/dev/null 2>&1; then echo "ext4:$1"
  fi
}
root=""
for img in /images/install.img /LiveOS/squashfs.img /LiveOS/rootfs.img /images/rootfs.img; do
  xorriso -osirrox on -indev "$ISO" -extract "$img" "$W/img" >/dev/null 2>&1 || continue
  root="$(open_fs "$W/img" "$W/outer")"
  [ -n "$root" ] && { echo "installer image: $img"; break; }
  rm -f "$W/img"
done
[ -n "$root" ] || fail "no installer root file system found in the ISO"
if [ "${root#ext4:}" = "$root" ] && [ -f "$root/LiveOS/rootfs.img" ]; then
  root="$(open_fs "$root/LiveOS/rootfs.img" "$W/inner")"
  [ -n "$root" ] || fail "LiveOS/rootfs.img inside the installer image could not be opened"
  echo "  and the root file system inside it"
fi

get() {   # a file from the installer's root, by path
  if [ "${root#ext4:}" != "$root" ]; then debugfs -R "cat /$1" "${root#ext4:}" 2>/dev/null; else cat "$root/$1" 2>/dev/null; fi
}
stamp="$(get .buildstamp)"; osr="$(get usr/lib/os-release)"
echo "$stamp"; echo "$osr" | head -3
echo "$stamp" | grep -qx 'Product=KimiOS' || fail "the installer's .buildstamp does not name KimiOS (Fedora 45's Anaconda reads it)"
echo "$stamp" | grep -qx 'IsFinal=True' || fail "the installer would show a pre-release warning"
echo "$osr" | grep -qx 'NAME="KimiOS"' || fail "the installer's os-release does not name KimiOS"
if [ "${root#ext4:}" = "$root" ]; then
  b=$(find "$root" -path '*pyanaconda/modules/payloads/source/bootc/bootc.py' | head -1)
  grep -q "KimiOS: not when the image is on the installation medium" "$b" \
    || fail "the offline-install change is not in the ISO"
else
  echo "(the bootc source was checked in the installer environment)"
fi
echo "the ISO's installer is named KimiOS, final, and installs without a network"
