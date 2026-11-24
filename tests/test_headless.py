"""0.15.0: the headless profile: one Containerfile, two images; KimiLM's engine as the person's service."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "system_files/usr/libexec/kimios/kimios-headless"
sys.path.insert(0, str(REPO / "build_files"))
import sign_policy  # noqa: E402

STUBS = {
    "systemctl": 'echo "systemctl $*" >> "$STUB_LOG"; case "$*" in *is-failed*) exit 1;; *is-active*) echo inactive;; esac',
    "loginctl": 'echo "loginctl $*" >> "$STUB_LOG"; case "$1" in show-user) echo no;; esac',
    "curl": 'echo "curl $*" >> "$STUB_LOG"; echo \'{"ok":true}\'',
    "journalctl": 'echo "(journal)"',
    "id": 'if [ "${1:-}" = -u ]; then echo "${STUB_UID:-1000}"; else /usr/bin/id "$@"; fi',
    "hostname": 'if [ "${1:-}" = -I ]; then echo "192.168.1.5 fe80::1"; else echo kimibox; fi',
}


class Image(unittest.TestCase):
    def test_one_containerfile_two_profiles(self):
        cf = (REPO / "Containerfile").read_text()
        self.assertIn("ARG BASE_IMAGE=quay.io/fedora/fedora-silverblue", cf)
        self.assertIn("FROM ${BASE_IMAGE}:${FEDORA_VERSION}", cf)
        self.assertIn('PROFILE="${PROFILE}" bash /tmp/build_files/build.sh', cf)
        sh = (REPO / "build_files/build.sh").read_text()
        desktop, headless = sh.split('if [[ "${PROFILE}" == desktop ]]; then', 1)[1].split("else", 1)
        self.assertIn("cage", desktop)
        for pkg in ("openssh-server", "avahi", "nss-mdns", "NetworkManager-wifi", "nodejs", "toolbox", "vulkan-loader"):
            self.assertIn(pkg, headless.split("\nfi\n", 1)[0], pkg)
        self.assertIn("systemctl enable sshd.service avahi-daemon.service", sh)
        out = subprocess.run(["bash", str(REPO / "build_files/build.sh")], env={**os.environ, "PROFILE": "kiosk"},
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 1)                      # nothing installed for an unknown profile
        self.assertIn("desktop or headless", out.stderr)

    def test_the_workflow_builds_tests_publishes_and_signs_both(self):
        wf = (REPO / ".github/workflows/build.yml").read_text()
        self.assertIn("profile: [desktop, headless]", wf)
        self.assertIn("base: quay.io/fedora/fedora-bootc", wf)
        self.assertIn('suffix: "-headless"', wf)
        self.assertIn("Headless - KimiLM's engine answers", wf)
        self.assertIn('for image in "$IMAGE_LC" "$IMAGE_LC-headless"', wf)

    def test_both_images_must_be_signed(self):
        tmp = Path(tempfile.mkdtemp())
        key = tmp / "kimios.pub"
        key.write_text("-----BEGIN PUBLIC KEY-----\nx\n-----END PUBLIC KEY-----\n")
        pol = tmp / "policy.json"
        pol.write_text(json.dumps({"default": [{"type": "insecureAcceptAnything"}]}))
        sign_policy.apply(pol, key)
        docker = json.loads(pol.read_text())["transports"]["docker"]
        for repo in ("ghcr.io/kiplabat/kimios", "ghcr.io/kiplabat/kimios-headless"):
            self.assertEqual(docker[repo][0]["type"], "sigstoreSigned", repo)
        self.assertIn("ghcr.io/kiplabat/kimios-headless:",
                      (REPO / "system_files/etc/containers/registries.d/kimios.yaml").read_text())

    def test_the_service_listens_on_this_computer_only(self):
        unit = (REPO / "system_files/usr/lib/systemd/user/kimilm-headless.service").read_text()
        self.assertIn("EnvironmentFile=%h/.config/kimilm-headless/engine.env", unit)
        self.assertIn("Slice=kimilm.slice", unit)
        self.assertIn("-m kimilm serve --port ${KIMILM_PORT} --data-dir %h/.config/KimiLM", unit)
        self.assertNotIn("0.0.0.0", unit)                         # the engine binds 127.0.0.1 itself
        self.assertNotIn("KIMILM_TOKEN=", unit)                   # the token is only in the person's file
        self.assertNotIn("DVA_", unit)                            # KimiLM 2.37.0 names: KIMILM_*


class Command(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.bin = self.home / "bin"
        self.bin.mkdir()
        for name, body in STUBS.items():
            p = self.bin / name
            p.write_text("#!/usr/bin/env bash\n" + body + "\n")
            p.chmod(0o755)
        py = self.home / "python3"
        py.write_text("#!/bin/sh\n")
        py.chmod(0o755)
        self.log = self.home / "stub.log"
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), "USER": "labatt",
                    "STUB_LOG": str(self.log), "KIMIOS_KIMILM_PYTHON": str(py)}

    def run_cli(self, *args):
        return subprocess.run(["bash", str(CLI), *args], env=self.env, capture_output=True, text=True, timeout=30)

    def test_setup_makes_a_private_token_starts_it_and_says_how_to_reach_it(self):
        out = self.run_cli("setup")
        self.assertEqual(out.returncode, 0, out.stderr)
        envf = self.home / ".config/kimilm-headless/engine.env"
        self.assertEqual(stat.S_IMODE(envf.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(envf.parent.stat().st_mode), 0o700)
        values = dict(line.split("=", 1) for line in envf.read_text().split())
        self.assertRegex(values["KIMILM_TOKEN"], r"^[A-Za-z0-9]{40}$")
        self.assertEqual(values["KIMILM_PORT"], "8765")
        log = self.log.read_text()
        self.assertIn("loginctl enable-linger labatt", log)
        self.assertIn("systemctl --user enable --now kimilm-headless.service", log)
        self.assertIn("ssh -N -L 8765:localhost:8765 labatt@kimibox.local", out.stdout)
        self.assertIn(f"http://localhost:8765/?token={values['KIMILM_TOKEN']}", out.stdout)
        self.assertIn("192.168.1.5", out.stdout)
        self.assertEqual(self.run_cli("setup").returncode, 0)    # again: the same token
        self.assertEqual(dict(line.split("=", 1) for line in envf.read_text().split())["KIMILM_TOKEN"],
                         values["KIMILM_TOKEN"])
        out = self.run_cli("rotate")
        self.assertEqual(out.returncode, 0, out.stderr)
        new = dict(line.split("=", 1) for line in envf.read_text().split())["KIMILM_TOKEN"]
        self.assertNotEqual(new, values["KIMILM_TOKEN"])
        self.assertIn(new, out.stdout)

    def test_a_token_written_by_an_earlier_kimios_still_works(self):
        envf = self.home / ".config/kimilm-headless/engine.env"
        envf.parent.mkdir(parents=True)
        envf.write_text("DVA_TOKEN=oldtoken123\nKIMILM_PORT=8765\n")
        out = self.run_cli("setup")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("http://localhost:8765/?token=oldtoken123", out.stdout)   # kept, not replaced
        self.assertEqual(self.run_cli("rotate").returncode, 0)
        values = dict(line.split("=", 1) for line in envf.read_text().split())
        self.assertNotIn("DVA_TOKEN", values)
        self.assertRegex(values["KIMILM_TOKEN"], r"^[A-Za-z0-9]{40}$")

    def test_it_refuses_without_kimilm_or_as_root(self):
        self.env["KIMIOS_KIMILM_PYTHON"] = "/nonexistent/python3"
        out = self.run_cli("setup")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("KimiLM is not in this image", out.stderr)
        self.assertNotEqual(self.run_cli("url").returncode, 0)    # nothing set up: no link
        self.env["KIMIOS_KIMILM_PYTHON"] = "/bin/sh"
        self.env["STUB_UID"] = "0"
        out = self.run_cli("setup")
        self.assertIn("not as root", out.stderr)
        self.assertFalse((self.home / ".config/kimilm-headless").exists())
        self.assertEqual(self.run_cli().returncode, 0)            # help
        self.assertEqual(self.run_cli("nonsense").returncode, 2)


if __name__ == "__main__":
    unittest.main()
