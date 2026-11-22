"""0.12.0: the installer ISO asks for the disk; it never installs to the first disk it finds (docs/SCOPE.md)."""
import re
import tomllib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class Installer(unittest.TestCase):
    def setUp(self):
        self.ks = tomllib.loads((REPO / "iso/config.toml").read_text())["customizations"]["installer"]["kickstart"][
            "contents"]
        self.lines = [ln.strip() for ln in self.ks.splitlines() if ln.strip() and not ln.strip().startswith("#")]

    def test_the_person_chooses_disk_partitions_and_user(self):
        self.assertIn("graphical", self.lines)
        for cmd in ("clearpart", "autopart", "zerombr", "ignoredisk", "part", "text", "cmdline", "user", "rootpw",
                    "reboot", "ostreecontainer"):      # ostreecontainer is added by bootc-image-builder
            self.assertFalse(any(ln.split()[0] == cmd for ln in self.lines), cmd)

    def test_the_installed_system_updates_from_the_registry(self):
        post = re.search(r"%post[^\n]*\n(.*?)%end", self.ks, re.S)
        self.assertIsNotNone(post)
        self.assertIn("bootc switch --mutate-in-place --transport registry ghcr.io/kiplabat/kimios:latest",
                      post.group(1))

    def test_the_workflow_builds_and_checks_it(self):
        wf = (REPO / ".github/workflows/iso.yml").read_text()
        self.assertIn("--type anaconda-iso", wf)
        self.assertIn("iso/config.toml:/config.toml", wf)
        self.assertIn("! grep -Eq '^(clearpart|autopart|zerombr|ignoredisk|text|cmdline)'", wf)


if __name__ == "__main__":
    unittest.main()
