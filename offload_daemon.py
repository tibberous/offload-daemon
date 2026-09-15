r"""offload_daemon.py - the Offload daemon (a cog), the exact shape as the Probe daemon.

ONE daemon that schedules every offload in C:\daemons\offload\offloads\, instead of a daemon per
bulk-mover. An "offload" moves BIG regeneratable data out of a hot place and keeps a small index so
it never redoes finished work (convo-strip drains image blobs from closed session logs; up-images
drains the VPS /up/ folder to the local archive). Drop a new <name>_offload.py with a FREQUENCY line
and it's scheduled automatically - config by convention, the worker-pool payoff.

Each offload is a PROCESS worker (define_worker), so a wedged network/disk call is reaped at its ttl.
The shared index is a DATASTORE (offload_base -> C:\datastores\offload_index), anti-fragile: delete
it any time to reclaim space and it rebuilds (see C:\datastores\README.md).

    pythonw offload_daemon.py             # run (or let Manager start it)
    py -3.14 offload_daemon.py --install   # create/define the projects row + services row
    py -3.14 offload_daemon.py --uninstall
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

sys.path.insert(0, r"C:\services\daemon-manager")               # kernel
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # this dir
OFFLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "offloads")
sys.path.insert(0, OFFLOADS_DIR)                                 # offload_base for status_extra
from daemon_base import Daemon  # noqa: E402
import svc_registry as reg      # noqa: E402

DEFAULT_FREQ = 3600
LAUNCH_SPEC = {
    "plane": "managed", "script": os.path.abspath(__file__),
    "port": "5105", "max_workers": "4", "frequency": "resident",
}


def _read_frequency(path: str) -> int:
    """Read an offload's FREQUENCY without importing it (offloads lazy-import heavy deps)."""
    try:
        m = re.search(r"^FREQUENCY\s*=\s*(\d+)", open(path, encoding="utf-8").read(), re.M)
        return int(m.group(1)) if m else DEFAULT_FREQ
    except OSError:
        return DEFAULT_FREQ


def discover() -> list[tuple[str, str, int]]:
    """(name, path, freq) for every *_offload.py (offload_base.py is the base, not an offload)."""
    out = []
    for path in sorted(glob.glob(os.path.join(OFFLOADS_DIR, "*_offload.py"))):
        name = re.sub(r"_offload\.py$", "", os.path.basename(path))
        out.append((name, path, _read_frequency(path)))
    return out


class OffloadDaemon(Daemon):
    slug = "offload"
    default_frequency = "resident"

    def configure(self) -> None:
        for name, path, freq in discover():
            # process worker; long ttl - stripping a 100MB log or draining /up/ takes a bit. count=1.
            self.define_worker(name, path, freq=freq, ttl_ms=300_000, count=1)

    def status_extra(self) -> dict:
        out = {"offloads": [n for n, _, _ in discover()]}
        try:
            import offload_base
            cx = offload_base.datastore()
            out["stripped_count"] = cx.execute("SELECT COUNT(*) FROM stripped").fetchone()[0]
            mb = cx.execute("SELECT COALESCE(SUM(size_before-size_after),0) FROM stripped").fetchone()[0]
            out["mb_reclaimed"] = round((mb or 0) / 1e6, 1)
            out["recent_runs"] = [{"offload": r[0], "status": r[1], "detail": r[2], "ts": r[3]}
                                  for r in cx.execute("SELECT offload,status,detail,ts FROM runs "
                                                      "ORDER BY id DESC LIMIT 8").fetchall()]
            cx.close()
        except Exception as exc:
            out["ds_error"] = str(exc)
        return out


# --------------------------- install (a NEW cog: create the projects row too) ---------------------------
def _ensure_project() -> int:
    with reg.connect() as cx, cx.cursor() as cur:
        cur.execute("SELECT id FROM projects WHERE slug='offload'")
        row = cur.fetchone()
        if row:
            pid = row[0]
        else:
            cur.execute("INSERT INTO projects(slug, name, kind) VALUES('offload','Offload','daemon') "
                        "RETURNING id")
            pid = cur.fetchone()[0]
        cur.execute("DELETE FROM project_meta WHERE project_id=%s AND kind = ANY(%s)",
                    (pid, list(LAUNCH_SPEC)))
        for k, v in LAUNCH_SPEC.items():
            cur.execute("INSERT INTO project_meta(project_id, kind, value) VALUES(%s,%s,%s)",
                        (pid, k, v))
        cx.commit()
    return pid


if __name__ == "__main__":
    if "--install" in sys.argv:
        _ensure_project()
        print(json.dumps(reg.install("offload"), indent=2))
    elif "--uninstall" in sys.argv:
        print(json.dumps(reg.uninstall("offload"), indent=2))
    else:
        raise SystemExit(OffloadDaemon().serve())
