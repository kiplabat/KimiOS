"""KimiOS envelope (0.9.0): drivers, controllers and the guard between them and a device.

Scope §1.5 and §2.2–2.3: what moves, heats, cools or switches power runs inside limits that agents cannot change.
The envelope is a system service (kimi-envelope) under its own user. It alone can open the devices it runs; agents
reach them only through its socket, and whatever they ask for passes the guard.

  Drivers are manifests, not code: for a text protocol, the commands that read the sensors (a regular expression
  with one named group per sensor) and the command template that sets each output. An agent writes one from the
  device's datasheet and its HELP, `driver_test` tries it on the device (reads must parse and be in range; writes
  send only the value the output already has, so a test moves nothing), and `driver_promote` keeps it as a new
  version (older versions stay; `driver_rollback`). No agent-written code ever touches a device.

  The guard applies the datasheet on every tick, whatever is asked: minimum on and off times and starts per hour
  for switched outputs, value range and rate for levels, trips (too cold, too hot), stale or impossible readings.
  A controller that fails, or whose sensors go quiet, is replaced by the device's own controller from its
  datasheet (fallback); without sensors to run that either, every output goes to its safe value.

  Controllers are specs: loops of sensor → output with a set point inside the datasheet's allowed range, on/off
  with a band or PI (a duty cycle for switched outputs), and `also` for outputs a loop needs too (the top of a
  fridge cools only while the compressor runs). Running one is the last step; `controller_check` says what is
  wrong first. Experiments (`experiment`) are steps of set outputs over time, recorded for control_identify.

Modes of a device: default (its datasheet's own controller), custom (a spec), experiment, fallback (custom failed),
safe (no usable readings). Time is device time (simulated devices run faster than the wall clock).

The socket speaks one JSON request per connection: {"op": ..., ...} → {"ok": true, ...} | {"ok": false, "error"}.
Standard library only."""
from __future__ import annotations

import collections
import json
import os
import re
import select
import socket
import socketserver
import sys
import threading
import time
import tty
from pathlib import Path

SIM_DIR = Path(os.environ.get("KIMIOS_SIM_DIR", "/run/kimios-sim"))
RUN_DIR = Path(os.environ.get("KIMIOS_ENVELOPE_DIR", "/run/kimios-envelope"))
STATE_DIR = Path(os.environ.get("KIMIOS_ENVELOPE_STATE", "/var/lib/kimios-envelope"))
SOCKET = RUN_DIR / "envelope.sock"
# 0.10.0: real devices, registered by an administrator (kimios-device register): their datasheets are the person's
# word, never an agent's
REAL_DIR = Path(os.environ.get("KIMIOS_DEVICES_DIR", "/etc/kimios/devices"))
DEV_ROOT = Path(os.environ.get("KIMIOS_DEV_ROOT", "/"))
MAX_EXPERIMENT_S = 6 * 3600          # device seconds
NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


class EnvelopeError(ValueError):
    """Said in words an agent can act on."""


# ================================================================================================ talking to a port
class PortTransport:
    """One command line out, one answer back (until a quiet moment, or END for multi-line answers)."""

    def __init__(self, path, terminator="\n", timeout=2.0):
        self.path, self.terminator, self.timeout = path, terminator, timeout
        self.fd = None
        self.lock = threading.Lock()

    def _open(self):
        if self.fd is None:
            self.fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                tty.setraw(self.fd)
            except Exception:
                pass

    def exchange(self, text, multiline=False):
        with self.lock:
            try:
                self._open()
                while select.select([self.fd], [], [], 0)[0]:          # drop anything stale
                    if not os.read(self.fd, 4096):
                        break
                os.write(self.fd, (text + self.terminator).encode())
                got, ends = b"", time.monotonic() + self.timeout
                while time.monotonic() < ends:
                    r, _, _ = select.select([self.fd], [], [], 0.05)
                    if r:
                        chunk = os.read(self.fd, 4096)
                        got += chunk
                        if not multiline and b"\n" in got:
                            break
                        if multiline and re.search(rb"(^|\n)END\s*$", got):
                            break
                    elif got and not multiline:
                        break
                return got.decode("utf-8", "replace").strip()
            except OSError as exc:
                self.close()
                raise EnvelopeError(f"the device's port did not answer: {exc}") from None

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


class DirectTransport:
    """A simulated device in the same process (tests)."""

    def __init__(self, device):
        self.device = device

    def exchange(self, text, multiline=False):
        return self.device.handle(text)

    def close(self):
        pass


