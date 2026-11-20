"""Tests for kimios-mcp, the KimiOS tool server: the protocol over stdio, and the tools against fake files."""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "system_files/usr/libexec/kimios/kimios-mcp"
PLUGIN = REPO / "system_files/usr/share/kimilm/plugins/kimios/plugin.json"

_loader = importlib.machinery.SourceFileLoader("kimios_mcp", str(SCRIPT))
_spec = importlib.util.spec_from_loader("kimios_mcp", _loader)
mcp = importlib.util.module_from_spec(_spec)
_loader.exec_module(mcp)


def put(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class ProtocolTest(unittest.TestCase):
    """The real program over stdio, as KimiLM starts it."""

    def converse(self, *messages):
        tmp = tempfile.mkdtemp()
        put(Path(tmp) / "hardware.json", json.dumps({"machine": {"vendor": "HP"}, "capabilities": ["gpu"],
                                                      "classes": {}, "devices": [{"bus": "usb"}], "unclaimed": []}))
        env = {**os.environ, "KIMIOS_HARDWARE_JSON": str(Path(tmp) / "hardware.json")}
        text = "".join(json.dumps(m) + "\n" for m in messages)
        out = subprocess.run([sys.executable, str(SCRIPT)], input=text, capture_output=True, text=True,
                             timeout=30, env=env).stdout
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def test_initialize_list_and_call(self):
        replies = self.converse(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "machine", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 5, "method": "ping"})
        self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4, 5])          # the notification gets no reply
        init = replies[0]["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        self.assertEqual(init["serverInfo"]["name"], "kimios")
        tools = {t["name"]: t for t in replies[1]["result"]["tools"]}
        self.assertIn("software", tools)
        self.assertTrue(tools["machine"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["app_remove"]["annotations"]["destructiveHint"])
        for t in tools.values():
            self.assertEqual(t["inputSchema"]["type"], "object")
        call = replies[2]["result"]
        self.assertFalse(call["isError"])
        self.assertEqual(call["structuredContent"]["machine"]["vendor"], "HP")
        self.assertEqual(json.loads(call["content"][0]["text"])["devices"], 1)
        self.assertEqual(replies[3]["error"]["code"], -32602)

    def test_bad_json_is_answered_not_fatal(self):
        proc = subprocess.run([sys.executable, str(SCRIPT)], input="not json\n" + json.dumps(
            {"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n", capture_output=True, text=True, timeout=30)
        replies = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["id"], 9)


class ToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_arguments_are_checked(self):
        r = mcp.call("setting_get", {"schema": "x"})
        self.assertTrue(r["isError"])
        self.assertIn("Missing arguments: key", r["content"][0]["text"])
        r = mcp.call("system_status", {"surprise": 1})
        self.assertTrue(r["isError"])

    def test_system_status_reads_the_machine(self):
        r = self.root
        put(r / "proc/meminfo", "MemTotal:       33300000 kB\nMemAvailable:   20000000 kB\nSwapFree: 0 kB\n")
        put(r / "proc/loadavg", "1.50 1.20 0.90 2/900 1234\n")
        put(r / "proc/uptime", "7200.5 100.0\n")
        put(r / "etc/os-release", 'NAME="Fedora Linux"\nPRETTY_NAME="Fedora Linux 45 (Silverblue)"\n')
        put(r / "sys/class/power_supply/BAT0/type", "Battery\n")
        put(r / "sys/class/power_supply/BAT0/capacity", "81\n")
        put(r / "sys/class/power_supply/BAT0/status", "Discharging\n")
        put(r / "sys/class/power_supply/AC/type", "Mains\n")
        put(r / "sys/class/power_supply/AC/online", "0\n")
        put(r / "sys/class/thermal/thermal_zone0/type", "x86_pkg_temp\n")
        put(r / "sys/class/thermal/thermal_zone0/temp", "55000\n")
        put(r / "sys/class/net/wlo1/operstate", "up\n")
        (r / "sys/class/net/wlo1/wireless").mkdir(parents=True)
        uid = os.getuid()
        sl = r / f"sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/kimilm.slice"
        put(sl / "memory.current", str(16 * 1024 ** 3))
        put(sl / "memory.max", str(26 * 1024 ** 3))
        put(sl / "memory.high", "max")
        with mock.patch.object(mcp, "ROOT", r):
            s = mcp.tool_system_status()
        self.assertEqual(s["os"], "Fedora Linux 45 (Silverblue)")
        self.assertAlmostEqual(s["memory"]["total_gb"], 31.76, places=1)
        self.assertEqual(s["load_average"], [1.5, 1.2, 0.9])
        self.assertEqual(s["uptime_hours"], 2.0)
        self.assertEqual(s["power"][0], {"name": "AC", "type": "mains", "online": False})
        self.assertEqual(s["power"][1]["percent"], 81)
        self.assertEqual(s["temperatures"][0]["celsius"], 55.0)
        self.assertEqual(s["network"], [{"name": "wlo1", "state": "up", "wireless": True}])
        self.assertEqual(s["kimilm_memory"], {"memory_gb": 16.0, "high_gb": None, "max_gb": 26.0})

    def test_software_registry_reads_desktop_entries(self):
        apps = self.root / "share/applications"
        put(apps / "org.gnome.TextEditor.desktop", "[Desktop Entry]\nType=Application\nName=Text Editor\n"
            "Comment=Edit text files\nCategories=Utility;TextEditor;\nMimeType=text/plain;\nExec=gnome-text-editor\n")
        put(apps / "hidden.desktop", "[Desktop Entry]\nType=Application\nName=Hidden\nNoDisplay=true\n")
        put(apps / "kde/sub.desktop", "[Desktop Entry]\nType=Application\nName=Sub App\nKeywords=spreadsheet;\n")
        put(apps / "broken.desktop", "this is not an ini file")
        entries = mcp.desktop_entries([self.root / "share"])
        ids = {e["id"] for e in entries}
        self.assertEqual(ids, {"org.gnome.TextEditor.desktop", "kde-sub.desktop"})
        editor = next(e for e in entries if e["name"] == "Text Editor")
        self.assertEqual(editor["opens"], ["text/plain"])
        with mock.patch.object(mcp, "desktop_entries", lambda dirs=None: entries):
            found = mcp.tool_software("spreadsheet")
            self.assertEqual([a["name"] for a in found["apps"]], ["Sub App"])
            self.assertEqual(mcp.tool_software("")["matched"], 2)

    def test_settings_outside_the_allow_list_are_refused(self):
        with self.assertRaises(mcp.ToolError) as cm:
            mcp.tool_setting_set("org.gnome.desktop.lockdown", "disable-lock-screen", "true")
        self.assertIn("not a setting agents may change", str(cm.exception))

    def test_setting_set_returns_the_old_value(self):
        calls = []

        def fake_run(cmd, timeout=20, input_text=None):
            calls.append(cmd)
            if cmd[:2] == ["gsettings", "get"]:
                return 0, ("'prefer-dark'\n" if any(c[:3] == ["gsettings", "set", cmd[2]] for c in calls[:-1])
                           else "'default'\n"), ""
            return 0, "", ""
        with mock.patch.object(mcp, "run", fake_run):
            r = mcp.tool_setting_set("org.gnome.desktop.interface", "color-scheme", "prefer-dark")
        self.assertEqual((r["old"], r["new"]), ("'default'", "'prefer-dark'"))
        self.assertIn(["gsettings", "set", "org.gnome.desktop.interface", "color-scheme", "prefer-dark"], calls)

    def test_drives_and_mount(self):
        lsblk = {"blockdevices": [{"path": "/dev/sda", "type": "disk", "fstype": None, "children": [
            {"path": "/dev/sda5", "label": "Labatt", "size": "156G", "fstype": "ntfs", "mountpoint": None,
             "type": "part", "rm": False}]}]}
        calls = []

        def fake_run(cmd, timeout=20, input_text=None):
            calls.append(cmd)
            if cmd[0] == "lsblk":
                return 0, json.dumps(lsblk), ""
            return 0, "Mounted /dev/sda5 at /run/media/u/Labatt\n", ""
        with mock.patch.object(mcp, "run", fake_run):
            self.assertEqual(mcp.tool_drives()["drives"][0]["label"], "Labatt")
            self.assertIn("Mounted", mcp.tool_mount_drive("Labatt")["result"])
            with self.assertRaises(mcp.ToolError):
                mcp.tool_mount_drive("Nope")
        self.assertIn(["udisksctl", "mount", "-b", "/dev/sda5"], calls)

    def test_notification_text_cannot_change_type(self):
        self.assertEqual(mcp._gv("true"), "'true'")
        self.assertEqual(mcp._gv("it's"), "'it\\'s'")


class PluginTest(unittest.TestCase):
    def test_kimilm_plugin_points_at_the_server(self):
        plugin = json.loads(PLUGIN.read_text())
        self.assertEqual(plugin["id"], "kimios")
        server = plugin["mcpServers"]["KimiOS"]
        self.assertEqual(server["command"], "/usr/libexec/kimios/kimios-mcp")
        self.assertEqual(plugin["version"], mcp.VERSION)


if __name__ == "__main__":
    unittest.main()
