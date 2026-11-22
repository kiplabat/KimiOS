"""0.11.0: a system updated from 0.9.0 keeps its simulated devices and the drivers people promoted.

0.10.0 moved kimi-simbench and kimi-envelope from DynamicUser= to a fixed user; on updated systems the services
could fail before the envelope's socket existed, and KimiLM's Devices showed no simulated devices. The setup unit
makes the user and moves the old store back; the envelope serves its socket whatever else fails and says why."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "system_files/usr/libexec/kimios"
UNITS = REPO / "system_files/usr/lib/systemd/system"
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(REPO / "tests"))
import kimios_bench as B  # noqa: E402
import kimios_envelope as E  # noqa: E402
from test_bench import FRIDGE_DRIVER  # noqa: E402

SETUP = LIB / "kimios-envelope-setup"


def run_setup(var_lib):
    env = {**os.environ, "KIMIOS_VAR_LIB": str(var_lib), "KIMIOS_NO_SYSUSERS": "1"}
    return subprocess.run(["sh", str(SETUP)], env=env, capture_output=True, text=True, check=True)


class Setup(unittest.TestCase):
    def test_the_dynamic_user_store_is_moved_back_with_its_drivers(self):
        root = Path(tempfile.mkdtemp())
        old = root / "private/kimios-envelope/drivers/sim-fridge"
        old.mkdir(parents=True)
        (old / "v1.json").write_text("{}")
        (old / "current").write_text("1")
        (root / "kimios-envelope").symlink_to("private/kimios-envelope")
        run_setup(root)
        store = root / "kimios-envelope"
        self.assertFalse(store.is_symlink())
        self.assertEqual((store / "drivers/sim-fridge/current").read_text(), "1")
        self.assertFalse((root / "private/kimios-envelope").exists())
        run_setup(root)                                   # again: nothing changes, nothing fails
        self.assertTrue((store / "drivers/sim-fridge/v1.json").exists())

    def test_both_stores_are_merged_without_overwriting(self):
        root = Path(tempfile.mkdtemp())
        for where, dev, cur in (("private/kimios-envelope", "sim-fridge", "2"), ("private/kimios-envelope", "sim-room", "1"),
                                ("kimios-envelope", "sim-fridge", "3")):
            d = root / where / "drivers" / dev
            d.mkdir(parents=True, exist_ok=True)
            (d / "current").write_text(cur)
        run_setup(root)
        self.assertEqual((root / "kimios-envelope/drivers/sim-fridge/current").read_text(), "3")
        self.assertEqual((root / "kimios-envelope/drivers/sim-room/current").read_text(), "1")

    def test_both_services_wait_for_the_setup(self):
        for name in ("kimi-envelope.service", "kimi-simbench.service"):
            text = (UNITS / name).read_text()
            self.assertRegex(text, r"(?m)^After=.*kimios-envelope-setup\.service")
            self.assertRegex(text, r"(?m)^Wants=.*kimios-envelope-setup\.service")
        self.assertIn("kimios-envelope-setup.service", (REPO / "build_files/build.sh").read_text())


class Resilience(unittest.TestCase):
    def test_a_store_that_cannot_be_written_moves_to_run_and_is_reported(self):
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        tmp = Path(tempfile.mkdtemp())
        locked = tmp / "state"
        locked.mkdir()
        locked.chmod(0o500)
        old_run = E.RUN_DIR
        E.RUN_DIR = tmp / "run"
        try:
            env = E.Envelope(sim_dir=tmp / "sim", state_dir=locked, real_dir=tmp / "none")
            self.assertFalse(env.check_state())
            self.assertEqual(env.state, tmp / "run/state")
            probs = env.handle({"op": "devices"})["problems"]
            self.assertTrue(any("cannot be written" in p for p in probs), probs)
        finally:
            E.RUN_DIR = old_run
            locked.chmod(0o700)

    def test_one_bad_device_does_not_hide_the_others_and_the_reason_is_listed(self):
        tmp = Path(tempfile.mkdtemp())
        bench = B.Bench(["fridge", "room"], speed=600, folder=tmp / "sim")
        bench.start()

        def transport(reg):
            if reg["id"] == "sim-room":
                raise PermissionError(13, "Permission denied")
            return E.PortTransport(reg["port"])

        env = E.Envelope(sim_dir=tmp / "sim", state_dir=tmp / "state", transport=transport, real_dir=tmp / "none")
        env.scan()
        rows = env.handle({"op": "devices"})
        ids = [d["id"] for d in rows["devices"]]
        self.assertIn("sim-fridge", ids)
        self.assertNotIn("sim-room", ids)
        self.assertTrue(any("sim-room" in p and "Permission" in p for p in rows["problems"]), rows["problems"])
        for run in env.runs.values():
            run.stop.set()
        bench.stop.set()

    def test_an_empty_bench_is_explained(self):
        tmp = Path(tempfile.mkdtemp())
        env = E.Envelope(sim_dir=tmp / "sim", state_dir=tmp / "state", real_dir=tmp / "none")
        env.scan()
        probs = env.handle({"op": "devices"})["problems"]
        self.assertTrue(any("kimi-simbench" in p for p in probs), probs)

    def test_promoted_drivers_survive_a_restart_of_the_envelope(self):
        tmp = Path(tempfile.mkdtemp())
        bench = B.Bench(["fridge"], speed=600, folder=tmp / "sim")
        bench.start()
        env = E.Envelope(sim_dir=tmp / "sim", state_dir=tmp / "state", real_dir=tmp / "none")
        env.scan()
        self.assertEqual(env.handle({"op": "driver_promote", "device": "sim-fridge",
                                     "manifest": FRIDGE_DRIVER})["version"], 1)
        for run in env.runs.values():
            run.stop.set()
        again = E.Envelope(sim_dir=tmp / "sim", state_dir=tmp / "state", real_dir=tmp / "none")
        again.check_state()
        again.scan()
        row = next(d for d in again.handle({"op": "devices"})["devices"] if d["id"] == "sim-fridge")
        self.assertEqual(row["driver"], 1)
        for run in list(env.runs.values()) + list(again.runs.values()):
            run.stop.set()
        bench.stop.set()


if __name__ == "__main__":
    unittest.main()
