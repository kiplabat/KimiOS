"""0.11.0: a snapshot before risky actions, and a one-step undo (docs/SCOPE.md, Phase 2).

On btrfs the root service takes whole-home snapshots (here a copy stands in for `btrfs subvolume snapshot`);
elsewhere the person copies the folders an action names. Changes and restores need no privilege."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "system_files/usr/libexec/kimios"
sys.path.insert(0, str(LIB))
import kimios_snapshot as K  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.homes = root / "var-home"
        self.home = self.homes / "amina"
        (self.home / "docs").mkdir(parents=True)
        (self.home / "docs/report.md").write_text("first draft\n")
        (self.home / "docs/data.csv").write_text("a,b\n1,2\n")
        self.saved = {k: os.environ.get(k) for k in ("HOME", "KIMIOS_SNAPSHOT_FAKE_BTRFS")}
        os.environ["HOME"] = str(self.home)
        self.old = (K.HOME_ROOT, K.STORE, K.SOCKET)
        K.HOME_ROOT, K.STORE, K.SOCKET = self.homes, self.homes / ".kimios-snapshots", root / "run/snapshot.sock"

    def tearDown(self):
        K.HOME_ROOT, K.STORE, K.SOCKET = self.old
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def damage(self):
        (self.home / "docs/report.md").write_text("overwritten by a script\n")
        (self.home / "docs/data.csv").unlink()
        (self.home / "docs/new.txt").write_text("made by the agent\n")


class CopySnapshots(Base):
    def test_without_btrfs_the_named_folders_are_copied_and_can_be_put_back(self):
        with self.assertRaises(K.SnapshotError):
            K.take("before a script")                      # no service, no paths: nothing to copy
        with self.assertRaises(K.SnapshotError):
            K.take("x", ["/etc"])                          # never outside the home folder
        snap = K.take("before a script", ["docs"])
        self.assertEqual(snap["kind"], "copy")
        self.damage()
        ch = K.changes("latest", "docs")
        self.assertEqual((ch["added"], ch["changed"], ch["removed"]), (["new.txt"], ["report.md"], ["data.csv"]))
        out = K.restore(snap["id"], "docs")
        self.assertEqual((self.home / "docs/report.md").read_text(), "first draft\n")
        self.assertTrue((self.home / "docs/data.csv").exists())
        self.assertFalse((self.home / "docs/new.txt").exists())
        aside = Path(out["kept_current_as"])
        self.assertEqual((aside / "new.txt").read_text(), "made by the agent\n")     # the undo can be undone
        self.assertEqual([m["id"] for m in K.snapshots()], [snap["id"]])
        with self.assertRaises(K.SnapshotError):
            K.restore(snap["id"], "~")                     # not the whole home folder at once

    def test_one_file_is_put_back_alone(self):
        snap = K.take("", [str(self.home / "docs/report.md")])
        self.damage()
        K.restore(snap["id"], "docs/report.md")
        self.assertEqual((self.home / "docs/report.md").read_text(), "first draft\n")
        self.assertTrue((self.home / "docs/new.txt").exists())              # nothing else is touched


class WholeHome(Base):
    def setUp(self):
        super().setUp()
        os.environ["KIMIOS_SNAPSHOT_FAKE_BTRFS"] = "1"
        self.svc = K.Service(store=K.STORE, root=self.homes)
        self.server = self.svc.serve(K.SOCKET)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_the_whole_home_is_kept_and_any_part_restored(self):
        snap = K.take("before an unattended task")
        self.assertEqual(snap["kind"], "btrfs")
        self.assertTrue(K.take("again")["recent"])           # asked again within two minutes: the same one
        self.damage()
        ch = K.changes("latest", "docs")
        self.assertEqual(ch["counts"], {"added": 1, "changed": 1, "removed": 1})
        K.restore(snap["id"], "docs/data.csv")
        self.assertEqual((self.home / "docs/data.csv").read_text(), "a,b\n1,2\n")
        meta = K.STORE / f"{snap['id']}.json"
        self.assertEqual(oct(meta.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(K.STORE.stat().st_mode & 0o777), "0o711")    # others cannot list the snapshots

    def test_each_person_sees_only_their_own_and_old_ones_go(self):
        mine = self.svc.take(os.getuid(), "a")
        self.assertEqual(self.svc.handle(os.getuid() + 1, {"op": "list"})["snapshots"], [])
        self.assertFalse(self.svc.handle(os.getuid() + 1, {"op": "delete", "id": mine["id"]})["ok"])
        for i in range(K.KEEP + 3):                          # many: the oldest beyond KEEP are deleted
            m = dict(mine, id=f"old-{i:02d}", created=time.time() - 10 * 86400 + i)
            (K.STORE / f"old-{i:02d}.json").write_text(json.dumps(m))
            (K.STORE / f"old-{i:02d}").mkdir()
        self.svc.prune(os.getuid())
        left = [m["id"] for m in self.svc._metas(os.getuid())]
        self.assertLessEqual(len(left), K.KEEP)
        self.assertIn(mine["id"], left)
        self.assertNotIn("old-00", left)


class Interfaces(Base):
    def test_the_command_line_and_the_tool(self):
        env = {**os.environ, "KIMIOS_LIBEXEC": str(LIB), "KIMIOS_HOME_ROOT": str(self.homes),
               "KIMIOS_SNAPSHOT_SOCKET": str(K.SOCKET)}
        run = lambda *a: json.loads(subprocess.run([sys.executable, str(LIB / "kimios-snapshot"), *a],   # noqa: E731
                                                   env=env, capture_output=True, text=True).stdout)
        took = run("take", "--reason", "test", str(self.home / "docs"))
        self.assertTrue(took["ok"], took)
        self.damage()
        self.assertEqual(run("changes", "latest", str(self.home / "docs"))["counts"]["removed"], 1)
        self.assertTrue(run("restore", took["id"], str(self.home / "docs"))["ok"])
        self.assertFalse(run("restore", took["id"], "/etc/passwd")["ok"])
        text = (LIB / "kimios-mcp").read_text()
        self.assertIn('"snapshot": (tool_snapshot', text)
        units = REPO / "system_files/usr/lib/systemd/system/kimi-snapshot.service"
        self.assertIn("ReadWritePaths=-/var/home", units.read_text())
        self.assertIn("kimi-snapshot.service", (REPO / "build_files/build.sh").read_text())


if __name__ == "__main__":
    unittest.main()
