# Claude Code Image Offload — shrink runaway session logs *and* unlock your trapped images

**Two separate problems, one clean move.** It doesn't just *strip* images to cut log size (that's
half the story and throws your pictures away) — it *offloads* them: moves every image out to a real
file on disk and leaves a reference. So you get **(1)** small, fast session logs **and** **(2)** a
browsable archive of every screenshot you ever pasted — all cache-safe.

> A log *stripper* is an afternoon — nobody needs help building one. The idea worth shipping is the
> **reframe**: those old images aren't bloat to *delete*, they're an asset to *relocate*. Strip and
> you've cleaned up; **offload and you've handed the user an image library for free** — the exact
> same operation, seen through a different lens. That lens is the whole point.

The word matters: **offload, not strip.** A *stripper* deletes the images to save space. An
*offloader* moves them to real files on disk and leaves a reference behind — so you lose nothing,
you just stop storing pictures inside a log file.

---

## Two problems, one solution

**Problem 1 — the logs are bloated.** Claude Code stores each conversation as an append-only
`.jsonl`, and pasted / tool-returned images are inlined as **base64 (~1 MB each)**. A
screenshot-heavy session balloons to tens or hundreds of MB, which slows every read and, at the
extreme, hangs the client or OOMs the host — well documented against `anthropics/claude-code`:

- [#22365](https://github.com/anthropics/claude-code/issues/22365) — large session `.jsonl` files make Claude Code **hang and consume all available RAM**.
- [#18905](https://github.com/anthropics/claude-code/issues/18905) — session files grow to **multi-GB**.
- [#79196](https://github.com/anthropics/claude-code/issues/79196) — `--resume` reifies the whole transcript; **a 12.4 GB balloon OOM-killed a 16 GB host**.

(One analysis put base64 screenshots at **~22 MB of a single 73 MB session.**)

**Problem 2 — and nobody names this one: your images are trapped.** Every screenshot you ever
pasted is sitting in those logs as base64 goo. You are **paying disk space to store images you
cannot use** — you can't open them, browse them, or delete just the ones you don't want, because
they're buried mid-line in a JSONL blob. It's write-only storage.

**Offloading fixes both at once.** Move each image out to `~/.claude/images/` as an actual
`.png`/`.jpg` (named by content hash, so duplicates collapse) and leave a tiny reference in the log.
Now the log is small (problem 1) **and** every image is a real file you can view, back up, or
selectively clean (problem 2) — the same disk you were already paying for, finally accessible. One
clean move, two wins.

## The catch: you can't do this to a *live* log

Claude's API **prompt cache is an exact-prefix match.** If you rewrite a transcript that is *still
being sent to the model*, the cached prefix no longer matches and the entire prompt is re-billed as
new. So touching the *active* log **saves disk but costs money** on the next turn. That one
constraint shapes the whole design — and it's exactly why `compact` (below) is the elegant native
trigger.

## The fix: only ever touch conversations that are DONE

A **closed** conversation is never re-sent, so offloading its images is free. This tool only
processes a log that is **both**:

- **not one of the newest N sessions** (your live conversation(s)), and
- **not modified in the last few minutes** (still being appended / still the cached prefix).

For each eligible log, every base64 image block is written out to your offload directory as a real
file (named by content hash, so duplicates collapse) and **replaced in the log by a tiny reference**
(`{"type":"image_offloaded","path":..,"bytes":..,"media_type":..}`). All other bytes are preserved
exactly; the result is still valid JSONL; and it's **idempotent** — a re-run finds no base64 and
does nothing.

## Try it (standalone, stdlib only)

[`demo/offload_claude_images.py`](demo/offload_claude_images.py) is the whole idea in one dependency-free
file:

```bash
python demo/offload_claude_images.py --offload-dir ./claude_images --dry-run   # report only
python demo/offload_claude_images.py --offload-dir ./claude_images             # do it
#   --projects-dir <dir>   (default: ~/.claude/projects)
#   --keep-newest 2        never touch the N newest sessions
#   --min-age-min 15       never touch a log modified in the last N minutes
```

The rest of this repo is how we run it in production: a supervised **offload daemon** (a
scheduler-as-a-daemon that also handles other bulk "offloads"), with the offload logic as one worker
and a small SQLite index so it never re-scans finished work. We run it as a daemon **only because
we live outside the extension** and have to watch the files from the side.

## How Anthropic could just build this in — on `compact`, no daemon needed

Here's the part worth the issue: **the extension already fires the perfect trigger.**

The one hard constraint above is the prompt cache — you must not rewrite the *active* prefix. But a
**compact** *already discards the old prefix* (it summarizes the prior turns and continues from the
summary). So the moment Claude Code compacts is exactly the moment the pre-compact images are no
longer part of any cached prefix — offloading them then is **free and cache-safe by construction.**

> The event that shrinks the *context* is the same event that should shrink the *log on disk.*

So the native version isn't a background service at all — it's a few lines on the compact event:
when Claude Code compacts a session, walk the now-superseded portion of that `.jsonl`, move each
base64 image to a user-configured directory (`~/.claude/images/` or a setting), and leave a
reference. No daemon, no polling, no cache risk — the log stops growing without bound **and** the
user gets a real folder of their screenshots they can browse and clean. The external daemon in this
repo is what you have to build to do it safely *from the outside*; from the *inside*, it's ~20 lines
on an event you already emit.

---

Built by **Trent Tompkins**. MIT-licensed — take the idea, ship it in Claude Code, everybody wins.
