r"""convo_strip_offload.py - strip base64 image blobs out of CLOSED session logs.

WHY: a Claude Code session .jsonl grows mostly from inline base64 image blobs (~1MB each); a busy
day can push one past 100MB and make everything that reads it (read-aloud, log tools) crawl. This
reclaims that space by replacing each blob with a short "[[IMG_STRIPPED ...]]" marker.

CACHING-SAFE, so it never triggers the "stripped != what Anthropic cached -> billed as 100% new
prompt" trap: we ONLY touch logs that are DONE.
  - the NEWEST 2 sessions (by mtime) are never touched - those are your live conversation(s),
  - nor anything written in the last 15 min (still being appended / still the cached prefix).
So only closed convos get stripped, and the active one keeps its exact bytes for the cache.

IDEMPOTENT + INDEXED: stripping an already-stripped log finds no base64 and is a clean no-op. The
offload datastore (offload_base) remembers which files were stripped (and at what mtime) so it does
not redo finished work - but losing that index just costs one extra scan pass, never damage.

Reuses the WP-approved stripper (strip_log_images.strip_file) which handles BOTH base64 locations
the whitepaper warns about, and archives each image while stripping.
"""
FREQUENCY = 1800  # run every 30 min (the daemon reads this line WITHOUT importing the module)

import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # offload_base (same folder)
sys.path.insert(0, r"C:\Users\moren\Desktop\claude")             # strip_log_images (the WP tool)
import offload_base                                              # noqa: E402
import strip_log_images                                          # noqa: E402

SESSIONS_GLOB = r"C:\Users\moren\.claude\projects\*\*.jsonl"
SKIP_NEWEST = 2                       # never touch the live conversation(s)
SKIP_RECENT_S = 900                   # nor anything written in the last 15 min (cache-safe)
MIN_SIZE = 2 * 1024 * 1024            # not worth touching small logs


def _sessions_newest_first() -> list[str]:
    files = glob.glob(SESSIONS_GLOB)
    files.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
    return files


def main() -> int:
    files = _sessions_newest_first()
    protect = set(files[:SKIP_NEWEST])
    now = time.time()
    cx = offload_base.datastore()
    done = {r[0]: r[1] for r in cx.execute("SELECT path, mtime FROM stripped").fetchall()}
    n = 0
    saved = 0
    for f in files:
        if f in protect:
            continue
        try:
            st = os.stat(f)
        except OSError:
            continue
        if st.st_size < MIN_SIZE:
            continue
        if now - st.st_mtime < SKIP_RECENT_S:      # active / still-cached -> leave it alone
            continue
        prev = done.get(f)
        if prev is not None and abs(prev - st.st_mtime) < 1.0:  # already stripped, unchanged
            continue
        tmp = f + ".stripping"
        try:
            blobs, _ = strip_log_images.strip_file(f, tmp)
        except Exception as exc:
            offload_base.record_run("convo-strip", "error", f"{os.path.basename(f)}: {exc}")
            _rm(tmp)
            continue
        if blobs == 0:
            # nothing to strip (already stripped, or never had images) - do NOT rewrite the file:
            # an identical rewrite would churn its mtime for nothing (and confuse the newest-N guard).
            # Just index it at its CURRENT mtime so we skip it next cycle.
            _rm(tmp)
            cx.execute("INSERT OR REPLACE INTO stripped"
                       "(path,mtime,size_before,size_after,blobs,stripped_at) VALUES(?,?,?,?,?,?)",
                       (f, st.st_mtime, st.st_size, st.st_size, 0, time.strftime("%Y-%m-%d %H:%M:%S")))
            cx.commit()
            continue
        # SAFETY GATE before overwriting the real log: the output must exist, be non-empty, and be
        # no LARGER than the original (stripping only shrinks - a marker is shorter than its blob).
        try:
            tsz = os.path.getsize(tmp)
        except OSError:
            tsz = 0
        if tsz <= 0 or tsz > st.st_size:
            offload_base.record_run("convo-strip", "error", f"{os.path.basename(f)}: bad output")
            _rm(tmp)
            continue
        os.replace(tmp, f)                          # atomic swap
        st2 = os.stat(f)
        cx.execute("INSERT OR REPLACE INTO stripped"
                   "(path,mtime,size_before,size_after,blobs,stripped_at) VALUES(?,?,?,?,?,?)",
                   (f, st2.st_mtime, st.st_size, st2.st_size, blobs,
                    time.strftime("%Y-%m-%d %H:%M:%S")))
        cx.commit()
        saved += st.st_size - st2.st_size
        n += 1
    cx.close()
    offload_base.record_run("convo-strip", "ok", f"{n} logs stripped, {saved/1e6:.1f} MB reclaimed")
    return 0


def _rm(p: str) -> None:
    try:
        os.remove(p)
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
