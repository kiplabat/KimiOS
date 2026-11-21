#!/usr/bin/env bash
# Runs inside the image build (see Containerfile).
set -euo pipefail

PACKAGES=(
  cage            # kiosk Wayland compositor for the KimiOS session
  python3         # kimi-hwprobe (standard library only)
  openssl3-libs   # libssl.so.3 for KimiLM's llama.cpp builds (Fedora 45's own OpenSSL is 4.0, libssl.so.4)
  usbutils pciutils i2c-tools libgpiod-utils   # device inspection for agents
  # 0.8.0: programs that control what people plug in (KimiLM's Devices): Android phones, iPhones, displays
  android-tools                     # adb and fastboot
  libimobiledevice-utils usbmuxd    # ideviceinfo for iPhones and iPads
  ddcutil                           # an external display's brightness and input (DDC/CI)
)

if command -v dnf5 >/dev/null 2>&1; then
  dnf5 -y install --setopt=install_weak_deps=False "${PACKAGES[@]}"
  dnf5 clean all
else
  rpm-ostree install --idempotent "${PACKAGES[@]}"
fi

chmod 0755 /usr/libexec/kimios/kimios-session /usr/libexec/kimios/kimi-hwprobe /usr/libexec/kimios/kimios-mcp \
  /usr/libexec/kimios/kimi-telemetry /usr/libexec/kimios/kimi-simbench /usr/libexec/kimios/kimi-envelope

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
    install -Dm0644 /usr/lib/kimilm/resources/ui/icon.png /usr/share/icons/hicolor/512x512/apps/kimilm.png
    install -Dm0644 /tmp/build_files/kimilm.desktop /usr/share/applications/kimilm.desktop
  fi
fi

systemctl enable kimi-hwprobe.service
systemctl enable kimi-telemetry.service   # 0.6.0: readings for KimiLM's Performance window
systemctl --global enable kimios-toolbox.service   # 0.8.0: KimiLM's toolbox, made in the background at first login
systemctl enable kimi-simbench.service kimi-envelope.service   # 0.9.0: simulated devices and the guard