# ================================================================================================ datasheets
def check_datasheet(ds):
    """Problems with a datasheet (a real device's, written by a person; the simulator's are checked too)."""
    errs = []
    if not isinstance(ds, dict):
        return ["a datasheet is a JSON object"]
    if not NAME.match(str(ds.get("id", "")).replace("-", "_")):
        errs.append("id: lower-case letters, digits, - and _ (e.g. kitchen-fridge)")
    sensors, outputs = ds.get("sensors"), ds.get("outputs")
    if not isinstance(sensors, dict) or not sensors:
        errs.append("sensors: at least one, {name: {unit, valid: [low, high], setpoints: [low, high]}}")
        sensors = {}
    if not isinstance(outputs, dict):
        errs.append("outputs: {name: {type: bool|level, ...limits, safe}} ({} for a sensor-only device)")
        outputs = {}
    for k, v in sensors.items():
        if not NAME.match(k):
            errs.append(f"sensor {k!r}: lower-case letters, digits and _")
        rng = (v or {}).get("valid")
        if not (isinstance(rng, list) and len(rng) == 2 and all(isinstance(x, (int, float)) for x in rng)
                and rng[0] < rng[1]):
            errs.append(f"sensor {k}: valid is [low, high], the range a reading can be right in")
        sp = (v or {}).get("setpoints")
        if sp is not None and not (isinstance(sp, list) and len(sp) == 2 and sp[0] <= sp[1]):
            errs.append(f"sensor {k}: setpoints is [low, high]")
    for k, v in outputs.items():
        v = v or {}
        if not NAME.match(k):
            errs.append(f"output {k!r}: lower-case letters, digits and _")
        if v.get("type") not in ("bool", "level"):
            errs.append(f"output {k}: type is bool (on/off) or level (min…max)")
        if "safe" not in v:
            errs.append(f"output {k}: safe is the value it goes to when anything is wrong (usually 0)")
        if v.get("type") == "level":
            try:
                if not float(v.get("min", 0)) <= float(v.get("safe", 0)) <= float(v.get("max", 1)):
                    errs.append(f"output {k}: safe must be between min and max")
            except (TypeError, ValueError):
                errs.append(f"output {k}: min, max and safe are numbers")
        for lim in ("min_on_s", "min_off_s", "max_starts_per_hour", "max_rate_per_s"):
            if lim in v and not (isinstance(v[lim], (int, float)) and v[lim] >= 0):
                errs.append(f"output {k}: {lim} is a number ≥ 0")
    for i, trip in enumerate(ds.get("trips") or []):
        if trip.get("sensor") not in sensors or not (set(trip.get("force") or {}) <= set(outputs)) or \
                not ("below" in trip or "above" in trip):
            errs.append(f"trips[{i}]: {{sensor, below or above, force: {{output: value}}}} naming its own sensors "
                        "and outputs")
    default = ds.get("default") or {"loops": []}
    if not errs and default.get("loops"):
        errs += [f"default: {e}" for e in check_spec(default, ds)]
    if not isinstance(ds.get("stale_s", 60), (int, float)):
        errs.append("stale_s is a number of seconds")
    return errs


def resolve_port(reg, root=None):
    """Where a registered real device is now: /dev/kimios/<id> (made by its udev rule), else its port, else a
    serial port whose USB device matches (vendor, product, serial). None when it is not plugged in."""
    root = Path(root or DEV_ROOT)
    for cand in (root / "dev/kimios" / reg["id"], Path(str(reg.get("port") or "/nonexistent"))):
        if cand.exists():
            return str(cand)
    m = reg.get("match") or {}
    if not m.get("vendor_id"):
        return None
    for tty in sorted((root / "sys/class/tty").glob("tty*")):
        try:
            real = (tty / "device").resolve()
        except OSError:
            continue
        for parent in [real, *real.parents]:
            vid = parent / "idVendor"
            if vid.exists():
                got = {"vendor_id": vid.read_text().strip(),
                       "product_id": (parent / "idProduct").read_text().strip(),
                       "serial": (parent / "serial").read_text().strip() if (parent / "serial").exists() else ""}
                if all(not m.get(k) or str(m[k]).lower() == got[k].lower() for k in got):
                    return str(root / "dev" / tty.name)
                break
    return None


def udev_rule(reg):
    """The rule that gives a registered device's port to the envelope alone (not the signed-in session)."""
    m = reg.get("match") or {}
    if not m.get("vendor_id") or not m.get("product_id"):
        return None
    keys = [f'ATTRS{{idVendor}}=="{m["vendor_id"]}"', f'ATTRS{{idProduct}}=="{m["product_id"]}"']
    if m.get("serial"):
        keys.append(f'ATTRS{{serial}}=="{m["serial"]}"')
    return (f'SUBSYSTEM=="tty", {", ".join(keys)}, OWNER="root", GROUP="kimi-envelope", MODE="0660", '
            f'TAG-="uaccess", SYMLINK+="kimios/{reg["id"]}"')


# ================================================================================================ drivers
class Driver:
    """A manifest made usable: read() → {sensor: value}; write(output, value) → None (or raises)."""

    def __init__(self, manifest, datasheet, transport):
        self.m, self.ds, self.port = manifest, datasheet, transport
        self.reads = []
        for i, r in enumerate(manifest.get("read") or []):
            try:
                self.reads.append((r["command"], re.compile(r["pattern"])))
            except (KeyError, TypeError):
                raise EnvelopeError(f"read[{i}] needs a command and a pattern") from None
            except re.error as exc:
                raise EnvelopeError(f"read[{i}].pattern is not a valid regular expression: {exc}") from None

    def read(self):
        out = {}
        for cmd, pat in self.reads:
            reply = self.port.exchange(cmd)
            m = pat.search(reply)
            if not m:
                continue
            for k, v in m.groupdict().items():
                try:
                    out[k] = float(v)
                except (TypeError, ValueError):
                    out[k] = None
        return out

    def format(self, output, value):
        w = (self.m.get("write") or {}).get(output)
        if not w:
            raise EnvelopeError(f"the driver has no command for {output}")
        spec = self.ds["outputs"][output]
        v = float(value)
        if spec.get("type") == "bool":
            v = 1 if v >= 0.5 else 0
            text = str((w.get("values") or {}).get(str(v), v))
        else:
            scaled = v * float(w.get("scale", 1))
            text = str(int(round(scaled))) if w.get("integer", True) else f"{scaled:g}"
        return w["command"].replace("{value}", text)

    def write(self, output, value):
        w = self.m["write"][output]
        reply = self.port.exchange(self.format(output, value))
        ok = w.get("ok", "^OK")
        if not re.search(ok, reply or ""):
            raise EnvelopeError(f"{output}: the device answered {reply!r}")
        return reply


