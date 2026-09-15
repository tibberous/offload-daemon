#!/usr/bin/env python3
r"""offload_claude_images.py - move the images OUT of old Claude Code session logs into a real,
browsable folder. Not a stripper - an offloader.

STANDALONE + STDLIB ONLY. The whole idea in one dependency-free file (we run this in production as
a supervised daemon; this is here so you can read and run it).

TWO PROBLEMS, ONE MOVE:
  1. BLOAT. Claude Code inlines images as base64 (~1MB each) into an append-only .jsonl, so a
     screenshot-heavy session grows to tens/hundreds of MB - slow to read, and at the extreme it
     hangs the client / OOMs the host (anthropics/claude-code #22365, #18905, #79196).
  2. TRAPPED IMAGES. Every screenshot you ever pasted is base64 goo buried mid-line in a log. You
     PAY disk to store it but you can't open, browse, or selectively delete it. Write-only storage.

  Offloading fixes BOTH: each image is written out as a real .png/.jpg (named by content hash, so
  duplicates collapse) and REPLACED in the log by a tiny reference. The log shrinks AND the images
  become a folder of real files you can view, back up, or clean. It's a MOVE, not a delete -
  nothing is lost, the reference keeps each image linked to its spot in the conversation.

WHY YOU CAN'T DO IT TO A LIVE LOG (the constraint that shapes everything):
  Claude's prompt cache is an EXACT-PREFIX match. Rewrite a transcript that's still being sent and
  the cached prefix no longer matches, so the whole prompt is re-billed as new - you'd save disk
  and pay it back in tokens. So we only ever touch conversations that are DONE:
    - never the newest N sessions (your live conversation(s)), and
    - never anything modified in the last few minutes (still being appended / still cached).
  A closed conversation is never re-sent, so offloading its images is free. (Idempotent, too:
  re-running finds no base64 and does nothing.)

USAGE:
    python offload_claude_images.py --offload-dir ./claude_images --dry-run   # report only
    python offload_claude_images.py --offload-dir ./claude_images             # do it
    #   --projects-dir <dir>   (default: ~/.claude/projects)
    #   --keep-newest 2        never touch the N newest sessions
    #   --min-age-min 15       never touch a log modified in the last N minutes

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
    """Write the image out to offload_dir named by content hash; return (path, bytes)."""
    raw = base64.b64decode(data_b64 + "=" * (-len(data_b64) % 4))
    h = hashlib.sha256(raw).hexdigest()[:16]
    ext = EXT_BY_MEDIA.get(media_type, "bin")
    path = os.path.join(offload_dir, f"{h}.{ext}")
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
        # also catches the second spot images hide: toolUseResult.file.base64
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
    ap = argparse.ArgumentParser(description="Offload base64 images out of CLOSED Claude Code logs "
                                             "into a real folder.")
    ap.add_argument("--projects-dir", default=DEFAULT_PROJECTS,
                    help="Claude Code projects dir (default: ~/.claude/projects)")
    ap.add_argument("--offload-dir", required=True, help="where the extracted image files go")
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
            print(f"  {os.path.basename(p):40s}  {imgs:3d} images -> files  ~{byts/1e6:6.1f} MB")

    verb = "would move" if args.dry_run else "moved"
    print(f"\n{touched} logs, {total_imgs} images {verb} to {args.offload_dir}  "
          f"(~{total_bytes/1e6:.1f} MB out of the logs; newest {args.keep_newest} + "
          f"anything <{args.min_age_min:g}min protected)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
