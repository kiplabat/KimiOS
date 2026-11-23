#!/usr/bin/python3
"""KimiOS 0.13.0: require a valid signature on KimiOS images, once the public key is in the image.

Adds to /etc/containers/policy.json, keeping everything else in it:
    transports.docker["ghcr.io/kiplabat/kimios"] = [sigstoreSigned by /etc/pki/containers/kimios.pub]
Without the key file (the repository has no cosign.pub yet) the policy is left as it is: nothing is enforced, so
updates cannot break before images are actually signed. bootc checks the policy on `bootc upgrade` once the system
has been switched with `--enforce-container-sigpolicy` (docs/SCOPE.md).

0.13.1: bootc refuses to enforce while the policy's top-level default is insecureAcceptAnything (Fedora's). So the
default becomes reject, and each transport gets its own "" scope that keeps what Fedora's default allowed (other
images from any registry, local storage, archives, podman build): only KimiOS's scope requires a signature, and
the more specific scope wins over a transport's ""."""
import json
import sys
from pathlib import Path

REPO = "ghcr.io/kiplabat/kimios"
ACCEPT = [{"type": "insecureAcceptAnything"}]
# every transport podman, buildah, skopeo, toolbox and bootc use; each keeps accepting what it did before
TRANSPORTS = ("docker", "docker-daemon", "docker-archive", "oci", "oci-archive", "dir", "containers-storage",
              "tarball", "sif")


def apply(policy_path, key_path):
    key, policy_file = Path(key_path), Path(policy_path)
    if not key.is_file() or "PUBLIC KEY" not in key.read_text():
        print(f"sign_policy: no public key at {key}: KimiOS images are not required to be signed")
        return False
    policy = json.loads(policy_file.read_text()) if policy_file.exists() else {"default": [{"type": "insecureAcceptAnything"}]}
    transports = policy.setdefault("transports", {})
    if policy.get("default") != [{"type": "reject"}]:
        was = policy.get("default") or ACCEPT
        for t in TRANSPORTS:
            transports.setdefault(t, {}).setdefault("", was)
        policy["default"] = [{"type": "reject"}]
    transports.setdefault("docker", {})[REPO] = [
        {"type": "sigstoreSigned", "keyPath": str(key), "signedIdentity": {"type": "matchRepository"}}]
    policy_file.write_text(json.dumps(policy, indent=2) + "\n")
    print(f"sign_policy: {REPO} must be signed by {key}")
    return True


if __name__ == "__main__":
    apply(sys.argv[1] if len(sys.argv) > 1 else "/etc/containers/policy.json",
          sys.argv[2] if len(sys.argv) > 2 else "/etc/pki/containers/kimios.pub")
