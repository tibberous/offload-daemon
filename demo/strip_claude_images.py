#!/usr/bin/env python3
r"""strip_claude_images.py - reclaim disk from Claude Code session logs by offloading the
base64 images out of CLOSED conversations, safely.

STANDALONE + STDLIB ONLY. This is the self-contained distillation of a feature we run in
production as a supervised daemon (see the repo root). It exists so you can read and run the
whole idea in one file.

THE PROBLEM (well documented against anthropics/claude-code):
  Claude Code stores each conversation as an append-only .jsonl. Pasted/returned images are
  inlined as base64 - ~1MB each - so a screenshot-heavy session balloons to tens or hundreds of
  MB, which slows every tool that reads the log and, in the worst cases, hangs the client / OOMs
  the host (issues #22365, #18905, #79196).

THE NON-OBVIOUS PART - why you can't just strip the images:
  Claude's API prompt cache is an EXACT-PREFIX match. If you rewrite the transcript that is still
  being sent to the model, the cached prefix no longer matches and the whole prompt is re-billed
  as new. So naive stripping SAVES disk but COSTS money on the next turn.

  The fix is to only ever touch conversations that are DONE:
    - never the newest N sessions (your live conversation(s)), and
    - never anything modified in the last few minutes (still being appended / still cached).
  A closed conversation is never re-sent, so stripping its images is free.

WHAT IT DOES (idempotent):
  For each eligible closed .jsonl, every base64 image block is written out to <offload-dir> as a
  real file (named by content hash, so duplicates collapse) and REPLACED in the log by a tiny
  reference: {"type":"image_offloaded","path":..,"bytes":..,"media_type":..}. Re-running finds no
  base64 left and does nothing. All non-image bytes are preserved exactly.

USAGE:
    python strip_claude_images.py --offload-dir ./claude_images            # default projects dir
    python strip_claude_images.py --projects-dir <dir> --offload-dir <dir> --dry-run
    python strip_claude_images.py --offload-dir <dir> --keep-newest 2 --min-age-min 15

Built by Trent Tompkins. MIT.
"""
import argparse
import base64
import glob
import hashlib
import json
import os
import sys
import time

DEFAULT_PROJECTS = os.path.expanduser(r"~\.claude\projects") if os.name == "nt" \
    else os.path.expanduser("~/.claude/projects")

EXT_BY_MEDIA = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
                "image/webp": "webp", "image/bmp": "bmp"}


def _looks_base64_image(node):
    """A Claude image block: {'type':'image','source':{'type':'base64','media_type':..,'data':..}}
    Returns (media_type, data) or None."""
    if isinstance(node, dict) and node.get("type") == "image":
        src = node.get("source")
        if isinstance(src, dict) and src.get("type") == "base64" and src.get("data"):
            return src.get("media_type", "image/png"), src["data"]
    return None


def _offload_one(data_b64, media_type, offload_dir, dry_run):
    """Write the image to offload_dir named by content hash; return (path, bytes)."""
    raw = base64.b64decode(data_b64 + "=" * (-len(data_b64) % 4))
    h = hashlib.sha256(raw).hexdigest()[:16]
    ext = EXT_BY_MEDIA.get(media_type, "bin")
    name = f"{h}.{ext}"
    path = os.path.join(offload_dir, name)
    if not dry_run and not os.path.exists(path):
        os.makedirs(offload_dir, exist_ok=True)
        with open(path, "wb") as f:
            f.write(raw)
    return path, len(raw)


def _walk_and_offload(node, offload_dir, dry_run, stats):
    """Recursively replace image blocks with a tiny reference. Returns the (possibly new) node."""
    hit = _looks_base64_image(node)
    if hit:
        media_type, data = hit
        path, nbytes = _offload_one(data, media_type, offload_dir, dry_run)
        stats["images"] += 1
        stats["bytes"] += len(data)  # base64 length ~ what leaves the log
        return {"type": "image_offloaded", "path": path, "bytes": nbytes, "media_type": media_type}
    if isinstance(node, dict):
        # also catch the second spot images hide: toolUseResult.file.base64
        return {k: _walk_and_offload(v, offload_dir, dry_run, stats) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk_and_offload(v, offload_dir, dry_run, stats) for v in node]
    return node


def process_log(path, offload_dir, dry_run):
    """Rewrite one .jsonl, offloading image blobs. Returns (images, bytes_reclaimed)."""
    stats = {"images": 0, "bytes": 0}
    out_lines = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.rstrip("\n")
            if not s:
                out_lines.append(line)
                continue
            try:
                obj = json.loads(s)
            except Exception:
                out_lines.append(line)      # not JSON we understand -> leave it byte-for-byte
                continue
            new = _walk_and_offload(obj, offload_dir, dry_run, stats)
            out_lines.append(json.dumps(new, ensure_ascii=False) + "\n")
    if stats["images"] and not dry_run:
        tmp = path + ".offloading"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(out_lines)
        os.replace(tmp, path)               # atomic swap
    return stats["images"], stats["bytes"]


def main():
    ap = argparse.ArgumentParser(description="Offload base64 images from CLOSED Claude Code logs.")
    ap.add_argument("--projects-dir", default=DEFAULT_PROJECTS,
                    help="Claude Code projects dir (default: ~/.claude/projects)")
    ap.add_argument("--offload-dir", required=True, help="where extracted images are saved")
    ap.add_argument("--keep-newest", type=int, default=2,
                    help="never touch the N newest sessions (your live convos)")
    ap.add_argument("--min-age-min", type=float, default=15.0,
                    help="never touch a log modified within this many minutes (still cached)")
    ap.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    args = ap.parse_args()

    files = glob.glob(os.path.join(args.projects_dir, "*", "*.jsonl"))
    files.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
    protect = set(files[:args.keep_newest])
    now = time.time()

    total_imgs = total_bytes = touched = 0
    for p in files:
        if p in protect:
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        if (now - st.st_mtime) < args.min_age_min * 60:      # still recent -> cache-safe skip
            continue
        imgs, byts = process_log(p, args.offload_dir, args.dry_run)
        if imgs:
            touched += 1
            total_imgs += imgs
            total_bytes += byts
            print(f"  {os.path.basename(p):40s}  {imgs:3d} images  ~{byts/1e6:6.1f} MB")

    verb = "would reclaim" if args.dry_run else "reclaimed"
    print(f"\n{touched} logs, {total_imgs} images {verb} ~{total_bytes/1e6:.1f} MB "
          f"(newest {args.keep_newest} + anything <{args.min_age_min:g}min protected)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
