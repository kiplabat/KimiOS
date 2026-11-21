"""0.9.0: simulated devices, drivers as manifests, the guard, controllers, and the bench tools end to end."""
import csv
import importlib.machinery
import importlib.util
import json
import os
import statistics
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "system_files/usr/libexec/kimios"
sys.path.insert(0, str(LIB))
import kimios_bench as B  # noqa: E402
import kimios_control as C  # noqa: E402
import kimios_envelope as E  # noqa: E402

FRIDGE_DRIVER = {
    "read": [{"command": "MEAS?",
              "pattern": r"T_TOP=(?P<t_top>-?[\d.]+) T_BOT=(?P<t_bottom>-?[\d.]+) DOOR=(?P<door>[01])"}],
    "write": {"compressor": {"command": "OUTP:COMP {value}", "ok": "^OK"},
              "damper": {"command": "OUTP:DAMP {value}"}},
}
ROOM_DRIVER = {"read": [{"command": "READ", "pattern": r"TEMP (?P<temp>-?[\d.]+) C"}],
               "write": {"heater": {"command": "POWER {value}", "scale": 100}}}
FRIDGE_GOAL = {"period_s": 10, "loops": [
    {"name": "freezer", "sensor": "t_bottom", "output": "compressor", "setpoint": -18, "band": 2, "action": "cool"},
    {"name": "fresh food", "sensor": "t_top", "output": "damper", "setpoint": 4, "band": 1, "action": "cool",
     "also": ["compressor"]}]}


def bench(model="fridge", seed=3):
    sim = B.make(model, speed=60, seed=seed)
    env = E.Envelope(sim_dir="/nonexistent", state_dir=tempfile.mkdtemp())
    env.add(sim.id, sim.datasheet(), E.DirectTransport(sim))
    return sim, env


def promote(env, sim, manifest):
    out = env.handle({"op": "driver_promote", "device": sim.id, "manifest": manifest, "_thread": False})
    assert out["ok"], out
    run = env.runs[sim.id]
    run.clock = lambda: sim.t
    return run


def advance(sim, run, hours, period=10):
    rows = []
    for _ in range(int(hours * 3600 / period)):
        sim.advance(period)
        rows.append(run.tick())
    return rows


class SimulatedDevices(unittest.TestCase):
    def test_the_fridge_answers_like_firmware_and_protects_its_compressor(self):
        sim = B.make("fridge", seed=1)
        self.assertIn("OUTP:COMP <0|1>", sim.handle("HELP"))
        self.assertRegex(sim.handle("MEAS?"), r"^T_TOP=-?\d+\.\d T_BOT=-?\d+\.\d DOOR=0$")
        self.assertEqual(sim.handle("OUTP:COMP 1"), "OK")
        sim.advance(60)
        self.assertEqual(sim.handle("OUTP:COMP 0"), "OK")
        sim.advance(60)
        self.assertTrue(sim.handle("OUTP:COMP 1").startswith("ERR lockout"))   # too soon after stopping
        self.assertIn("FAULT=LOCKOUT", sim.handle("STAT?"))
        self.assertTrue(sim.handle("FOO").startswith("ERR unknown command"))
        sim.event("door_open", seconds=60)
        self.assertTrue(sim.handle("MEAS?").endswith("DOOR=1"))
        sim.event("sensor_fault", sensor="t_top", mode="dead", seconds=30)
        self.assertIn("T_TOP=NAN", sim.handle("MEAS?"))

    def test_the_room_heats_with_its_heater_and_the_datasheets_are_complete(self):
        room = B.make("room", seed=1)
        self.assertEqual(room.handle("POWER 100"), "OK")
        room.advance(3 * 3600)
        self.assertGreater(room.truth()["temp"], 22)
        self.assertTrue(room.handle("POWER 150").startswith("ERR"))
        for name in B.MODELS:
            ds = B.make(name).datasheet()
            for k in ("id", "kind", "simulated", "speed", "sensors", "outputs", "default", "stale_s", "risk"):
                self.assertIn(k, ds, (name, k))
            self.assertTrue(all("safe" in o for o in ds["outputs"].values()))


