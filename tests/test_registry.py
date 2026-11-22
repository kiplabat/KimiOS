"""0.12.0: the software capability registry (docs/SCOPE.md §2.4): what installed software can do, and how to reach
it. The programs it asks (man, rpm, systemctl, busctl, flatpak) are stand-ins here."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "system_files/usr/libexec/kimios"
sys.path.insert(0, str(LIB))
import kimios_registry as R  # noqa: E402

MAN_K = """tar (1)              - an archiving utility
pdftotext (1)        - Portable Document Format (PDF) to text converter (version 24.02.0)
grim (1)             - Grab images from a Wayland compositor
ls (1)               - list directory contents
open (2)             - open and possibly create a file
nmcli (1)            - command-line tool for controlling NetworkManager
"""
RPM = ("/usr/bin/tar\ttar: GNU file archiving program\n/usr/bin/zstd\tzstd: Zstd compression library\n"
       "/usr/share/doc/tar/README\ttar: GNU file archiving program\n")
UNITS = ("bluetooth.service loaded active running Bluetooth service\n"
         "cups.service loaded inactive dead CUPS Scheduler\n")
UNIT_FILES = "bluetooth.service enabled enabled\nsshd.service disabled disabled\ngetty@.service static -\n"
BUS = ("org.freedesktop.DBus 1 dbus-broker root :1.0 - - -\n"
       "org.gnome.Shell.Screenshot 2481 gnome-shell amina :1.20 user@1000.service - -\n"
       ":1.33 999 foo amina :1.33 - - -\n"
       "org.freedesktop.Notifications 2481 gnome-shell amina :1.20 - - -\n")
INTROSPECT = """org.freedesktop.DBus.Peer interface - - -
.Ping method - - -
org.gnome.Shell.Screenshot interface - - -
.Screenshot method bbs bs -
.ScreenshotArea method iiiibs bs -
"""


def fake_run(cmd, timeout=20, env=None):
    joined = " ".join(cmd)
    if cmd[:2] == ["man", "-k"]:
        return 0, MAN_K, ""
    if cmd[0] == "man":
        if cmd[1] == "tar":
            return 0, "NAME\n       tar - an archiving utility\n\nSYNOPSIS\n       tar -c [-f ARCHIVE] [OPTIONS] [FILE...]\n\n" \
                      "OPTIONS\n       -c, --create\n              Create a new archive.\n\nSEE ALSO\n       gzip(1)\n", ""
        return 16, "", "No manual entry"
    if cmd[:2] == ["rpm", "-qa"]:
        return 0, RPM.replace("/usr/bin/", os.environ["PATH"] + "/"), ""
    if cmd[:2] == ["rpm", "-qf"]:
        return 0, "tar\tGNU file archiving program\n", ""
    if cmd[0] == "systemctl" and "list-units" in cmd:
        return (0, UNITS, "") if "--system" in cmd else (0, "pipewire.service loaded active running PipeWire\n", "")
    if cmd[0] == "systemctl" and "list-unit-files" in cmd:
        return (0, UNIT_FILES, "") if "--system" in cmd else (0, "", "")
    if cmd[0] == "systemctl" and "show" in cmd:
        return 0, ("Description=Bluetooth service\nActiveState=active\nSubState=running\n"
                   "ExecStart={ path=/usr/libexec/bluetooth/bluetoothd ; argv[]=/usr/libexec/bluetooth/bluetoothd ; }\n"
                   "Documentation=man:bluetoothd(8)\n"), ""
    if cmd[0] == "busctl" and "list" in cmd:
        return (0, BUS, "") if "--user" in cmd else (0, "org.freedesktop.login1 812 systemd-logind root :1.4 - - -\n", "")
    if cmd[0] == "busctl" and "tree" in cmd:
        if "org.example.Sleeping" in cmd:
            return 1, "", "Unit not running"
        return 0, "/\n/org\n/org/gnome/Shell/Screenshot\n", ""
    if cmd[0] == "busctl" and "introspect" in cmd:
        return (0, INTROSPECT, "") if cmd[-3] == "/org/gnome/Shell/Screenshot" else (0, "", "")
    if cmd[0].endswith("/mystery") and cmd[1] == "--help":
        return 0, "Usage: mystery [--fast] FILE\nMakes mysteries.\n", ""
    raise AssertionError(f"unexpected command: {joined}")


class Registry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        for name in ("tar", "pdftotext", "grim", "zstd", "mystery", "reboot"):
            f = self.bin / name
            f.write_text("#!/bin/sh\n")
            f.chmod(0o755)
        (self.bin / "notes.txt").write_text("not a program")
        share = root / "share"
        put = lambda p, t: (p.parent.mkdir(parents=True, exist_ok=True), p.write_text(t))  # noqa: E731
        put(share / "kimilm/plugins/kimios/plugin.json", json.dumps(
            {"id": "kimios", "name": "KimiOS", "description": "The operating system: hardware, apps, settings",
             "mcpServers": {"KimiOS": {"description": "See and act on this computer"}}}))
        put(share / "dbus-1/services/org.example.Sleeping.service",
            "[D-BUS Service]\nName=org.example.Sleeping\nExec=/usr/libexec/sleepyd --dbus\n")
        self.env = mock.patch.dict(os.environ, {"PATH": str(self.bin), "XDG_DATA_DIRS": str(share),
                                                "XDG_DATA_HOME": str(root / "home-share")})
        self.env.start()
        self.patches = [mock.patch.object(R, "run", fake_run), mock.patch.object(R, "CACHE", root / "cache/reg.json"),
                        mock.patch.object(R.shutil, "which", lambda n: str(self.bin / n) if (self.bin / n).exists() else None)]
        for p in self.patches:
            p.start()
        self.apps = [{"id": "org.gnome.Papers.desktop", "name": "Papers", "about": "Read documents",
                      "categories": ["Office"], "keywords": ["pdf"], "opens": ["application/pdf"], "actions": [],
                      "flatpak": False}]

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_the_index_has_every_kind_with_how_to_reach_it(self):
        idx = R.load(apps=self.apps)
        c = idx["counts"]
        self.assertEqual((c["apps"], c["commands"], c["plugins"]), (1, 6, 1))
        self.assertEqual(c["services"], 4)             # bluetooth, cups, sshd (system, not loaded), pipewire (user)
        by = {(e["kind"], e["id"]): e for e in idx["entries"]}
        self.assertEqual(by[("commands", "tar")]["about"], "an archiving utility")            # the manual wins
        self.assertEqual(by[("commands", "zstd")]["about"], "zstd: Zstd compression library")  # else the package
        self.assertNotIn(("commands", "notes.txt"), by)
        self.assertNotIn(("services", "system:getty@.service"), by)
        self.assertEqual(by[("services", "system:bluetooth.service")]["state"], "active (running)")
        self.assertIn("need an administrator", by[("services", "system:cups.service")]["reach"])
        self.assertTrue(by[("dbus", "session:org.gnome.Shell.Screenshot")]["running"])
        sleeping = by[("dbus", "session:org.example.Sleeping")]
        self.assertFalse(sleeping["running"])
        self.assertEqual(sleeping["program"], "sleepyd")
        self.assertNotIn(("dbus", "session:org.freedesktop.DBus"), by)
        self.assertIn("system:org.freedesktop.login1", {e["id"] for e in idx["entries"] if e["kind"] == "dbus"})
        self.assertEqual(by[("plugins", "kimios")]["servers"], {"KimiOS": "See and act on this computer"})

    def test_words_find_the_right_tool_name_first(self):
        idx = R.load(apps=self.apps)
        found, n = R.search(idx, "pdf")
        self.assertEqual([a["name"] for a in found["apps"]], ["Papers"])
        self.assertEqual([c["id"] for c in found["commands"]], ["pdftotext"])
        found, _ = R.search(idx, "screenshot", kinds=("dbus",))
        self.assertEqual([d["name"] for d in found["dbus"]], ["org.gnome.Shell.Screenshot"])
        found, _ = R.search(idx, "archiving tar")
        self.assertEqual(found["commands"][0]["id"], "tar")
        found, _ = R.search(idx, "compress archive")                          # no entry has both words
        self.assertTrue(found["commands"] and all(c.get("loose") for c in found["commands"]))
        found, _ = R.search(idx, "bluetooth", kinds=("services",))
        self.assertEqual(found["services"][0]["name"], "bluetooth.service")

    def test_the_index_is_rebuilt_only_when_software_changes(self):
        calls = []
        R.load(apps=self.apps)
        with mock.patch.object(R, "build", lambda apps=None: calls.append(1) or {"entries": []}):
            R.load(apps=self.apps)
            self.assertEqual(calls, [])                                       # the cache answers
            new = self.bin / "pandoc"
            new.write_text("#!/bin/sh\n")
            new.chmod(0o755)
            os.utime(self.bin, ns=(1, 2 * 10**18))                            # installing changes the folder
            R.load(apps=self.apps)
            self.assertEqual(calls, [1])

    def test_describe_goes_deeper_and_never_acts(self):
        idx = R.load(apps=self.apps)
        tar = R.describe(idx, "commands", "tar")
        self.assertEqual(tar["package"]["name"], "tar")
        self.assertIn("--create", tar["manual"]["OPTIONS"])
        self.assertNotIn("SEE ALSO", tar["manual"])
        self.assertIn("Usage: mystery", R.describe(idx, "commands", "mystery")["help"])  # no manual: its --help
        self.assertIn("could act", R.describe(idx, "commands", "reboot")["note"])       # never run with --help
        svc = R.describe(idx, "services", "system:bluetooth.service")
        self.assertEqual(svc["ExecStart"], "/usr/libexec/bluetooth/bluetoothd")
        shot = R.describe(idx, "dbus", "session:org.gnome.Shell.Screenshot")
        members = shot["interfaces"]["org.gnome.Shell.Screenshot"]["members"]
        self.assertEqual(members[0], {"name": "Screenshot", "type": "method", "in": "bbs", "out": "bs"})
        self.assertNotIn("org.freedesktop.DBus.Peer", shot["interfaces"])
        sleepy = R.describe(idx, "dbus", "session:org.example.Sleeping")
        self.assertFalse(sleepy["running"])                                    # not started to be described
        with self.assertRaises(LookupError):
            R.describe(idx, "commands", "not-installed")

    def test_the_terminal_command_lists_apps_too(self):
        import contextlib
        import io
        share = Path(os.environ["XDG_DATA_DIRS"])
        (share / "applications").mkdir(parents=True, exist_ok=True)
        (share / "applications/org.gnome.Papers.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=Papers\nComment=Read documents\nExec=papers %U\n"
            "MimeType=application/pdf;\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(R.main(["search", "pdf"]), 0)
        found = json.loads(out.getvalue())
        self.assertEqual([a["name"] for a in found["apps"]], ["Papers"])         # 0.12.1: it listed none
        self.assertEqual(found["commands"][0]["id"], "pdftotext")

    def test_the_tools_in_kimios_mcp(self):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("kimios_mcp_reg", str(LIB / "kimios-mcp"))
        spec = importlib.util.spec_from_loader("kimios_mcp_reg", loader)
        mcp = importlib.util.module_from_spec(spec)
        loader.exec_module(mcp)
        with mock.patch.object(mcp, "desktop_entries", lambda dirs=None: self.apps):
            out = mcp.tool_software("pdf")
            self.assertEqual(out["apps"][0]["name"], "Papers")
            self.assertEqual(out["commands"][0]["id"], "pdftotext")
            self.assertNotIn("commands", mcp.tool_software("pdf", commands=False))   # 0.3.0's flag still works
            self.assertIn("manual", mcp.tool_software(kind="commands", id="tar"))            # one entry in depth
            with self.assertRaises(mcp.ToolError):
                mcp.tool_software(kind="plugins", id="nope")
        self.assertNotIn("software_describe", mcp.TOOLS)          # one tool: KimiLM's tool budget stays the same
        self.assertIn("id", mcp.TOOLS["software"][2]["properties"])


if __name__ == "__main__":
    unittest.main()
