"""0.14.0: drivers and controllers shared on purpose (kimios_share.py and the driver/controller tools)."""
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "system_files/usr/libexec/kimios"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import kimios_share as SH  # noqa: E402
import test_bench as T  # noqa: E402


def registry(entries):
    """A registry folder as GitHub serves it: the entries and index.json with their sha256."""
    root = Path(tempfile.mkdtemp())
    rows = []
    for e in entries:
        kind = e["kind"]
        rel = f"{kind}s/{e['slug']}/{SH.entry_id(kind, e[kind])}.json"
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(e, indent=2))
        rows.append({"path": rel, "kind": kind, "model": e["model"], "slug": e["slug"], "shape": e["shape"],
                     "simulated": e["simulated"], "note": e["note"], "date": e["date"], "evidence": e["evidence"],
                     "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
    (root / "index.json").write_text(json.dumps({"schema": 1, "entries": rows}))
    return root, rows


def mcp_with(env):
    loader = importlib.machinery.SourceFileLoader("kimios_mcp_share", str(REPO / "system_files/usr/libexec/kimios/kimios-mcp"))
    spec = importlib.util.spec_from_loader("kimios_mcp_share", loader)
    mcp = importlib.util.module_from_spec(spec)
    loader.exec_module(mcp)

    def ask(req, timeout=150.0):
        out = env.handle(dict(req))
        if not out.pop("ok", False):
            raise mcp.ToolError(out.get("error"))
        return out
    mcp._ask = ask
    return mcp


class Entries(unittest.TestCase):
    def test_what_is_shared_names_no_machine(self):
        sim, _ = T.bench("fridge")
        ds = sim.datasheet()
        self.assertIn("SN0001", ds["identity"])
        pub = SH.public_datasheet({**ds, "port": "/dev/ttyUSB0", "usb": "1-7", "speed": 60})
        self.assertNotIn("id", pub)
        self.assertNotIn("port", pub)
        self.assertNotIn("usb", pub)
        self.assertNotIn("speed", pub)
        self.assertEqual(pub["identity"], "KimiOS-Sim,FRIDGE2,,1.2")
        e = SH.make_entry("driver", ds, T.FRIDGE_DRIVER, {"test": "passed"}, "note", simulated=True)
        self.assertEqual(e["shape"], {"sensors": ["door", "t_bottom", "t_top"], "outputs": ["compressor", "damper"]})
        self.assertNotIn("sim-fridge", json.dumps(e))

    def test_sharing_writes_a_file_and_a_link_that_proposes_it(self):
        sim, _ = T.bench("room")
        e = SH.make_entry("driver", sim.datasheet(), T.ROOM_DRIVER, {"test": "passed"}, "heater", simulated=True)
        out = SH.prepare(e, folder=tempfile.mkdtemp())
        self.assertTrue(Path(out["file"]).is_file())
        self.assertRegex(out["path"], r"^drivers/simulated-room-with-an-electric-heater/driver-[0-9a-f]{12}\.json$")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(out["link"]).query)
        self.assertTrue(out["link"].startswith(f"{SH.WEB}/new/main/drivers/simulated-room"))
        self.assertEqual(json.loads(q["value"][0]), e)                   # what the person sees is the file
        self.assertEqual(SH.prepare(e, folder=tempfile.mkdtemp())["path"], out["path"])   # same driver, same file

    def test_only_what_the_index_lists_unchanged_and_fits_is_used(self):
        sim, _ = T.bench("fridge")
        ds = sim.datasheet()
        e = SH.make_entry("driver", ds, T.FRIDGE_DRIVER, {"test": "passed"}, "fridge", simulated=True)
        root, rows = registry([e])
        with mock.patch.dict(os.environ, {"KIMIOS_REGISTRY_URL": str(root)}):
            found = SH.find(ds, "driver")
            self.assertEqual([r["path"] for r in found["entries"]], [rows[0]["path"]])
            self.assertEqual(SH.get(rows[0]["path"], ds)["driver"], T.FRIDGE_DRIVER)
            room, _ = T.bench("room")
            self.assertEqual(SH.find(room.datasheet(), "driver")["entries"], [])
            other = {**ds, "outputs": {"compressor": ds["outputs"]["compressor"]}}
            self.assertIn("why_none", SH.find(other, "driver"))
            with self.assertRaises(SH.ShareError):
                SH.get(rows[0]["path"], other)                            # does not fit this device
            (root / rows[0]["path"]).write_text((root / rows[0]["path"]).read_text().replace("MEAS?", "MEAS!"))
            with self.assertRaisesRegex(SH.ShareError, "does not match"):
                SH.get(rows[0]["path"], ds)                               # changed after review
            with self.assertRaisesRegex(SH.ShareError, "path from find"):
                SH.get("../../etc/passwd", ds)
        with mock.patch.dict(os.environ, {"KIMIOS_REGISTRY_URL": "http://127.0.0.1:9"}):
            with self.assertRaisesRegex(SH.ShareError, "could not be reached"):
                SH.find(ds, "driver")


class Tools(unittest.TestCase):
    def test_a_shared_driver_is_tested_here_before_it_can_be_promoted(self):
        sim, env = T.bench("fridge")
        ds = sim.datasheet()
        good = SH.make_entry("driver", ds, T.FRIDGE_DRIVER, {"test": "passed"}, "works", simulated=True)
        broken = dict(T.FRIDGE_DRIVER, read=[{"command": "MEAS?", "pattern": T.FRIDGE_DRIVER["read"][0]["pattern"]
                                              .replace("T_TOP=", "T_TOP: ")}])
        bad = SH.make_entry("driver", ds, broken, {"test": "passed"}, "claims to work", simulated=True)
        root, rows = registry([good, bad])
        mcp = mcp_with(env)
        with mock.patch.dict(os.environ, {"KIMIOS_REGISTRY_URL": str(root)}):
            self.assertEqual(len(mcp.tool_driver(sim.id, "find")["entries"]), 2)
            got = mcp.tool_driver(sim.id, "get", entry=rows[0]["path"])
            self.assertTrue(got["test"]["passed"])
            self.assertIn("promote", got["next"])
            got = mcp.tool_driver(sim.id, "get", entry=rows[1]["path"])
            self.assertFalse(got["test"]["passed"])
            self.assertIn("do not promote", got["next"])
        self.assertIsNone(mcp.tool_driver(sim.id, "versions")["current"])   # getting never promotes

    def test_sharing_only_what_works_here_now(self):
        sim, env = T.bench("fridge")
        mcp = mcp_with(env)
        with self.assertRaisesRegex(mcp.ToolError, "no driver to share"):
            mcp.tool_driver(sim.id, "share")
        T.promote(env, sim, T.FRIDGE_DRIVER)
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": tempfile.mkdtemp()}):
            out = mcp.tool_driver(sim.id, "share", note="tested on the bench")
            e = json.loads(Path(out["file"]).read_text())
            self.assertEqual(e["driver"], T.FRIDGE_DRIVER)
            self.assertEqual(e["evidence"]["test"], "passed")
            self.assertEqual(e["kimios"], mcp.VERSION)
            with self.assertRaisesRegex(mcp.ToolError, "guard accepts"):
                mcp.tool_controller(sim.id, "share", spec={"period_s": 10, "loops": [
                    {"name": "x", "sensor": "t_bottom", "output": "compressor", "setpoint": -60, "band": 2,
                     "action": "cool"}]})
            out = mcp.tool_controller(sim.id, "share", spec=T.FRIDGE_GOAL,
                                      results={"compressor_starts_per_hour": 2.8, "secret": {"nested": 1}})
            e = json.loads(Path(out["file"]).read_text())
            self.assertEqual(e["evidence"]["results"], {"compressor_starts_per_hour": 2.8})
            root, rows = registry([e])
        with mock.patch.dict(os.environ, {"KIMIOS_REGISTRY_URL": str(root)}):
            got = mcp.tool_controller(sim.id, "get", entry=rows[0]["path"])
            self.assertTrue(got["check"]["valid"])
            self.assertEqual(got["spec"], T.FRIDGE_GOAL)
        self.assertNotEqual(env.handle({"op": "device", "device": sim.id})["status"].get("mode"), "custom")


if __name__ == "__main__":
    unittest.main()
