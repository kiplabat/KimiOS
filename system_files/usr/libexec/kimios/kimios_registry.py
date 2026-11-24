"""The software capability registry (KimiOS 0.12.0; docs/SCOPE.md §2.4, Phase 2).

The hardware graph answers "what is attached"; this answers "what can the installed software do", so that a request
in plain words reaches the right program instead of a web search. One index of:
  - apps: desktop entries (system, user, Flatpak): what each is for, the files it opens (kimios-mcp passes them in)
  - commands: every program on PATH, with the one-line summary of its manual page (`man -k`)
  - services: systemd services of the system and of the person, with their description and state
  - dbus: services on the session and system buses (running or started on demand), with the program behind them
  - plugins: KimiLM plugins and the MCP servers they bring
Each entry says how an agent reaches it ("reach"). `describe()` goes deeper for one entry: a command's package,
synopsis, description and options (from its manual page, or its --help when it has none), a service's command line
and documentation, a D-Bus service's interfaces and methods (never starting one that is not running), an app's
Flatpak permissions.

Kept in ~/.cache/kimios/registry.json with a fingerprint of the folders software is installed into (PATH, the
application folders, D-Bus and systemd unit folders, plugins, the manual index, the package database): when any
of them changes (something was installed or removed), the next question rebuilds it. kimios-registry.path
rebuilds it in the background as soon as they change.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

CACHE = Path(os.environ.get("KIMIOS_REGISTRY_CACHE",
                            str(Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "kimios/registry.json")))
VERSION = 1
KINDS = ("apps", "commands", "plugins", "services", "dbus")
# never run with --help: a program that ignores it would act (the manual page is still read)
NO_HELP = {"reboot", "poweroff", "halt", "shutdown", "init", "telinit", "kexec", "systemctl", "loginctl", "dd", "rm",
           "shred", "wipefs", "mkfs", "mkswap", "fdisk", "sfdisk", "parted", "sgdisk", "cfdisk", "kill", "killall",
           "pkill", "xkill", "yes", "sl", "sudo", "su", "pkexec", "run0", "bootc", "rpm-ostree", "ostree", "dnf",
           "flatpak", "logout", "gnome-session-quit", "sh", "bash", "zsh", "fish", "dash", "python3", "python", "perl",
           "node", "env", "nohup", "watch", "cat", "tee", "vi", "vim", "nano", "less", "more", "top", "htop"}
SKIP_DBUS = ("org.freedesktop.DBus",)


def run(cmd, timeout=20, env=None):
    """(code, stdout, stderr), never raising (tests replace this)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           env=env, errors="replace")
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not installed"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: took longer than {timeout} s"
    except OSError as exc:
        return 126, "", str(exc)


def _home():
    return Path.home()


def path_dirs():
    out = []
    for p in os.environ.get("PATH", "/usr/bin:/bin").split(":"):
        if p and p not in out and Path(p).is_dir():
            out.append(p)
    return out


def data_dirs():
    xdg_home = Path(os.environ.get("XDG_DATA_HOME") or _home() / ".local/share")
    dirs = [xdg_home, xdg_home / "flatpak/exports/share", Path("/var/lib/flatpak/exports/share")]
    dirs += [Path(d) for d in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if d]
    return [d for i, d in enumerate(dirs) if d not in dirs[:i]]


def watched():
    """The folders whose change means software was installed or removed."""
    dirs = [Path(p) for p in path_dirs()]
    for d in data_dirs():
        dirs += [d / "applications", d / "dbus-1/services", d / "kimilm/plugins"]
    dirs += [Path("/usr/share/dbus-1/system-services"), Path("/etc/systemd/system"), Path("/usr/lib/systemd/system"),
             Path("/usr/lib/systemd/user"), _home() / ".config/systemd/user", Path("/var/cache/man"),
             Path("/usr/lib/sysimage/rpm"), Path("/var/lib/flatpak/app"), _home() / ".local/share/flatpak/app"]
    return dirs


