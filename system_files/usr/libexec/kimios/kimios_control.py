"""KimiOS control toolkit: learn how a device responds, tune a controller, and practise in simulation.

Scope §2.3 (skill learning), steps 1 to 3, with nothing connected to real hardware:

  identify   from recorded samples (time, input, output), fit a first-order-plus-dead-time model
             y' = (K·u(t − θ) + offset − y) / τ, the usual first description of heating, cooling, motors
             and tanks: gain K, time constant τ, dead time θ, and the output with the input off (offset)
  tune       PI gains from the model (SIMC rules, Skogestad 2003) and on/off advice for switched devices
  simulate   run a controller against the model inside a safety envelope (input limits, minimum on and
             off times, output limits) and report how well it holds the set point, how often it switches
             and how much it uses the actuator (an energy measure)

Example: a refrigerator. Input: compressor on (1) or off (0). Output: cabinet temperature. Identification
gives K < 0 (cooling), τ of tens of minutes, θ of a minute or so; simulation compares thermostat bands,
minimum off times (compressor wear) and duty cycle (energy).

Standard library only: KimiOS's tool server runs on the system Python.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path


class ControlError(ValueError):
    """Bad data or arguments, said in words an agent can act on."""


# ------------------------------------------------------------------------------------------------- data
def load_samples(samples=None, csv_path=None, columns=None):
    """[(t, u, y)] sorted by time, from a list of triples or a CSV file with a header."""
    rows = []
    if csv_path:
        p = Path(csv_path).expanduser()
        if not p.exists():
            raise ControlError(f"{csv_path} does not exist")
        names = {"t": "t", "u": "u", "y": "y", **(columns or {})}
        with p.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            missing = [v for v in names.values() if v not in (reader.fieldnames or [])]
            if missing:
                raise ControlError(f"the CSV has no column {', '.join(missing)}; its columns: "
                                   f"{', '.join(reader.fieldnames or [])}. Pass columns={{t, u, y}}.")
            for r in reader:
                try:
                    rows.append((float(r[names["t"]]), float(r[names["u"]]), float(r[names["y"]])))
                except (TypeError, ValueError):
                    continue
    elif samples:
        for s in samples:
            if len(s) != 3:
                raise ControlError("each sample is [time in seconds, input, output]")
            rows.append(tuple(float(x) for x in s))
    rows.sort()
    if len(rows) < 20:
        raise ControlError(f"need at least 20 samples, got {len(rows)}")
    return rows


def resample(rows, dt=None):
    """Uniform samples at dt (default: the median spacing), linear interpolation in between."""
    gaps = sorted(b[0] - a[0] for a, b in zip(rows, rows[1:]) if b[0] > a[0])
    if not gaps:
        raise ControlError("the samples all have the same time")
    dt = float(dt or gaps[len(gaps) // 2])
    t0, t1 = rows[0][0], rows[-1][0]
    out, j = [], 0
    n = int((t1 - t0) / dt) + 1
    for i in range(n):
        t = t0 + i * dt
        while j < len(rows) - 2 and rows[j + 1][0] <= t:
            j += 1
        (ta, ua, ya), (tb, ub, yb) = rows[j], rows[j + 1]
        f = 0.0 if tb == ta else min(1.0, max(0.0, (t - ta) / (tb - ta)))
        out.append((t, ua, ya + f * (yb - ya)))      # the input holds its value until it changes (zero-order)
    return dt, out


def _solve3(m, v):
    """Solve a 3×3 linear system (Gaussian elimination with pivoting); None if singular."""
    a = [row[:] + [v[i]] for i, row in enumerate(m)]
    for c in range(3):
        p = max(range(c, 3), key=lambda r: abs(a[r][c]))
        if abs(a[p][c]) < 1e-12:
            return None
        a[c], a[p] = a[p], a[c]
        for r in range(3):
            if r != c:
                f = a[r][c] / a[c][c]
                a[r] = [x - f * y for x, y in zip(a[r], a[c])]
    return [a[i][3] / a[i][i] for i in range(3)]


# ------------------------------------------------------------------------------------------------- identify
def identify(samples=None, csv_path=None, columns=None, dt=None, max_dead_time=None):
    rows = load_samples(samples, csv_path, columns)
    dt, data = resample(rows, dt)
    u = [r[1] for r in data]
    y = [r[2] for r in data]
    if max(u) - min(u) < 1e-9:
        raise ControlError("the input never changes, so its effect cannot be seen; record a few on/off changes")
    n = len(y)
    max_d = int((max_dead_time or (n * dt / 4)) / dt)
    max_d = max(0, min(max_d, n // 3))
    best = None
    for d in range(max_d + 1):
        s = [[0.0] * 3 for _ in range(3)]
        v = [0.0] * 3
        sse_rows = []
        for k in range(d, n - 1):
            x = (y[k], u[k - d], 1.0)
            for i in range(3):
                v[i] += x[i] * y[k + 1]
                for j in range(3):
                    s[i][j] += x[i] * x[j]
            sse_rows.append((x, y[k + 1]))
        coef = _solve3(s, v)
        if coef is None:
            continue
        sse = sum((t - sum(c * xi for c, xi in zip(coef, x))) ** 2 for x, t in sse_rows) / max(1, len(sse_rows))
        if best is None or sse < best[0] - 1e-15:
            best = (sse, d, coef, sse_rows)
    if best is None:
        raise ControlError("the data could not be fitted (too little variation)")
    sse, d, (a, b, c), used = best
    if not 0 < a < 1:
        raise ControlError(f"the response is not a first-order lag (a = {a:.3f}); the device may integrate (a "
                           "level that keeps rising) or the samples are too far apart")
    tau = -dt / math.log(a)
    K = b / (1 - a)
    offset = c / (1 - a)
    targets = [t for _, t in used]
    mean = sum(targets) / len(targets)
    sst = sum((t - mean) ** 2 for t in targets) / len(targets)
    r2 = 1 - sse / sst if sst > 0 else 0.0
    return {"model": "first order plus dead time", "K": round(K, 6), "tau_s": round(tau, 3),
            "theta_s": round(d * dt, 3), "offset": round(offset, 6), "dt_s": dt, "samples": n,
            "fit_r2": round(r2, 5),
            "meaning": (f"Full input moves the output by {K:+.4g} from {offset:.4g}; it gets 63 % of the way in "
                        f"{tau:.4g} s after a delay of {d * dt:.4g} s."),
            "trust": "good" if r2 > 0.98 else "fair" if r2 > 0.9 else "poor: more varied data, or another model"}


# ------------------------------------------------------------------------------------------------- tune
def tune(K, tau_s, theta_s, tau_c_s=None):
    """SIMC PI gains, and on/off advice for switched devices."""
    if K == 0 or tau_s <= 0 or theta_s < 0:
        raise ControlError("need K ≠ 0, τ > 0 and θ ≥ 0 (from identify)")
    tau_c = float(tau_c_s if tau_c_s is not None else max(theta_s, 0.1 * tau_s))
    Kc = tau_s / (K * (tau_c + theta_s))
    Ti = min(tau_s, 4 * (tau_c + theta_s))
    return {"pi": {"Kc": round(Kc, 6), "Ti_s": round(Ti, 3), "tau_c_s": round(tau_c, 3)},
            "rule": "SIMC (Skogestad 2003): Kc = τ / (K (τc + θ)), Ti = min(τ, 4 (τc + θ)); larger τc is gentler",
            "on_off": {"note": "For a device that can only be on or off (a compressor, a heater relay), use on/off "
                               "control with a band, or the PI output as a duty cycle over a period much longer "
                               "than θ. A dead time θ makes the output overshoot the band by about |K|·θ/τ.",
                       "overshoot_per_switch": round(abs(K) * theta_s / tau_s, 6)}}


# ------------------------------------------------------------------------------------------------- simulate
def simulate(K, tau_s, theta_s, offset=0.0, controller=None, duration_s=None, dt_s=None, y0=None,
             u_min=0.0, u_max=1.0, min_on_s=0.0, min_off_s=0.0, y_limits=None, band=None, disturbance=None):
    """Run a controller against the model. controller: {"type": "onoff", "setpoint", "hysteresis"} or
    {"type": "pi", "setpoint", "Kc", "Ti_s", "pwm_period_s"?}. disturbance: [[from_s, change_in_offset], …]."""
    c = dict(controller or {})
    kind = c.get("type")
    if kind not in ("onoff", "pi"):
        raise ControlError('controller.type must be "onoff" or "pi"')
    if "setpoint" not in c:
        raise ControlError("controller.setpoint is needed")
    if tau_s <= 0:
        raise ControlError("τ must be > 0")
    sp = float(c["setpoint"])
    dt = float(dt_s or max(0.05, min(tau_s / 50, theta_s / 5 if theta_s else tau_s / 50)))
    duration = float(duration_s or 20 * tau_s)
    steps = int(duration / dt)
    if steps > 2_000_000:
        raise ControlError("too many steps: shorten the duration or raise dt_s")
    delay = int(round(theta_s / dt))
    queue = [u_min] * max(1, delay)
    y = float(offset if y0 is None else y0)
    band = float(band if band is not None else c.get("hysteresis", 0.5) or 0.5)
    direction = 1 if K > 0 else -1             # +1: input raises the output (heating); −1: lowers it (cooling)
    dist = sorted(disturbance or [])
    u, on_since, off_since = u_min, -1e18, -1e18
    integral = 0.0
    in_band = switches = violations = counted = 0
    used = abs_err = max_over = 0.0
    settled_at = None                              # holding is measured from the first time inside the band
    trace, every = [], max(1, steps // 60)
    pwm = float(c.get("pwm_period_s") or 0)
    for i in range(steps):
        t = i * dt
        extra = sum(v for start, v in dist if t >= start)
        e = sp - y
        if kind == "onoff":
            h = float(c.get("hysteresis", 0.5))
            want = u
            if direction * e > h:
                want = u_max
            elif direction * e < -h:
                want = u_min
        else:
            Kc, Ti = float(c["Kc"]), float(c["Ti_s"])
            raw = Kc * (e + integral / Ti)
            want = min(u_max, max(u_min, raw))
            # no wind-up: do not integrate further into a limit the input is already at
            if not ((raw > u_max and Kc * e > 0) or (raw < u_min and Kc * e < 0)):
                integral += e * dt
            if pwm:                                    # a switched device: the PI output as a duty cycle
                duty = (want - u_min) / max(1e-12, u_max - u_min)
                want = u_max if (t % pwm) < duty * pwm else u_min
        # the safety envelope: minimum on and off times for switched devices
        if want > u and u == u_min and t - off_since < min_off_s:
            want = u
        if want < u and u == u_max and t - on_since < min_on_s:
            want = u
        if want != u:
            if want == u_max or (kind == "pi" and want > u):
                on_since = t
            if want == u_min:
                off_since = t
            if u == u_min and want > u_min:
                switches += 1
            u = want
        queue.append(u)
        applied = queue.pop(0) if delay else u
        y += dt * (K * applied + offset + extra - y) / tau_s
        err = abs(sp - y)
        if settled_at is None and err <= band:
            settled_at = t
        if settled_at is not None:
            counted += 1
            abs_err += err
            in_band += err <= band
            max_over = max(max_over, err - band)
            used += (applied - u_min) / max(1e-12, u_max - u_min)
        if y_limits and not (y_limits[0] <= y <= y_limits[1]):
            violations += 1
        if i % every == 0:
            trace.append([round(t, 1), round(applied, 3), round(y, 4)])
    hours = duration / 3600
    held = max(1, counted)
    return {"controller": c, "duration_s": duration, "dt_s": dt,
            "reached_band_after_s": None if settled_at is None else round(settled_at, 1),
            "time_in_band_pct": round(100 * in_band / held, 2) if counted else 0.0, "band": band,
            "mean_abs_error": round(abs_err / held, 5) if counted else None,
            "worst_outside_band": round(max(0.0, max_over), 5) if counted else None,
            "duty_pct": round(100 * used / held, 2) if counted else None,
            "switch_ons_per_hour": round(switches / hours, 3) if hours else None,
            "note": "Band, error and duty are measured from the first time the output is inside the band.",
            "limit_violations_pct": round(100 * violations / steps, 3) if y_limits else None,
            "final_output": round(y, 5), "trace": trace,
            "trace_columns": ["time_s", "input", "output"]}
