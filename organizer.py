#!/usr/bin/env python3
"""
File Organizer — drops files placed in <root>/_inbox/ into the right
folder, leaves the rest of <root>/ alone, asks before doing anything weird.

Pipeline per item:
  1. Quiet-period check (skip files younger than QUIET_SECONDS, still syncing)
  2. Folders dropped in _inbox are treated as ONE UNIT (no flattening)
  3. Regex pre-filter — known patterns route with no LLM
  4. Content peek — read first 3KB for text-y files, give Ollama real signal
  5. Ollama classify — qwen2.5:7b (small, fast) returns folder + filename + confidence
  6. Confidence gate:
       >= AUTO_CONFIDENCE       -> move silently
       < AUTO_CONFIDENCE        -> move to _inbox/_review/ with _proposal.txt
  7. Naming preservation — files with meaningful filenames keep them (index.html,
     favicon.ico, README.md, files already matching YYYYMMDD_... convention)
  8. Write undo_<timestamp>.sh — one command rolls back the run
  9. Send macOS notification

NEVER walks anywhere outside _inbox/. The taxonomy below is the only thing
this script knows how to file into.

Run modes:
  python3 organizer.py            # apply mode (scheduled)
  python3 organizer.py --dry-run  # print what would happen, touch nothing
  python3 organizer.py --review   # just dump per-item classification

Point it at your own corpus with the ORGANIZER_ROOT environment variable.
Config knobs near the top.
"""

from __future__ import annotations
import argparse, hashlib, json, logging, os, re, shutil, subprocess, sys, time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List

# --- Configuration -----------------------------------------------------------

HOME = Path.home()
# The managed folder. _inbox/ and _review/ derive from it. Override with the
# ORGANIZER_ROOT env var; defaults to ~/Documents/Organized.
ROOT = Path(os.environ.get("ORGANIZER_ROOT", str(HOME / "Documents" / "Organized")))
INBOX = ROOT / "_inbox"
REVIEW = INBOX / "_review"
LOG_DIR = HOME / "Library/Logs"
LOG_FILE = LOG_DIR / "organizer.log"
LATEST_FILE = LOG_DIR / "organizer-latest.txt"
UNDO_DIR = ROOT / "meta" / "organizer_undo"
LOCK_FILE = Path("/tmp/organizer.lock")
ICON_FILE = Path(__file__).resolve().parent / "icon.png"

QUIET_SECONDS = 60         # don't touch files younger than this
AUTO_CONFIDENCE = 0.80     # below this, route to _inbox/_review/
OLLAMA_MODEL = "qwen2.5:7b"
OLLAMA_FALLBACK_MODEL = "qwen2.5-coder:7b"  # if the general one isn't pulled
OLLAMA_HOST = "http://localhost:11434"
OLLAMA_TIMEOUT_SEC = 30

# Taxonomy. These are illustrative defaults — customize for your own corpus.
VALID_FOLDERS = {"data", "docs", "transcripts", "brand", "reference", "meta", "_archive"}
# Subfolders allowed under these for high-volume topics
ALLOWED_SUBFOLDERS = {
    "data":     {"analytics", "exports", "charts"},
    "docs":     {"api", "email", "security", "seo"},
    "brand":    {"design-system", "charts"},
    "_archive": {"_discard", "audio", "data", "docs", "reference"},
}

# YYYYMMDD_category_descriptor[_vN].ext — the house naming convention
CANONICAL_NAME_RE = re.compile(r"^\d{8}_[a-z_]+_[a-z0-9_]+(_v\d+)?\.[a-z0-9]+$")

# Filenames whose existing name is sacred — don't rename even if they look odd
KEEP_NAME_PATTERNS = [
    re.compile(r"^README\.md$", re.I),
    re.compile(r"^index\.(html|css|js)$", re.I),
    re.compile(r"^favicon\.(ico|png|svg)$", re.I),
    CANONICAL_NAME_RE,
]

# Vocabulary the descriptor's category slot can use
CATEGORY_VOCAB = [
    "playbook", "toolkit", "brief", "pack", "plan", "strategy", "interview",
    "transcript", "email", "drip", "outreach", "asset", "data", "reference",
    "prompt", "notes", "inventory", "article", "template", "audit", "spec",
]

# --- Logging -----------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("organizer")


# --- Lock (so two runs don't overlap) ----------------------------------------

