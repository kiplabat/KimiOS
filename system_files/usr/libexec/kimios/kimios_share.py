"""KimiOS 0.14.0: drivers and controllers shared on purpose (principle 6, docs/SCOPE.md §2.3, open decision 3).

The registry is a public GitHub repository, kiplabat/kimios-registry:
    drivers/<model>/<id>.json         a driver manifest that passed its test on a device of that model
    controllers/<model>/<id>.json     a controller spec that the guard accepted for that model
    index.json                        every entry, rebuilt by the registry's own check on main
Each entry carries the model, the shape it fits (the datasheet's sensor and output names), the datasheet it was
written for (without anything that names the machine: device id, port, serial number) and what showed it works.

Getting: `find` lists the entries for a device's model and shape; `get` downloads one and checks it against the
index's sha256. Nothing from the registry runs without passing on this machine first: a driver is tested on the
device (it moves nothing) and promoted only if it passes; a controller is checked against this device's datasheet
and started by the guard, which holds it to the datasheet's limits whatever the entry says.

Sharing: nothing leaves the computer on its own. `prepare` writes the entry into ~/.local/share/kimios/share and
gives a link to the registry's "new file" page with the entry filled in; the person reads it there and presses
"Propose changes", which opens a pull request that is reviewed before it is merged. No GitHub sign-in is kept on
the machine."""
import datetime
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REGISTRY = "kiplabat/kimios-registry"
WEB = f"https://github.com/{REGISTRY}"
RAW = f"https://raw.githubusercontent.com/{REGISTRY}/main"
KINDS = ("driver", "controller")
# what a shared datasheet keeps: the limits and the device's own controller, never what names this machine
DATASHEET_KEEP = ("model", "kind", "risk", "sensors", "outputs", "trips", "default", "stale_s", "period_s",
                  "protocol", "notes")
MAX_ENTRY = 64 * 1024
LINK_MAX = 7000                  # longer prefilled links are cut by browsers: then the file is uploaded instead


class ShareError(Exception):
    pass


def source():
    """Where entries are read from: the registry on GitHub, or a folder (an offline copy, the tests)."""
    return os.environ.get("KIMIOS_REGISTRY_URL", RAW).rstrip("/")


def slug(model):
    s = re.sub(r"[^a-z0-9]+", "-", str(model or "").lower()).strip("-")
    if not s:
        raise ShareError("the device's datasheet has no model, so it cannot be matched with anything shared")
    return s[:80]


def shape(datasheet):
    return {"sensors": sorted((datasheet.get("sensors") or {})), "outputs": sorted((datasheet.get("outputs") or {}))}


def public_datasheet(datasheet):
    ds = {k: datasheet[k] for k in DATASHEET_KEEP if k in datasheet}
    ident = str(datasheet.get("identity") or "")
    if ident.count(",") >= 1:                                   # SCPI *IDN?: maker,model,serial,firmware
        parts = [p.strip() for p in ident.split(",")]
        ds["identity"] = ",".join([parts[0], parts[1]] + ([""] if len(parts) > 2 else []) + parts[3:4])
    return ds


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_id(kind, body):
    return f"{kind}-{hashlib.sha256(canonical(body).encode()).hexdigest()[:12]}"