class Drivers(unittest.TestCase):
    def test_a_manifest_is_checked_against_the_datasheet_in_words_an_agent_can_act_on(self):
        ds = B.make("fridge").datasheet()
        errs = E.check_manifest({"read": [{"command": "MEAS?", "pattern": r"T_TOP=(?P<top>[\d.]+)"}],
                                 "write": {"compressor": {"command": "OUTP:COMP"}}}, ds)
        text = " | ".join(errs)
        self.assertIn("'top', which the datasheet does not have", text)
        self.assertIn("no read gives the sensor 't_bottom'", text)
        self.assertIn("no write for the output 'damper'", text)
        self.assertIn("must contain {value}", text)
        self.assertEqual(E.check_manifest(FRIDGE_DRIVER, ds), [])

    def test_a_driver_test_moves_nothing_and_says_what_the_device_answered(self):
        sim, env = bench()
        bad = json.loads(json.dumps(FRIDGE_DRIVER))
        bad["read"][0]["pattern"] = r"TOP=(?P<t_top>[\d.]+) BOT=(?P<t_bottom>[\d.]+) D=(?P<door>\d)"
        out = env.handle({"op": "driver_test", "device": sim.id, "manifest": bad})
        self.assertFalse(out["passed"])
        self.assertIn("the device answered 'T_TOP=", out["checks"][0]["detail"])
        self.assertEqual(sim.comp, 0)
        refused = env.handle({"op": "driver_promote", "device": sim.id, "manifest": bad})
        self.assertFalse(refused["ok"])
        self.assertIn("did not pass its test", refused["error"])
        good = env.handle({"op": "driver_test", "device": sim.id, "manifest": FRIDGE_DRIVER})
        self.assertTrue(good["passed"], good)
        self.assertEqual((sim.comp, sim.damp), (0, 0))                  # written its current (safe) value only

    def test_drivers_are_versioned_and_can_be_rolled_back(self):
        sim, env = bench()
        promote(env, sim, FRIDGE_DRIVER)
        second = json.loads(json.dumps(FRIDGE_DRIVER))
        second["read"].append({"command": "STAT?", "pattern": r"FAULT=(?P<fault>\w+)"})
        self.assertFalse(env.handle({"op": "driver_promote", "device": sim.id, "manifest": second})["ok"])
        second["read"].pop()
        second["write"]["damper"]["ok"] = "OK"
        self.assertEqual(env.handle({"op": "driver_promote", "device": sim.id, "manifest": second})["version"], 2)
        info = env.handle({"op": "device", "device": sim.id})["driver"]
        self.assertEqual([v["current"] for v in info["versions"]], [False, True])
        self.assertTrue(env.handle({"op": "driver_rollback", "device": sim.id, "version": 1})["ok"])
        self.assertEqual(env.current_version(sim.id), 1)


class GuardTest(unittest.TestCase):
    def test_minimum_times_starts_per_hour_levels_and_trips(self):
        ds = B.make("fridge").datasheet()
        g = E.Guard(ds)
        g.apply({"compressor": 1}, {}, 0, 10)
        out, notes = g.apply({"compressor": 0}, {}, 60, 10)
        self.assertEqual(out["compressor"], 1)
        self.assertIn("minimum on time", notes[0])
        g.apply({"compressor": 0}, {}, 130, 10)
        out, notes = g.apply({"compressor": 1}, {}, 200, 10)
        self.assertEqual(out["compressor"], 0)
        self.assertIn("minimum off time", notes[0])
        t = 500
        for _ in range(20):                                             # ask for far too many starts
            g.apply({"compressor": 1}, {}, t, 10)
            g.apply({"compressor": 0}, {}, t + 130, 10)
            t += 440
        self.assertLessEqual(len(g.state["compressor"]["starts"]), 8)
        out, notes = g.apply({"compressor": 1}, {"t_bottom": -31}, t + 1000, 10)
        self.assertEqual(out["compressor"], 0)
        self.assertTrue(any("freezer too cold" in n for n in notes))
        out, notes = g.apply({"damper": 1}, {"t_top": -2}, t + 2000, 10)
        self.assertEqual(out["damper"], 0)                              # fresh food would freeze
        room = E.Guard(B.make("room").datasheet())
        out, notes = room.apply({"heater": 5}, {"temp": 20}, 0, 10)
        self.assertEqual(out["heater"], 1.0)
        out, _ = room.apply({"heater": 0}, {"temp": 20}, 10, 10)
        self.assertAlmostEqual(out["heater"], 0.9)                      # 0.01 per second at most
        out, notes = room.apply({"heater": 1}, {"temp": 28}, 20, 10)
        self.assertEqual(out["heater"], 0)                             # too hot: off, whatever the rate