class Lock:
    def __enter__(self):
        if LOCK_FILE.exists():
            age = time.time() - LOCK_FILE.stat().st_mtime
            if age < 600:
                log.info(f"Another run is in progress (lock age {age:.0f}s). Skipping.")
                sys.exit(0)
            log.warning(f"Stale lock ({age:.0f}s old), reclaiming.")
        LOCK_FILE.write_text(str(os.getpid()))
        return self
    def __exit__(self, *a):
        try: LOCK_FILE.unlink()
        except FileNotFoundError: pass


# --- Classification result ---------------------------------------------------

class Decision:
    def __init__(self, src: Path, *, folder: str, new_name: str,
                 confidence: float, source: str, reason: str):
        self.src = src
        self.folder = folder      # e.g. "data/analytics" or "docs"
        self.new_name = new_name  # final filename only
        self.confidence = confidence
        self.source = source      # "ext", "regex", "ollama", "folder", etc.
        self.reason = reason

    @property
    def dst(self) -> Path:
        return ROOT / self.folder / self.new_name

    def to_dict(self):
        return {
            "src": str(self.src.relative_to(INBOX)),
            "folder": self.folder, "new_name": self.new_name,
            "confidence": self.confidence, "source": self.source,
            "reason": self.reason,
        }


# --- Pre-filter: regex routing for known patterns ----------------------------

EXT_TO_FOLDER = {
    # Tabular
    ".csv": "data", ".tsv": "data", ".xlsx": "data", ".json": "data",
    # Visual
    ".png": "brand", ".jpg": "brand", ".jpeg": "brand", ".webp": "brand",
    ".svg": "brand", ".gif": "brand", ".ico": "brand",
    # Fonts
    ".woff": "brand/design-system", ".woff2": "brand/design-system",
    ".ttf": "brand/design-system", ".otf": "brand/design-system",
    # Reference
    ".pdf": "reference",
    # Design components
    ".html": "brand/design-system", ".css": "brand/design-system",
    # Audio
    ".m4a": "_archive/audio", ".mp3": "_archive/audio", ".wav": "_archive/audio",
}

# Illustrative routing rules — replace the patterns with ones from your own sources.
REGEX_RULES = [
    # Grafana CSV exports
    (re.compile(r"(grafana|data_grafana)", re.I), ".csv", "data", 0.95),
    # Dated analytics export (descriptor normalized in propose_canonical)
    (re.compile(r"^export-data-"), ".csv", "data", 0.95),
    # Import error reports
    (re.compile(r"^error_\d+\.csv$", re.I), ".csv", "data", 0.95),
    # Google Search Console / Analytics exports
    (re.compile(r"(_gsc_|_ga_|seo)", re.I), ".csv", "data/analytics", 0.95),
    # Meeting transcripts (file-named)
    (re.compile(r"^Meet-|^transcript-|recording_download_link"), ".md", "transcripts", 0.85),
    # Already-canonical YYYYMMDD_*_*_v*.ext — trust the filename
    (re.compile(r"^\d{8}_transcript_"), ".md", "transcripts", 0.95),
    (re.compile(r"^\d{8}_email_"), ".md", "docs/email", 0.90),
    (re.compile(r"^\d{8}_prompt_"), ".md", "meta", 0.85),
]


def name_should_be_kept(name: str) -> bool:
    return any(p.match(name) for p in KEEP_NAME_PATTERNS)


def regex_classify(p: Path) -> Optional[Decision]:
    name = p.name
    ext = p.suffix.lower()
    for rx, expected_ext, folder, conf in REGEX_RULES:
        if rx.search(name) and (not expected_ext or ext == expected_ext):
            new_name = name if name_should_be_kept(name) else propose_canonical(p, folder)
            return Decision(p, folder=folder, new_name=new_name,
                            confidence=conf, source="regex",
                            reason=f"matched pattern: {rx.pattern}")
    if ext in EXT_TO_FOLDER:
        folder = EXT_TO_FOLDER[ext]
        # Lower confidence — ext is a coarse signal
        new_name = name if name_should_be_kept(name) else propose_canonical(p, folder)
        return Decision(p, folder=folder, new_name=new_name,
                        confidence=0.75, source="ext",
                        reason=f"extension {ext} -> {folder}")
    return None


