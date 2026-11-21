"""KimiOS simulated device bench (0.9.0): devices that behave like real ones, for building control without hardware.

Each simulated device is what a small board on a serial port would be: a text protocol (with HELP, as many real
instruments have), outputs that move something (a compressor, a damper, a heater), sensors with noise and
delays, and physics underneath that runs faster than real time (`speed`: 60 means one second on the wall is a
minute for the device), so an experiment that takes two hours on a real fridge takes two minutes here.

Every device comes with a datasheet: what each sensor measures (unit, the range a reading can be valid in, the
set points that are allowed) and each output's hard limits (minimum on and off times, starts per hour, value
range, the safe value), the trips that force an output (too cold, too hot), its built-in controller (what the
appliance does on its own) and its risk class. The datasheet is the manufacturer's word: the envelope
(kimios_envelope.py) enforces it whatever a driver or a controller asks for, and agents can read it but not
change it.

Devices here:
  sim-fridge   a two-compartment refrigerator: compressor (on/off) and an air damper (open/closed) between the
               freezer at the bottom and the fresh-food compartment at the top; temperature in each; a door
               switch. The compressor has its own protection: restarted within 3 minutes of stopping, or more
               than 12 times an hour, it locks out for 10 minutes (as real ones do).
  sim-room     a room with an electric heater (0–100 % power) and a thermometer; colder outside.
  sim-air      a temperature and humidity sensor (read only).

Scenario events (the bench's control socket, never the device's port): door_open, warm_load, ambient, sensor_fault.

The service (kimi-simbench) gives each device a pseudo-terminal, readable only by the envelope's user, and
registers it in /run/kimios-sim/<id>.json with its datasheet. Standard library only.
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import socket
import socketserver
import sys
import threading
import time
import tty
from pathlib import Path

SIM_DIR = Path(os.environ.get("KIMIOS_SIM_DIR", "/run/kimios-sim"))
DEFAULT_SPEED = float(os.environ.get("KIMIOS_SIM_SPEED", "60"))


def _q(x, step=0.1):
    return round(round(x / step) * step, 2)


class SimError(ValueError):
    pass


# ================================================================================================ the devices
class SimDevice:
    """A device's firmware and physics. `advance(dt)` moves time on (device seconds); `handle(line)` answers one
    command line, as the firmware would."""
    model = "simulated device"
    identity = "KimiOS-Sim,DEVICE,SN0000,1.0"

    def __init__(self, device_id, speed=DEFAULT_SPEED, seed=None):
        self.id, self.speed = device_id, speed
        self.t = 0.0
        self.rng = random.Random(seed)
        self.faulty = {}                # sensor -> "stuck" | "dead" until device time
        self.lock = threading.RLock()

    def noisy(self, name, value, sigma=0.05):
        mode = self.faulty.get(name)
        if mode and mode[1] > self.t:
            if mode[0] == "dead":
                return None
            if mode[0] == "stuck":
                return mode[2]
        return _q(value + self.rng.gauss(0, sigma))

    def advance(self, dt):
        with self.lock:
            while dt > 1e-9:
                step = min(dt, 2.0)
                self.step(step)
                self.t += step
                dt -= step

    def handle(self, line):
        with self.lock:
            text = line.strip()
            if not text:
                return ""
            up = text.upper()
            if up in ("*IDN?", "ID?"):
                return self.identity
            if up == "HELP":
                return self.help()
            try:
                return self.command(text)
            except SimError as exc:
                return f"ERR {exc}"

    def event(self, name, **args):
        with self.lock:
            if name == "sensor_fault":
                sensor, mode = args.get("sensor"), args.get("mode", "dead")
                seconds = float(args.get("seconds", 600))
                self.faulty[sensor] = (mode, self.t + seconds, self.truth().get(sensor))
                return f"{sensor} is {mode} for {seconds:g} s"
            return self.scenario(name, **args)

    # to be filled by each model
    def step(self, dt): ...
    def help(self): ...
    def command(self, text): ...
    def scenario(self, name, **args): raise SimError(f"no such event {name}")
    def truth(self): return {}
    def datasheet(self): ...


class Fridge(SimDevice):
    """Two compartments. The evaporator sits in the freezer; the damper lets cold air into the top compartment;
    air moves mostly while the compressor (and its fan) runs."""
    model = "simulated two-compartment refrigerator"
    identity = "KimiOS-Sim,FRIDGE2,SN0001,1.2"
    T_COLD = -32.0          # what the evaporator would pull the freezer to
    TAU_F_AMB, TAU_F_COMP, TAU_F_RET = 20000.0, 3000.0, 6000.0
    TAU_R_AMB, TAU_R_DAMP = 15000.0, 1500.0
    TAU_EVAP = 90.0         # the compressor's cooling builds up over a minute and a half

    def __init__(self, device_id="sim-fridge", speed=DEFAULT_SPEED, seed=None):
        super().__init__(device_id, speed, seed)
        self.ambient = 25.0
        self.t_top, self.t_bot = 6.0, -16.0
        self.comp, self.damp = 0, 0
        self.cool = 0.0                 # effective cooling (0..1), lags the compressor
        self.door_until = -1.0
        self.last_stop = -1e9
        self.starts = []
        self.lockout_until = -1.0

    def step(self, dt):
        if self.comp and self.t < self.lockout_until:
            self.comp = 0
        self.cool += (self.comp - self.cool) * min(1.0, dt / self.TAU_EVAP)
        flow = self.damp * (0.15 + 0.85 * self.cool)
        door = self.t < self.door_until
        tau_r_amb = self.TAU_R_AMB / (15.0 if door else 1.0)
        d_bot = ((self.ambient - self.t_bot) / self.TAU_F_AMB + self.cool * (self.T_COLD - self.t_bot) / self.TAU_F_COMP
                 + flow * (self.t_top - self.t_bot) / self.TAU_F_RET)
        d_top = (self.ambient - self.t_top) / tau_r_amb + flow * (self.t_bot - self.t_top) / self.TAU_R_DAMP
        self.t_bot += d_bot * dt
        self.t_top += d_top * dt

    def help(self):
        return ("COMMANDS (one per line, answers end with a newline):\n"
                "*IDN?            identity\n"
                "HELP             this list\n"
                "MEAS?            all readings: T_TOP=<degC> T_BOT=<degC> DOOR=<0|1>\n"
                "MEAS:T_TOP?      top (fresh food) compartment temperature, degC\n"
                "MEAS:T_BOT?      bottom (freezer) compartment temperature, degC\n"
                "OUTP:COMP <0|1>  compressor off/on -> OK, or ERR <reason>\n"
                "OUTP:DAMP <0|1>  air damper closed/open (freezer air into the top) -> OK\n"
                "OUTP?            outputs: COMP=<0|1> DAMP=<0|1>\n"
                "STAT?            FAULT=<NONE|LOCKOUT> TIME=<device seconds>\n"
                "END")

    def _reading(self, name):
        v = {"T_TOP": self.noisy("t_top", self.t_top), "T_BOT": self.noisy("t_bottom", self.t_bot)}[name]
        return "NAN" if v is None else f"{v:.1f}"

    def command(self, text):
        up = text.upper().split()
        if up[0] == "MEAS?":
            return f"T_TOP={self._reading('T_TOP')} T_BOT={self._reading('T_BOT')} DOOR={int(self.t < self.door_until)}"
        if up[0] in ("MEAS:T_TOP?", "MEAS:T_BOT?"):
            return self._reading(up[0][5:-1])
        if up[0] == "OUTP?":
            return f"COMP={self.comp} DAMP={self.damp}"
        if up[0] == "STAT?":
            return f"FAULT={'LOCKOUT' if self.t < self.lockout_until else 'NONE'} TIME={self.t:.0f}"
        if up[0] in ("OUTP:COMP", "OUTP:DAMP") and len(up) == 2:
            if up[1] not in ("0", "1"):
                raise SimError("value must be 0 or 1")
            v = int(up[1])
            if up[0] == "OUTP:DAMP":
                self.damp = v
                return "OK"
            if v == self.comp:
                return "OK"
            if v == 1:
                if self.t < self.lockout_until:
                    raise SimError("lockout")
                self.starts = [s for s in self.starts if s > self.t - 3600]
                if self.t - self.last_stop < 180 or len(self.starts) >= 12:
                    self.lockout_until = self.t + 600       # the compressor's own protection
                    raise SimError("lockout (restarted too soon or too often): 10 minutes")
                self.starts.append(self.t)
            else:
                self.last_stop = self.t
            self.comp = v
            return "OK"
        raise SimError(f"unknown command {text!r}; send HELP")

    def scenario(self, name, **args):
        if name == "door_open":
            seconds = float(args.get("seconds", 120))
            self.door_until = self.t + seconds
            return f"top door open for {seconds:g} s"
        if name == "warm_load":
            where = args.get("compartment", "top")
            degrees = float(args.get("degrees", 4))
            if where == "bottom":
                self.t_bot += degrees
            else:
                self.t_top += degrees
            return f"warm food in the {where}: +{degrees:g} °C"
        if name == "ambient":
            self.ambient = float(args.get("value", 25))
            return f"room at {self.ambient:g} °C"
        raise SimError(f"no such event {name}; events: door_open, warm_load, ambient, sensor_fault")

    def truth(self):
        return {"t_top": round(self.t_top, 3), "t_bottom": round(self.t_bot, 3), "compressor": self.comp,
                "damper": self.damp, "ambient": self.ambient, "lockout": self.t < self.lockout_until}

    def datasheet(self):
        return {
            "id": self.id, "model": self.model, "identity": self.identity, "kind": "appliance", "simulated": True,
            "speed": self.speed, "risk": "physical", "port": "serial (115200, text lines)",
            "about": "Two compartments: freezer at the bottom (the evaporator is there) and fresh food at the top, "
                     "cooled only by freezer air through the damper, and mostly while the compressor runs.",
            "sensors": {
                "t_top": {"unit": "degC", "about": "top (fresh food) compartment", "valid": [-40, 60],
                          "setpoints": [0, 10]},
                "t_bottom": {"unit": "degC", "about": "bottom (freezer) compartment", "valid": [-45, 60],
                             "setpoints": [-25, -12]},
                "door": {"unit": "0/1", "about": "top door open", "valid": [0, 1]},
            },
            "outputs": {
                "compressor": {"type": "bool", "about": "compressor and evaporator fan", "min_on_s": 120,
                               "min_off_s": 300, "max_starts_per_hour": 8, "safe": 0},
                "damper": {"type": "bool", "about": "air damper from the freezer into the top", "min_on_s": 10,
                           "min_off_s": 10, "safe": 0},
            },
            "trips": [{"sensor": "t_bottom", "below": -30, "force": {"compressor": 0},
                       "about": "freezer too cold"}],
            "stale_s": 90,
            "default": {"about": "the appliance's own thermostat", "period_s": 10, "loops": [
                {"name": "freezer", "sensor": "t_bottom", "output": "compressor", "setpoint": -16, "band": 4,
                 "action": "cool"},
                {"name": "fresh food", "sensor": "t_top", "output": "damper", "setpoint": 6, "band": 3,
                 "action": "cool", "also": ["compressor"]}]},
        }


class Room(SimDevice):
    """A room with an electric heater; colder outside. The element heats up over a couple of minutes."""
    model = "simulated room with an electric heater"
    identity = "KimiOS-Sim,ROOMHEAT,SN0002,0.9"

    def __init__(self, device_id="sim-room", speed=DEFAULT_SPEED, seed=None):
        super().__init__(device_id, speed, seed)
        self.outside, self.temp, self.power, self.element = 12.0, 15.0, 0.0, 0.0

    def step(self, dt):
        self.element += (self.power - self.element) * min(1.0, dt / 150.0)
        self.temp += ((self.outside - self.temp) + 14.0 * self.element) / 2400.0 * dt

    def help(self):
        return ("Room heater controller v0.9. Commands:\n"
                "  READ          -> TEMP <degC> C, POWER <percent> %\n"
                "  POWER <0-100> set the heater power in percent -> OK\n"
                "  ID?           identity\n"
                "END")

    def command(self, text):
        parts = text.upper().split()
        if parts[0] == "READ":
            v = self.noisy("temp", self.temp, 0.04)
            return f"TEMP {'NAN' if v is None else f'{v:.2f}'} C, POWER {round(self.power * 100)} %"
        if parts[0] == "POWER" and len(parts) == 2:
            try:
                p = float(parts[1])
            except ValueError:
                raise SimError("power must be a number from 0 to 100") from None
            if not 0 <= p <= 100:
                raise SimError("power must be from 0 to 100")
            self.power = p / 100.0
            return "OK"
        raise SimError(f"unknown command {text!r}; send HELP")

    def scenario(self, name, **args):
        if name in ("ambient", "outside"):
            self.outside = float(args.get("value", 12))
            return f"outside at {self.outside:g} °C"
        if name == "door_open":
            seconds = float(args.get("seconds", 120))
            self.temp -= min(3.0, seconds / 120.0)
            return f"door open for {seconds:g} s"
        raise SimError(f"no such event {name}; events: ambient, door_open, sensor_fault")

    def truth(self):
        return {"temp": round(self.temp, 3), "heater": round(self.power, 3), "outside": self.outside}

    def datasheet(self):
        return {
            "id": self.id, "model": self.model, "identity": self.identity, "kind": "heater", "simulated": True,
            "speed": self.speed, "risk": "physical", "port": "serial (115200, text lines)",
            "about": "An electric heater in a room that loses heat to the outside.",
            "sensors": {"temp": {"unit": "degC", "about": "room temperature", "valid": [-30, 60],
                                 "setpoints": [10, 25]}},
            "outputs": {"heater": {"type": "level", "about": "heater power, 0 to 1", "min": 0, "max": 1,
                                   "max_rate_per_s": 0.01, "safe": 0}},
            "trips": [{"sensor": "temp", "above": 27, "force": {"heater": 0}, "about": "room too hot"}],
            "stale_s": 120,
            "default": {"about": "off unless asked", "period_s": 10, "loops": []},
        }


class Air(SimDevice):
    """A temperature and humidity sensor: nothing to drive."""
    model = "simulated temperature and humidity sensor"
    identity = "KimiOS-Sim,AIRSENSE,SN0003,2.1"

    def step(self, dt):
        pass

    def help(self):
        return "MEAS? -> T=<degC> RH=<percent>\nID? -> identity\nEND"

    def command(self, text):
        if text.upper().split()[0] == "MEAS?":
            day = 2 * math.pi * (self.t % 86400) / 86400
            t = self.noisy("temp", 22 + 3 * math.sin(day))
            rh = self.noisy("humidity", 55 - 10 * math.sin(day), 0.3)
            return f"T={'NAN' if t is None else f'{t:.1f}'} RH={'NAN' if rh is None else f'{rh:.1f}'}"
        raise SimError(f"unknown command {text!r}; send HELP")

    def datasheet(self):
        return {"id": self.id, "model": self.model, "identity": self.identity, "kind": "sensor", "simulated": True,
                "speed": self.speed, "risk": "sensor", "port": "serial (115200, text lines)",
                "about": "Reads the air's temperature and humidity.",
                "sensors": {"temp": {"unit": "degC", "about": "air temperature", "valid": [-40, 80]},
                            "humidity": {"unit": "%", "about": "relative humidity", "valid": [0, 100]}},
                "outputs": {}, "trips": [], "stale_s": 120, "default": {"period_s": 30, "loops": []}}


MODELS = {"fridge": Fridge, "room": Room, "air": Air}


def make(name, speed=DEFAULT_SPEED, seed=None):
    cls = MODELS[name]
    return cls(f"sim-{name}", speed=speed, seed=seed)


# ================================================================================================ the service
class Bench:
    """Runs the devices: physics on a clock, each device's firmware on its own pseudo-terminal."""

    def __init__(self, names=("fridge", "room", "air"), speed=DEFAULT_SPEED, folder=SIM_DIR):
        self.devices = {d.id: d for d in (make(n, speed) for n in names)}
        self.folder = Path(folder)
        self.ports = {}
        self.stop = threading.Event()

    def start(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        for dev in self.devices.values():
            master, slave = os.openpty()
            tty.setraw(slave)
            path = os.ttyname(slave)
            os.chmod(path, 0o600)                      # only the envelope's user (the same as this service)
            self.ports[dev.id] = (master, slave, path)
            threading.Thread(target=self._serve_port, args=(dev, master), daemon=True).start()
            reg = {"id": dev.id, "port": path, "simulated": True, "datasheet": dev.datasheet(),
                   "started": time.time()}
            tmp = self.folder / f".{dev.id}.json"
            tmp.write_text(json.dumps(reg, indent=1))
            os.replace(tmp, self.folder / f"{dev.id}.json")
        threading.Thread(target=self._physics, daemon=True).start()

    def _physics(self):
        last = time.monotonic()
        while not self.stop.is_set():
            time.sleep(0.05)
            now = time.monotonic()
            for dev in self.devices.values():
                dev.advance((now - last) * dev.speed)
            last = now

    def _serve_port(self, dev, fd):
        buf = b""
        while not self.stop.is_set():
            try:
                chunk = os.read(fd, 1024)
            except OSError:
                time.sleep(0.2)
                continue
            if not chunk:
                time.sleep(0.05)
                continue
            buf += chunk
            while b"\n" in buf or b"\r" in buf:
                line, buf = re.split(rb"\r\n|\n|\r", buf, maxsplit=1)
                if not line.strip():
                    continue
                reply = dev.handle(line.decode("ascii", "replace"))
                try:
                    os.write(fd, (reply + "\n").encode())
                except OSError:
                    pass

    def control(self, req):
        """Scenario events and the truth behind the sensors (for the bench's own page, never for a driver)."""
        dev = self.devices.get(req.get("device"))
        if req.get("op") == "list":
            return {"ok": True, "devices": [{"id": d.id, "model": d.model} for d in self.devices.values()]}
        if not dev:
            return {"ok": False, "error": f"no simulated device {req.get('device')!r}"}
        if req.get("op") == "truth":
            return {"ok": True, "truth": dev.truth(), "time": dev.t}
        try:
            return {"ok": True, "result": dev.event(req.get("event", ""), **(req.get("args") or {}))}
        except (SimError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def serve_control(self):
        path = self.folder / "control.sock"
        path.unlink(missing_ok=True)
        bench = self

        class H(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    req = json.loads(self.rfile.readline(65536) or b"{}")
                    out = bench.control(req)
                except Exception as exc:                  # a bad request must not end the bench
                    out = {"ok": False, "error": str(exc)[:300]}
                self.wfile.write((json.dumps(out) + "\n").encode())

        server = socketserver.ThreadingUnixStreamServer(str(path), H)
        os.chmod(path, 0o666)                             # scenario events: any local user (they are simulations)
        return server


def main(argv=None):
    names = [n for n in os.environ.get("KIMIOS_SIMS", "fridge,room,air").split(",") if n in MODELS]
    bench = Bench(names)
    bench.start()
    server = bench.serve_control()
    print(f"kimi-simbench: {', '.join(bench.devices)} at {DEFAULT_SPEED:g}x in {bench.folder}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
