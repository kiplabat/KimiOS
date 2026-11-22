"""0.10.0: real devices join the envelope through a person (kimios-device, sudo): a checked datasheet, a udev rule
that gives the port to the envelope alone, and the same drivers, guard and controllers as simulated devices."""
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
sys.path.insert(0, str(REPO / "tests"))
import kimios_bench as B  # noqa: E402
import kimios_envelope as E  # noqa: E402
from test_bench import FRIDGE_DRIVER  # noqa: E402

CLI = LIB / "kimios-device"


def template():
    out = subprocess.run([sys.executable, str(CLI), "template"], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


class Datasheets(unittest.TestCase):
    def test_the_template_and_the_simulators_datasheets_are_valid(self):
        reg = template()
        self.assertEqual(E.check_datasheet({**reg["datasheet"], "id": reg["id"]}), [])
        for name in B.MODELS:
            self.assertEqual(E.check_datasheet(B.make(name).datasheet()), [], name)

    def test_a_bad_datasheet_is_explained(self):
        ds = {"id": "Bad Fridge", "sensors": {"t": {"valid": [5, 1]}},
              "outputs": {"heater": {"type": "level", "min": 0, "max": 1, "safe": 2},
                          "pump": {"type": "on", "min_on_s": -1}},
              "trips": [{"sensor": "nope", "below": 1, "force": {"x": 0}}]}
        text = " | ".join(E.check_datasheet(ds))
        for want in ("id: lower-case", "valid is [low, high]", "safe must be between min and max",
                     "type is bool (on/off) or level", "safe is the value", "min_on_s is a number",
                     "trips[0]"):
            self.assertIn(want, text)


class Ports(unittest.TestCase):
    def test_a_registered_device_is_found_by_its_link_or_its_usb_ids(self):
        root = Path(tempfile.mkdtemp())
        dev = root / "sys/devices/usb1/1-2"
        (dev / "1-2:1.0").mkdir(parents=True)
        for k, v in {"idVendor": "1a86", "idProduct": "7523", "serial": "A1"}.items():
            (dev / k).write_text(v + "\n")
        tty = root / "sys/class/tty/ttyUSB0"
        tty.mkdir(parents=True)
        (tty / "device").symlink_to(dev / "1-2:1.0")
        reg = {"id": "kitchen-fridge", "match": {"vendor_id": "1a86", "product_id": "7523", "serial": "A1"}}
        self.assertEqual(E.resolve_port(reg, root), str(root / "dev/ttyUSB0"))
        self.assertIsNone(E.resolve_port({**reg, "match": {**reg["match"], "serial": "B2"}}, root))
        (root / "dev/kimios").mkdir(parents=True)
        (root / "dev/kimios/kitchen-fridge").write_text("")
        self.assertEqual(E.resolve_port(reg, root), str(root / "dev/kimios/kitchen-fridge"))
        rule = E.udev_rule(reg)
        for want in ('ATTRS{idVendor}=="1a86"', 'ATTRS{serial}=="A1"', 'GROUP="kimi-envelope"', 'MODE="0660"',
                     'TAG-="uaccess"', 'SYMLINK+="kimios/kitchen-fridge"'):
            self.assertIn(want, rule)


class Registering(unittest.TestCase):
    def run_cli(self, env, *args):
        return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True,
                              env={**os.environ, **env})

    def test_register_writes_the_registration_and_the_rule_and_unregister_takes_both_back(self):
        tmp = Path(tempfile.mkdtemp())
        env = {"KIMIOS_DEVICES_DIR": str(tmp / "devices"), "KIMIOS_UDEV_RULES": str(tmp / "71.rules"),
               "KIMIOS_NO_UDEVADM": "1"}
        (tmp / "reg.json").write_text(json.dumps(template()))
        refused = self.run_cli(env, "register", str(tmp / "reg.json"))
        if os.geteuid() != 0:
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("run it with sudo", refused.stderr)
        env["KIMIOS_ALLOW_USER"] = "1"
        bad = template()
        bad["datasheet"]["outputs"]["compressor"].pop("safe")
        (tmp / "bad.json").write_text(json.dumps(bad))
        out = self.run_cli(env, "register", str(tmp / "bad.json"))
        self.assertEqual(out.returncode, 1)
        self.assertIn("Not registered", out.stdout)
        self.assertEqual(self.run_cli(env, "check", str(tmp / "reg.json")).stdout.strip(), "No problems found.")
        out = self.run_cli(env, "register", str(tmp / "reg.json"))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue((tmp / "devices/kitchen-fridge.json").exists())
        self.assertIn('TAG-="uaccess"', (tmp / "71.rules").read_text())
        self.assertIn("not plugged in", self.run_cli(env, "list").stdout)
        self.assertEqual(self.run_cli(env, "unregister", "kitchen-fridge").returncode, 0)
        self.assertFalse((tmp / "devices/kitchen-fridge.json").exists())
        self.assertNotIn("kitchen-fridge", (tmp / "71.rules").read_text())


class RealInTheEnvelope(unittest.TestCase):
    def test_a_registered_device_runs_like_a_simulated_one_but_answers_no_raw_commands(self):
        tmp = Path(tempfile.mkdtemp())
        bench = B.Bench(["fridge"], speed=600, folder=tmp / "sim")     # stands in for a board on a real port
        bench.start()
        port = bench.ports["sim-fridge"][2]
        ds = {k: v for k, v in bench.devices["sim-fridge"].datasheet().items() if k not in ("id", "simulated")}
        real = tmp / "devices"
        real.mkdir()
        (real / "garage-fridge.json").write_text(json.dumps({"id": "garage-fridge", "port": port,
                                                             "datasheet": ds}))
        (real / "cellar-fridge.json").write_text(json.dumps({"id": "cellar-fridge", "port": str(tmp / "gone"),
                                                             "datasheet": ds}))
        (real / "broken.json").write_text(json.dumps({"id": "broken", "port": port, "datasheet": {"sensors": {}}}))
        env = E.Envelope(sim_dir=tmp / "nothing", state_dir=tmp / "state", real_dir=real)
        env.scan()
        rows = env.handle({"op": "devices"})
        by = {d["id"]: d for d in rows["devices"]}
        self.assertFalse(by["garage-fridge"]["simulated"])
        self.assertEqual(by["garage-fridge"]["speed"], 1)
        self.assertEqual(by["cellar-fridge"]["mode"], "not connected")
        self.assertIn("broken.json", rows["invalid"])
        refused = env.handle({"op": "probe", "device": "garage-fridge", "text": "HELP"})
        self.assertFalse(refused["ok"])
        self.assertIn("only to simulated devices", refused["error"])
        self.assertTrue(env.handle({"op": "driver_test", "device": "garage-fridge", "manifest": FRIDGE_DRIVER})
                        ["passed"])
        out = env.handle({"op": "driver_promote", "device": "garage-fridge", "manifest": FRIDGE_DRIVER})
        self.assertEqual(out["version"], 1)
        time.sleep(0.5)
        st = env.handle({"op": "status", "device": "garage-fridge"})["status"]
        self.assertEqual(st["mode"], "default")                              # its own controller until told
        bad = {"loops": [{"name": "fresh", "sensor": "t_top", "output": "damper", "setpoint": 12, "action": "cool"}]}
        self.assertFalse(env.handle({"op": "controller_start", "device": "garage-fridge", "spec": bad})["ok"])
        for run in env.runs.values():
            run.stop.set()
        bench.stop.set()


if __name__ == "__main__":
    unittest.main()
