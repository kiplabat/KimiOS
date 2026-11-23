"""0.13.0: KimiOS images are signed, and a system can require it (docs/SCOPE.md, open decision 2)."""
import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "build_files"))
import sign_policy  # noqa: E402

FEDORA_POLICY = {"default": [{"type": "insecureAcceptAnything"}],
                 "transports": {"docker-daemon": {"": [{"type": "insecureAcceptAnything"}]},
                                "docker": {"registry.access.redhat.com": [{"type": "signedBy"}]}}}
KEY = "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE\n-----END PUBLIC KEY-----\n"


class Policy(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.policy = self.tmp / "policy.json"
        self.policy.write_text(json.dumps(FEDORA_POLICY))

    def test_nothing_is_enforced_until_the_key_is_there(self):
        self.assertFalse(sign_policy.apply(self.policy, self.tmp / "kimios.pub"))
        self.assertEqual(json.loads(self.policy.read_text()), FEDORA_POLICY)

    def test_the_key_requires_kimios_to_be_signed_and_keeps_the_rest(self):
        key = self.tmp / "kimios.pub"
        key.write_text(KEY)
        self.assertTrue(sign_policy.apply(self.policy, key))
        p = json.loads(self.policy.read_text())
        rule = p["transports"]["docker"]["ghcr.io/kiplabat/kimios"]
        self.assertEqual(rule, [{"type": "sigstoreSigned", "keyPath": str(key),
                                 "signedIdentity": {"type": "matchRepository"}}])
        self.assertEqual(p["transports"]["docker"]["registry.access.redhat.com"], [{"type": "signedBy"}])
        self.assertEqual(p["default"], FEDORA_POLICY["default"])           # other images as before

    def test_the_image_reads_signatures_and_the_build_applies_the_policy(self):
        reg = (REPO / "system_files/etc/containers/registries.d/kimios.yaml").read_text()
        self.assertIn("ghcr.io/kiplabat/kimios:", reg)
        self.assertIn("use-sigstore-attachments: true", reg)
        self.assertIn("sign_policy.py", (REPO / "build_files/build.sh").read_text())
        pub = REPO / "system_files/etc/pki/containers/kimios.pub"
        if pub.exists():                                                   # once committed, it must be a key
            self.assertIn("BEGIN PUBLIC KEY", pub.read_text())

    def test_the_workflow_signs_what_it_publishes(self):
        wf = (REPO / ".github/workflows/build.yml").read_text()
        self.assertIn("--digestfile /tmp/digest", wf)
        self.assertIn("cosign sign --yes --recursive --key env://COSIGN_PRIVATE_KEY", wf)
        self.assertIn("cosign-release: v2.4.1", wf)                       # signatures bootc and podman read
        self.assertIn("secrets.SIGNING_SECRET", wf)
        self.assertIn('if [ -z "$COSIGN_PRIVATE_KEY" ]', wf)                # no key yet: published unsigned


class Status(unittest.TestCase):
    def test_os_status_says_whether_updates_are_checked(self):
        loader = importlib.machinery.SourceFileLoader("kimios_mcp_sig", str(REPO / "system_files/usr/libexec/kimios/kimios-mcp"))
        spec = importlib.util.spec_from_loader("kimios_mcp_sig", loader)
        mcp = importlib.util.module_from_spec(spec)
        loader.exec_module(mcp)

        def fake(ref):
            return lambda cmd, **kw: (0, json.dumps({"deployments": [{"booted": True, "container-image-reference": ref,
                                                                    "version": "45.20261123.0"}]}), "")
        with mock.patch.object(mcp, "run", fake("ostree-unverified-registry:ghcr.io/kiplabat/kimios:latest")):
            out = mcp.tool_os_status()
            self.assertFalse(out["deployments"][0]["signature_checked"])
            self.assertIn("--enforce-container-sigpolicy", out["signatures"])
        with mock.patch.object(mcp, "run", fake("ostree-image-signed:docker://ghcr.io/kiplabat/kimios:latest")):
            out = mcp.tool_os_status()
            self.assertTrue(out["deployments"][0]["signature_checked"])
            self.assertNotIn("signatures", out)


if __name__ == "__main__":
    unittest.main()
