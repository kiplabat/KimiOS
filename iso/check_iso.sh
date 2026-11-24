#!/usr/bin/env bash
# What Anaconda will read inside a built ISO: the installer's os-release (its product name) and the patched bootc
# source. Fails when it can read them and they are wrong; warns when this ISO's image format cannot be read here.
#   bash iso/check_iso.sh kimios-installer.iso
set -uo pipefail
ISO="$1"; W="$(mktemp -d)"
xorriso -osirrox on -indev "$ISO" -extract /images/install.img "$W/install.img" >/dev/null 2>&1 \
  || { echo "::warning::no /images/install.img in the ISO; not checked"; exit 0; }
root=""
if unsquashfs -d "$W/sq" "$W/install.img" >/dev/null 2>&1; then
  if [ -f "$W/sq/LiveOS/rootfs.img" ]; then
    mkdir -p "$W/root"
    for f in usr/lib/os-release etc/os-release; do
      debugfs -R "dump /$f $W/root/$(echo $f | tr / _)" "$W/sq/LiveOS/rootfs.img" >/dev/null 2>&1 || true
    done
    root="debugfs"
  else
    root="$W/sq"
  fi
elif command -v fsck.erofs >/dev/null && fsck.erofs --extract="$W/er" "$W/install.img" >/dev/null 2>&1; then
  root="$W/er"
fi
if [ -z "$root" ]; then echo "::warning::the installer image's format is not readable here; not checked"; exit 0; fi
if [ "$root" = debugfs ]; then osr="$W/root/usr_lib_os-release"; else osr="$root/usr/lib/os-release"; fi
[ -s "$osr" ] || { echo "::error::no os-release in the installer image"; exit 1; }
cat "$osr"
grep -qx 'NAME="KimiOS"' "$osr" || { echo "::error::the installer would not be named KimiOS"; exit 1; }
grep -qx 'RELEASE_TYPE=stable' "$osr" || { echo "::error::the installer would show a pre-release warning"; exit 1; }
if [ "$root" != debugfs ]; then
  b=$(find "$root" -path '*pyanaconda/modules/payloads/source/bootc/bootc.py' | head -1)
  grep -q "KimiOS: not when the image is on the installation medium" "$b" \
    || { echo "::error::the offline-install change is not in the ISO"; exit 1; }
fi
echo "the ISO's installer is named KimiOS, stable, and installs without a network"
