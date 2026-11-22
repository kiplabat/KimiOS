"""KimiOS 0.11.0: a snapshot before risky actions, and a one-step undo (docs/SCOPE.md, Phase 2).

What it protects. The system itself is an image (bootc): a bad update is undone with `bootc rollback`. What an agent
can damage is the person's own files: a script that overwrites a folder, an unattended task that edits the wrong
document, a program installed into the toolbox (which lives in the home folder). So a snapshot is of the home folder.

How.
  - btrfs (Fedora's default for /var/home): a read-only snapshot of the home subvolume, taken by `kimi-snapshot`
    (a small system service: making a btrfs snapshot needs root). It is instant and costs only what changes
    afterwards. Snapshots live in /var/home/.kimios-snapshots (root, mode 0711): each person can read their own
    home folder inside a snapshot (their folder keeps its owner and its 0700), nobody can read anyone else's.
  - Anything else: a copy of the folders the action will touch (`paths`), made by the person themself into
    ~/.local/share/kimios/snapshots (with reflinks where the filesystem has them), up to COPY_LIMIT bytes.
Undo needs no privilege: `changes` lists what differs between a snapshot and now, and `restore` puts a file or a
folder back as it was, after moving the current version aside (`<name>.before-undo-<time>`), so an undo can itself be
undone. Restoring is done by the person (KimiLM, kimios-mcp or `kimios-snapshot restore`), never by root, so it can
only write where they can.

Kept: at most KEEP snapshots per person; older than MAX_AGE days are deleted, except the newest three. A snapshot is
taken at most once every MIN_GAP seconds per person: asking again sooner returns the one just taken."""
from __future__ import annotations

import json
import os
import pwd
import shutil
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

HOME_ROOT = Path(os.environ.get("KIMIOS_HOME_ROOT", "/var/home"))
STORE = Path(os.environ.get("KIMIOS_SNAPSHOT_STORE", str(HOME_ROOT / ".kimios-snapshots")))
SOCKET = Path(os.environ.get("KIMIOS_SNAPSHOT_SOCKET", "/run/kimios-snapshot/snapshot.sock"))
KEEP = 12
MAX_AGE = 7 * 86400
MIN_GAP = 120
COPY_LIMIT = 2 * 2**30
MAX_CHANGES = 400


class SnapshotError(RuntimeError):
    pass


def _now_id():
    return time.strftime("%Y%m%d-%H%M%S", time.localtime()) + f"-{os.getpid() % 1000:03d}"


def _run(cmd, timeout=60):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)
    return out.returncode, out.stdout, out.stderr


# ================================================================================================ btrfs (root)
def btrfs_ready(root=HOME_ROOT):
    """True when the home root is a btrfs subvolume (snapshots can be taken of it)."""
    if os.environ.get("KIMIOS_SNAPSHOT_FAKE_BTRFS"):     # tests: a plain folder stands in for a subvolume
        return True
    code, out, _ = _run(["stat", "-f", "-c", "%T", str(root)], 10)
    if code != 0 or out.strip() != "btrfs":
        return False
    return _run(["btrfs", "subvolume", "show", str(root)], 10)[0] == 0


def _btrfs_snapshot(src, dest):
    if os.environ.get("KIMIOS_SNAPSHOT_FAKE_BTRFS"):
        shutil.copytree(src, dest, symlinks=True, ignore=shutil.ignore_patterns(STORE.name))
        return
    code, out, err = _run(["btrfs", "subvolume", "snapshot", "-r", str(src), str(dest)], 120)
    if code != 0:
        raise SnapshotError(f"btrfs could not take the snapshot: {(err or out).strip()[-300:]}")


def _btrfs_delete(path):
    if os.environ.get("KIMIOS_SNAPSHOT_FAKE_BTRFS"):
        shutil.rmtree(path, ignore_errors=True)
        return
    _run(["btrfs", "subvolume", "delete", str(path)], 120)