def fingerprint():
    parts = []
    for d in watched():
        try:
            parts.append(f"{d}:{d.stat().st_mtime_ns}")
        except OSError:
            continue
    return f"{VERSION}|{os.environ.get('PATH', '')}|" + "|".join(parts)


# ------------------------------------------------------------------------------------------------ sources
_MANK = re.compile(r"^(?P<names>[^()]+?)\s*\((?P<sec>[0-9][a-z0-9]*)\)\s+-+\s+(?P<about>.+)$")


def man_summaries():
    """{command: one-line summary} from the manual index (sections 1, 6 and 8: programs)."""
    code, out, _ = run(["man", "-k", "."], timeout=30, env={**os.environ, "LC_ALL": "C.UTF-8", "MANPAGER": "cat"})
    found = {}
    if code not in (0, 16):
        return found
    for line in out.splitlines():
        m = _MANK.match(line.strip())
        if not m or m["sec"][0] not in "168":
            continue
        for name in m["names"].split(","):
            name = name.strip()
            if name and name not in found:
                found[name] = m["about"].strip()
    return found


def package_summaries():
    """{command: "package: summary"} for every program on PATH that a package installed (one rpm query). On an
    image-based system the manual index may not be built yet; every command still says what it is for."""
    dirs = tuple(d.rstrip("/") + "/" for d in path_dirs())
    code, out, _ = run(["rpm", "-qa", "--qf", "[%{FILENAMES}\t%{NAME}: %{SUMMARY}\n]"], timeout=60)
    found = {}
    if code != 0:
        return found
    for line in out.splitlines():
        path, _, about = line.partition("\t")
        if about and path.startswith(dirs):
            found.setdefault(path.rsplit("/", 1)[1], about)
    return found


def commands(summaries=None):
    summaries = man_summaries() if summaries is None else summaries
    seen = {}
    for d in path_dirs():
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if n in seen or n.startswith("."):
                continue
            p = os.path.join(d, n)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                seen[n] = {"kind": "commands", "id": n, "name": n, "about": summaries.get(n, ""), "where": p,
                           "reach": f"run `{n}` (see describe for its options)"}
    return list(seen.values())


def _scoped(user):
    return ["--user"] if user else ["--system"]


def services():
    out = []
    for user in (False, True):
        scope = "user" if user else "system"
        code, text, _ = run(["systemctl", *_scoped(user), "list-units", "--type=service", "--all", "--no-legend",
                             "--plain", "--no-pager"], timeout=15)
        seen = set()
        if code == 0:
            for line in text.splitlines():
                parts = line.split(None, 4)
                if len(parts) < 4 or not parts[0].endswith(".service"):
                    continue
                name = parts[0]
                seen.add(name)
                out.append({"kind": "services", "id": f"{scope}:{name}", "name": name,
                            "about": parts[4] if len(parts) > 4 else "", "state": f"{parts[2]} ({parts[3]})",
                            "scope": scope, "reach": f"systemctl {'--user ' if user else ''}status|start|stop {name}"
                                                     + ("" if user else " (start and stop need an administrator)")})
        code, text, _ = run(["systemctl", *_scoped(user), "list-unit-files", "--type=service", "--no-legend",
                             "--no-pager"], timeout=15)
        if code == 0:
            for line in text.splitlines():
                parts = line.split()
                if not parts or not parts[0].endswith(".service") or parts[0] in seen or "@" in parts[0]:
                    continue
                out.append({"kind": "services", "id": f"{scope}:{parts[0]}", "name": parts[0], "about": "",
                            "state": parts[1] if len(parts) > 1 else "", "scope": scope,
                            "reach": f"systemctl {'--user ' if user else ''}start {parts[0]}"})
    return out


def _dbus_files():
    """{(bus, name): program} from the D-Bus service files (services started on demand)."""
    found = {}
    places = [(d / "dbus-1/services", "session") for d in data_dirs()]
    places.append((Path("/usr/share/dbus-1/system-services"), "system"))
    for folder, bus in places:
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.service")):
            name = exe = ""
            try:
                for line in f.read_text(errors="replace").splitlines():
                    if line.startswith("Name="):
                        name = line[5:].strip()
                    elif line.startswith("Exec="):
                        exe = line[5:].strip().split()[0] if line[5:].strip() else ""
            except OSError:
                continue
            if name:
                found.setdefault((bus, name), exe)
    return found


