# organizer

Local LLM file organizer for macOS. Watches an inbox folder, classifies each dropped file with a model running on your own machine, and routes it into a fixed folder taxonomy. Every run is reversible and nothing leaves the machine.

The problem it solves: an inbox folder that fills with CSV exports, meeting transcripts, PDFs, screenshots, and design assets, all named whatever the source tool happened to call them. Sorting them by hand is tedious and easy to defer indefinitely. Cloud-based auto-filing means handing every file's contents to a third-party API. This does the filing automatically, renames files to a consistent convention, and runs inference locally against Ollama, so file contents never touch the network. The safety properties matter more than the automation: a confidence gate means uncertain files get flagged instead of misfiled, and every run writes a one-command undo script.

## Quickstart

Prerequisites:

- macOS (uses launchd for scheduling and `osascript` / `terminal-notifier` for notifications)
- Python 3.9+ (standard library only, no pip dependencies)
- [Ollama](https://ollama.com) running locally, with the classifier model pulled
- Optional: `terminal-notifier` (`brew install terminal-notifier`) for richer notifications. Without it, the script falls back to `osascript`.

Setup:

```bash
# 1. Pull the model the classifier uses (about 4.7 GB)
ollama pull qwen2.5:7b
# optional fallback used if the primary model isn't available
ollama pull qwen2.5-coder:7b

# 2. Make sure Ollama is serving on the default port
ollama serve            # or just have the Ollama app running
curl http://localhost:11434/api/tags   # should return JSON

# 3. Point the organizer at a root folder (see Configuration)
#    By default it manages ~/Documents/Organized
mkdir -p ~/Documents/Organized/_inbox

# 4. Drop a file in the inbox, then do a dry run
cp ~/Downloads/export-data-2026-06-22.csv ~/Documents/Organized/_inbox/
python3 organizer.py --dry-run
```

Expected output from `--dry-run` (nothing is moved):

```
================================================================
  File Organizer — DRY RUN
  2026-06-22 14:03:11
================================================================

  1 moved
  0 flagged for review

-- Moved ----------------------------------------------------------
  export-data-2026-06-22.csv                ->  data/20260622_data_analytics_export_v1.csv
    via regex (0.95): matched pattern: ^export-data-
```

Note: this filename matches a built-in routing rule, so it's classified deterministically at 0.95 with no model call (the result is identical whether or not Ollama is running). Files that match no rule fall to the local model instead; anything that lands below the 0.80 confidence gate is routed to `_review/` with a written proposal rather than being filed. `--dry-run` writes the same summary it prints here without moving anything; when the output looks right, drop the flag to actually move files.

## How it works

A single script (`organizer.py`) runs the whole pipeline. There is no daemon and no server; each invocation processes whatever is currently in `_inbox/` and exits.

Per item, in order:

1. **Quiet-period check.** Skip anything modified in the last 60 seconds. This avoids touching files that are still being written or synced.
2. **Folders as units.** A folder dropped into `_inbox/` is classified as a single thing (by looking at the mix of file extensions inside it) and moved whole. It is never flattened.
3. **Regex / extension pre-filter.** Known filename patterns and extensions route immediately with no LLM call. This is the fast path and keeps the model out of the loop for the obvious cases.
4. **Content peek.** For text-like files, read the first ~3 KB and pass it to the model as context, so classification is based on contents, not just the filename.
5. **Local LLM classify.** Send the filename, size, and content sample to Ollama (`qwen2.5:7b`, falling back to `qwen2.5-coder:7b`). The model returns a target folder, a canonical filename, a confidence score, and a one-sentence reason, as strict JSON.
6. **Confidence gate.** At or above 0.80, the file is moved silently. Below 0.80, it goes to `_inbox/_review/` alongside a `_proposal.txt` showing what the organizer wanted to do, so you can approve or correct it by hand.
7. **Naming.** Files are renamed to `YYYYMMDD_category_descriptor_v1.ext`. Files whose names are already meaningful (`README.md`, `index.html`, `favicon.ico`, or anything already matching the convention) keep their names.
8. **Undo + notify.** The run writes `undo_<timestamp>.sh` (a plain shell script of `mv` commands that reverses everything) and fires a macOS notification with the moved / flagged counts.

Key components in the single file:

- `regex_classify` / `EXT_TO_FOLDER` / `REGEX_RULES`: the no-LLM fast path.
- `peek_content`: bounded content sampling for text files.
- `ollama_classify`: the local model call, with model fallback and strict JSON parsing.
- `propose_canonical`: filename normalization (date extraction, snake_case, version suffix).
- `classify_folder`: extension-ratio heuristics for dropped folders.
- `apply`: the move / review split, collision handling, and undo-script generation.
- `Lock`: a lockfile in `/tmp` so overlapping scheduled runs do not collide.

## Key technical decisions and tradeoffs

**Local inference, no cloud, no API key.** Inference runs against Ollama on `localhost:11434`. File contents never leave the machine, there is no key to manage, and there is no per-call cost or rate limit. The tradeoff is that a 7B local model is weaker than a frontier cloud model and adds a few seconds of latency per file, so quality leans on the confidence gate and the regex fast path rather than on raw model strength. An earlier version of this tool called a cloud API; moving to local inference removed the key-handling problem entirely and made the security story trivial.

**Confidence gate instead of blind automation.** The model is asked to return a confidence score, and anything under 0.80 is routed to `_review/` with a written proposal rather than being filed. The cost is that low-confidence items still need a human, so this is assistive, not fully autonomous. That is deliberate: a misfiled document you cannot find is worse than one sitting in a review queue.

**Regex fast path before the LLM.** Known patterns (specific export filenames, file extensions) route deterministically with no model call. This makes the common cases instant and predictable, and keeps the model reserved for genuinely ambiguous files. The cost is a hand-maintained rule list that is specific to your own corpus and has to be tuned (see Configuration).

**Per-run undo scripts.** Every run emits an executable `undo_<timestamp>.sh` that reverses exactly that run's moves. Recovery is one command and requires no state beyond the script itself. The tradeoff is that undo scripts are append-only artifacts you have to clean up yourself, and running an old one after later changes can collide with files that have since moved.

**Lockfile + dry-run + review modes.** A `/tmp` lockfile (with stale-lock reclaim after 10 minutes) prevents overlapping scheduled runs. `--dry-run` shows every proposed move without touching disk; `--review` dumps raw per-item classifications as JSON. These exist because the tool runs unattended on a schedule, and unattended file moves need to be inspectable and idempotent-ish.

**Scoped to `_inbox/` only.** The script never walks outside the inbox. The rest of the managed root is never scanned, read, or modified. This bounds the blast radius: the worst case is a file in the wrong subfolder of a known root, not an edit somewhere across your filesystem.

**Standard library only.** No third-party Python packages. The Ollama call is a raw `urllib` request. The cost is a little more boilerplate; the benefit is that `python3 organizer.py` runs on a stock macOS Python with nothing to install.

**What I'd improve:**

- The taxonomy, regex rules, and category vocabulary are constants at the top of the file. They should live in an external config (TOML or YAML) so customizing for a different corpus does not mean editing source.
- The root path is configurable via the `ORGANIZER_ROOT` environment variable, but the taxonomy still is not; both should be config.
- Folder classification is pure extension-ratio heuristics and never consults the model. Routing ambiguous folders through the LLM (or at least through the review queue) would catch the mixed-content cases it currently dumps into `_archive`.
- There are no automated tests. The pure functions (`propose_canonical`, `regex_classify`, the confidence gate) are deterministic and would be straightforward to cover, which matters more than usual for code that moves files unattended.
- Undo scripts accumulate with no retention policy.

## Running on a schedule

A launchd agent (`com.example.organizer.plist`) runs the script every 10 minutes and once at load. Before loading it, edit the placeholder paths and label:

- The absolute path to `organizer.py` in `ProgramArguments`.
- The `ORGANIZER_ROOT` value in `EnvironmentVariables`.
- `StandardOutPath` / `StandardErrorPath` log locations.
- The `Label` (`com.example.organizer`), if you want a different one.

Then:

```bash
cp com.example.organizer.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.example.organizer.plist
# to stop:
launchctl unload ~/Library/LaunchAgents/com.example.organizer.plist
```

The 10-minute interval is a predictable cadence, not event-driven file watching; combined with the 60-second quiet period it gives sync and download activity time to settle before a file is touched.

## Configuration

Configuration lives in the constants block at the top of `organizer.py` (plus the `ORGANIZER_ROOT` environment variable):

- **Root path** (`ORGANIZER_ROOT` env var, default `~/Documents/Organized`): the managed folder. `_inbox/` and `_review/` derive from it. Point this at your own directory.
- **`AUTO_CONFIDENCE`** (default `0.80`): the auto-file threshold. Raise it to send more items to review; lower it to file more automatically.
- **`QUIET_SECONDS`** (default `60`): how long a file must be untouched before processing.
- **`OLLAMA_MODEL` / `OLLAMA_FALLBACK_MODEL` / `OLLAMA_HOST`**: which local model to use and where to reach Ollama.
- **Taxonomy** (`VALID_FOLDERS`, `ALLOWED_SUBFOLDERS`): the destination folders. The shipped set (`data`, `docs`, `transcripts`, `brand`, `reference`, `meta`, `_archive`) is illustrative. Replace it with folders that match how you actually organize.
- **`EXT_TO_FOLDER` / `REGEX_RULES`**: the no-LLM routing rules. The shipped examples are placeholders; replace them with patterns from your own sources.
- **`CATEGORY_VOCAB`**: the words allowed in the `category` slot of the naming convention.

**Naming convention:** files are renamed to `YYYYMMDD_category_descriptor_v1.ext`, all lowercase, where the date is pulled from inside the original filename when present (otherwise the file's modification date), `category` is one word from the vocabulary, and `descriptor` is a short snake_case topic. Example: `20260514_transcript_team_standup_v1.md`. Files already matching this pattern, plus a small allowlist of meaningful names, are never renamed.

## Limitations / scope

- **macOS only.** Scheduling (launchd) and notifications (`osascript` / `terminal-notifier`) are macOS-specific. The classification logic is portable; the integration glue is not.
- **Single machine.** State is the inbox, the lockfile, and the undo scripts on local disk. There is no coordination across machines, and running it against a folder that another machine is also syncing into is not supported beyond the quiet-period heuristic.
- **Only touches `_inbox/`.** It will not organize files that are already elsewhere in the root, and it will not reach outside the configured root at all.
- **Quiet period is a heuristic, not a guarantee.** A 60-second window covers ordinary downloads and sync, but a very slow transfer that pauses mid-write for over a minute could still be picked up early. The undo script is the backstop.
- **Classification quality is bounded by a 7B local model.** Ambiguous files are expected to land in `_review/` rather than be filed perfectly; that is the design working as intended, not a failure mode to tune away entirely.
- **Shipped taxonomy and rules are examples.** Out of the box it will route by extension and a few generic filename patterns. It becomes genuinely useful only after you adapt the taxonomy and rules to your own files.

## License

MIT.
