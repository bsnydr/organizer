"""Deterministic unit tests for organizer.py.

These tests never touch Ollama or the network. They exercise only the
deterministic logic the README and prior audit flagged as load-bearing:
the confidence gate, the _review/ collision guard, the move-branch
collision guard, undo-script generation, the regex/extension fast path,
the canonical-naming helper, and dry-run safety.

The organizer module reads ROOT / INBOX / REVIEW / UNDO_DIR from the
ORGANIZER_ROOT environment variable AT IMPORT TIME, so each test points
ORGANIZER_ROOT at a fresh temp dir and reloads the module to rebind those
module-level globals.
"""

import importlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path


def load_organizer(root: Path):
    """(Re)import organizer.py with ORGANIZER_ROOT pointed at `root`.

    The module computes ROOT/INBOX/REVIEW/UNDO_DIR at import time, so we set
    the env var first and reload to rebind those globals to `root`.
    """
    os.environ["ORGANIZER_ROOT"] = str(root)
    import organizer  # noqa: WPS433 (import inside function is intentional)
    importlib.reload(organizer)
    return organizer


class OrganizerTestBase(unittest.TestCase):
    """Gives each test a fresh ROOT temp dir and a freshly reloaded module."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.org = load_organizer(self.root)
        # Sanity: the module's globals should now point inside our temp root.
        self.assertEqual(self.org.ROOT, self.root)
        self.assertEqual(self.org.INBOX, self.root / "_inbox")
        self.assertEqual(self.org.REVIEW, self.root / "_inbox" / "_review")
        self.org.INBOX.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def make_inbox_file(self, name: str, content: str = "x") -> Path:
        """Create a real file inside INBOX and return its path."""
        p = self.org.INBOX / name
        p.write_text(content)
        return p

    def decision(self, src: Path, *, folder, new_name, confidence,
                 source="test", reason="test"):
        return self.org.Decision(
            src, folder=folder, new_name=new_name,
            confidence=confidence, source=source, reason=reason,
        )


class ConfidenceGateTests(OrganizerTestBase):
    """Risk 1: the AUTO_CONFIDENCE gate routes high vs low confidence."""

    def test_auto_confidence_threshold_value(self):
        self.assertEqual(self.org.AUTO_CONFIDENCE, 0.80)

    def test_high_confidence_file_is_moved_to_destination(self):
        src = self.make_inbox_file("report.csv", "col1,col2\n1,2\n")
        d = self.decision(src, folder="data", new_name="20260101_data_report_v1.csv",
                          confidence=0.95)
        moved, reviewed = self.org.apply([d], dry_run=False)

        self.assertEqual(len(moved), 1)
        self.assertEqual(len(reviewed), 0)
        _, dst = moved[0]
        # Destination is ROOT/folder/new_name and the content arrived intact.
        self.assertEqual(dst, self.root / "data" / "20260101_data_report_v1.csv")
        self.assertTrue(dst.exists())
        self.assertEqual(dst.read_text(), "col1,col2\n1,2\n")
        # Original no longer in the inbox.
        self.assertFalse(src.exists())
        # Nothing landed in the review queue.
        self.assertFalse(any(self.org.REVIEW.glob("*")) if self.org.REVIEW.exists() else False)

    def test_confidence_at_threshold_is_moved_not_reviewed(self):
        # Exactly AUTO_CONFIDENCE counts as "auto" (gate is >=).
        src = self.make_inbox_file("edge.csv")
        d = self.decision(src, folder="data", new_name="20260101_data_edge_v1.csv",
                          confidence=self.org.AUTO_CONFIDENCE)
        moved, reviewed = self.org.apply([d], dry_run=False)
        self.assertEqual(len(moved), 1)
        self.assertEqual(len(reviewed), 0)

    def test_low_confidence_file_is_routed_to_review_not_destination(self):
        src = self.make_inbox_file("mystery.bin", "unknown")
        dst_folder_before = self.root / "data"
        d = self.decision(src, folder="data", new_name="20260101_data_mystery_v1.bin",
                          confidence=0.50)
        moved, reviewed = self.org.apply([d], dry_run=False)

        self.assertEqual(len(moved), 0)
        self.assertEqual(len(reviewed), 1)
        # It is NOT at the proposed destination.
        self.assertFalse((self.root / "data" / "20260101_data_mystery_v1.bin").exists())
        self.assertFalse(dst_folder_before.exists())
        # It lives in _review/ under its ORIGINAL basename, content intact.
        review_target = self.org.REVIEW / "mystery.bin"
        self.assertTrue(review_target.exists())
        self.assertEqual(review_target.read_text(), "unknown")
        # A paired proposal note exists and is valid JSON describing the decision.
        proposal = self.org.REVIEW / "mystery_proposal.txt"
        self.assertTrue(proposal.exists())
        payload = json.loads(proposal.read_text())
        self.assertEqual(payload["folder"], "data")
        self.assertEqual(payload["confidence"], 0.50)


class ReviewCollisionGuardTests(OrganizerTestBase):
    """Risk 2: two different low-confidence files sharing a basename must
    both survive in _review/ with correctly paired proposals."""

    def test_two_same_named_low_confidence_files_both_survive(self):
        # Two DIFFERENT source files with the same basename, in different
        # inbox subdirs, both below the gate.
        sub_a = self.org.INBOX / "a"
        sub_b = self.org.INBOX / "b"
        sub_a.mkdir()
        sub_b.mkdir()
        src1 = sub_a / "notes.txt"
        src2 = sub_b / "notes.txt"
        src1.write_text("CONTENT-ONE")
        src2.write_text("CONTENT-TWO")

        d1 = self.decision(src1, folder="docs", new_name="20260101_notes_a_v1.txt",
                           confidence=0.40)
        d2 = self.decision(src2, folder="docs", new_name="20260101_notes_b_v1.txt",
                           confidence=0.40)
        moved, reviewed = self.org.apply([d1, d2], dry_run=False)

        self.assertEqual(len(moved), 0)
        self.assertEqual(len(reviewed), 2)

        first = self.org.REVIEW / "notes.txt"
        second = self.org.REVIEW / "notes_2.txt"
        self.assertTrue(first.exists(), "first low-conf file should keep its basename")
        self.assertTrue(second.exists(), "second should be de-duped to notes_2.txt")

        # Neither original's content was lost or overwritten.
        contents = {first.read_text(), second.read_text()}
        self.assertEqual(contents, {"CONTENT-ONE", "CONTENT-TWO"})

        # Correctly paired proposals: notes_proposal.txt and notes_2_proposal.txt.
        prop1 = self.org.REVIEW / "notes_proposal.txt"
        prop2 = self.org.REVIEW / "notes_2_proposal.txt"
        self.assertTrue(prop1.exists())
        self.assertTrue(prop2.exists(), "second proposal must pair with notes_2.txt")
        # The _2 proposal describes the SECOND decision (folder/new_name match d2).
        payload2 = json.loads(prop2.read_text())
        self.assertEqual(payload2["new_name"], "20260101_notes_b_v1.txt")

    def test_three_same_named_low_confidence_files_increment(self):
        srcs = []
        for i, marker in enumerate(("ONE", "TWO", "THREE")):
            sub = self.org.INBOX / f"d{i}"
            sub.mkdir()
            p = sub / "dup.txt"
            p.write_text(marker)
            srcs.append(p)
        decisions = [
            self.decision(p, folder="docs", new_name=f"20260101_dup_{i}_v1.txt",
                          confidence=0.30)
            for i, p in enumerate(srcs)
        ]
        self.org.apply(decisions, dry_run=False)

        names = {p.name for p in self.org.REVIEW.glob("dup*.txt")
                 if not p.name.endswith("_proposal.txt")}
        self.assertEqual(names, {"dup.txt", "dup_2.txt", "dup_3.txt"})
        markers = {(self.org.REVIEW / n).read_text()
                   for n in ("dup.txt", "dup_2.txt", "dup_3.txt")}
        self.assertEqual(markers, {"ONE", "TWO", "THREE"})


class MoveCollisionGuardTests(OrganizerTestBase):
    """Risk 3: two high-confidence files mapping to the same dst de-dup
    without overwriting."""

    def test_two_high_confidence_same_dst_dedup(self):
        sub_a = self.org.INBOX / "a"
        sub_b = self.org.INBOX / "b"
        sub_a.mkdir()
        sub_b.mkdir()
        src1 = sub_a / "f.csv"
        src2 = sub_b / "f.csv"
        src1.write_text("FIRST")
        src2.write_text("SECOND")

        # Both map to the exact same destination filename/folder.
        same_name = "20260101_data_f_v1.csv"
        d1 = self.decision(src1, folder="data", new_name=same_name, confidence=0.95)
        d2 = self.decision(src2, folder="data", new_name=same_name, confidence=0.95)
        moved, reviewed = self.org.apply([d1, d2], dry_run=False)

        self.assertEqual(len(moved), 2)
        self.assertEqual(len(reviewed), 0)

        dst_dir = self.root / "data"
        first = dst_dir / same_name
        second = dst_dir / "20260101_data_f_v1_2.csv"
        self.assertTrue(first.exists())
        self.assertTrue(second.exists(), "second high-conf file must de-dup to _2")
        self.assertEqual({first.read_text(), second.read_text()}, {"FIRST", "SECOND"})
        # The recorded dst paths in `moved` reflect the de-dup.
        recorded = {str(dst) for _, dst in moved}
        self.assertEqual(recorded, {str(first), str(second)})

    def test_three_high_confidence_same_dst_increment_to_3(self):
        markers = ("A", "B", "C")
        decisions = []
        for i, marker in enumerate(markers):
            sub = self.org.INBOX / f"s{i}"
            sub.mkdir()
            p = sub / "g.csv"
            p.write_text(marker)
            decisions.append(
                self.decision(p, folder="data", new_name="20260101_data_g_v1.csv",
                              confidence=0.99)
            )
        moved, _ = self.org.apply(decisions, dry_run=False)
        self.assertEqual(len(moved), 3)
        dst_dir = self.root / "data"
        names = {p.name for p in dst_dir.glob("*.csv")}
        self.assertEqual(
            names,
            {"20260101_data_g_v1.csv",
             "20260101_data_g_v1_2.csv",
             "20260101_data_g_v1_3.csv"},
        )


class UndoScriptTests(OrganizerTestBase):
    """Risk 4: a real apply writes an executable undo_*.sh whose mv lines
    would restore each moved file to its original path."""

    def test_undo_script_written_executable_and_restores_paths(self):
        src_move = self.make_inbox_file("doc.md", "BODY")
        src_review = self.make_inbox_file("weird.dat", "REVIEWME")
        d_move = self.decision(src_move, folder="docs",
                               new_name="20260101_notes_doc_v1.md", confidence=0.95)
        d_review = self.decision(src_review, folder="docs",
                                 new_name="20260101_notes_weird_v1.dat", confidence=0.40)
        moved, reviewed = self.org.apply([d_move, d_review], dry_run=False)

        self.assertEqual(len(moved), 1)
        self.assertEqual(len(reviewed), 1)

        scripts = list(self.org.UNDO_DIR.glob("undo_*.sh"))
        self.assertEqual(len(scripts), 1, "exactly one undo script per run")
        undo = scripts[0]

        # Executable bit set.
        mode = undo.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR, "undo script must be user-executable")

        text = undo.read_text()
        self.assertTrue(text.startswith("#!/bin/bash"))

        _, moved_dst = moved[0]
        # The moved file's undo line restores dst -> original src path.
        self.assertIn(f'mv "{moved_dst}" "{src_move}"', text)
        # The reviewed file's undo line restores its review target -> original src.
        review_target = self.org.REVIEW / "weird.dat"
        self.assertIn(f'mv "{review_target}" "{src_review}"', text)
        # And the proposal note it wrote gets cleaned up on undo.
        self.assertIn('rm -f "', text)
        self.assertIn("weird_proposal.txt", text)

    def test_no_undo_script_when_nothing_to_do(self):
        moved, reviewed = self.org.apply([], dry_run=False)
        self.assertEqual(moved, [])
        self.assertEqual(reviewed, [])
        # UNDO_DIR may be created, but no undo script should be written.
        if self.org.UNDO_DIR.exists():
            self.assertEqual(list(self.org.UNDO_DIR.glob("undo_*.sh")), [])


class RegexAndExtensionClassifyTests(OrganizerTestBase):
    """Risk 5a: deterministic regex / extension fast-path classification."""

    def test_regex_rule_routes_grafana_csv_high_confidence(self):
        p = self.make_inbox_file("grafana_dashboard.csv", "a,b\n")
        d = self.org.regex_classify(p)
        self.assertIsNotNone(d)
        self.assertEqual(d.folder, "data")
        self.assertEqual(d.source, "regex")
        self.assertGreaterEqual(d.confidence, self.org.AUTO_CONFIDENCE)

    def test_regex_rule_routes_gsc_to_data_analytics(self):
        p = self.make_inbox_file("report_gsc_2026.csv", "a,b\n")
        d = self.org.regex_classify(p)
        self.assertIsNotNone(d)
        self.assertEqual(d.folder, "data/analytics")
        self.assertEqual(d.source, "regex")

    def test_extension_fast_path_lower_confidence(self):
        # A bare .pdf with no regex hit routes via the extension map, and at
        # 0.75 it is BELOW the auto-confidence gate (so it would go to review).
        p = self.make_inbox_file("whatever.pdf", "%PDF-1.4")
        d = self.org.regex_classify(p)
        self.assertIsNotNone(d)
        self.assertEqual(d.folder, "reference")
        self.assertEqual(d.source, "ext")
        self.assertAlmostEqual(d.confidence, 0.75)
        self.assertLess(d.confidence, self.org.AUTO_CONFIDENCE)

    def test_unknown_extension_returns_none(self):
        p = self.make_inbox_file("thing.xyz", "data")
        self.assertIsNone(self.org.regex_classify(p))

    def test_name_should_be_kept_for_sacred_names(self):
        self.assertTrue(self.org.name_should_be_kept("README.md"))
        self.assertTrue(self.org.name_should_be_kept("index.html"))
        self.assertTrue(self.org.name_should_be_kept("favicon.ico"))
        # Already-canonical name is also "kept".
        self.assertTrue(self.org.name_should_be_kept("20260101_data_report_v1.csv"))
        # An arbitrary name is not sacred.
        self.assertFalse(self.org.name_should_be_kept("random_file.csv"))


class CanonicalNamingTests(OrganizerTestBase):
    """Risk 5b: the canonical / slugify naming helper is deterministic."""

    def test_already_canonical_name_is_preserved(self):
        p = self.make_inbox_file("20260101_data_user_strategy_v1.csv")
        self.assertEqual(
            self.org.propose_canonical(p, "data"),
            "20260101_data_user_strategy_v1.csv",
        )

    def test_export_data_pattern_normalized(self):
        p = self.make_inbox_file("export-data-2026-05-15.csv")
        self.assertEqual(
            self.org.propose_canonical(p, "data"),
            "20260515_data_analytics_export_v1.csv",
        )

    def test_embedded_date_extracted_and_slugified(self):
        # Date inside the name is reused; the rest is snake_cased and lowercased.
        p = self.make_inbox_file("My Cool Report 2026-03-09.md")
        result = self.org.propose_canonical(p, "docs")
        self.assertTrue(result.startswith("20260309_notes_"))
        self.assertTrue(result.endswith("_v1.md"))
        # Slug is all-lowercase snake_case, no spaces or capitals.
        self.assertNotIn(" ", result)
        self.assertEqual(result, result.lower())
        self.assertIn("my_cool_report", result)

    def test_category_guessed_from_folder(self):
        p = self.make_inbox_file("plain notes.md")
        # transcripts -> "transcript" category slot.
        self.assertIn("_transcript_", self.org.propose_canonical(p, "transcripts"))
        # brand -> "asset".
        self.assertIn("_asset_", self.org.propose_canonical(p, "brand"))


class DryRunTests(OrganizerTestBase):
    """Risk 6: dry_run=True moves nothing and writes no artifacts."""

    def test_dry_run_moves_nothing(self):
        src_move = self.make_inbox_file("a.csv", "MOVE")
        src_review = self.make_inbox_file("b.dat", "REVIEW")
        d_move = self.decision(src_move, folder="data",
                               new_name="20260101_data_a_v1.csv", confidence=0.95)
        d_review = self.decision(src_review, folder="data",
                                 new_name="20260101_data_b_v1.dat", confidence=0.40)
        moved, reviewed = self.org.apply([d_move, d_review], dry_run=True)

        # The lists are still populated (so the summary can report intent)...
        self.assertEqual(len(moved), 1)
        self.assertEqual(len(reviewed), 1)
        # ...but the originals are untouched and intact.
        self.assertTrue(src_move.exists())
        self.assertTrue(src_review.exists())
        self.assertEqual(src_move.read_text(), "MOVE")
        self.assertEqual(src_review.read_text(), "REVIEW")
        # No destination, no review file, no proposal, no undo script.
        self.assertFalse((self.root / "data" / "20260101_data_a_v1.csv").exists())
        if self.org.REVIEW.exists():
            self.assertEqual(list(self.org.REVIEW.glob("*")), [])
        if self.org.UNDO_DIR.exists():
            self.assertEqual(list(self.org.UNDO_DIR.glob("undo_*.sh")), [])


if __name__ == "__main__":
    unittest.main()
