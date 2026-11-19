"""Tests for kimi-hwprobe against a fake /sys tree."""
import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "system_files/usr/libexec/kimios/kimi-hwprobe"

_loader = importlib.machinery.SourceFileLoader("kimi_hwprobe", str(SCRIPT))
_spec = importlib.util.spec_from_loader("kimi_hwprobe", _loader)
hwprobe = importlib.util.module_from_spec(_spec)
_loader.exec_module(hwprobe)


def put(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def bind(dev: Path, driver: str, root: Path) -> None:
    target = root / "sys/bus/drivers" / driver
    target.mkdir(parents=True, exist_ok=True)
    dev.mkdir(parents=True, exist_ok=True)
    os.symlink(target, dev / "driver")


class FakeMachine:
    """A laptop like the reference machine, plus one unknown USB gadget."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        r = self.root = Path(self.tmp.name)
        put(r / "sys/class/dmi/id/sys_vendor", "HP\n")
        put(r / "sys/class/dmi/id/product_name", "HP EliteBook x360\n")
        put(r / "proc/cpuinfo",
            "processor\t: 0\nmodel name\t: Intel(R) Core(TM) i7-10810U CPU @ 1.10GHz\n\n"
            "processor\t: 1\nmodel name\t: Intel(R) Core(TM) i7-10810U CPU @ 1.10GHz\n")
        put(r / "proc/meminfo", "MemTotal:       33300000 kB\n")

        pci = r / "sys/bus/pci/devices"
        put(pci / "0000:00:02.0/class", "0x030000\n")      # Intel UHD graphics
        put(pci / "0000:00:02.0/vendor", "0x8086\n")
        put(pci / "0000:00:02.0/device", "0x9bca\n")
        bind(pci / "0000:00:02.0", "i915", r)
        put(pci / "0000:00:00.0/class", "0x060000\n")      # host bridge, no driver
        put(pci / "0000:00:12.0/class", "0x118000\n")      # signal processing, no driver
        put(pci / "0000:00:12.0/vendor", "0x8086\n")

        usb = r / "sys/bus/usb/devices"
        put(usb / "usb1/idVendor", "1d6b\n")               # root hub
        put(usb / "1-0:1.0/bInterfaceClass", "09\n")
        bind(usb / "1-0:1.0", "hub", r)
        put(usb / "1-3/idVendor", "046d\n")                 # webcam, driven
        put(usb / "1-3/idProduct", "0825\n")
        put(usb / "1-3:1.0/bInterfaceClass", "0e\n")
        bind(usb / "1-3:1.0", "uvcvideo", r)
        put(usb / "1-4/idVendor", "1234\n")                 # unknown gadget, no driver
        put(usb / "1-4/idProduct", "abcd\n")
        put(usb / "1-4/product", "Mystery Sensor\n")
        put(usb / "1-4:1.0/bInterfaceClass", "ff\n")

        cls = r / "sys/class"
        (cls / "drm/card1").mkdir(parents=True)
        (cls / "drm/card1-eDP-1").mkdir(parents=True)
        (cls / "sound/card0").mkdir(parents=True)
        (cls / "net/wlo1").mkdir(parents=True)
        (cls / "video4linux/video0").mkdir(parents=True)
        (cls / "tty/tty0").mkdir(parents=True)              # virtual console: ignored
        (cls / "tty/ttyACM0/device").mkdir(parents=True)    # real serial port

    def close(self) -> None:
        self.tmp.cleanup()


class HwprobeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.m = FakeMachine()
        self.data = hwprobe.probe(self.m.root)

    def tearDown(self) -> None:
        self.m.close()

    def test_machine(self):
        machine = self.data["machine"]
        self.assertEqual(machine["vendor"], "HP")
        self.assertEqual(machine["cpu"]["threads"], 2)
        self.assertIn("i7-10810U", machine["cpu"]["model"])
        self.assertEqual(machine["memory_bytes"], 33300000 * 1024)

    def test_drivers_read_from_symlinks(self):
        by_addr = {d["address"]: d for d in self.data["devices"]}
        self.assertEqual(by_addr["0000:00:02.0"]["driver"], "i915")
        self.assertEqual(by_addr["0000:00:02.0"]["class"], "display")
        self.assertEqual(by_addr["1-3"]["driver"], "uvcvideo")
        self.assertIsNone(by_addr["1-4"]["driver"])

    def test_capabilities(self):
        caps = self.data["capabilities"]
        for cap in ("gpu", "display", "audio", "network", "camera", "serial"):
            self.assertIn(cap, caps)

    def test_classes_filter_virtual_entries(self):
        classes = self.data["classes"]
        self.assertEqual(classes["drm"], ["card1"])
        self.assertEqual(classes["tty"], ["ttyACM0"])

    def test_unclaimed_queue(self):
        unclaimed = {(d["bus"], d["address"]) for d in self.data["unclaimed"]}
        # The unknown gadget and the driverless PCI function need drivers;
        # hubs and bridges do not.
        self.assertEqual(unclaimed, {("usb", "1-4"), ("pci", "0000:00:12.0")})

    def test_cli_writes_json_atomically(self):
        out = self.m.root / "var/lib/kimios/hardware.json"
        self.assertEqual(hwprobe.main(["--root", str(self.m.root), "--output", str(out)]), 0)
        data = json.loads(out.read_text())
        self.assertEqual(data["schema"], hwprobe.SCHEMA)
        self.assertEqual(oct(out.stat().st_mode & 0o777), "0o644")
        self.assertEqual(list(out.parent.glob(".hardware-*")), [])

    def test_empty_root_does_not_crash(self):
        with tempfile.TemporaryDirectory() as empty:
            data = hwprobe.probe(Path(empty))
        self.assertEqual(data["devices"], [])
        self.assertEqual(data["unclaimed"], [])


if __name__ == "__main__":
    unittest.main()