def check_manifest(manifest, datasheet):
    """Problems with a manifest before it touches the device."""
    errs = []
    if not isinstance(manifest, dict):
        return ["the manifest is a JSON object with read and write"]
    sensors, outputs = datasheet.get("sensors") or {}, datasheet.get("outputs") or {}
    covered = set()
    for i, r in enumerate(manifest.get("read") or []):
        if not isinstance(r, dict) or "command" not in r or "pattern" not in r:
            errs.append(f"read[{i}] needs a command and a pattern")
            continue
        try:
            names = set(re.compile(r["pattern"]).groupindex)
        except re.error as exc:
            errs.append(f"read[{i}].pattern: {exc}")
            continue
        if not names:
            errs.append(f"read[{i}].pattern has no named groups: name each value (?P<{next(iter(sensors), 'name')}>…)")
        for n in names - set(sensors):
            errs.append(f"read[{i}] names {n!r}, which the datasheet does not have; its sensors: {', '.join(sensors)}")
        covered |= names & set(sensors)
    for s in set(sensors) - covered:
        errs.append(f"no read gives the sensor {s!r}")
    writes = manifest.get("write") or {}
    if not isinstance(writes, dict):
        errs.append("write is an object: output name → {command, ok}")
        writes = {}
    for o in set(outputs) - set(writes):
        errs.append(f"no write for the output {o!r}")
    for o, w in writes.items():
        if o not in outputs:
            errs.append(f"write names {o!r}, which the datasheet does not have; its outputs: {', '.join(outputs)}")
        elif not isinstance(w, dict) or "{value}" not in str(w.get("command", "")):
            errs.append(f"write.{o}.command must contain {{value}}, e.g. \"SET {o.upper()} {{value}}\"")
        else:
            try:
                re.compile(w.get("ok", "^OK"))
            except re.error as exc:
                errs.append(f"write.{o}.ok: {exc}")
    return errs


def test_driver(manifest, datasheet, transport, current=None):
    """Try a manifest on the device. Writes send only the value each output has now (or its safe value), so the
    test moves nothing. Returns {passed, checks: [{what, ok, detail}]}."""
    checks = [{"what": "manifest", "ok": False, "detail": e} for e in check_manifest(manifest, datasheet)]
    if checks:
        return {"passed": False, "checks": checks}
    drv = Driver(manifest, datasheet, transport)
    for cmd, pat in drv.reads:
        reply = transport.exchange(cmd)
        m = pat.search(reply)
        if not m:
            checks.append({"what": f"read {cmd!r}", "ok": False,
                           "detail": f"the device answered {reply!r}, which the pattern does not match"})
            continue
        for k, v in m.groupdict().items():
            lo, hi = datasheet["sensors"][k].get("valid", [-1e9, 1e9])
            try:
                val = float(v)
                good = lo <= val <= hi
                checks.append({"what": f"{k} from {cmd!r}", "ok": good,
                               "detail": f"{val:g}" + ("" if good else f", outside the valid range {lo}…{hi}")})
            except (TypeError, ValueError):
                checks.append({"what": f"{k} from {cmd!r}", "ok": False,
                               "detail": f"{v!r} is not a number (the group must match only the number)"})
    for o, spec in (datasheet.get("outputs") or {}).items():
        value = (current or {}).get(o, spec.get("safe", 0))
        try:
            text = drv.format(o, value)
            reply = drv.write(o, value)
            checks.append({"what": f"write {o}", "ok": True, "detail": f"sent {text!r}, answered {reply!r}"})
        except EnvelopeError as exc:
            checks.append({"what": f"write {o}", "ok": False, "detail": str(exc)})
    return {"passed": all(c["ok"] for c in checks), "checks": checks}


