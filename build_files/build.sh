#!/usr/bin/env bash
# Runs inside the image build (see Containerfile).
set -euo pipefail

PACKAGES=(
  cage            # kiosk Wayland compositor for the KimiOS session
  python3         # kimi-hwprobe (standard library only)
  usbutils pciutils i2c-tools libgpiod-utils   # device inspection for agents
)

if command -v dnf5 >/dev/null 2>&1; then
  dnf5 -y install --setopt=install_weak_deps=False "${PACKAGES[@]}"
  dnf5 clean all
else
  rpm-ostree install --idempotent "${PACKAGES[@]}"
fi

chmod 0755 /usr/libexec/kimios/kimios-session /usr/libexec/kimios/kimi-hwprobe

# KimiLM (optional until the KimiLM Linux build exists; docs/SCOPE.md Phase 1)
if [[ -n "${KIMILM_URL:-}" ]]; then
  mkdir -p /usr/lib/kimilm
  curl -fsSL --retry 3 "${KIMILM_URL}" | tar -xz -C /usr/lib/kimilm --strip-components=1
  for name in kimilm KimiLM; do
    if [[ -x "/usr/lib/kimilm/${name}" ]]; then
      ln -sf "/usr/lib/kimilm/${name}" /usr/bin/kimilm
      break
    fi
  done
  [[ -x /usr/bin/kimilm ]] || { echo "KimiLM archive has no kimilm executable" >&2; exit 1; }
fi

systemctl enable kimi-hwprobe.service