def dbus(summaries=None):
    summaries = summaries or {}
    files = _dbus_files()
    names = {}
    for bus in ("session", "system"):
        code, text, _ = run(["busctl", "--user" if bus == "session" else "--system", "list", "--no-legend",
                             "--no-pager"], timeout=15)
        if code != 0:
            continue
        for line in text.splitlines():
            parts = line.split()
            if not parts or parts[0].startswith(":") or parts[0].startswith(SKIP_DBUS):
                continue
            running = len(parts) > 1 and parts[1] != "-"
            process = parts[2] if running and len(parts) > 2 else ""
            names[(bus, parts[0])] = (running, process)
    for key in files:
        names.setdefault(key, (False, ""))
    out = []
    for (bus, name), (running, process) in sorted(names.items()):
        exe = files.get((bus, name), "")
        prog = Path(exe).name if exe else process
        out.append({"kind": "dbus", "id": f"{bus}:{name}", "name": name, "bus": bus,
                    "about": summaries.get(prog, "") or (f"provided by {prog}" if prog else ""), "program": prog,
                    "running": running, "reach": f"busctl {'--user' if bus == 'session' else '--system'} "
                                                 f"call {name} … (describe lists its methods)"})
    return out


def plugins():
    out, seen = [], set()
    for d in data_dirs():
        folder = d / "kimilm/plugins"
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*/plugin.json")):
            try:
                m = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            pid = m.get("id") or f.parent.name
            if pid in seen:
                continue
            seen.add(pid)
            servers = m.get("mcpServers") or {}
            out.append({"kind": "plugins", "id": pid, "name": m.get("name") or pid, "about": m.get("description") or "",
                        "servers": {k: (v or {}).get("description", "") for k, v in servers.items()},
                        "reach": "KimiLM → Plugins & connectors (its tools appear to agents when it is on)"})
    return out


def from_apps(apps):
    out = []
    for a in apps or []:
        if a.get("kimilm_app"):        # 0.16.0: a game or control panel KimiLM built: it opens in a KimiLM window
            reach = (f"launch_app {a['id']} (opens in its own KimiLM window); changed, rebuilt or removed in KimiLM → "
                     "Apps")
        else:
            reach = f"launch_app {a['id']}; or open a file it handles with `open`"
        out.append({**a, "kind": "apps", "reach": reach})
    return out


# ------------------------------------------------------------------------------------------------ the index
def build(apps=None):
    summaries = {**package_summaries(), **man_summaries()}      # a manual's line says more than a package's
    entries = from_apps(apps) + commands(summaries) + plugins() + services() + dbus(summaries)
    return {"version": VERSION, "built": time.time(), "fingerprint": fingerprint(), "entries": entries,
            "counts": {k: sum(1 for e in entries if e["kind"] == k) for k in KINDS}}


def load(apps=None, refresh=False):
    """The index, rebuilt when software changed since it was made (or when asked)."""
    fp = fingerprint()
    if not refresh:
        try:
            data = json.loads(CACHE.read_text())
            if data.get("version") == VERSION and data.get("fingerprint") == fp:
                if apps is not None:          # apps are cheap and always current
                    data["entries"] = from_apps(apps) + [e for e in data["entries"] if e["kind"] != "apps"]
                return data
        except (OSError, ValueError):
            pass
    data = build(apps)
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(CACHE)
    except OSError:
        pass
    return data


def _text(e):
    extra = [*(e.get("categories") or []), *(e.get("keywords") or []), *(e.get("opens") or []),
             *(e.get("servers") or {}).keys(), *(e.get("servers") or {}).values(), e.get("program") or ""]
    return f"{e['name']} {e['id']}".lower(), f"{e.get('about') or ''} {' '.join(extra)}".lower()