# ================================================================================================ controllers
def check_spec(spec, datasheet):
    """Problems with a controller spec against the datasheet."""
    errs = []
    if not isinstance(spec, dict) or not isinstance(spec.get("loops"), list):
        return ["a controller spec has loops: [{name, sensor, output, setpoint, band, action, mode, also}]"]
    sensors, outputs = datasheet.get("sensors") or {}, datasheet.get("outputs") or {}
    for i, lp in enumerate(spec["loops"]):
        name = lp.get("name") or f"loop {i}"
        if lp.get("sensor") not in sensors:
            errs.append(f"{name}: sensor {lp.get('sensor')!r} is not one of {', '.join(sensors)}")
            continue
        if lp.get("output") not in outputs:
            errs.append(f"{name}: output {lp.get('output')!r} is not one of {', '.join(outputs) or 'none'}")
            continue
        for o in lp.get("also") or []:
            if o not in outputs:
                errs.append(f"{name}: also names {o!r}, not an output")
        allowed = sensors[lp["sensor"]].get("setpoints")
        try:
            sp = float(lp.get("setpoint"))
        except (TypeError, ValueError):
            errs.append(f"{name}: setpoint must be a number")
            continue
        if not allowed:
            errs.append(f"{name}: the datasheet allows no set point for {lp['sensor']}")
        elif not allowed[0] <= sp <= allowed[1]:
            errs.append(f"{name}: setpoint {sp:g} is outside what the datasheet allows ({allowed[0]}…{allowed[1]})")
        if lp.get("action") not in ("cool", "heat"):
            errs.append(f"{name}: action is 'cool' (the output lowers the reading) or 'heat'")
        mode = lp.get("mode", "onoff")
        if mode == "onoff":
            try:
                if float(lp.get("band", 1)) <= 0:
                    errs.append(f"{name}: band must be more than 0")
            except (TypeError, ValueError):
                errs.append(f"{name}: band must be a number")
        elif mode == "pi":
            for k in ("kp", "ti_s"):
                try:
                    if float(lp.get(k)) <= 0:
                        raise ValueError
                except (TypeError, ValueError):
                    errs.append(f"{name}: a PI loop needs {k} > 0 (control_tune gives them)")
        else:
            errs.append(f"{name}: mode is 'onoff' or 'pi'")
    try:
        if not 1 <= float(spec.get("period_s", 10)) <= 600:
            errs.append("period_s must be from 1 to 600 device seconds")
    except (TypeError, ValueError):
        errs.append("period_s must be a number")
    return errs


class Controller:
    """Runs a spec: requests for each output from the readings (the guard decides what is applied)."""

    def __init__(self, spec, datasheet):
        self.spec, self.ds = spec, datasheet
        self.demand = {}
        self.integral = {}
        self.pwm_start = {}

    def compute(self, readings, now, dt):
        req = {}
        for i, lp in enumerate(self.spec["loops"]):
            key = lp.get("name") or str(i)
            y = readings.get(lp["sensor"])
            if y is None:
                raise EnvelopeError(f"no reading for {lp['sensor']}")
            sp = float(lp["setpoint"])
            err = (y - sp) if lp["action"] == "cool" else (sp - y)
            if lp.get("mode", "onoff") == "onoff":
                half = float(lp.get("band", 1)) / 2
                if err > half:
                    self.demand[key] = True
                elif err < -half:
                    self.demand[key] = False
                u = 1.0 if self.demand.get(key) else 0.0
            else:
                kp, ti = float(lp["kp"]), float(lp["ti_s"])
                integ = self.integral.get(key, 0.0) + err * dt / ti
                u_raw = kp * (err + integ)
                if 0.0 < u_raw < 1.0 or (u_raw >= 1.0 and err < 0) or (u_raw <= 0.0 and err > 0):
                    self.integral[key] = integ               # no wind-up while saturated
                u = min(1.0, max(0.0, u_raw))
                if (self.ds["outputs"][lp["output"]].get("type") == "bool"):
                    period = float(lp.get("pwm_period_s", 900))
                    start = self.pwm_start.get(key, now)
                    if now - start >= period:
                        start = now
                    self.pwm_start[key] = start
                    u = 1.0 if (now - start) < u * period else 0.0
            req[lp["output"]] = max(req.get(lp["output"], 0.0), u)
            if u > 0.05:
                for o in lp.get("also") or []:
                    req[o] = max(req.get(o, 0.0), 1.0)
        return req


# ================================================================================================ the guard
class Guard:
    """The datasheet, applied to what is asked. Nothing an agent sends gets past it."""

    def __init__(self, datasheet):
        self.ds = datasheet
        self.state = {o: {"value": None, "since": -1e12, "starts": collections.deque()}
                      for o in datasheet.get("outputs") or {}}

    def apply(self, requests, readings, now, dt):
        applied, notes = {}, []
        for o, spec in (self.ds.get("outputs") or {}).items():
            st = self.state[o]
            want = float(requests.get(o, st["value"] if st["value"] is not None else spec.get("safe", 0)))
            tripped = False
            for trip in self.ds.get("trips") or []:
                y = readings.get(trip["sensor"])
                if y is None or o not in trip.get("force", {}):
                    continue
                if ("below" in trip and y < trip["below"]) or ("above" in trip and y > trip["above"]):
                    if want != trip["force"][o]:
                        notes.append(f"trip: {trip.get('about', trip['sensor'])}: {o} forced to {trip['force'][o]}")
                    want, tripped = float(trip["force"][o]), True
            cur = st["value"]
            if tripped:
                pass                    # a trip is applied at once: no minimum time or rate holds it back
            elif spec.get("type") == "bool":
                want = 1.0 if want >= 0.5 else 0.0
                if cur is None:
                    pass
                elif want != cur:
                    held = now - st["since"]
                    while st["starts"] and st["starts"][0] < now - 3600:
                        st["starts"].popleft()
                    if cur == 1 and held < spec.get("min_on_s", 0):
                        notes.append(f"{o} kept on: minimum on time {spec['min_on_s']} s")
                        want = cur
                    elif cur == 0 and held < spec.get("min_off_s", 0):
                        notes.append(f"{o} kept off: minimum off time {spec['min_off_s']} s")
                        want = cur
                    elif want == 1 and len(st["starts"]) >= spec.get("max_starts_per_hour", 1e9):
                        notes.append(f"{o} kept off: at most {spec['max_starts_per_hour']} starts an hour")
                        want = cur
            else:
                lo, hi = float(spec.get("min", 0)), float(spec.get("max", 1))
                if not lo <= want <= hi:
                    notes.append(f"{o} limited to {lo:g}…{hi:g}")
                want = min(hi, max(lo, want))
                rate = spec.get("max_rate_per_s")
                if rate and cur is not None:
                    step = float(rate) * max(dt, 1e-9)
                    if abs(want - cur) > step + 1e-12:
                        want = cur + (step if want > cur else -step)
            if cur is None or want != cur:
                if spec.get("type") == "bool" and want == 1:
                    st["starts"].append(now)
                st["since"] = now
            st["value"] = want
            applied[o] = want
        return applied, notes

    def force(self, values, now):
        """Set outputs without the timing rules (safe values: stopping is always allowed)."""
        for o, v in values.items():
            if o in self.state and self.state[o]["value"] != v:
                self.state[o]["value"], self.state[o]["since"] = v, now


