"""0.12.0: the installer ISO asks for the disk; it never installs to the first disk it finds (docs/SCOPE.md)."""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


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

    def test_the_workflow_embeds_kimios_and_checks_the_installer(self):
        self.assertIn("--bootc-ref localhost/kimios-installer", self.wf)
        self.assertIn('--bootc-installer-payload-ref "$REF"', self.wf)
        self.assertIn("generic-iso", self.wf)
        self.assertIn("! echo \"$KS\" | grep -Eq '^(clearpart|autopart|zerombr|ignoredisk|part|text|cmdline)'", self.wf)
        self.assertNotIn("anaconda-iso", self.wf.split("(bootc-image-builder's anaconda-iso")[0])


if __name__ == "__main__":
    unittest.main()
