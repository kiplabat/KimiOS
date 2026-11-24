"""0.12.0: the installer ISO asks for the disk; it never installs to the first disk it finds (docs/SCOPE.md)."""
import sys
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ISO = REPO / "iso"


class Installer(unittest.TestCase):
    def setUp(self):
        self.cf = (REPO / "iso/Containerfile.installer").read_text()
        self.wf = (REPO / ".github/workflows/iso.yml").read_text()

    def test_anaconda_is_only_told_what_to_install(self):
        m = re.search(r'echo "(bootc [^"]+)"\s*\\\s*\n\s*> /usr/share/anaconda/interactive-defaults.ks', self.cf)
        self.assertIsNotNone(m, "interactive-defaults.ks is written from one bootc line")
        self.assertEqual(m.group(1), "bootc --source-imgref containers-storage:${PAYLOAD} --target-imgref ${PAYLOAD}")
        self.assertIn("ARG PAYLOAD=ghcr.io/kiplabat/kimios:latest", self.cf)    # updates come from the registry
        for cmd in ("clearpart", "autopart", "zerombr", "ignoredisk", "inst.ks=", "inst.text", "inst.cmdline"):
            self.assertNotIn(cmd, self.cf, cmd)
        menu = (REPO / "iso/files/iso.yaml").read_text()
        entries = [ln for ln in menu.splitlines() if ln.strip().startswith("linux:")]
        self.assertEqual(len(entries), 2)
        for ln in entries:                  # 0.12.1: without it the installer stops at start (SELinux)
            self.assertIn("enforcing=0", ln)
            self.assertIn("inst.graphical", ln)
        self.assertIn("nomodeset", entries[1])
        self.assertNotIn("<<EOT", self.cf)                    # the runner's podman does not read heredocs
        for pkg in ("NetworkManager-wifi", "wpa_supplicant", "iwlwifi-mvm-firmware"):    # 0.12.2: Wi-Fi to install
            self.assertIn(pkg, self.cf)

    def test_the_workflow_embeds_kimios_and_checks_the_installer(self):
        self.assertIn("--bootc-ref localhost/kimios-installer", self.wf)
        self.assertIn('--bootc-installer-payload-ref "$REF"', self.wf)
        self.assertIn("generic-iso", self.wf)
        self.assertIn("! echo \"$KS\" | grep -Eq '^(clearpart|autopart|zerombr|ignoredisk|part|text|cmdline)'", self.wf)
        self.assertNotIn("anaconda-iso", self.wf.split("(bootc-image-builder's anaconda-iso")[0])


if __name__ == "__main__":
    unittest.main()


class Polish(unittest.TestCase):
    """The installer is named KimiOS, needs no network when KimiOS is on the stick, and exists for aarch64."""

    def test_named_kimios_and_stable_with_fedoras_profile(self):
        osr = dict(line.split("=", 1) for line in (ISO / "files/os-release").read_text().split("\n") if "=" in line)
        self.assertEqual(osr["NAME"], '"KimiOS"')
        self.assertEqual(osr["ID"], "fedora")            # Anaconda's Fedora profile is chosen by ID
        self.assertEqual(osr["RELEASE_TYPE"], "stable")  # no pre-release warning
        self.assertEqual(osr["VERSION_ID"], "45")
        cf = (ISO / "Containerfile.installer").read_text()
        self.assertIn("COPY files/os-release /usr/lib/os-release", cf)
        self.assertIn("cp /usr/lib/os-release /etc/os-release", cf)

    def test_offline_change_applies_once_and_only_to_the_expected_code(self):
        import subprocess
        import tempfile
        tool = ISO / "files/offline_bootc.py"
        exec_ns = {}
        src = tool.read_text()
        exec(src.split("paths = ")[0], exec_ns)                  # OLD and NEW, without running the patch
        upstream = ("class BootcSourceModule:\n" + exec_ns["OLD"] + "\n    @property\n    def required_space(self):\n"
                    "        return 1\n")
        f = Path(tempfile.mkdtemp()) / "bootc.py"
        f.write_text(upstream)
        first = subprocess.run([sys.executable, str(tool), str(f)], capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        again = subprocess.run([sys.executable, str(tool), str(f)], capture_output=True, text=True)
        self.assertNotEqual(again.returncode, 0)               # changed code is never patched blindly
        ns = {}
        exec(f.read_text(), ns)
        mod = ns["BootcSourceModule"].__new__(ns["BootcSourceModule"])
        for ref, needs in [("containers-storage:ghcr.io/kiplabat/kimios:latest", False),
                           ("ghcr.io/kiplabat/kimios:latest", True), (None, True)]:
            mod.configuration = type("C", (), {"sourceImgRef": ref})()
            self.assertIs(mod.network_required, needs, ref)

    def test_both_architectures_are_built_and_the_iso_is_checked(self):
        wf = (REPO / ".github/workflows/iso.yml").read_text()
        self.assertIn("runner: ubuntu-24.04-arm", wf)
        self.assertIn("grub2-efi-aa64-cdboot", (ISO / "Containerfile.installer").read_text())
        self.assertIn("bash iso/check_iso.sh", wf)
        self.assertIn("kimios-installer-${{ matrix.arch }}", wf)