def search(index, query="", kinds=KINDS, limit=25):
    """Entries matching every word of the query (name counts more than description), best first, per kind."""
    words = [w for w in re.split(r"\s+", (query or "").lower().strip()) if w]
    order = {k: i for i, k in enumerate(KINDS)}
    rows = []
    for e in index["entries"]:
        if e["kind"] not in kinds:
            continue
        if not words:
            rows.append((0, e))
            continue
        name, about = _text(e)
        score = 0
        for w in words:
            if w == e["name"].lower():
                score += 6
            elif w in name:
                score += 3
            elif w in about:
                score += 1
            else:
                score = -1
                break
        if score > 0:
            rows.append((score + (1 if e.get("about") else 0), e))
    if words and not rows:            # nothing has every word: anything with one of them, marked as loose
        for e in index["entries"]:
            if e["kind"] in kinds:
                name, about = _text(e)
                s = sum(3 if w in name else 1 if w in about else 0 for w in words)
                if s >= 3 or (s and e.get("about")):
                    rows.append((s, {**e, "loose": True}))
    rows.sort(key=lambda r: (-r[0], order[r[1]["kind"]], r[1]["name"]))
    limit = max(1, min(int(limit or 25), 200))
    out = {k: [] for k in kinds}
    for _s, e in rows:
        if len(out[e["kind"]]) < limit:
            out[e["kind"]].append({k: v for k, v in e.items() if k not in ("kind",)})
    return out, len(rows)


# ------------------------------------------------------------------------------------------------ one entry, deeper
_OVERSTRIKE = re.compile(r".\x08")


def _sections(page, wanted=("NAME", "SYNOPSIS", "DESCRIPTION", "OPTIONS", "EXAMPLES"), per=40):
    out, cur, lines = {}, None, page.splitlines()
    for line in lines:
        if line and not line[0].isspace() and line.strip().isupper() and len(line.strip()) < 40:
            cur = line.strip()
            continue
        if cur in wanted:
            out.setdefault(cur, [])
            if len(out[cur]) < per:
                out[cur].append(line.rstrip())
    return {k: "\n".join(v).strip() for k, v in out.items() if "".join(v).strip()}


def describe_command(name):
    where = shutil.which(name)
    if not where:
        raise LookupError(f"no command {name!r} on PATH")
    info = {"kind": "commands", "id": name, "where": where}
    code, out, _ = run(["rpm", "-qf", "--qf", "%{NAME}\t%{SUMMARY}\n", os.path.realpath(where)], timeout=10)
    if code == 0 and "\t" in out:
        pkg, summary = out.splitlines()[0].split("\t", 1)
        info["package"] = {"name": pkg, "summary": summary}
    code, page, _ = run(["man", name], timeout=15, env={**os.environ, "MANPAGER": "cat", "MANWIDTH": "100",
                                                        "LC_ALL": "C.UTF-8", "MAN_KEEP_FORMATTING": ""})
    manual = _sections(_OVERSTRIKE.sub("", page)) if code == 0 else {}
    if manual:
        info["manual"] = manual
    elif name not in NO_HELP and not name.startswith(("mkfs", "fsck", "kimios-")):
        code, out, err = run([where, "--help"], timeout=3, env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C.UTF-8",
                                                                "HOME": str(_home()), "TERM": "dumb"})
        text = (out or err).strip()
        if text:
            info["help"] = "\n".join(text.splitlines()[:80])[:4000]
    if "manual" not in info and "help" not in info:
        info["note"] = ("no manual page; not run with --help, because it could act instead of explaining"
                        if name in NO_HELP else "no manual page and no --help text")
    return info


def describe_service(entry_id):
    scope, _, name = entry_id.partition(":")
    if not name:
        scope, name = "system", entry_id
    code, out, err = run(["systemctl", *_scoped(scope == "user"), "show", name, "--no-pager", "-p",
                          "Description,ActiveState,SubState,UnitFileState,ExecStart,Documentation,FragmentPath,"
                          "User,After,WantedBy"], timeout=10)
    if code != 0:
        raise LookupError(err.strip()[:200] or f"no service {name}")
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    m = re.search(r"argv\[\]=([^;]+)", props.get("ExecStart", ""))
    if m:
        props["ExecStart"] = m.group(1).strip()
    return {"kind": "services", "id": entry_id, "scope": scope, "name": name, **props}


