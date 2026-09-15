# Claude Code Image Offload

**Reclaim gigabytes from Claude Code session logs by moving inlined images out of *closed*
conversations — safely, without breaking the prompt cache.**

---

## The problem

Claude Code stores each conversation as an append-only `.jsonl`. Pasted or tool-returned images
are inlined as **base64 (~1 MB each)**, so a screenshot-heavy session balloons to tens or hundreds
of MB. That log is read constantly, so it slows everything down and, at the extremes, hangs the
client or OOMs the host. This is well documented against `anthropics/claude-code`:

- [#22365](https://github.com/anthropics/claude-code/issues/22365) — large session `.jsonl` files make Claude Code **hang and consume all available RAM**.
- [#18905](https://github.com/anthropics/claude-code/issues/18905) — session files grow to **multi-GB**.
- [#79196](https://github.com/anthropics/claude-code/issues/79196) — `--resume` reifies the whole transcript in memory; **a 12.4 GB balloon OOM-killed a 16 GB host**.

Independent analysis has put **base64 screenshots at ~22 MB of a single 73 MB session.**

## The non-obvious catch: you can't just strip the images

Claude's API **prompt cache is an exact-prefix match.** If you rewrite a transcript that is *still
being sent to the model*, the cached prefix no longer matches and the entire prompt is re-billed as
new. So naive stripping **saves disk but costs money** on the next turn. This is the reason a
cleanup tool has to be careful, and it's the crux of the design.

## The fix: only ever touch conversations that are DONE

A **closed** conversation is never re-sent, so stripping its images is free. This tool only
processes a log that is **both**:

- **not one of the newest N sessions** (your live conversation(s)), and
- **not modified in the last few minutes** (still being appended / still the cached prefix).

For each eligible log, every base64 image block is written out to your offload directory as a real
file (named by content hash, so duplicates collapse) and **replaced in the log by a tiny reference**
(`{"type":"image_offloaded","path":..,"bytes":..,"media_type":..}`). All other bytes are preserved
exactly; the result is still valid JSONL; and it's **idempotent** — a re-run finds no base64 and
does nothing.

## Try it (standalone, stdlib only)

[`demo/strip_claude_images.py`](demo/strip_claude_images.py) is the whole idea in one dependency-free
file:

```bash
python demo/strip_claude_images.py --offload-dir ./claude_images --dry-run   # report only
python demo/strip_claude_images.py --offload-dir ./claude_images             # do it
#   --projects-dir <dir>   (default: ~/.claude/projects)
#   --keep-newest 2        never touch the N newest sessions
#   --min-age-min 15       never touch a log modified in the last N minutes
```

The rest of this repo is how we run it in production: a supervised **offload daemon** (a
scheduler-as-a-daemon that also handles other bulk "offloads"), with the strip logic as one worker
and a small SQLite index so it never re-scans finished work. We run it as a daemon **only because
we live outside the extension** and have to watch the files from the side.

## How Anthropic could just build this in — on `compact`, no daemon needed

Here's the part worth the issue: **the extension already fires the perfect trigger.**

The one hard constraint above is the prompt cache — you must not rewrite the *active* prefix. But a
**compact** *already discards the old prefix* (it summarizes the prior turns and continues from the
summary). So the moment Claude Code compacts is exactly the moment the pre-compact images are no
longer part of any cached prefix — stripping them then is **free and cache-safe by construction.**

> The event that shrinks the *context* is the same event that should shrink the *log on disk.*

So the native version isn't a background service at all — it's a few lines on the compact event:
when Claude Code compacts a session, walk the now-superseded portion of that `.jsonl`, move each
base64 image to a user-configured offload directory (`~/.claude/images/` or a setting), and leave a
reference. No daemon, no polling, no cache risk, and the log stops growing without number. The
external daemon in this repo is what you have to build to do it safely *from the outside*; from the
*inside*, it's ~20 lines on an event you already emit.

---

Built by **Trent Tompkins**. MIT-licensed — take the idea, ship it in Claude Code, everybody wins.