class Service:
    """The root part: take, list and delete whole-home snapshots for the person who asks (their uid)."""

    def __init__(self, store=STORE, root=HOME_ROOT):
        self.store, self.root = Path(store), Path(root)
        self.lock = threading.Lock()

    def ready(self):
        return btrfs_ready(self.root)

    def _metas(self, uid=None):
        out = []
        for f in sorted(self.store.glob("*.json")):
            try:
                m = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if uid is None or m.get("uid") == uid:
                out.append(m)
        return sorted(out, key=lambda m: m.get("created", 0))

    def take(self, uid, reason=""):
        if not self.ready():
            raise SnapshotError(f"{self.root} is not on btrfs: name the folders to keep a copy of instead")
        with self.lock:
            mine = self._metas(uid)
            if mine and time.time() - mine[-1]["created"] < MIN_GAP:
                return {**mine[-1], "recent": True}
            self.store.mkdir(parents=True, exist_ok=True)
            os.chmod(self.store, 0o711)
            sid = _now_id()
            _btrfs_snapshot(self.root, self.store / sid)
            meta = {"id": sid, "uid": uid, "created": time.time(), "reason": str(reason or "")[:200],
                    "kind": "btrfs", "path": str(self.store / sid)}
            f = self.store / f"{sid}.json"
            f.write_text(json.dumps(meta))
            try:
                os.chown(f, uid, -1)
            except OSError:
                pass
            os.chmod(f, 0o600)
            self.prune(uid)
            return meta

    def prune(self, uid):
        mine = self._metas(uid)
        now = time.time()
        for i, m in enumerate(mine):
            newest = i >= len(mine) - 3
            if (len(mine) - i > KEEP) or (not newest and now - m["created"] > MAX_AGE):
                self._drop(m)

    def _drop(self, m):
        _btrfs_delete(self.store / m["id"])
        (self.store / f"{m['id']}.json").unlink(missing_ok=True)

    def delete(self, uid, sid):
        m = next((m for m in self._metas(uid) if m["id"] == sid), None)
        if not m:
            raise SnapshotError(f"no snapshot {sid!r} of yours")
        self._drop(m)
        return {"deleted": sid}

    def handle(self, uid, req):
        op = req.get("op")
        try:
            if op == "status":
                return {"ok": True, "btrfs": self.ready(), "root": str(self.root)}
            if op == "take":
                return {"ok": True, "snapshot": self.take(uid, req.get("reason"))}
            if op == "list":
                return {"ok": True, "snapshots": self._metas(uid)}
            if op == "delete":
                return {"ok": True, **self.delete(uid, str(req.get("id")))}
            return {"ok": False, "error": f"unknown op {op!r}"}
        except SnapshotError as exc:
            return {"ok": False, "error": str(exc)}

    def serve(self, path=None):
        path = Path(path or SOCKET)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        svc = self

        class H(socketserver.StreamRequestHandler):
            def handle(self):
                creds = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                _pid, uid, _gid = struct.unpack("3i", creds)
                try:
                    req = json.loads(self.rfile.readline(1 << 16) or b"{}")
                    out = svc.handle(uid, req)
                except Exception as exc:
                    out = {"ok": False, "error": str(exc)[:300]}
                self.wfile.write((json.dumps(out) + "\n").encode())

        server = socketserver.ThreadingUnixStreamServer(str(path), H)
        server.daemon_threads = True
        os.chmod(path, 0o666)        # anyone may ask; each gets only their own snapshots
        return server


def ask(req, path=None, timeout=180.0):
    path = path or SOCKET
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect(str(path))
        except OSError as exc:
            raise SnapshotError(f"the snapshot service is not running ({exc})") from None
        s.sendall((json.dumps(req) + "\n").encode())
        out = json.loads(s.makefile().readline() or "{}")
    if not out.get("ok"):
        raise SnapshotError(out.get("error") or "refused")
    return out


# ================================================================================================ the person's part
def home():
    return Path(os.environ.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir).resolve()


def copy_store():
    return home() / ".local/share/kimios/snapshots"


def _inside_home(path):
    p = Path(os.path.expanduser(str(path)))
    p = (home() / p) if not p.is_absolute() else p
    p = Path(os.path.abspath(p))
    h = home()
    if p != h and h not in p.parents:
        raise SnapshotError(f"{p} is not in your home folder ({h})")
    return p


def _size(path):
    if path.is_symlink() or path.is_file():
        return path.lstat().st_size
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def _copy(src, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir() and not src.is_symlink():
        code = _run(["cp", "-a", "--reflink=auto", str(src), str(dest)], 1800)[0] if shutil.which("cp") else 1
        if code != 0 or not dest.exists():
            shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True)
    elif src.is_symlink():
        os.symlink(os.readlink(src), dest)
    else:
        if not shutil.which("cp") or _run(["cp", "-a", "--reflink=auto", str(src), str(dest)], 600)[0] != 0:
            shutil.copy2(src, dest)


def take(reason="", paths=None, path=None):
    """A snapshot before something risky: the whole home folder where btrfs allows it, else a copy of `paths`."""
    try:
        if ask({"op": "status"}, path, 10).get("btrfs"):
            return ask({"op": "take", "reason": reason}, path)["snapshot"]
    except SnapshotError:
        pass
    if not paths:
        raise SnapshotError("this home folder is not on btrfs (or the snapshot service is not running): name the "
                            "files or folders the action will change, and they are copied")
    targets = [_inside_home(p) for p in paths]
    targets = [t for t in targets if t.exists() or t.is_symlink()]
    total = sum(_size(t) for t in targets)
    if total > COPY_LIMIT:
        raise SnapshotError(f"those folders hold {total / 2**30:.1f} GB, more than the {COPY_LIMIT / 2**30:.0f} GB a "
                            "copy may take: name smaller folders")
    sid = _now_id()
    base = copy_store() / sid
    for t in targets:
        _copy(t, base / "files" / t.relative_to(home()))
    meta = {"id": sid, "uid": os.getuid(), "created": time.time(), "reason": str(reason or "")[:200], "kind": "copy",
            "paths": [str(t) for t in targets], "path": str(base / "files"), "bytes": total}
    (base / "meta.json").write_text(json.dumps(meta))
    _prune_copies()
    return meta


