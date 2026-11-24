#!/usr/bin/env bash
# Runs inside the image build (see Containerfile).
set -euo pipefail

PROFILE="${PROFILE:-desktop}"   # 0.15.0: desktop or headless (Containerfile)
case "${PROFILE}" in desktop|headless) ;; *) echo "PROFILE is desktop or headless, not ${PROFILE}" >&2; exit 1 ;; esac

PACKAGES=(
  python3         # kimi-hwprobe (standard library only)
  openssl3-libs   # libssl.so.3 for KimiLM's llama.cpp builds (Fedora 45's own OpenSSL is 4.0, libssl.so.4)
  usbutils pciutils i2c-tools libgpiod-utils   # device inspection for agents
  # 0.8.0: programs that control what people plug in (KimiLM's Devices): Android phones, iPhones, displays
  android-tools                     # adb and fastboot
  libimobiledevice-utils usbmuxd    # ideviceinfo for iPhones and iPads
  ddcutil                           # an external display's brightness and input (DDC/CI)
  btrfs-progs                       # 0.11.0: snapshots of the home folder before risky actions (kimi-snapshot)
)
if [[ "${PROFILE}" == desktop ]]; then
  PACKAGES+=(cage)                  # kiosk Wayland compositor for the KimiOS session
else
  # 0.15.0 headless: reached over the network (SSH, found by name on the local network), Wi-Fi, Node.js for the
  # MCP servers KimiLM starts (the desktop uses KimiLM's own Electron for that), toolbox for KimiLM's programs,
  # and the GPU drivers llama.cpp's Vulkan build uses. fedora-bootc already has NetworkManager and podman.
  PACKAGES+=(openssh-server avahi nss-mdns NetworkManager-wifi wpa_supplicant nodejs toolbox
             vulkan-loader mesa-vulkan-drivers)
fi

if command -v dnf5 >/dev/null 2>&1; then
  dnf5 -y install --setopt=install_weak_deps=False "${PACKAGES[@]}"
  if [[ "${PROFILE}" == headless ]]; then
    # Wi-Fi firmware for common laptop and board cards (as the installer, 0.12.2); missing names are skipped
    dnf5 -y install --setopt=install_weak_deps=False --skip-unavailable linux-firmware iwlwifi-mvm-firmware \
      iwlwifi-dvm-firmware atheros-firmware brcmfmac-firmware realtek-firmware mt7xxx-firmware
  fi
  dnf5 clean all
else
  rpm-ostree install --idempotent "${PACKAGES[@]}"
fi

chmod 0755 /usr/libexec/kimios/kimios-session /usr/libexec/kimios/kimi-hwprobe /usr/libexec/kimios/kimios-mcp \
  /usr/libexec/kimios/kimi-telemetry /usr/libexec/kimios/kimi-simbench /usr/libexec/kimios/kimi-envelope \
  /usr/libexec/kimios/kimios-device /usr/libexec/kimios/kimios-envelope-setup /usr/libexec/kimios/kimi-snapshot \
  /usr/libexec/kimios/kimios-snapshot /usr/libexec/kimios/kimios-registry /usr/libexec/kimios/kimios-headless
ln -sf /usr/libexec/kimios/kimios-registry /usr/bin/kimios-registry   # 0.12.0: what installed software can do
ln -sf /usr/libexec/kimios/kimios-snapshot /usr/bin/kimios-snapshot   # 0.11.0: snapshots and one-step undo
ln -sf /usr/libexec/kimios/kimios-device /usr/bin/kimios-device   # 0.10.0: register real devices (sudo)

# KimiLM (docs/SCOPE.md Phase 1): the Linux build from KimiLM's GitHub releases.
# Before the first release exists (HTTP 404) the image is built without it and the session opens GNOME.
# Any other download failure stops the build, so a network fault never ships an image without KimiLM.
if [[ -n "${KIMILM_URL:-}" ]]; then
  status="$(curl -sSL --retry 3 -o /dev/null -w '%{http_code}' -r 0-0 "${KIMILM_URL}" || true)"
  if [[ "${status}" == "404" ]]; then
    echo "WARNING: no KimiLM release at ${KIMILM_URL} yet; building without KimiLM" >&2
  else
    mkdir -p /usr/lib/kimilm
    curl -fsSL --retry 3 "${KIMILM_URL}" | tar -xz -C /usr/lib/kimilm --strip-components=1
    [[ -x /usr/lib/kimilm/kimilm ]] || { echo "KimiLM archive has no kimilm executable" >&2; exit 1; }
    ln -sf /usr/lib/kimilm/kimilm /usr/bin/kimilm
    # Electron's sandbox helper, for systems where user namespaces are switched off
    chmod 4755 /usr/lib/kimilm/chrome-sandbox
    if [[ "${PROFILE}" == desktop ]]; then
      install -Dm0644 /usr/lib/kimilm/resources/ui/icon.png /usr/share/icons/hicolor/512x512/apps/kimilm.png
      install -Dm0644 /tmp/build_files/kimilm.desktop /usr/share/applications/kimilm.desktop
    fi
  fi
fi

if [[ "${PROFILE}" == headless ]]; then
  # 0.15.0: no desktop session to choose; KimiLM's engine runs as the person's service (kimios-headless setup)
  rm -f /usr/share/wayland-sessions/kimios.desktop
  ln -sf /usr/libexec/kimios/kimios-headless /usr/bin/kimios-headless
  systemctl enable sshd.service avahi-daemon.service
  systemctl set-default multi-user.target
fi

systemctl enable kimi-hwprobe.service
systemctl enable kimi-telemetry.service   # 0.6.0: readings for KimiLM's Performance window
systemctl --global enable kimios-registry.path kimios-registry.service   # 0.12.0: the software registry
systemctl --global enable kimios-toolbox.service   # 0.8.0: KimiLM's toolbox, made in the background at first login
systemctl enable kimi-simbench.service kimi-envelope.service   # 0.9.0: simulated devices and the guard
python3 /tmp/build_files/sign_policy.py   # 0.13.0: KimiOS images must be signed, once the public key is here
systemctl enable kimi-snapshot.service   # 0.11.0: snapshots of the home folder before risky actions
systemctl enable kimios-envelope-setup.service   # 0.11.0: their user and driver store (systems updated from 0.9.0)