def describe_dbus(entry_id, limit=80):
    bus, _, name = entry_id.partition(":")
    if not name:
        bus, name = "session", entry_id
    flag = "--user" if bus == "session" else "--system"
    code, out, err = run(["busctl", flag, "--auto-start=no", "tree", name, "--list", "--no-pager"], timeout=10)
    if code != 0:
        return {"kind": "dbus", "id": entry_id, "running": False,
                "note": "not running now; it starts when something calls it, and its methods are listed once it "
                        "runs (describe does not start it)", "error": err.strip()[:200]}
    paths = [p.strip() for p in out.splitlines() if p.strip().startswith("/")][:12]
    interfaces = {}
    count = 0
    for path in paths:
        code, text, _ = run(["busctl", flag, "--auto-start=no", "introspect", name, path, "--no-legend",
                             "--no-pager"], timeout=10)
        if code != 0:
            continue
        current = None
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            if parts[1] == "interface":
                current = parts[0]
                if current.startswith("org.freedesktop.DBus."):
                    current = None
                continue
            if current and parts[0].startswith(".") and count < limit:
                member = {"name": parts[0][1:], "type": parts[1]}
                if parts[1] == "method" and len(parts) > 3:
                    member["in"], member["out"] = parts[2], parts[3]
                interfaces.setdefault(current, {"path": path, "members": []})["members"].append(member)
                count += 1
    return {"kind": "dbus", "id": entry_id, "running": True, "paths": paths, "interfaces": interfaces,
            "how": f"busctl {flag} call {name} PATH INTERFACE METHOD SIGNATURE ARGS…"}


def describe_app(entry, flatpak_id=None):
    info = {k: v for k, v in entry.items() if k != "kind"}
    info["kind"] = "apps"
    if entry.get("flatpak"):
        fid = flatpak_id or (entry["id"][:-8] if entry["id"].endswith(".desktop") else entry["id"])
        code, out, _ = run(["flatpak", "info", "--show-permissions", fid], timeout=10)
        if code == 0:
            info["permissions"] = out.strip()[:2000]
    return info


def describe(index, kind, entry_id):
    if kind == "commands":
        return describe_command(entry_id)
    if kind == "services":
        return describe_service(entry_id)
    if kind == "dbus":
        return describe_dbus(entry_id)
    e = next((e for e in index["entries"] if e["kind"] == kind and e["id"] == entry_id), None)
    if e is None:
        raise LookupError(f"no {kind} entry {entry_id!r}: search first")
    if kind == "apps":
        return describe_app(e)
    return {k: v for k, v in e.items()}


def _desktop_apps():
    """The apps, read the way kimios-mcp reads them (0.12.1: the terminal command listed no apps)."""
    import importlib.machinery
    import importlib.util
    here = Path(__file__).resolve().parent / "kimios-mcp"
    try:
        loader = importlib.machinery.SourceFileLoader("kimios_mcp_apps", str(here))
        spec = importlib.util.spec_from_loader("kimios_mcp_apps", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod.desktop_entries()
    except Exception:
        return None


def main(argv=None):
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="kimios-registry", description="KimiOS's software capability registry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("refresh")
    s = sub.add_parser("search")
    s.add_argument("query", nargs="*")
    d = sub.add_parser("describe")
    d.add_argument("kind", choices=KINDS)
    d.add_argument("id")
    a = ap.parse_args(argv)
    apps = _desktop_apps()
    if a.cmd == "refresh":
        data = load(apps=apps, refresh=True)
        print(json.dumps({"ok": True, "counts": data["counts"], "cache": str(CACHE)}))
        return 0
    index = load(apps=apps)
    try:
        out = search(index, " ".join(a.query))[0] if a.cmd == "search" else describe(index, a.kind, a.id)
    except LookupError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    json.dump(out, sys.stdout, indent=1)
    print()
    return 0