def propose_canonical(p: Path, folder: str) -> str:
    """If file lacks the YYYYMMDD_category_descriptor pattern, propose one.
    If it already has it, keep it. Returns a filename only (no folder)."""
    name = p.name
    if CANONICAL_NAME_RE.match(name):
        return name
    stem = p.stem
    ext = p.suffix.lower()
    # Common export naming conventions — normalize to the house format
    m = re.match(r"^export-data-(\d{4})-(\d{2})-(\d{2})", name)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}_data_analytics_export_v1{ext}"
    m = re.match(r"^error_(\d+)$", stem)
    if m:
        dt = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y%m%d")
        return f"{dt}_data_error_{m.group(1)}_v1{ext}"
    # Prefer a date embedded in the filename so we don't double up
    m = re.search(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})", stem)
    if m:
        dt = f"{m.group(1)}{m.group(2)}{m.group(3)}"
        stem = stem[:m.start()] + stem[m.end():]
    else:
        dt = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y%m%d")
    # Strip residual time-of-day patterns (e.g. 18_13_37, 18-13, 18:13:37)
    stem = re.sub(r"\d{1,2}[_:\-.]\d{2}([_:\-.]\d{2})?", "", stem)
    # snake_case cleanup
    cleaned = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned) or "file"
    # Guess category from folder
    category = {
        "data": "data", "docs": "notes", "transcripts": "transcript",
        "brand": "asset", "reference": "reference", "meta": "notes",
        "_archive": "archive",
    }.get(folder.split("/")[0], "file")
    return f"{dt}_{category}_{cleaned}_v1{ext}"


# --- Content peek ------------------------------------------------------------

def peek_content(p: Path, limit: int = 3000) -> str:
    """Return a sample of file content suitable for LLM context."""
    ext = p.suffix.lower()
    try:
        if ext in {".md", ".txt", ".html", ".css", ".js", ".json", ".yaml", ".yml", ".toml", ".csv", ".tsv"}:
            return p.read_text(encoding="utf-8", errors="replace")[:limit]
    except Exception as e:
        return f"<read error: {e}>"
    return f"<binary {ext}, {p.stat().st_size} bytes>"


# --- Ollama classify ---------------------------------------------------------

OLLAMA_SYSTEM = """You route files into one of these folders for the user's corpus:

data            tabular: csv, xlsx, json, tsv
docs            business writing (.md) — subfolders: api, email, security, seo
transcripts     meeting transcripts (.md) — verbatim speaker dialogue with timestamps
brand           visual assets: images, design-system HTML/CSS
reference       PDFs and external research kept as-is
meta            ops/spec/prompts/inventories about the system itself
_archive        superseded versions

Filename format: YYYYMMDD_<category>_<descriptor>_v1.<ext>
  - YYYYMMDD: 8-digit date. Prefer a date found inside the original filename; otherwise use today.
  - category: ONE word from this list (pick the best fit):
      __CATEGORY_VOCAB__
  - descriptor: short snake_case topic (1-4 words). No dates, no times, no spaces, no dashes.
  - All lowercase. Examples:
      20260515_data_user_strategy_v1.csv
      20260514_transcript_team_standup_v1.md
      20260507_prompt_podcast_outline_v1.md

Respond with ONLY a single JSON object. No explanation, no prose, no markdown fence:
{"folder": "<folder or folder/subfolder>", "filename": "<canonical filename>", "confidence": <0.0-1.0>, "reason": "<one sentence>"}

Confidence rule: 0.9+ only if you're certain. Otherwise 0.6-0.85. If uncertain, say so with confidence < 0.5.""".replace("__CATEGORY_VOCAB__", ", ".join(CATEGORY_VOCAB))