class Control(unittest.TestCase):
    def test_a_spec_outside_the_datasheet_is_refused(self):
        ds = B.make("fridge").datasheet()
        bad = {"loops": [{"sensor": "t_top", "output": "damper", "setpoint": -5, "action": "cool"},
                         {"sensor": "t_bottom", "output": "heater", "setpoint": -18, "action": "cool"},
                         {"sensor": "t_bottom", "output": "compressor", "setpoint": -18, "action": "chill",
                          "mode": "pi"}]}
        text = " | ".join(E.check_spec(bad, ds))
        self.assertIn("outside what the datasheet allows (0…10)", text)
        self.assertIn("'heater' is not one of", text)
        self.assertIn("action is 'cool'", text)
        self.assertIn("needs kp > 0", text)
        self.assertEqual(E.check_spec(FRIDGE_GOAL, ds), [])

    def test_from_a_step_experiment_to_a_fridge_that_holds_both_compartments(self):
        sim, env = bench()
        run = promote(env, sim, FRIDGE_DRIVER)
        advance(sim, run, 4)                                            # the fridge's own thermostat first
        run.start_experiment([{"set": {"compressor": 0, "damper": 0}, "seconds": 1800},
                              {"set": {"compressor": 1}, "seconds": 7200}], now=sim.t)
        advance(sim, run, 2.6)
        rows = run.last_experiment
        self.assertIsNotNone(rows)
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            w = csv.writer(f)
            w.writerow(["t", "u", "y"])
            for r in rows:
                w.writerow([r["t"] - rows[0]["t"], r["compressor"], r["t_bottom"]])
        model = C.identify(csv_path=f.name)
        self.assertLess(model["K"], 0)                                  # the compressor cools
        self.assertTrue(1000 < model["tau_s"] < 8000, model)
        self.assertEqual(env.handle({"op": "controller_start", "device": sim.id, "spec": FRIDGE_GOAL})["ok"], True)
        tail = advance(sim, run, 12)[-6 * 360:]
        top = [r["t_top"] for r in tail]
        bottom = [r["t_bottom"] for r in tail]
        self.assertAlmostEqual(statistics.mean(top), 4, delta=0.4)
        self.assertAlmostEqual(statistics.mean(bottom), -18, delta=0.6)
        self.assertLess(max(top) - min(top), 2.5)
        starts = sum(1 for a, b in zip(tail, tail[1:]) if b["compressor"] > a["compressor"])
        self.assertLessEqual(starts / 6, 8)
        self.assertFalse(sim.truth()["lockout"])                       # the guard never let it short-cycle

    def test_door_warm_food_and_a_dead_sensor_are_handled(self):
        sim, env = bench()
        run = promote(env, sim, FRIDGE_DRIVER)
        env.handle({"op": "controller_start", "device": sim.id, "spec": FRIDGE_GOAL})
        advance(sim, run, 8)
        sim.event("door_open", seconds=300)
        sim.event("warm_load", compartment="top", degrees=6)
        rows = advance(sim, run, 4)
        self.assertGreater(max(r["t_top"] for r in rows[:30]), 7)
        self.assertAlmostEqual(statistics.mean(r["t_top"] for r in rows[-360:]), 4, delta=0.6)
        sim.event("sensor_fault", sensor="t_bottom", mode="dead", seconds=2700)
        rows = advance(sim, run, 0.5)
        self.assertEqual(rows[-1]["mode"], "safe")                      # nothing to run the default on either
        self.assertEqual(rows[-1]["compressor"], 0)
        text = " ".join(e["text"] for e in run.events)
        self.assertIn("the controller stopped", text)
        self.assertIn("every output to its safe value", text)
        rows = advance(sim, run, 1)
        self.assertEqual(rows[-1]["mode"], "fallback")                  # back to the fridge's own thermostat
        self.assertIn("readings are back", " ".join(e["text"] for e in run.events))

    def test_a_pi_controller_keeps_the_room_warm(self):
        sim, env = bench("room")
        run = promote(env, sim, ROOM_DRIVER)
        spec = {"period_s": 10, "loops": [{"name": "room", "sensor": "temp", "output": "heater", "setpoint": 21,
                                           "action": "heat", "mode": "pi", "kp": 0.5, "ti_s": 1200}]}
        self.assertTrue(env.handle({"op": "controller_start", "device": sim.id, "spec": spec})["ok"])
        tail = advance(sim, run, 6)[-360:]
        self.assertAlmostEqual(statistics.mean(r["temp"] for r in tail), 21, delta=0.3)
        env.handle({"op": "controller_stop", "device": sim.id})
        rows = advance(sim, run, 0.1)
        self.assertEqual(rows[-1]["heater"], 0)                         # stopped: the heater goes off
        self.assertFalse(env.handle({"op": "experiment", "device": "sim-nothing", "steps": []})["ok"])