def _prune_copies():
    rows = snapshots(kinds=("copy",))
    now = time.time()
    for i, m in enumerate(rows):
        newest = i >= len(rows) - 3
        if len(rows) - i > KEEP or (not newest and now - m["created"] > MAX_AGE):
            shutil.rmtree(copy_store() / m["id"], ignore_errors=True)


def snapshots(kinds=("btrfs", "copy"), path=None):
    """The person's snapshots, oldest first."""
    out = []
    if "btrfs" in kinds:
        try:
            out += ask({"op": "list"}, path, 10)["snapshots"]
        except SnapshotError:
            for f in STORE.glob("*.json"):                  # the service is down: the files are still readable
                try:
                    m = json.loads(f.read_text())
                except (OSError, ValueError):
                    continue
                if m.get("uid") == os.getuid():
                    out.append(m)
    if "copy" in kinds:
        for f in copy_store().glob("*/meta.json"):
            try:
                out.append(json.loads(f.read_text()))
            except (OSError, ValueError):
                continue
    return sorted(out, key=lambda m: m.get("created", 0))


def find(sid):
    if sid in (None, "", "latest"):
        rows = snapshots()
        if not rows:
            raise SnapshotError("there is no snapshot yet")
        return rows[-1]
    m = next((m for m in snapshots() if m["id"] == sid), None)
    if not m:
        raise SnapshotError(f"no snapshot {sid!r}")
    return m


def in_snapshot(meta, target):
    """Where `target` (a path in the home folder) is inside the snapshot."""
    rel = target.relative_to(home())
    if meta["kind"] == "btrfs":
        try:
            sub = home().relative_to(HOME_ROOT.resolve())
        except ValueError:
            sub = Path(home().name)
        return Path(meta["path"]) / sub / rel
    return Path(meta["path"]) / rel


def _walk(base):
    out = {}
    if base.is_file() or base.is_symlink():
        st = base.lstat()
        return {".": (st.st_size, st.st_mtime_ns, stat.S_ISDIR(st.st_mode))}
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in (".cache", ".kimios-snapshots")]
        for name in files:
            p = Path(dirpath) / name
            try:
                st = p.lstat()
            except OSError:
                continue
            out[str(p.relative_to(base))] = (st.st_size, st.st_mtime_ns, False)
    return out


def changes(sid=None, target="~"):
    """What differs between the snapshot and now under `target`: added, changed and removed files."""
    meta = find(sid)
    t = _inside_home(target)
    then_base = in_snapshot(meta, t)
    if meta["kind"] == "copy" and not any(t == Path(p) or Path(p) in t.parents or t in Path(p).parents
                                          for p in meta.get("paths") or []):
        raise SnapshotError(f"the snapshot {meta['id']} has copies of {', '.join(meta.get('paths') or [])} only")
    then = _walk(then_base) if then_base.exists() or then_base.is_symlink() else {}
    now = _walk(t) if t.exists() or t.is_symlink() else {}
    added = sorted(k for k in now if k not in then)
    removed = sorted(k for k in then if k not in now)
    changed = sorted(k for k in now if k in then and now[k][:2] != then[k][:2])
    cut = lambda xs: xs[:MAX_CHANGES]                                                          # noqa: E731
    return {"snapshot": meta["id"], "taken": meta["created"], "reason": meta.get("reason"), "path": str(t),
            "added": cut(added), "changed": cut(changed), "removed": cut(removed),
            "counts": {"added": len(added), "changed": len(changed), "removed": len(removed)}}


def restore(sid=None, target=None):
    """Put a file or folder back as it was in the snapshot. The current version is moved aside first."""
    if not target:
        raise SnapshotError("name the file or folder to put back")
    meta = find(sid)
    t = _inside_home(target)
    if t == home():
        raise SnapshotError("restore a folder inside your home folder, not all of it at once")
    src = in_snapshot(meta, t)
    if not (src.exists() or src.is_symlink()):
        raise SnapshotError(f"{t} was not there when the snapshot {meta['id']} was taken")
    aside = None
    if t.exists() or t.is_symlink():
        aside = t.with_name(f"{t.name}.before-undo-{time.strftime('%Y%m%d-%H%M%S')}")
        os.rename(t, aside)
    try:
        _copy(src, t)
    except Exception:
        if aside is not None and not t.exists():
            os.rename(aside, t)
        raise
    return {"restored": str(t), "from": meta["id"], "kept_current_as": str(aside) if aside else None}


# ================================================================================================ the service
def main(argv=None):
    svc = Service()
    server = svc.serve()
    print(f"kimi-snapshot: btrfs {'yes' if svc.ready() else 'no'} for {svc.root}; socket {SOCKET}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