def ollama_classify(p: Path) -> Optional[Decision]:
    content = peek_content(p, 2500)
    user = f"Filename: {p.name}\nSize: {p.stat().st_size} bytes\nExtension: {p.suffix}\n\nContent sample:\n{content[:2500]}"

    import urllib.request
    req = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": OLLAMA_SYSTEM},
            {"role": "user", "content": user},
        ],
        "options": {"temperature": 0.1},
    }
    for model in [OLLAMA_MODEL, OLLAMA_FALLBACK_MODEL]:
        req["model"] = model
        body = json.dumps(req).encode("utf-8")
        try:
            r = urllib.request.Request(f"{OLLAMA_HOST}/api/chat", data=body,
                                       headers={"Content-Type":"application/json"})
            with urllib.request.urlopen(r, timeout=OLLAMA_TIMEOUT_SEC) as resp:
                raw = json.loads(resp.read())
            out = json.loads(raw["message"]["content"])
            folder = out.get("folder","").strip("/")
            top = folder.split("/")[0]
            if top not in VALID_FOLDERS:
                log.warning(f"Ollama returned invalid folder '{folder}' for {p.name}")
                return None
            sub = folder.split("/")[1] if "/" in folder else None
            if sub and sub not in ALLOWED_SUBFOLDERS.get(top, set()):
                log.info(f"Subfolder '{sub}' not allowed under '{top}'; routing to '{top}'")
                folder = top
            candidate = (out.get("filename") or "").strip()
            if name_should_be_kept(p.name):
                new_name = p.name
            elif candidate and CANONICAL_NAME_RE.match(candidate):
                new_name = candidate
            else:
                new_name = propose_canonical(p, folder)
            conf = float(out.get("confidence", 0.5))
            reason = out.get("reason", "ollama classification")
            return Decision(p, folder=folder, new_name=new_name,
                            confidence=conf, source=f"ollama:{model}",
                            reason=reason)
        except Exception as e:
            log.warning(f"Ollama call failed with {model}: {e}")
            continue
    return None


# --- Folder handling ---------------------------------------------------------

def classify_folder(folder: Path) -> Decision:
    """A folder dropped into _inbox is treated as one unit.
    Look at its contents to guess what kind of folder it is."""
    items = list(folder.rglob("*"))
    file_items = [i for i in items if i.is_file()]
    if not file_items:
        return Decision(folder, folder="_archive", new_name=folder.name,
                        confidence=0.4, source="folder-empty",
                        reason="empty folder, archiving")
    exts = [i.suffix.lower() for i in file_items]
    img_ratio = sum(1 for e in exts if e in {".png",".jpg",".jpeg",".webp",".svg"}) / len(exts)
    code_ratio = sum(1 for e in exts if e in {".html",".css",".js",".tsx",".ts",".woff",".woff2",".ttf"}) / len(exts)
    csv_ratio  = sum(1 for e in exts if e in {".csv",".xlsx",".tsv"}) / len(exts)
    md_ratio   = sum(1 for e in exts if e == ".md") / len(exts)
    if code_ratio > 0.5 or "design" in folder.name.lower() or "tokens.css" in [i.name for i in file_items]:
        return Decision(folder, folder="brand/design-system", new_name=folder.name,
                        confidence=0.85, source="folder-design",
                        reason=f"folder looks like design-system ({code_ratio:.0%} code/css)")
    if img_ratio > 0.6:
        return Decision(folder, folder="brand", new_name=folder.name,
                        confidence=0.85, source="folder-images",
                        reason=f"{img_ratio:.0%} images")
    if csv_ratio > 0.5:
        return Decision(folder, folder="data", new_name=folder.name,
                        confidence=0.85, source="folder-data",
                        reason=f"{csv_ratio:.0%} tabular")
    if md_ratio > 0.5:
        return Decision(folder, folder="docs", new_name=folder.name,
                        confidence=0.7, source="folder-docs",
                        reason=f"{md_ratio:.0%} markdown")
    return Decision(folder, folder="_archive", new_name=folder.name,
                    confidence=0.4, source="folder-mixed",
                    reason="mixed content, can't classify with confidence")


# --- Main pipeline -----------------------------------------------------------

def gather_items() -> List[Path]:
    if not INBOX.exists():
        INBOX.mkdir(parents=True, exist_ok=True)
        return []
    items = []
    for entry in INBOX.iterdir():
        if entry.name.startswith(".") or entry.name == "_review":
            continue
        # quiet period
        try:
            age = time.time() - entry.stat().st_mtime
        except FileNotFoundError:
            continue
        if age < QUIET_SECONDS:
            log.info(f"Skipping (young, {age:.0f}s): {entry.name}")
            continue
        items.append(entry)
    return items


def classify(item: Path) -> Decision:
    if item.is_dir():
        return classify_folder(item)
    # Try regex/ext first
    d = regex_classify(item)
    if d and d.confidence >= AUTO_CONFIDENCE:
        return d
    # Ollama
    d2 = ollama_classify(item)
    if d2 and d2.confidence >= (d.confidence if d else 0):
        return d2
    if d:
        return d
    return Decision(item, folder="_archive", new_name=item.name,
                    confidence=0.3, source="fallback",
                    reason="no rule matched and Ollama unavailable")