# ================================================================================================ one device's run
class DeviceRun:
    """Ticks one device: read, decide, guard, write, record."""

    HISTORY = 2400

    def __init__(self, datasheet, driver, clock=None):
        self.ds, self.driver = datasheet, driver
        self.speed = float(datasheet.get("speed", 1))
        self.clock = clock or self._clock
        self._t0 = time.monotonic()
        self.guard = Guard(datasheet)
        self.default = Controller(datasheet.get("default") or {"loops": []}, datasheet)
        self.custom = None
        self.custom_by = None
        self.mode = "default"
        self.experiment = None
        self.last_experiment = None
        self.readings, self.last_ok = {}, {}
        self.history = collections.deque(maxlen=self.HISTORY)
        self.events = collections.deque(maxlen=200)
        self._seen = {}
        self.last_tick = None
        self.last_write = {}
        self.lock = threading.RLock()
        self.thread = None
        self.stop = threading.Event()

    def _clock(self):
        return (time.monotonic() - self._t0) * self.speed

    def note(self, text, now, quiet_s=300):
        if now - self._seen.get(text, -1e12) < quiet_s:
            return
        self._seen[text] = now
        self.events.append({"t": round(now, 1), "text": text})

    def period(self):
        spec = (self.custom.spec if self.custom and self.mode == "custom" else self.ds.get("default") or {})
        return float(spec.get("period_s", 10))

    def _read(self, now):
        try:
            got = self.driver.read()
        except Exception as exc:
            got = {}
            self.note(f"reading failed: {exc}", now)
        for k, spec in (self.ds.get("sensors") or {}).items():
            v = got.get(k)
            lo, hi = spec.get("valid", [-1e9, 1e9])
            if v is not None and lo <= v <= hi:
                self.readings[k] = v
                self.last_ok[k] = now
            elif k in got:
                self.note(f"{k}: reading {v!r} not usable", now)
        stale = float(self.ds.get("stale_s", 60))
        return {k: v for k, v in self.readings.items() if now - self.last_ok.get(k, -1e12) <= stale}

    def tick(self, now=None):
        with self.lock:
            now = self.clock() if now is None else now
            dt = (now - self.last_tick) if self.last_tick is not None else self.period()
            self.last_tick = now
            fresh = self._read(now)
            # outputs no loop drives rest at their safe value (a stopped heater is off, not left where it was)
            requests = {o: float(s.get("safe", 0)) for o, s in (self.ds.get("outputs") or {}).items()}
            if self.experiment:
                requests = self._experiment_step(now, fresh)
            elif self.mode == "custom":
                try:
                    requests.update(self.custom.compute(fresh, now, dt))
                except Exception as exc:
                    self.mode = "fallback"
                    self.note(f"the controller stopped ({exc}); the device's own controller took over", now, 0)
            if self.mode in ("default", "fallback", "safe") and not self.experiment:
                try:
                    requests.update(self.default.compute(fresh, now, dt))
                    if self.mode == "safe":
                        self.mode = "fallback" if self.custom else "default"
                        self.note("readings are back: the device's own controller runs again", now, 0)
                except Exception as exc:
                    if self.mode != "safe":
                        self.note(f"no usable readings ({exc}): every output to its safe value", now, 0)
                    self.mode = "safe"
                    safe = {o: float(s.get("safe", 0)) for o, s in (self.ds.get("outputs") or {}).items()}
                    self.guard.force(safe, now)
                    requests = safe
            applied, notes = self.guard.apply(requests, fresh, now, dt)
            for n in notes:
                self.note(n, now)
            for o, v in applied.items():
                last = self.last_write.get(o)
                if last is None or last[0] != v or now - last[1] > 60:
                    try:
                        self.driver.write(o, v)
                        self.last_write[o] = (v, now)
                    except Exception as exc:
                        self.note(f"writing {o} failed: {exc}", now)
            row = {"t": round(now, 1), "mode": "experiment" if self.experiment else self.mode,
                   **{k: fresh.get(k) for k in self.ds.get("sensors") or {}}, **applied}
            self.history.append(row)
            if self.experiment is not None:
                self.experiment["rows"].append(row)
            return row

    # ---------------------------------------------------------------- experiments
    def start_experiment(self, steps, now=None):
        now = self.clock() if now is None else now
        outs = self.ds.get("outputs") or {}
        total = 0.0
        for i, s in enumerate(steps):
            for o in (s.get("set") or {}):
                if o not in outs:
                    raise EnvelopeError(f"step {i}: {o!r} is not an output; outputs: {', '.join(outs)}")
            total += float(s.get("seconds", 0))
        if not steps or total <= 0:
            raise EnvelopeError("an experiment is steps: [{set: {output: value}, seconds}]")
        if total > MAX_EXPERIMENT_S:
            raise EnvelopeError(f"an experiment may last at most {MAX_EXPERIMENT_S} device seconds")
        with self.lock:
            hold = {o: (self.guard.state[o]["value"] if self.guard.state[o]["value"] is not None
                        else float(s.get("safe", 0))) for o, s in outs.items()}
            self.experiment = {"steps": steps, "start": now, "hold": hold, "rows": [], "done": threading.Event(),
                               "total": total}
            self.note(f"experiment started: {len(steps)} step(s), {total:g} s", now, 0)
        return self.experiment

    def _experiment_step(self, now, fresh):
        ex = self.experiment
        elapsed = now - ex["start"]
        acc = 0.0
        for s in ex["steps"]:
            acc += float(s["seconds"])
            ex["hold"].update({o: float(v) for o, v in (s.get("set") or {}).items()})
            if elapsed < acc:
                return dict(ex["hold"])
        ex["done"].set()
        self.last_experiment = ex["rows"]
        self.experiment = None
        self.note("experiment finished", now, 0)
        return dict(ex["hold"])

    # ---------------------------------------------------------------- the clock
    def run(self):
        self.stop.clear()
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as exc:                          # the loop itself must keep going
                self.note(f"tick failed: {exc}", self.clock())
            self.stop.wait(max(0.05, self.period() / self.speed))

    def start(self):
        if not self.thread or not self.thread.is_alive():
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    def status(self):
        with self.lock:
            return {"mode": "experiment" if self.experiment else self.mode, "time": round(self.clock(), 1),
                    "readings": dict(self.readings),
                    "outputs": {o: s["value"] for o, s in self.guard.state.items()},
                    "controller": self.custom.spec if self.custom else None, "controller_by": self.custom_by,
                    "default": self.ds.get("default"), "events": list(self.events)[-20:],
                    "experiment": ({"elapsed": round(self.clock() - self.experiment["start"]),
                                    "total": self.experiment["total"]} if self.experiment else None)}


