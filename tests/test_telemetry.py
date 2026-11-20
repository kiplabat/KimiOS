"""Tests for kimi-telemetry, the KimiOS service that reads what only the system can (GPU busy time, power, fans,
throttling) for KimiLM's Performance window. Against a fake /sys; no root needed."""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "system_files/usr/libexec/kimios/kimi-telemetry"
UNIT = REPO / "system_files/usr/lib/systemd/system/kimi-telemetry.service"

_loader = importlib.machinery.SourceFileLoader("kimi_telemetry", str(SCRIPT))
_spec = importlib.util.spec_from_loader("kimi_telemetry", _loader)
tel = importlib.util.module_from_spec(_spec)
_loader.exec_module(tel)


def put(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class TelemetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def machine(self):
        r = self.root
        rapl = r / "sys/class/powercap/intel-rapl:0"
        put(rapl / "name", "package-0\n")
        put(rapl / "energy_uj", "1000000\n")
        put(rapl / "max_energy_range_uj", "262143328850\n")
        put(r / "sys/class/powercap/intel-rapl:0:2/name", "uncore\n")
        put(r / "sys/class/powercap/intel-rapl:0:2/energy_uj", "500000\n")
        put(r / "sys/class/powercap/intel-rapl:0:2/max_energy_range_uj", "262143328850\n")
        hw = r / "sys/class/hwmon/hwmon3"
        put(hw / "name", "coretemp\n")
        put(hw / "temp1_label", "Package id 0\n")
        put(hw / "temp1_input", "71000\n")
        put(hw / "temp2_label", "Core 0\n")
        put(hw / "temp2_input", "69000\n")
        put(r / "sys/class/hwmon/hwmon4/name", "thinkpad\n")
        put(r / "sys/class/hwmon/hwmon4/fan1_input", "3100\n")
        for c in ("cpu0", "cpu1"):
            put(r / f"sys/devices/system/cpu/{c}/thermal_throttle/package_throttle_count", "4\n")
        card = r / "sys/class/drm/card0"
        put(card / "gt_act_freq_mhz", "600\n")
        put(card / "gt_RP0_freq_mhz", "1200\n")
        (card / "device").mkdir(parents=True, exist_ok=True)
        amd = r / "sys/class/drm/card1/device"
        put(amd / "gpu_busy_percent", "42\n")
        put(amd / "mem_info_vram_used", str(2 * 2 ** 30))
        put(amd / "mem_info_vram_total", str(8 * 2 ** 30))
        put(amd / "hwmon/hwmon9/temp1_input", "64000\n")
        put(amd / "hwmon/hwmon9/power1_average", "35500000\n")
        sl = r / "sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/kimilm.slice"
        put(sl / "memory.current", str(6 * 2 ** 30))
        put(sl / "memory.high", "max")
        put(sl / "memory.max", str(27 * 2 ** 30))
        bat = r / "sys/class/power_supply/BAT0"
        put(bat / "status", "Discharging\n")
        put(bat / "power_now", "14250000\n")

    def test_power_heat_fans_graphics_and_slice(self):
        self.machine()
        s = tel.Sampler(self.root)
        first = s.sample(now=100.0)
        self.assertNotIn("power_cpu_w", first)                  # power needs two readings
        put(self.root / "sys/class/powercap/intel-rapl:0/energy_uj", "13000000\n")     # +12 J in 2 s
        put(self.root / "sys/class/powercap/intel-rapl:0:2/energy_uj", "4500000\n")    # +4 J
        put(self.root / "sys/devices/system/cpu/cpu1/thermal_throttle/package_throttle_count", "5\n")
        out = s.sample(now=102.0)
        self.assertEqual(out["power_cpu_w"], 6.0)
        self.assertEqual(out["power_gpu_w"], 2.0)
        self.assertEqual(out["temp_cpu"], 71.0)
        self.assertEqual(out["fan_rpm"], 3100)
        self.assertTrue(out["throttled"])
        self.assertEqual(out["power_w"], 14.25)
        self.assertEqual(out["slice_mem_mb"], 6144)
        self.assertEqual(out["slice_max_mb"], 27648)
        self.assertNotIn("slice_high_mb", out)                  # "max" means no limit
        by = {g["card"]: g for g in out["gpus"]}
        self.assertEqual(by["card0"]["clock_pct"], 50.0)         # i915 without its counters: the clock only
        self.assertNotIn("util", by["card0"])
        self.assertEqual(by["card1"]["util"], 42.0)
        self.assertEqual(by["card1"]["mem_used_mb"], 2048)
        self.assertEqual(by["card1"]["temp"], 64.0)
        self.assertEqual(by["card1"]["power_w"], 35.5)
        # the throttle flag stays up for ten seconds, then drops
        self.assertTrue(s.sample(now=105.0)["throttled"])
        self.assertFalse(s.sample(now=120.0)["throttled"])

    def test_energy_counter_wraps(self):
        self.machine()
        put(self.root / "sys/class/powercap/intel-rapl:0/max_energy_range_uj", "10000000\n")
        put(self.root / "sys/class/powercap/intel-rapl:0/energy_uj", "9000000\n")
        r = tel.Rapl(self.root)
        r.watts(0.0)
        put(self.root / "sys/class/powercap/intel-rapl:0/energy_uj", "1000000\n")    # wrapped: 2 J in 1 s
        self.assertEqual(r.watts(1.0)["package"], 2.0)

    def test_an_empty_machine_gives_an_empty_reading(self):
        out = tel.Sampler(self.root).sample(now=1.0)
        self.assertEqual(out["gpus"], [])
        self.assertEqual(set(out) - {"schema", "t", "gpus"}, set())

    def test_written_atomically_and_world_readable(self):
        self.machine()
        out = self.root / "run/kimios/telemetry.json"
        tel.write(out, {"t": 1})
        self.assertEqual(json.loads(out.read_text()), {"t": 1})
        self.assertEqual(oct(out.stat().st_mode & 0o777), "0o644")
        self.assertEqual([p.name for p in out.parent.iterdir()], ["telemetry.json"])

    def test_once_prints_a_reading(self):
        self.machine()
        r = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root), "--once", "--interval", "0.1"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("gpus", json.loads(r.stdout))

    def test_perf_counter_without_a_pmu_is_quiet(self):
        c = tel.PerfCounter(self.root / "nothing", "rcs0-busy")
        self.assertIsNone(c.fd)
        self.assertIsNone(c.busy_percent(1.0))

    def test_service_is_confined(self):
        unit = UNIT.read_text()
        for line in ("CapabilityBoundingSet=CAP_PERFMON", "ProtectSystem=strict", "PrivateNetwork=yes",
                     "RuntimeDirectory=kimios", "NoNewPrivileges=yes"):
            self.assertIn(line, unit)
        self.assertIn("systemctl enable kimi-telemetry.service", (REPO / "build_files/build.sh").read_text())
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main()