class Services(unittest.TestCase):
    """The bench and the envelope as their services run them: ports, sockets, and the agents' tools."""

    def test_the_tools_from_help_to_a_running_controller(self):
        tmp = Path(tempfile.mkdtemp())
        old = dict(os.environ)
        try:
            bench_ = B.Bench(["fridge", "air"], speed=600, folder=tmp / "sim")
            bench_.start()
            ctl = bench_.serve_control()
            threading.Thread(target=ctl.serve_forever, daemon=True).start()
            env = E.Envelope(sim_dir=tmp / "sim", state_dir=tmp / "state")
            env.scan()
            srv = env.serve(tmp / "env" / "envelope.sock")
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            os.environ.update({"KIMIOS_ENVELOPE_SOCKET": str(tmp / "env" / "envelope.sock"),
                               "KIMIOS_SIM_DIR": str(tmp / "sim"), "XDG_DATA_HOME": str(tmp / "data")})
            loader = importlib.machinery.SourceFileLoader("kimios_mcp_b", str(LIB / "kimios-mcp"))
            spec = importlib.util.spec_from_loader("kimios_mcp_b", loader)
            mcp = importlib.util.module_from_spec(spec)
            loader.exec_module(mcp)
            listing = mcp.tool_bench()
            self.assertEqual({d["id"] for d in listing["devices"]}, {"sim-fridge", "sim-air"})
            self.assertEqual(listing["devices"][0]["mode"], "no driver")
            info = mcp.tool_bench("sim-fridge")
            self.assertIn("No driver yet", info["status"]["next"])
            self.assertIn("OUTP:COMP", mcp.tool_bench("sim-fridge", send="HELP")["reply"])
            with self.assertRaises(mcp.ToolError):
                mcp.tool_bench("sim-fridge", send="OUTP:COMP 1")           # questions only
            self.assertTrue(mcp.tool_driver("sim-fridge", "test", FRIDGE_DRIVER)["passed"])
            self.assertEqual(mcp.tool_driver("sim-fridge", "promote", FRIDGE_DRIVER)["version"], 1)
            out = mcp.tool_bench_experiment("sim-fridge", output="compressor", value=1, seconds=1800, after_s=600)
            if out.get("running"):
                time.sleep(5)
                out = mcp.tool_bench_experiment("sim-fridge", collect=True)
            self.assertTrue(Path(out["csv_path"]).exists())
            self.assertIn("compressor", out["columns"])
            fit = mcp.call("control_identify", {"csv_path": out["csv_path"],
                                                 "columns": {"t": "t", "u": "compressor", "y": "t_bottom"}})
            self.assertFalse(fit.get("isError"), fit)
            self.assertEqual(mcp.tool_controller("sim-fridge", "check", FRIDGE_GOAL)["problems"], [])
            started = mcp.tool_controller("sim-fridge", "start", FRIDGE_GOAL)
            self.assertEqual(started["status"]["mode"], "custom")
            self.assertIn("top door open", mcp.tool_bench_event("sim-fridge", "door_open", {"seconds": 60})["result"])
            time.sleep(1.5)
            st = mcp.tool_bench("sim-fridge")["status"]
            self.assertEqual(st["mode"], "custom")
            self.assertIn("t_top", st["readings"])
            self.assertEqual(mcp.tool_controller("sim-fridge", "stop")["status"]["mode"], "default")
            ann = {n: a for n, (_, _, _, a) in mcp.TOOLS.items()}
            self.assertTrue(ann["controller"]["physicalHint"] and ann["bench_experiment"]["physicalHint"])
            self.assertTrue(ann["bench"]["readOnlyHint"])
            listed = {t["name"]: t for t in mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                      ["result"]["tools"]}
            self.assertEqual(listed["controller"]["_meta"], {"kimios/physical": True})
            self.assertNotIn("physicalHint", listed["controller"]["annotations"])
            self.assertNotIn("_meta", listed["bench"])
            srv.shutdown()
            ctl.shutdown()
            bench_.stop.set()
        finally:
            os.environ.clear()
            os.environ.update(old)


if __name__ == "__main__":
    unittest.main()
