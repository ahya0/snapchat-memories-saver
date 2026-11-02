import csv
import datetime as dt
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import audit_export  # noqa: E402


IMAGE_NAME = "2024-01-01_AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA-main.jpg"
OVERLAY_NAME = "2024-01-01_AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA-overlay.png"
VIDEO_NAME = "2024-01-02_BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB-main.mp4"


def utc_timestamp(date_text: str) -> float:
    return dt.datetime.strptime(date_text, "%Y-%m-%d %H:%M:%S UTC").replace(
        tzinfo=dt.timezone.utc
    ).timestamp()


def make_export(
    root: Path,
    *,
    rows=None,
    media=None,
    html_names=None,
    extra_history=None,
) -> None:
    memories = root / "memories"
    history_dir = root / "json"
    memories.mkdir(parents=True)
    history_dir.mkdir(parents=True)
    if rows is None:
        rows = [
            {
                "Date": "2024-01-01 00:00:00 UTC",
                "Media Type": "Image",
                "Location": "Private place",
                "Unexpected": "https://private.example/token",
            }
        ]
    payload = {"Saved Media": rows}
    if extra_history:
        payload.update(extra_history)
    (history_dir / "memories_history.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    if media is None:
        media = {IMAGE_NAME: b"image-bytes", OVERLAY_NAME: b"overlay-bytes"}
    for name, data in media.items():
        path = memories / name
        path.write_bytes(data)
        date = "2024-01-02 00:00:00 UTC" if name == VIDEO_NAME else "2024-01-01 00:00:00 UTC"
        timestamp = utc_timestamp(date)
        os.utime(path, (timestamp, timestamp))
    if html_names is None:
        html_names = list(media)
    tags = []
    for name in html_names:
        tag = "video" if name.endswith(".mp4") else "img"
        tags.append(f'<{tag} src=".//{name}">')
    (memories / "memories.html").write_text("\n".join(tags), encoding="utf-8")


def snapshot_tree(root: Path):
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        result[relative] = (
            stat.S_IFMT(info.st_mode),
            info.st_size,
            info.st_mtime_ns,
            path.read_bytes() if path.is_file() and not path.is_symlink() else None,
        )
    return result


class AuditExportTests(unittest.TestCase):
    def test_deeply_nested_history_is_a_controlled_cli_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "deep-history"
            make_export(export)
            nested = "[" * 100_000 + "0" + "]" * 100_000
            (export / "json" / "memories_history.json").write_text(
                '{"Saved Media": [], "Ignored": ' + nested + "}",
                encoding="utf-8",
            )
            output = base / "report"

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "audit_export.py"),
                    "--export",
                    str(export),
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 2)
            self.assertEqual(
                result.stderr, "audit failed: unsafe or invalid input/output\n"
            )
            self.assertNotIn("Traceback", result.stdout + result.stderr)
            self.assertFalse(output.exists())

    def test_open_regular_closes_descriptor_when_fstat_fails(self):
        with mock.patch.object(audit_export.os, "open", return_value=123), mock.patch.object(
            audit_export.os, "fstat", side_effect=OSError("synthetic fstat failure")
        ), mock.patch.object(audit_export.os, "close") as close_mock:
            with self.assertRaises(OSError):
                audit_export._open_regular_at(10, "file.jpg")

        close_mock.assert_called_once_with(123)

    def test_max_files_is_global_across_all_export_parts(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            first = base / "first"
            second = base / "second"
            make_export(first)
            make_export(second)

            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports(
                    [first, second], base / "report", max_files=7
                )

            self.assertFalse((base / "report").exists())

    def test_csv_cells_neutralize_spreadsheet_formula_prefixes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(
                export,
                rows=[
                    {
                        "Date": "=1+1",
                        "Media Type": "@SUM(1,2)",
                        "Location": "+cmd",
                    }
                ],
                media={IMAGE_NAME: b"image-bytes"},
                html_names=[IMAGE_NAME],
            )
            output = base / "report"

            audit_export.audit_exports(
                [export], output, include_location=True
            )

            for filename in ("manifest.csv", "missing_history_records.csv"):
                with (output / filename).open(encoding="utf-8", newline="") as handle:
                    for row in csv.reader(handle):
                        for cell in row:
                            stripped = cell.lstrip()
                            self.assertFalse(
                                stripped.startswith(("=", "+", "-", "@")),
                                (filename, cell),
                            )
            with (output / "missing_history_records.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["Date"], "'=1+1")
            self.assertEqual(row["Media Type"], "'@SUM(1,2)")
            self.assertEqual(row["Location"], "'+cmd")

    def test_resource_budgets_reject_dense_edges_and_media_bytes_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "dense"
            rows = [
                {"Date": "2024-01-01 00:00:00 UTC", "Media Type": "Image"}
                for _ in range(20)
            ]
            media = {}
            for index in range(20):
                name = (
                    f"2024-01-01_{index:08X}-0000-0000-0000-000000000000-main.jpg"
                )
                media[name] = b"x"
            make_export(export, rows=rows, media=media, html_names=list(media))

            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports(
                    [export], base / "edge-report", max_candidate_edges=100
                )
            self.assertFalse((base / "edge-report").exists())

            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports(
                    [export], base / "byte-report", max_total_media_bytes=10
                )
            self.assertFalse((base / "byte-report").exists())

            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports(
                    [export], base / "metadata-report", max_total_metadata_bytes=16
                )
            self.assertFalse((base / "metadata-report").exists())

            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports(
                    [export], base / "history-report", max_history_records=10
                )
            self.assertFalse((base / "history-report").exists())

    def test_html_reference_requires_exact_relative_media_path(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            (export / "memories" / "memories.html").write_text(
                f'<img src=".//does-not-exist/{IMAGE_NAME}">'
                f'<img src=".//{OVERLAY_NAME}">',
                encoding="utf-8",
            )

            summary = audit_export.audit_exports([export], base / "report")

            self.assertEqual(summary["status"], "incomplete")
            self.assertEqual(summary["html_missing_reference_count"], 1)
            self.assertEqual(summary["html_unreferenced_media_count"], 1)

    def test_report_write_failure_never_publishes_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            output = base / "report"
            original = audit_export._write_exclusive_atomic
            calls = 0

            def fail_second(directory_fd, name, payload):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("synthetic write failure")
                return original(directory_fd, name, payload)

            with mock.patch.object(
                audit_export, "_write_exclusive_atomic", side_effect=fail_second
            ):
                with self.assertRaises(OSError):
                    audit_export.audit_exports([export], output)

            self.assertFalse(output.exists())
            self.assertEqual(
                [path.name for path in base.iterdir() if path.name.startswith(".audit-stage-")],
                [],
            )

    def test_multipart_export_allows_media_only_parts_when_one_part_has_history(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            history_part = base / "part-1"
            media_part = base / "part-2"
            make_export(history_part)
            make_export(
                media_part,
                rows=[],
                media={VIDEO_NAME: b"video-bytes"},
                html_names=[VIDEO_NAME],
            )
            (media_part / "json" / "memories_history.json").unlink()
            history = json.loads(
                (history_part / "json" / "memories_history.json").read_text(encoding="utf-8")
            )
            history["Saved Media"].append(
                {"Date": "2024-01-02 00:00:00 UTC", "Media Type": "Video"}
            )
            (history_part / "json" / "memories_history.json").write_text(
                json.dumps(history), encoding="utf-8"
            )

            summary = audit_export.audit_exports(
                [history_part, media_part], base / "report"
            )

            self.assertEqual(summary["status"], "complete")
            self.assertEqual(summary["export_count"], 2)
            self.assertEqual(summary["history_record_count"], 2)
            self.assertEqual(summary["main_media_file_count"], 2)

    def test_happy_path_creates_private_relative_reports_without_mutating_input(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "private-user-export-name"
            make_export(export)
            before = snapshot_tree(export)
            output = base / "report"

            summary = audit_export.audit_exports([export], output)

            self.assertEqual(summary["status"], "complete")
            self.assertTrue(summary["history_file_reconciliation_complete"])
            self.assertTrue(summary["html_evidence_complete"])
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {"summary.json", "manifest.csv", "missing_history_records.csv", "report.txt"},
            )
            self.assertEqual(snapshot_tree(export), before)
            combined = "\n".join(
                path.read_text(encoding="utf-8") for path in output.iterdir()
            )
            self.assertNotIn(str(base), combined)
            self.assertNotIn(export.name, combined)
            self.assertNotIn("Private place", combined)
            self.assertNotIn("http://", combined)
            self.assertNotIn("https://", combined)
            self.assertIn("export-1/memories/", combined)
            self.assertIn("do not prove every image pixel or video frame decodes", combined)
            self.assertIn("ZIP integrity was not tested", combined)
    def test_retry_exports_use_multiset_max_and_deduplicate_identical_media(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            first = base / "first"
            second = base / "second"
            make_export(first)
            make_export(second)
            output = base / "report"

            summary = audit_export.audit_exports([first, second], output)

            self.assertEqual(summary["status"], "complete")
            self.assertEqual(summary["history_record_count"], 1)
            self.assertEqual(summary["logical_media_file_count"], 2)
            self.assertEqual(summary["identical_repeated_media_count"], 2)
            with (output / "manifest.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual({row["identical_source_count"] for row in rows}, {"2"})

    def test_history_merge_preserves_maximum_multiplicity_per_export(self):
        repeated = {"Date": "2024-01-01 00:00:00 UTC", "Media Type": "Image"}
        new = {"Date": "2024-01-02 00:00:00 UTC", "Media Type": "Video"}
        scans = [
            audit_export.ExportScan(1, [[repeated, repeated]], [], [], set()),
            audit_export.ExportScan(2, [[repeated, repeated, new]], [], [], set()),
        ]

        merged = audit_export._merge_history(scans)

        self.assertEqual(merged.count(repeated), 2)
        self.assertEqual(merged.count(new), 1)
        self.assertEqual(len(merged), 3)

    def test_conflicting_same_name_media_fails_before_output_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            first = base / "first"
            second = base / "second"
            make_export(first)
            make_export(
                second,
                media={IMAGE_NAME: b"different", OVERLAY_NAME: b"overlay-bytes"},
            )
            output = base / "report"

            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([first, second], output)

            self.assertFalse(output.exists())

    def test_unicode_and_case_aliases_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            decomposed = "Cafe\u0301-main.jpg"
            composed = "Caf\u00e9-main.jpg"
            first_media = audit_export.MediaFile(
                1, (decomposed,), f"export-1/{decomposed}", decomposed, "main", "jpg", 1, 0, "a" * 64
            )
            second_media = audit_export.MediaFile(
                2, (composed,), f"export-2/{composed}", composed, "main", "jpg", 1, 0, "a" * 64
            )
            with self.assertRaises(audit_export.AuditError):
                audit_export._logical_media(
                    [
                        audit_export.ExportScan(1, [], [first_media], [], set()),
                        audit_export.ExportScan(2, [], [second_media], [], set()),
                    ]
                )

            first = base / "case-one"
            second = base / "case-two"
            make_export(first)
            lower_name = IMAGE_NAME.lower()
            make_export(
                second,
                rows=[{"Date": "2024-01-01 00:00:00 UTC", "Media Type": "Image"}],
                media={lower_name: b"image-bytes"},
                html_names=[lower_name],
            )
            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([first, second], base / "case-report")

    def test_iterative_matching_handles_2500_sparse_and_1100_dense_records(self):
        def logical(count, same_timestamp):
            items = []
            for index in range(count):
                timestamp = 1704067200.0 if same_timestamp else 1704067200.0 + index
                name = f"2024-01-01_{index:08X}-0000-0000-0000-000000000000-main.jpg"
                media = audit_export.MediaFile(
                    1,
                    ("memories", name),
                    f"export-1/memories/{name}",
                    name,
                    "main",
                    "jpg",
                    1,
                    timestamp,
                    f"{index:064x}",
                )
                items.append({"key": audit_export.alias(name), "file": media, "source_count": 1, "empty": False})
            return items

        sparse_history = [
            {
                "Date": dt.datetime.fromtimestamp(1704067200 + index, tz=dt.timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"
                ),
                "Media Type": "Image",
            }
            for index in range(2500)
        ]
        _, sparse_matches, sparse_missing, sparse_unmatched = audit_export._match_history(
            sparse_history, logical(2500, False)
        )
        self.assertEqual(len(sparse_matches), 2500)
        self.assertEqual(sparse_missing, [])
        self.assertEqual(sparse_unmatched, [])

        dense_history = [
            {"Date": "2024-01-01 00:00:00 UTC", "Media Type": "Image"}
            for _ in range(1100)
        ]
        dense_media = logical(1100, True)
        first = audit_export._match_history(dense_history, dense_media)
        second = audit_export._match_history(dense_history, list(reversed(dense_media)))
        self.assertEqual(len(first[1]), 1100)
        self.assertEqual(first[2], [])
        self.assertEqual(first[3], [])
        self.assertEqual(first[1], second[1])
    def test_malformed_history_shapes_and_metadata_limits_fail_before_output(self):
        invalid_payloads = [
            [],
            {},
            {"Saved Media": {}},
            {"Saved Media": ["not-a-record"]},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                base = Path(os.path.realpath(directory))
                export = base / "export"
                make_export(export)
                (export / "json" / "memories_history.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )
                output = base / "report"
                with self.assertRaises(audit_export.AuditError):
                    audit_export.audit_exports([export], output)
                self.assertFalse(output.exists())

        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "json-limit"
            make_export(export)
            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], base / "report-json", max_json_bytes=8)
            self.assertFalse((base / "report-json").exists())

        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "html-limit"
            make_export(export)
            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], base / "report-html", max_html_bytes=8)
            self.assertFalse((base / "report-html").exists())

    def test_file_count_depth_symlink_and_special_file_limits_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "count"
            make_export(export)
            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], base / "count-report", max_files=2)
            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], base / "depth-report", max_depth=1)
            self.assertFalse((base / "count-report").exists())
            self.assertFalse((base / "depth-report").exists())

        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "symlink"
            make_export(export)
            (export / "link").symlink_to(export / "memories" / IMAGE_NAME)
            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], base / "symlink-report")
            self.assertFalse((base / "symlink-report").exists())

        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "fifo"
            make_export(export)
            os.mkfifo(export / "named-pipe")
            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], base / "fifo-report")
            self.assertFalse((base / "fifo-report").exists())

    def test_output_existing_symlink_unsafe_ancestor_and_inside_input_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)

            existing = base / "existing"
            existing.mkdir()
            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], existing)

            target = base / "target"
            target.mkdir()
            output_link = base / "output-link"
            output_link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], output_link)

            real_parent = base / "real-parent"
            real_parent.mkdir()
            linked_parent = base / "linked-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], linked_parent / "report")

            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], export / "report")

            self.assertEqual(list(target.iterdir()), [])
            self.assertEqual(list(real_parent.iterdir()), [])
            self.assertFalse((export / "report").exists())

    def test_location_is_opt_in_and_url_like_location_is_never_written(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            output = base / "with-location"

            audit_export.audit_exports([export], output, include_location=True)

            combined = "\n".join(path.read_text(encoding="utf-8") for path in output.iterdir())
            self.assertIn("Location", combined)
            self.assertIn("Private place", combined)

        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "url-location"
            make_export(
                export,
                rows=[
                    {
                        "Date": "2024-01-01 00:00:00 UTC",
                        "Media Type": "Image",
                        "Location": "https://private.example/location",
                    }
                ],
            )
            output = base / "report"
            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], output, include_location=True)
            self.assertFalse(output.exists())

    def test_html_evidence_is_relative_and_causes_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "secret-source-name"
            make_export(export, html_names=[])
            memories = export / "memories"
            extra = memories / "note.txt"
            extra.write_text("note", encoding="utf-8")
            missing = "2024-01-09_CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC-main.jpg"
            (memories / "memories.html").write_text(
                f'<img src=".//{missing}"><img src=".//{extra.name}"><img src=".//../escape">',
                encoding="utf-8",
            )
            output = base / "report"

            summary = audit_export.audit_exports([export], output)

            self.assertEqual(summary["status"], "incomplete")
            self.assertTrue(summary["history_file_reconciliation_complete"])
            self.assertFalse(summary["html_evidence_complete"])
            self.assertEqual(summary["html_missing_reference_count"], 1)
            self.assertEqual(summary["html_unexpected_reference_count"], 2)
            self.assertEqual(summary["html_unreferenced_media_count"], 2)
            summary_text = (output / "summary.json").read_text(encoding="utf-8")
            self.assertIn("export-1/", summary_text)
            self.assertNotIn(str(base), summary_text)
            self.assertNotIn(export.name, summary_text)

    def test_media_hashing_streams_without_path_read_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            output = base / "report"

            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read_bytes used")):
                summary = audit_export.audit_exports([export], output)

            self.assertEqual(summary["status"], "complete")

    def test_cli_returns_controlled_exit_codes_without_sensitive_error_values(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            complete_export = base / "complete-secret"
            make_export(complete_export)
            complete = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "audit_export.py"),
                    "--export",
                    str(complete_export),
                    "--output",
                    str(base / "complete-report"),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(complete.returncode, 0, complete.stdout + complete.stderr)

            incomplete_export = base / "incomplete-secret"
            make_export(
                incomplete_export,
                rows=[
                    {
                        "Date": "2024-01-02 00:00:00 UTC",
                        "Media Type": "Video",
                    }
                ],
                media={IMAGE_NAME: b"image-bytes"},
                html_names=[IMAGE_NAME],
            )
            incomplete = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "audit_export.py"),
                    "--export",
                    str(incomplete_export),
                    "--output",
                    str(base / "incomplete-report"),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(incomplete.returncode, 3, incomplete.stdout + incomplete.stderr)

            sensitive = base / "sensitive-do-not-echo"
            failed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "audit_export.py"),
                    "--export",
                    str(sensitive),
                    "--output",
                    str(base / "failed-report"),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(failed.stderr, "audit failed: unsafe or invalid input/output\n")
            self.assertNotIn(str(sensitive), failed.stdout + failed.stderr)
            self.assertFalse((base / "failed-report").exists())
    def test_report_rejects_non_http_url_like_location(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(
                export,
                rows=[
                    {
                        "Date": "2024-01-01 00:00:00 UTC",
                        "Media Type": "Image",
                        "Location": "ftp://private.example/location",
                    }
                ],
            )
            output = base / "report"

            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], output, include_location=True)

            self.assertFalse(output.exists())

    def test_default_history_csv_allowlist_excludes_location(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(
                export,
                rows=[
                    {
                        "Date": "invalid",
                        "Media Type": "Image",
                        "Location": "Private place",
                        "Secret": "not allowed",
                    }
                ],
            )
            output = base / "report"

            audit_export.audit_exports([export], output)

            with (output / "missing_history_records.csv").open(encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
            self.assertEqual(reader.fieldnames, ["history_index", "Date", "Media Type", "reason"])
            self.assertEqual(len(rows), 1)
            self.assertNotIn("Private place", (output / "missing_history_records.csv").read_text())
            self.assertNotIn("Secret", (output / "missing_history_records.csv").read_text())

    def test_matching_uses_maximum_cardinality_not_greedy_assignment(self):
        def item(name, timestamp):
            media = audit_export.MediaFile(
                1,
                ("memories", name),
                f"export-1/memories/{name}",
                name,
                "main",
                "jpg",
                1,
                timestamp,
                "a" * 64,
            )
            return {"key": audit_export.alias(name), "file": media, "source_count": 1, "empty": False}

        base = 1704067200.0
        alternative = item(IMAGE_NAME, base - 0.75)
        scarce_name = "2024-01-01_BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB-main.jpg"
        scarce = item(scarce_name, base + 0.5)
        history = [
            {"Date": "2024-01-01 00:00:00 UTC", "Media Type": "Image"},
            {"Date": "2024-01-01 00:00:01 UTC", "Media Type": "Image"},
        ]

        _, matches, missing, unmatched = audit_export._match_history(history, [scarce, alternative])

        self.assertEqual(len(matches), 2)
        self.assertEqual(missing, [])
        self.assertEqual(unmatched, [])
    def test_offset_selection_uses_match_cardinality_not_dense_edge_count(self):
        history = [
            {
                "Date": dt.datetime.fromtimestamp(index, tz=dt.timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"
                ),
                "Media Type": "Image",
            }
            for index in range(6)
        ]
        logical = []
        timestamps = [2.0] * 6 + [3600.0 + index for index in range(6)]
        for index, timestamp in enumerate(timestamps):
            name = f"1970-01-01_{index:08X}-0000-0000-0000-000000000000-main.jpg"
            media = audit_export.MediaFile(
                1,
                ("memories", name),
                f"export-1/memories/{name}",
                name,
                "main",
                "jpg",
                1,
                timestamp,
                f"{index:064x}",
            )
            logical.append(
                {"key": audit_export.alias(name), "file": media, "source_count": 1, "empty": False}
            )

        self.assertEqual(audit_export._choose_offset(history, logical), 1)


if __name__ == "__main__":
    unittest.main()