def apply(decisions: List[Decision], dry_run: bool = False) -> Tuple[List, List]:
    moved = []
    reviewed = []
    REVIEW.mkdir(parents=True, exist_ok=True)
    UNDO_DIR.mkdir(parents=True, exist_ok=True)
    undo_path = UNDO_DIR / f"undo_{datetime.now():%Y%m%d_%H%M%S}.sh"
    undo_lines = ["#!/bin/bash", "# Reverse the most recent organizer run", "set -e"]

    for d in decisions:
        if d.confidence < AUTO_CONFIDENCE:
            # Route to _inbox/_review/ with a proposal note
            target = REVIEW / d.src.name
            proposal = REVIEW / f"{d.src.stem}_proposal.txt"
            if not dry_run:
                shutil.move(str(d.src), str(target))
                proposal.write_text(json.dumps(d.to_dict(), indent=2))
                undo_lines.append(f'mv "{target}" "{d.src}"')
                undo_lines.append(f'rm -f "{proposal}"')
            reviewed.append(d)
            log.info(f"REVIEW  {d.src.name} (conf={d.confidence:.2f} via {d.source})")
        else:
            dst = d.dst
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                # Collision: append -2 etc.
                i = 2
                while dst.with_name(f"{dst.stem}_{i}{dst.suffix}").exists():
                    i += 1
                dst = dst.with_name(f"{dst.stem}_{i}{dst.suffix}")
            if not dry_run:
                shutil.move(str(d.src), str(dst))
                undo_lines.append(f'mv "{dst}" "{d.src}"')
            moved.append((d, dst))
            log.info(f"MOVE    {d.src.name} -> {dst.relative_to(ROOT)} (conf={d.confidence:.2f} via {d.source})")

    if not dry_run and (moved or reviewed):
        undo_lines.append("echo 'Undo complete.'")
        undo_path.write_text("\n".join(undo_lines))
        undo_path.chmod(0o755)
    return moved, reviewed


def write_latest(moved, reviewed, dry: bool):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "=" * 64,
        f"  File Organizer — {'DRY RUN' if dry else 'EXECUTE'}",
        f"  {ts}",
        "=" * 64, "",
        f"  {len(moved)} moved",
        f"  {len(reviewed)} flagged for review",
        "",
    ]
    if moved:
        lines.append("-- Moved ----------------------------------------------------------")
        for d, dst in moved:
            lines.append(f"  {d.src.name:<40}  ->  {dst.relative_to(ROOT)}")
            lines.append(f"    via {d.source} ({d.confidence:.2f}): {d.reason[:60]}")
    if reviewed:
        lines.append("")
        lines.append("-- Needs review (-> _inbox/_review/) ------------------------------")
        for d in reviewed:
            lines.append(f"  {d.src.name}  (conf {d.confidence:.2f}, {d.source})")
            lines.append(f"    proposal: {d.folder}/{d.new_name}")
    lines.append("")
    lines.append(f"Full log: {LOG_FILE}")
    LATEST_FILE.write_text("\n".join(lines))


def notify(title: str, body: str):
    tn = shutil.which("terminal-notifier")
    if tn and ICON_FILE.exists():
        try:
            subprocess.run([
                tn, "-title", title, "-message", body,
                "-contentImage", str(ICON_FILE),
                "-appIcon", str(ICON_FILE),
                "-group", "organizer",
            ], timeout=5, check=True)
            return
        except Exception:
            pass
    try:
        subprocess.run([
            "osascript", "-e",
            f'display notification "{body}" with title "{title}"'
        ], timeout=5)
    except Exception:
        pass


# --- Entry point -------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Don't move anything")
    ap.add_argument("--review", action="store_true", help="Print classifications, exit")
    args = ap.parse_args()

    with Lock():
        log.info("=== Organizer run starting ===")
        items = gather_items()
        if not items:
            log.info("Nothing in _inbox/ to process.")
            return

        decisions = [classify(it) for it in items]

        if args.review:
            for d in decisions:
                print(json.dumps(d.to_dict(), indent=2))
            return

        moved, reviewed = apply(decisions, dry_run=args.dry_run)
        write_latest(moved, reviewed, args.dry_run)
        print(LATEST_FILE.read_text())

        if moved or reviewed:
            summary = f"{len(moved)} filed, {len(reviewed)} need review"
            if not args.dry_run:
                notify("File Organizer", summary)
            log.info(f"Run complete: {summary}")
        else:
            log.info("Run complete: nothing to do")


if __name__ == "__main__":
    main()