# ================================================================================================ the service
class Envelope:
    def __init__(self, sim_dir=SIM_DIR, state_dir=STATE_DIR, transport=None, real_dir=None):
        self.sim_dir, self.state = Path(sim_dir), Path(state_dir)
        self.real_dir = Path(real_dir) if real_dir else REAL_DIR
        self.unplugged = {}             # registered real devices that are not attached now: id -> datasheet
        self.invalid = {}               # registrations whose datasheet has problems: file -> problems
        self.devices = {}               # id -> {"datasheet", "port", "transport"}
        self.runs = {}                  # id -> DeviceRun
        self.lock = threading.RLock()
        self.make_transport = transport or (lambda reg: PortTransport(reg["port"]))

    # ---------------------------------------------------------------- devices
    def scan(self):
        with self.lock:
            seen = set()
            regs = []
            for f in sorted(self.sim_dir.glob("sim-*.json")):
                try:
                    regs.append(json.loads(f.read_text()))
                except (OSError, ValueError):
                    continue
            self.unplugged, self.invalid = {}, {}
            for f in sorted(self.real_dir.glob("*.json")) if self.real_dir.is_dir() else []:
                try:
                    reg = json.loads(f.read_text())
                except (OSError, ValueError) as exc:
                    self.invalid[f.name] = [f"not readable: {exc}"]
                    continue
                ds = reg.get("datasheet") or {}
                ds.setdefault("id", reg.get("id"))
                errs = check_datasheet(ds) if reg.get("id") else ["no id"]
                if errs:
                    self.invalid[f.name] = errs
                    continue
                ds.update({"simulated": False, "speed": 1})
                port = resolve_port(reg)
                if not port:
                    self.unplugged[reg["id"]] = ds
                    continue
                regs.append({"id": reg["id"], "port": port, "datasheet": ds, "simulated": False})
            for reg in regs:
                seen.add(reg["id"])
                old = self.devices.get(reg["id"])
                if old and old["port"] == reg["port"]:
                    continue
                if old:
                    old["transport"].close()
                self.devices[reg["id"]] = {"datasheet": reg["datasheet"], "port": reg["port"],
                                           "transport": self.make_transport(reg),
                                           "simulated": reg.get("simulated", True)}
                run = self.runs.pop(reg["id"], None)
                if run:
                    run.stop.set()
                self._start_if_driver(reg["id"])
            for gone in set(self.devices) - seen:
                dev = self.devices.pop(gone)
                dev["transport"].close()
                run = self.runs.pop(gone, None)
                if run:
                    run.stop.set()

    def add(self, device_id, datasheet, transport, simulated=True):
        """A device without registration files (tests)."""
        with self.lock:
            self.devices[device_id] = {"datasheet": datasheet, "port": "(direct)", "transport": transport,
                                       "simulated": simulated}
            self._start_if_driver(device_id, start_thread=False)

    def dev(self, device_id):
        d = self.devices.get(device_id)
        if not d:
            raise EnvelopeError(f"no device {device_id!r}; devices: {', '.join(self.devices) or 'none'}")
        return d

    # ---------------------------------------------------------------- driver store
    def _dir(self, device_id):
        p = self.state / "drivers" / device_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def versions(self, device_id):
        folder = self._dir(device_id)
        out = []
        for f in sorted(folder.glob("v*.json"), key=lambda p: int(p.stem[1:])):
            try:
                rec = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            out.append({"version": int(f.stem[1:]), "promoted": rec.get("promoted"), "by": rec.get("by"),
                        "note": rec.get("note", "")})
        cur = self.current_version(device_id)
        for v in out:
            v["current"] = v["version"] == cur
        return out

    def current_version(self, device_id):
        try:
            return int((self._dir(device_id) / "current").read_text().strip())
        except (OSError, ValueError):
            return None

    def manifest(self, device_id, version=None):
        version = version or self.current_version(device_id)
        if not version:
            return None
        try:
            return json.loads((self._dir(device_id) / f"v{version}.json").read_text())["manifest"]
        except (OSError, ValueError, KeyError):
            return None

    def _start_if_driver(self, device_id, start_thread=True):
        man = self.manifest(device_id)
        if not man or device_id in self.runs:
            return
        d = self.devices[device_id]
        run = DeviceRun(d["datasheet"], Driver(man, d["datasheet"], d["transport"]))
        self.runs[device_id] = run
        if start_thread:
            run.start()

    # ---------------------------------------------------------------- requests
    def handle(self, req):
        op = req.get("op")
        fn = getattr(self, "op_" + str(op), None)
        if not fn:
            return {"ok": False, "error": f"unknown op {op!r}"}
        try:
            return {"ok": True, **(fn(req) or {})}
        except EnvelopeError as exc:
            return {"ok": False, "error": str(exc)}
        except (KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "error": f"bad request: {exc}"}

    def op_devices(self, req):
        out = []
        for did, d in self.devices.items():
            run = self.runs.get(did)
            ds = d["datasheet"]
            out.append({"id": did, "model": ds.get("model"), "kind": ds.get("kind"), "simulated": d["simulated"],
                        "risk": ds.get("risk"), "speed": ds.get("speed"), "driver": self.current_version(did),
                        "mode": (run.status()["mode"] if run else "no driver"), "connected": True})
        for did, ds in self.unplugged.items():
            out.append({"id": did, "model": ds.get("model"), "kind": ds.get("kind"), "simulated": False,
                        "risk": ds.get("risk"), "speed": 1, "driver": self.current_version(did),
                        "mode": "not connected", "connected": False})
        return {"devices": out, "invalid": self.invalid}

    def op_device(self, req):
        did = req["device"]
        d = self.dev(did)
        run = self.runs.get(did)
        return {"device": did, "datasheet": d["datasheet"], "simulated": d["simulated"],
                "driver": {"current": self.current_version(did), "versions": self.versions(did),
                           "manifest": self.manifest(did)},
                "status": run.status() if run else {"mode": "no driver"}}

    def op_probe(self, req):
        d = self.dev(req["device"])
        if not d["simulated"]:
            raise EnvelopeError("raw commands go only to simulated devices; on a real one write a driver from its "
                                "datasheet")
        text = str(req.get("text", "")).strip()
        if not text or len(text) > 200 or "\n" in text:
            raise EnvelopeError("send one command line")
        run = self.runs.get(req["device"])
        if run and run.mode == "custom" and re.search(r"\d", text) and not text.endswith("?"):
            raise EnvelopeError("a controller is running on this device: stop it before sending commands that set "
                                "things")
        return {"reply": d["transport"].exchange(text, multiline=text.upper() == "HELP")}

    def _current_outputs(self, did):
        run = self.runs.get(did)
        return {o: s["value"] for o, s in run.guard.state.items() if s["value"] is not None} if run else {}

    def op_driver_test(self, req):
        did = req["device"]
        d = self.dev(did)
        run = self.runs.get(did)
        lock = run.lock if run else threading.RLock()
        with lock:
            return test_driver(req.get("manifest"), d["datasheet"], d["transport"], self._current_outputs(did))

    def op_driver_promote(self, req):
        did = req["device"]
        d = self.dev(did)
        result = self.op_driver_test(req)
        if not result["passed"]:
            raise EnvelopeError("the driver did not pass its test: "
                                + "; ".join(f"{c['what']}: {c['detail']}" for c in result["checks"] if not c["ok"]))
        folder = self._dir(did)
        version = max([v["version"] for v in self.versions(did)] or [0]) + 1
        (folder / f"v{version}.json").write_text(json.dumps(
            {"manifest": req["manifest"], "promoted": time.strftime("%Y-%m-%d %H:%M:%S"),
             "by": str(req.get("by") or "")[:80], "note": str(req.get("note") or "")[:300], "test": result},
            indent=1))
        (folder / "current").write_text(str(version))
        with self.lock:
            run = self.runs.get(did)
            if run:
                with run.lock:
                    run.driver = Driver(req["manifest"], d["datasheet"], d["transport"])
            else:
                self._start_if_driver(did, start_thread=req.get("_thread", True))
        return {"version": version, "checks": result["checks"]}

    def op_driver_rollback(self, req):
        did = req["device"]
        d = self.dev(did)
        version = int(req["version"])
        man = self.manifest(did, version)
        if not man:
            raise EnvelopeError(f"there is no version {version}")
        (self._dir(did) / "current").write_text(str(version))
        run = self.runs.get(did)
        if run:
            with run.lock:
                run.driver = Driver(man, d["datasheet"], d["transport"])
        return {"version": version}

    def _run(self, did):
        self.dev(did)
        run = self.runs.get(did)
        if not run:
            raise EnvelopeError(f"{did} has no driver yet: write a manifest, test it (driver_test) and promote it")
        return run

    def op_status(self, req):
        return {"status": self._run(req["device"]).status()}

    def op_history(self, req):
        run = self._run(req["device"])
        since = float(req.get("since", -1e12))
        with run.lock:
            rows = [r for r in run.history if r["t"] > since]
        return {"rows": rows[-int(req.get("limit", 2400)):], "time": run.clock()}

    def op_experiment(self, req):
        run = self._run(req["device"])
        if req.get("collect"):                     # the rows of the experiment that ran (or how far it is)
            if run.experiment:
                return {"running": True, "elapsed": round(run.clock() - run.experiment["start"]),
                        "total": run.experiment["total"]}
            if run.last_experiment is None:
                raise EnvelopeError("no experiment has run on this device since the envelope started")
            return {"rows": run.last_experiment, "running": False}
        if run.mode == "custom":
            raise EnvelopeError("a controller is running: stop it first (controller_stop)")
        if run.experiment:
            raise EnvelopeError("an experiment is already running on this device")
        ex = run.start_experiment(req["steps"])
        wall = ex["total"] / run.speed + 30
        if req.get("wait", True):
            if not ex["done"].wait(min(wall, float(req.get("max_wait_s", 900)))):
                return {"running": True, "note": "still running: ask for history later"}
            return {"rows": ex["rows"], "running": False}
        return {"running": True}

    def op_controller_check(self, req):
        d = self.dev(req["device"])
        errs = check_spec(req.get("spec"), d["datasheet"])
        return {"valid": not errs, "problems": errs}

    def op_controller_start(self, req):
        did = req["device"]
        d = self.dev(did)
        run = self._run(did)
        errs = check_spec(req.get("spec"), d["datasheet"])
        if errs:
            raise EnvelopeError("the controller is not valid: " + "; ".join(errs))
        if run.experiment:
            raise EnvelopeError("an experiment is running on this device; wait for it")
        with run.lock:
            run.custom = Controller(req["spec"], d["datasheet"])
            run.custom_by = str(req.get("by") or "")[:80]
            run.mode = "custom"
            run.note(f"controller started{' by ' + run.custom_by if run.custom_by else ''}", run.clock(), 0)
        return {"status": run.status()}

    def op_controller_stop(self, req):
        run = self._run(req["device"])
        with run.lock:
            run.custom, run.mode = None, "default"
            run.note("controller stopped: the device's own controller runs", run.clock(), 0)
        return {"status": run.status()}

    # ---------------------------------------------------------------- watchdog and socket
    def watchdog(self, stop):
        while not stop.wait(3):
            try:
                self.scan()
            except Exception as exc:
                print(f"kimi-envelope: scan failed: {exc}", file=sys.stderr, flush=True)
            for did, run in list(self.runs.items()):
                if not run.thread or not run.thread.is_alive():
                    run.note("the device's loop had stopped and was started again", run.clock(), 0)
                    run.start()

    def serve(self, path=SOCKET):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        env = self

        class H(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    req = json.loads(self.rfile.readline(1 << 20) or b"{}")
                    out = env.handle(req)
                except Exception as exc:                  # a bad request must not end the envelope
                    out = {"ok": False, "error": str(exc)[:300]}
                self.wfile.write((json.dumps(out) + "\n").encode())

        server = socketserver.ThreadingUnixStreamServer(str(path), H)
        server.daemon_threads = True
        os.chmod(path, 0o666)        # any local user may ask; the guard decides what reaches a device
        return server


def request(req, path=SOCKET, timeout=1000.0):
    """Ask the envelope (used by kimios-mcp and KimiLM)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect(str(path))
        except OSError as exc:
            raise EnvelopeError(f"the envelope service is not running ({exc}): systemctl status kimi-envelope") \
                from None
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
    out = json.loads(buf or b"{}")
    if not out.get("ok"):
        raise EnvelopeError(out.get("error") or "the envelope refused")
    return out


def main(argv=None):
    env = Envelope()
    stop = threading.Event()
    env.scan()
    threading.Thread(target=env.watchdog, args=(stop,), daemon=True).start()
    server = env.serve()
    print(f"kimi-envelope: {len(env.devices)} device(s); socket {SOCKET}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


if __name__ == "__main__":
    sys.exit(main())
