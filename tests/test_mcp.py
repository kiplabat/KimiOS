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
        self.assertTrue(tools["snapshot"]["annotations"]["destructiveHint"])      # 0.11.0: restore changes files
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

    def test_performance_measures_the_processor_and_reads_kimi_telemetry(self):
        r = self.root
        stats = iter(["cpu  100 0 100 800 0 0 0 0\n", "cpu  160 0 140 900 0 0 0 0\n"])
        put(r / "proc/meminfo", "MemTotal:       33300000 kB\nMemAvailable:   20000000 kB\n")
        put(r / "proc/loadavg", "2.00 1.00 0.50 2/900 1234\n")
        tel = r / "run/kimios/telemetry.json"
        put(tel, json.dumps({"t": __import__("time").time(), "gpus": [{"card": "card0", "util": 61.5, "source": "pmu"}],
                             "power_cpu_w": 11.2, "fan_rpm": 2900, "temp_cpu": 78.0, "throttled": False}))
        real_read = mcp.read

        def fake_read(path, default=None):
            if str(path).endswith("proc/stat"):
                return next(stats)
            return real_read(path, default)
        with mock.patch.object(mcp, "ROOT", r), mock.patch.object(mcp, "TELEMETRY_JSON", tel), \
                mock.patch.object(mcp, "read", fake_read), mock.patch.object(mcp.time, "sleep", lambda s: None):
            out = mcp.tool_performance()
        self.assertEqual(out["cpu_percent"], 50.0)            # 100 busy of 200 ticks
        self.assertEqual(out["graphics"][0]["util"], 61.5)
        self.assertEqual(out["power_watts"], {"power_cpu_w": 11.2})
        self.assertEqual(out["fan_rpm"], 2900)
        self.assertFalse(out["slowed_by_heat"])
        put(tel, json.dumps({"t": 0}))
        with mock.patch.object(mcp, "ROOT", r), mock.patch.object(mcp, "TELEMETRY_JSON", tel), \
                mock.patch.object(mcp.time, "sleep", lambda s: None):
            self.assertIn("has not written", mcp.tool_performance()["telemetry"]["note"])

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
        reg = mcp._registry()
        with mock.patch.object(mcp, "desktop_entries", lambda dirs=None: entries), \
                mock.patch.object(reg, "CACHE", self.root / "cache/registry.json"):
            found = mcp.tool_software("spreadsheet", kinds=["apps"])
            self.assertEqual([a["name"] for a in found["apps"]], ["Sub App"])
            self.assertEqual(mcp.tool_software("", kinds=["apps"])["matched"], 2)

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

    def test_a_setting_can_be_changed_for_a_while(self):
        calls = []

        def fake_run(cmd, timeout=20, input_text=None):
            calls.append(cmd)
            if cmd[:2] == ["gsettings", "get"]:
                return 0, ("'default'\n" if any(c[:2] == ["gsettings", "set"] for c in calls[:-1]) else "'prefer-dark'\n"), ""
            return 0, "", ""
        with mock.patch.object(mcp, "run", fake_run):
            r = mcp.tool_setting_set("org.gnome.desktop.interface", "color-scheme", "default", for_seconds=60)
        self.assertEqual((r["old"], r["new"]), ("'prefer-dark'", "'default'"))
        self.assertTrue(r["reverts_at"] and r["revert_unit"].startswith("kimios-revert-org-gnome-desktop-interface"))
        timer = next(c for c in calls if c[0] == "systemd-run")
        self.assertIn("--on-active=60s", timer)
        self.assertEqual(timer[-5:], ["gsettings", "set", "org.gnome.desktop.interface", "color-scheme", "'prefer-dark'"])
        with mock.patch.object(mcp, "run", fake_run), self.assertRaises(mcp.ToolError):
            mcp.tool_setting_set("org.gnome.desktop.interface", "color-scheme", "default", for_seconds=-5)

    def test_drives_and_mount(self):
        lsblk = {"blockdevices": [{"path": "/dev/sda", "type": "disk", "fstype": None, "children": [
            {"path": "/dev/sda3", "label": None, "size": "317.6G", "fstype": "crypto_LUKS", "mountpoints": [None],
             "type": "part", "rm": False, "children": [
                 {"path": "/dev/mapper/luks-1", "label": "fedora", "size": "317.6G", "fstype": "btrfs",
                  "fsavail": "292G", "mountpoints": ["/var/home", "/sysroot"], "type": "crypt", "rm": False}]},
            {"path": "/dev/sda5", "label": "Labatt", "size": "156G", "fstype": "ntfs", "mountpoints": [None],
             "type": "part", "rm": False}]}]}
        calls = []

        def fake_run(cmd, timeout=20, input_text=None):
            calls.append(cmd)
            if cmd[0] == "lsblk":
                return 0, json.dumps(lsblk), ""
            return 0, "Mounted /dev/sda5 at /run/media/u/Labatt\n", ""
        with mock.patch.object(mcp, "run", fake_run):
            luks, inner, labatt = mcp.tool_drives()["drives"]
            self.assertEqual((luks["encrypted"], luks["unlocked_as"]), (True, "/dev/mapper/luks-1"))
            self.assertEqual((inner["mounted_at"], inner["also_mounted_at"], inner["free"]),
                             ("/var/home", ["/sysroot"], "292G"))
            self.assertEqual((labatt["label"], labatt["mounted_at"]), ("Labatt", None))
            self.assertIn("Mounted", mcp.tool_mount_drive("Labatt")["result"])
            with self.assertRaises(mcp.ToolError):
                mcp.tool_mount_drive("Nope")
        self.assertIn(["udisksctl", "mount", "-b", "/dev/sda5"], calls)

    def test_notification_text_cannot_change_type(self):
        self.assertEqual(mcp._gv("true"), "'true'")
        self.assertEqual(mcp._gv("it's"), "'it\\'s'")


class DeviceAndAbilitiesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = self.root = Path(self.tmp.name)
        usb = r / "sys/bus/usb/devices"
        put(usb / "1-7/idVendor", "06cb\n")
        put(usb / "1-7/idProduct", "00e9\n")
        put(usb / "1-7:1.0/bInterfaceClass", "ff\n")
        put(usb / "1-7:1.0/modalias", "usb:v06CBp00E9d0000dcFFdsc10dp00icFFisc00ip00in00\n")
        put(usb / "1-3/idVendor", "8087\n")
        put(usb / "1-3/idProduct", "0026\n")
        put(usb / "1-3:1.0/bInterfaceClass", "e0\n")
        put(usb / "1-3:1.0/modalias", "usb:v8087p0026d0002dcE0dsc01dp01icE0isc01ip01in00\n")
        pci = r / "sys/bus/pci/devices/0000:00:14.2"
        put(pci / "vendor", "0x8086\n")
        put(pci / "device", "0x02ef\n")
        put(pci / "class", "0x050000\n")
        put(pci / "modalias", "pci:v00008086d000002EFsv0000103Csd0000876Dbc05sc00i00\n")
        (r / "sys/module/btusb").mkdir(parents=True)
        put(r / "hwdata/usb.ids", "# comment\n06cb  Synaptics, Inc.\n\t00e9  Prometheus Fingerprint Reader\n"
            "8087  Intel Corp.\n\t0026  AX201 Bluetooth\n")
        put(r / "hwdata/pci.ids", "8086  Intel Corporation\n\t02ef  Comet Lake PCH-LP Shared SRAM\n")
        put(r / "modules/modules.alias", "alias usb:v8087p0026d*dc*dsc*dp*ic*isc*ip*in* btusb\n"
            "alias usb:v*p*d*dc*dsc*dp*icE0isc01ip01in* btusb\n")
        self.patches = [mock.patch.object(mcp, "ROOT", r), mock.patch.object(mcp, "HWDATA", r / "hwdata"),
                        mock.patch.object(mcp, "MODULES", r / "modules")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_a_fingerprint_reader_without_a_driver(self):
        d = mcp.tool_device_inspect("1-7")
        self.assertEqual((d["bus"], d["vendor"], d["product"]), ("usb", "Synaptics, Inc.", "Prometheus Fingerprint Reader"))
        self.assertEqual(d["kernel_modules"], [])
        self.assertIn("fprintd", d["next_step"])

    def test_a_device_whose_kernel_driver_is_known_and_loaded(self):
        d = mcp.tool_device_inspect("1-3")
        self.assertEqual(d["kernel_modules"], ["btusb"])
        self.assertEqual(d["loaded_modules"], ["btusb"])

    def test_a_pci_memory_controller_needs_nothing(self):
        d = mcp.tool_device_inspect("0000:00:14.2")
        self.assertEqual(d["bus"], "pci")
        self.assertEqual(d["product"], "Comet Lake PCH-LP Shared SRAM")
        self.assertIn("needs no driver", d["next_step"])

    def test_unknown_address(self):
        self.assertIn("error", mcp.tool_device_inspect("9-9"))

    def test_abilities_come_from_kimilm_measurements(self):
        import sqlite3
        db = self.root / "kimilm.sqlite3"
        con = sqlite3.connect(db)
        con.executescript(
            "CREATE TABLE models (id TEXT, name TEXT, size INTEGER, quant TEXT, bench_json TEXT);"
            "CREATE TABLE eval_runs (model TEXT, created TEXT, version TEXT, status TEXT, summary_json TEXT);")
        con.execute("INSERT INTO models VALUES ('qwen3.5-4b','Qwen3.5 4B',2700000000,'Q4_K_M',"
                    "'{\"prompt_per_second\": 14, \"predicted_per_second\": 3.9}')")
        con.execute("INSERT INTO models VALUES ('qwen3.5-9b','Qwen3.5 9B',5400000000,'Q4_K_M','{}')")
        con.execute("INSERT INTO models VALUES ('gemma','Gemma',9000000000,'Q3_K_M','{}')")
        con.execute("INSERT INTO eval_runs VALUES ('qwen3.5-4b','2026-11-20T05:57','2.22.0','done',"
                    "'{\"passed\": 12, \"total\": 12, \"seconds\": 1440}')")
        con.execute("INSERT INTO eval_runs VALUES ('qwen3.5-9b','2026-11-20T06:22','2.22.0','done',"
                    "'{\"passed\": 12, \"total\": 12, \"seconds\": 3120}')")
        con.commit()
        con.close()
        put(self.root / "proc/meminfo", "MemTotal: 33300000 kB\nMemAvailable: 20000000 kB\n")
        with mock.patch.object(mcp, "KIMILM_DB", db), \
                mock.patch.object(mcp, "tool_machine", lambda section="summary": {"machine": {"vendor": "HP"}}):
            a = mcp.tool_abilities()
        self.assertEqual(a["best_for_agent_work"]["model"], "qwen3.5-4b")        # as accurate, and faster
        self.assertEqual(a["best_for_agent_work"]["minutes"], 24)
        self.assertEqual(a["not_measured_yet"], ["gemma"])
        four = next(m for m in a["local_models"] if m["id"] == "qwen3.5-4b")
        self.assertEqual((four["reads_per_second"], four["writes_per_second"]), (14, 3.9))

    def test_abilities_without_kimilm(self):
        put(self.root / "proc/meminfo", "MemTotal: 1000 kB\n")
        with mock.patch.object(mcp, "KIMILM_DB", self.root / "none.sqlite3"), \
                mock.patch.object(mcp, "tool_machine", lambda section="summary": {}):
            a = mcp.tool_abilities()
        self.assertEqual(a["local_models"], [])
        self.assertIsNone(a["best_for_agent_work"])


class PluginTest(unittest.TestCase):
    def test_kimilm_plugin_points_at_the_server(self):
        plugin = json.loads(PLUGIN.read_text())
        self.assertEqual(plugin["id"], "kimios")
        server = plugin["mcpServers"]["KimiOS"]
        self.assertEqual(server["command"], "/usr/libexec/kimios/kimios-mcp")
        self.assertEqual(plugin["version"], mcp.VERSION)


if __name__ == "__main__":
    unittest.main()


class ControlTest(unittest.TestCase):
    """Skill learning in simulation (Phase 4): a simulated refrigerator, identified, tuned and controlled."""

    @staticmethod
    def fridge(K=-24.0, tau=1800.0, theta=60.0, ambient=20.0, dt=10.0, n=2400, noise=0.02):
        import random
        rnd = random.Random(1)
        y, queue, rows, u = 5.0, [0] * int(theta / dt), [], 0
        for i in range(n):
            if i % 120 == 0:
                u = 1 - u
            queue.append(u)
            applied = queue.pop(0)
            y += dt * (K * applied + ambient - y) / tau
            rows.append([i * dt, u, y + rnd.gauss(0, noise)])
        return rows

    def test_identify_recovers_the_fridge(self):
        r = mcp.call("control_identify", {"samples": self.fridge()})
        self.assertFalse(r["isError"], r)
        m = r["structuredContent"]
        self.assertAlmostEqual(m["K"], -24, delta=1.0)
        self.assertAlmostEqual(m["tau_s"], 1800, delta=90)
        self.assertAlmostEqual(m["theta_s"], 60, delta=15)
        self.assertAlmostEqual(m["offset"], 20, delta=0.5)
        self.assertEqual(m["trust"], "good")

    def test_identify_from_csv_and_bad_data(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "log.csv"
            path.write_text("time,compressor,temp\n" + "".join(f"{t},{u},{y}\n" for t, u, y in self.fridge(n=600)))
            r = mcp.call("control_identify", {"csv_path": str(path), "columns": {"t": "time", "u": "compressor", "y": "temp"}})
            self.assertFalse(r["isError"], r)
            r = mcp.call("control_identify", {"csv_path": str(path)})
            self.assertTrue(r["isError"])
            self.assertIn("no column", r["content"][0]["text"])
        flat = [[i, 1, 5.0] for i in range(50)]
        r = mcp.call("control_identify", {"samples": flat})
        self.assertTrue(r["isError"])
        self.assertIn("never changes", r["content"][0]["text"])

    def test_tune_and_simulate_hold_the_set_point(self):
        g = mcp.call("control_tune", {"K": -24, "tau_s": 1800, "theta_s": 60})["structuredContent"]
        self.assertLess(g["pi"]["Kc"], 0)                    # cooling: more input for a warmer cabinet
        model = {"K": -24, "tau_s": 1800, "theta_s": 60, "offset": 20, "duration_s": 6 * 3600, "y0": 8, "band": 1.0}
        tight = mcp.call("control_simulate", {**model, "controller": {"type": "onoff", "setpoint": 4, "hysteresis": 0.5}})
        loose = mcp.call("control_simulate", {**model, "controller": {"type": "onoff", "setpoint": 4, "hysteresis": 1.0}})
        pi = mcp.call("control_simulate", {**model, "controller": {"type": "pi", "setpoint": 4, "Kc": g["pi"]["Kc"],
                                                                   "Ti_s": g["pi"]["Ti_s"]}})
        t, l, p = (x["structuredContent"] for x in (tight, loose, pi))
        self.assertGreater(t["time_in_band_pct"], 95)
        self.assertLess(l["switch_ons_per_hour"], t["switch_ons_per_hour"])     # less wear, worse holding
        self.assertLess(l["time_in_band_pct"], t["time_in_band_pct"])
        self.assertAlmostEqual(p["final_output"], 4, delta=0.05)
        self.assertAlmostEqual(t["duty_pct"], 100 * (20 - 4) / 24, delta=3)     # energy follows the physics
        self.assertLessEqual(len(t["trace"]), 61)

    def test_minimum_off_time_is_respected(self):
        r = mcp.call("control_simulate", {"K": -24, "tau_s": 1800, "theta_s": 60, "offset": 20, "duration_s": 4 * 3600,
                                          "y0": 4, "min_off_s": 1200,
                                          "controller": {"type": "onoff", "setpoint": 4, "hysteresis": 0.2}})
        self.assertLessEqual(r["structuredContent"]["switch_ons_per_hour"], 3600 / 1200 + 0.5)

    def test_bad_controller_is_explained(self):
        r = mcp.call("control_simulate", {"K": 1, "tau_s": 10, "theta_s": 0, "controller": {"type": "fuzzy"}})
        self.assertTrue(r["isError"])
        self.assertIn("onoff", r["content"][0]["text"])
