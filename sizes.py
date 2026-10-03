"""DevPilot — folder sizes and other slow scans, computed in the background.

Pages must answer instantly. A size is read from the cache; if it is missing
or old, ONE background `du` is started for that folder (never two at once),
and the page gets the last known value (or None = "calcul..."). The cache is
kept in data/sizes.json so a restart does not start from zero.
"""

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import db

CACHE_FILE = db.DB_PATH.parent / "sizes.json"
TTL = 15 * 60                     # a size older than this is refreshed in the background

_lock = threading.Lock()
_sizes = {}                       # path -> {"size": int, "at": float}
_running = set()                  # keys being computed
_calls = {}                       # key -> {"value": any, "at": float}
_slots = threading.Semaphore(2)   # at most 2 du at a time: the machine stays usable


def _load():
    try:
        data = json.loads(CACHE_FILE.read_text())
        _sizes.update({k: v for k, v in data.items() if isinstance(v, dict) and "size" in v})
    except (OSError, ValueError):
        pass


def _save():
    try:
        tmp = CACHE_FILE.with_suffix(".tmp")
        with _lock:
            data = dict(_sizes)
        tmp.write_text(json.dumps(data))
        tmp.replace(CACHE_FILE)
    except OSError:
        pass


def _du(path):
    """Bytes of a folder with du (C, much faster than Python), low priority."""
    cmd = ["du", "-sb", "--", path]
    if os.path.exists("/usr/bin/ionice"):
        cmd = ["ionice", "-c3"] + cmd
    cmd = ["nice", "-n", "15"] + cmd
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        return int(r.stdout.split()[0]) if r.stdout else None
    except (subprocess.TimeoutExpired, ValueError, IndexError, OSError):
        return None


def _compute(path):
    try:
        with _slots:
            size = _du(path)
        if size is not None:
            with _lock:
                _sizes[path] = {"size": size, "at": time.time()}
            _save()
    finally:
        with _lock:
            _running.discard(path)


def size(path, ttl=TTL, wait=0.0):
    """Last known size of a folder (bytes) or None while the first computation runs.
    A missing/old value starts one background `du`. wait: seconds to wait for a fresh one."""
    if not path:
        return None
    path = str(Path(os.path.expanduser(str(path))))
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return None
    if not os.path.isdir(path):
        return 0
    with _lock:
        cur = _sizes.get(path)
        stale = cur is None or time.time() - cur["at"] > ttl
        start = stale and path not in _running
        if start:
            _running.add(path)
    if start:
        t = threading.Thread(target=_compute, args=(path,), daemon=True)
        t.start()
        if wait:
            t.join(wait)
            with _lock:
                cur = _sizes.get(path, cur)
    return cur["size"] if cur else None


def invalidate_all():
    """After a cleanup / deletion: every size is recomputed in the background on next read."""
    with _lock:
        for v in _sizes.values():
            v["at"] = 0
        for v in _calls.values():
            v["at"] = 0


def invalidate(path):
    with _lock:
        _sizes.pop(str(Path(os.path.expanduser(str(path)))), None)


def computing(path):
    with _lock:
        return str(Path(os.path.expanduser(str(path)))) in _running


def cached_call(key, fn, ttl=TTL):
    """Result of a slow function (e.g. a scan of ~/Desktop): last result now, refresh in the background.
    Returns (value or None, computing: bool)."""
    with _lock:
        cur = _calls.get(key)
        stale = cur is None or time.time() - cur["at"] > ttl
        start = stale and key not in _running
        if start:
            _running.add(key)

    def run():
        try:
            value = fn()
            with _lock:
                _calls[key] = {"value": value, "at": time.time()}
        finally:
            with _lock:
                _running.discard(key)

    if start:
        threading.Thread(target=run, daemon=True).start()
    with _lock:
        return (cur["value"] if cur else None), key in _running


def fmt_or_pending(n, fmt):
    return fmt(n) if n is not None else "calcul..."


_load()