def _fetch(rel):
    base = source()
    if not base.startswith(("https://", "http://")):
        p = Path(base) / rel
        if not p.is_file():
            raise ShareError(f"{rel} is not in the registry copy at {base}")
        return p.read_bytes()
    req = urllib.request.Request(f"{base}/{urllib.parse.quote(rel)}", headers={"User-Agent": "KimiOS"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read(MAX_ENTRY * 64 + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ShareError(f"{rel} is not in the registry") from None
        raise ShareError(f"the registry answered {exc.code} for {rel}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise ShareError(f"the registry could not be reached ({getattr(exc, 'reason', exc)}): is there a "
                         "network?") from None
    return data


def index():
    try:
        rows = json.loads(_fetch("index.json"))
    except ValueError:
        raise ShareError("the registry's index is not readable") from None
    return rows.get("entries", []) if isinstance(rows, dict) else []


def find(datasheet, kind):
    """Entries of this kind for this device's model, those that fit its sensors and outputs first."""
    if kind not in KINDS:
        raise ShareError("kind is driver or controller")
    want, sh = slug(datasheet.get("model")), shape(datasheet)
    rows = [r for r in index() if r.get("kind") == kind and r.get("slug") == want]
    fit = [r for r in rows if r.get("shape") == sh]
    out = {"model": datasheet.get("model"), "slug": want, "registry": WEB,
           "entries": [{k: r.get(k) for k in ("path", "note", "date", "simulated", "evidence")} for r in fit]}
    if rows and not fit:
        out["other_shapes"] = [{"path": r.get("path"), "shape": r.get("shape")} for r in rows]
        out["why_none"] = ("entries for this model name other sensors or outputs than this device's datasheet: "
                           "they would not fit it")
    if not out["entries"]:
        out["next"] = (f"nothing shared for this {kind} yet; once one works here, it can be shared "
                       f"({kind}(action='share'))")
    return out


def get(path, datasheet=None):
    """One entry, checked: its path, its sha256 in the index, its kind, and (with a datasheet) that it fits."""
    path = str(path or "").strip().lstrip("/")
    m = re.fullmatch(r"(drivers|controllers)/([a-z0-9-]+)/((driver|controller)-[0-9a-f]{12})\.json", path)
    if not m or m.group(1)[:-1] != m.group(4):
        raise ShareError("entry is a path from find, such as drivers/<model>/driver-<id>.json")
    data = _fetch(path)
    if len(data) > MAX_ENTRY:
        raise ShareError("the entry is larger than an entry can be")
    listed = next((r for r in index() if r.get("path") == path), None)
    if listed is None:
        raise ShareError(f"{path} is not in the registry's index (not reviewed and merged yet)")
    if hashlib.sha256(data).hexdigest() != listed.get("sha256"):
        raise ShareError(f"{path} does not match the registry's index: not used")
    try:
        e = json.loads(data)
    except ValueError:
        raise ShareError(f"{path} is not JSON") from None
    kind = m.group(4)
    if e.get("kind") != kind or not isinstance(e.get(kind), dict) or entry_id(kind, e[kind]) != m.group(3):
        raise ShareError(f"{path} is not a {kind} entry as its name says")
    if datasheet is not None and e.get("shape") != shape(datasheet):
        raise ShareError(f"{path} is for a device with sensors {e.get('shape', {}).get('sensors')} and outputs "
                         f"{e.get('shape', {}).get('outputs')}; this one has {shape(datasheet)}")
    return e


def make_entry(kind, datasheet, body, evidence, note="", simulated=False, version=""):
    if kind not in KINDS:
        raise ShareError("kind is driver or controller")
    e = {"schema": 1, "kind": kind, "model": datasheet.get("model"), "slug": slug(datasheet.get("model")),
         "shape": shape(datasheet), "simulated": bool(simulated), kind: body,
         "datasheet": public_datasheet(datasheet), "evidence": evidence, "note": str(note or "")[:500],
         "kimios": version, "date": datetime.date.today().isoformat(), "licence": "Apache-2.0"}
    if len(canonical(e)) > MAX_ENTRY:
        raise ShareError("this is too large to share (an entry is at most 64 KB)")
    return e


def prepare(entry, folder=None):
    """Write the entry for the person to look at and give the link that proposes it to the registry."""
    kind = entry["kind"]
    eid = entry_id(kind, entry[kind])
    rel_dir, name = f"{kind}s/{entry['slug']}", f"{eid}.json"
    text = json.dumps(entry, indent=2, ensure_ascii=False) + "\n"
    folder = Path(folder or Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "kimios/share")
    folder.mkdir(parents=True, exist_ok=True)
    local = folder / name
    local.write_text(text, encoding="utf-8")
    link = f"{WEB}/new/main/{rel_dir}?" + urllib.parse.urlencode({"filename": name, "value": text})
    out = {"file": str(local), "path": f"{rel_dir}/{name}", "registry": WEB}
    if len(link) <= LINK_MAX:
        out["link"] = link
        out["next"] = ("show the person the file and the link: they open it, read the entry on GitHub and press "
                       "'Propose changes'. Nothing is sent before that; the registry reviews it before merging")
    else:
        out["link"] = f"{WEB}/upload/main/{rel_dir}"
        out["next"] = (f"too long for a filled-in link: the person opens the link and drops {local} there, then "
                       "presses 'Propose changes'")
    return out
