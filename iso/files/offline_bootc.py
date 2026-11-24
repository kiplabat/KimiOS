"""KimiOS installer: install without a network when KimiOS is on the stick.

Anaconda's bootc source says a network is required for every source (network_required returns True), so the
installer waits for one even though the ISO carries KimiOS (containers-storage). This changes exactly that property:
a network is required only when the image has to be fetched. It edits the one block it expects, once, and stops
the build if Anaconda's code has changed, so an upstream change is reviewed instead of patched blindly."""
import glob
import pathlib
import sys

OLD = '''    @property
    def network_required(self):
        """Does the source require a network?

        :return: True or False
        """
        return True
'''
NEW = '''    @property
    def network_required(self):
        """Does the source require a network?

        KimiOS: not when the image is on the installation medium.

        :return: True or False
        """
        ref = self.configuration.sourceImgRef or ""
        return not ref.startswith(("containers-storage:", "oci:", "oci-archive:", "dir:"))
'''

paths = sys.argv[1:] or glob.glob("/usr/lib*/python3*/site-packages/pyanaconda/modules/payloads/source/bootc/bootc.py")
if len(paths) != 1:
    sys.exit(f"offline_bootc: expected one Anaconda bootc source, found {paths}")
src = pathlib.Path(paths[0])
text = src.read_text()
if text.count(OLD) != 1:
    sys.exit(f"offline_bootc: {src} no longer has the expected network_required block: review Anaconda's change")
src.write_text(text.replace(OLD, NEW))
for pyc in (src.parent / "__pycache__").glob("bootc.*.pyc"):
    pyc.unlink()
print(f"offline_bootc: {src}: a network only when the image must be fetched")
