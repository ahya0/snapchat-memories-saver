import csv
import contextlib
import datetime as dt
import errno
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
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
    def setUp(self) -> None:
        marker = uuid.uuid4().hex
        self.stage_prefix = ".audit-stage-test-{}-".format(marker)
        counter = 0

        def test_uuid():
            nonlocal counter
            counter += 1
            return SimpleNamespace(hex="test-{}-{}".format(marker, counter))

        self.uuid_patcher = mock.patch.object(
            audit_export.uuid,
            "uuid4",
            side_effect=test_uuid,
        )
        self.uuid_patcher.start()

    def tearDown(self) -> None:
        self.uuid_patcher.stop()
        for stage in Path("/private/tmp").glob(self.stage_prefix + "*"):
            shutil.rmtree(stage, ignore_errors=True)

    def stage_paths(self, parent: Path):
        return list(parent.glob(self.stage_prefix + "*"))

    def stable_output(self) -> Path:
        output = Path("/private/tmp") / "sms-v02-test-report-{}".format(uuid.uuid4().hex)

        def cleanup() -> None:
            if output.is_dir() and not output.is_symlink():
                shutil.rmtree(output, ignore_errors=True)
            else:
                try:
                    output.unlink()
                except FileNotFoundError:
                    pass

        self.addCleanup(cleanup)
        return output

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
            output = self.stable_output()

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

            self.assertEqual(result.returncode, 65)
            self.assertEqual(
                result.stderr,
                "audit failed [SMS-E-INPUT]: the export could not be checked safely\n",
            )
            self.assertNotIn("Traceback", result.stdout + result.stderr)
            self.assertFalse(output.exists())

    def test_cli_uses_fixed_safe_error_code_when_output_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            output = self.stable_output()
            output.mkdir()

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

            self.assertEqual(result.returncode, 73)
            self.assertEqual(
                result.stderr,
                "audit failed [SMS-E-OUTPUT]: choose a new report folder\n",
            )
            self.assertNotIn(str(output), result.stderr)

    def test_cli_uses_output_status_when_output_parent_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            output = base / "missing-parent" / "report"

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

            self.assertEqual(result.returncode, 73)
            self.assertEqual(
                result.stderr,
                "audit failed [SMS-E-OUTPUT]: the report destination is unavailable\n",
            )
            self.assertNotIn(str(output), result.stderr)

    def test_cli_uses_fixed_safe_error_code_for_resource_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            output = self.stable_output()

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "audit_export.py"),
                    "--export",
                    str(export),
                    "--output",
                    str(output),
                    "--max-files",
                    "1",
                ],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 65)
            self.assertEqual(
                result.stderr,
                "audit failed [SMS-E-INPUT]: the export is larger than the configured safety limit\n",
            )
            self.assertNotIn(str(export), result.stderr)

    def test_cli_usage_and_unexpected_failures_use_private_stable_statuses(self):
        sensitive = "/private/secret/export"
        usage = subprocess.run(
            [sys.executable, str(SCRIPTS / "audit_export.py"), "--unknown", sensitive],
            capture_output=True,
            text=True,
        )
        self.assertEqual(usage.returncode, 64)
        self.assertEqual(
            usage.stderr,
            "audit failed [SMS-E-USAGE]: invalid command usage\n",
        )
        self.assertNotIn(sensitive, usage.stdout + usage.stderr)

        stderr = io.StringIO()
        with mock.patch.object(
            audit_export, "audit_exports", side_effect=RuntimeError("private internal detail")
        ), contextlib.redirect_stderr(stderr):
            status = audit_export.main(
                ["--export", "/private/export", "--output", "/private/report"]
            )
        self.assertEqual(status, 70)
        self.assertEqual(
            stderr.getvalue(),
            "audit failed [SMS-E-INTERNAL]: an unexpected internal error was contained\n",
        )
        self.assertNotIn("private", stderr.getvalue())

        stderr = io.StringIO()
        with mock.patch.object(
            audit_export, "audit_exports", side_effect=OSError("private I/O detail")
        ), contextlib.redirect_stderr(stderr):
            status = audit_export.main(
                ["--export", "/private/export", "--output", "/private/report"]
            )
        self.assertEqual(status, 74)
        self.assertEqual(
            stderr.getvalue(),
            "audit failed [SMS-E-IO]: a local file operation failed safely\n",
        )
        self.assertNotIn("private", stderr.getvalue())

        self.assertEqual(
            audit_export._safe_failure(OSError(errno.ENOSPC, "private disk detail")),
            (
                73,
                "SMS-E-OUTPUT",
                "the report destination does not have enough free space",
            ),
        )

    def test_open_regular_closes_descriptor_when_fstat_fails(self):
        with mock.patch.object(audit_export.os, "open", return_value=123), mock.patch.object(
            audit_export.os, "fstat", side_effect=OSError("synthetic fstat failure")
        ), mock.patch.object(audit_export.os, "close") as close_mock:
            with self.assertRaises(OSError):
                audit_export._open_regular_at(10, "file.jpg")

        close_mock.assert_called_once_with(123)

    def test_open_regular_closes_descriptor_when_interrupted(self):
        with mock.patch.object(audit_export.os, "open", return_value=123), mock.patch.object(
            audit_export.os, "fstat", side_effect=KeyboardInterrupt()
        ), mock.patch.object(audit_export.os, "close") as close_mock:
            with self.assertRaises(KeyboardInterrupt):
                audit_export._open_regular_at(10, "file.jpg")

        close_mock.assert_called_once_with(123)

    def test_open_regular_rejects_descriptor_with_multiple_links(self):
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=2)
        with mock.patch.object(audit_export.os, "open", return_value=123), mock.patch.object(
            audit_export.os, "fstat", return_value=info
        ), mock.patch.object(audit_export.os, "close") as close_mock:
            with self.assertRaises(audit_export.AuditError):
                audit_export._open_regular_at(10, "file.jpg")

        close_mock.assert_called_once_with(123)

    def test_ancestor_check_closes_parent_descriptor_when_fstat_fails(self):
        current_info = SimpleNamespace(st_dev=1, st_ino=2)
        with mock.patch.object(audit_export.os, "dup", return_value=20), mock.patch.object(
            audit_export.os, "open", return_value=21
        ), mock.patch.object(
            audit_export.os,
            "fstat",
            side_effect=[current_info, OSError("synthetic parent fstat failure")],
        ), mock.patch.object(audit_export.os, "close") as close_mock:
            with self.assertRaises(OSError):
                audit_export._directory_or_ancestor_matches(10, set())

        self.assertEqual(
            [call.args[0] for call in close_mock.call_args_list],
            [21, 20],
        )

    def test_max_files_is_global_across_all_export_parts(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            first = base / "first"
            second = base / "second"
            make_export(first)
            make_export(second)
            output = self.stable_output()

            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports(
                    [first, second], output, max_files=7
                )

            self.assertFalse(output.exists())

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
            output = self.stable_output()

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

            edge_output = self.stable_output()
            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports(
                    [export], edge_output, max_candidate_edges=100
                )
            self.assertFalse(edge_output.exists())

            byte_output = self.stable_output()
            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports(
                    [export], byte_output, max_total_media_bytes=10
                )
            self.assertFalse(byte_output.exists())

            metadata_output = self.stable_output()
            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports(
                    [export], metadata_output, max_total_metadata_bytes=16
                )
            self.assertFalse(metadata_output.exists())

            history_output = self.stable_output()
            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports(
                    [export], history_output, max_history_records=10
                )
            self.assertFalse(history_output.exists())

    def test_disk_preflight_rejects_before_scan_and_before_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            no_space = SimpleNamespace(f_bavail=0, f_frsize=4096, f_bsize=4096)
            enough = SimpleNamespace(
                f_bavail=10_000_000, f_frsize=4096, f_bsize=4096
            )
            initial_output = self.stable_output()

            with mock.patch.object(
                audit_export.os, "fstatvfs", return_value=no_space
            ), mock.patch.object(audit_export, "_scan_export") as scan_mock:
                with self.assertRaises(audit_export.AuditError) as initial:
                    audit_export.audit_exports([export], initial_output)
            self.assertEqual(initial.exception.code, "INSUFFICIENT_SPACE")
            scan_mock.assert_not_called()
            self.assertFalse(initial_output.exists())

            exact_output = self.stable_output()
            with mock.patch.object(
                audit_export.os, "fstatvfs", side_effect=[enough, no_space]
            ):
                with self.assertRaises(audit_export.AuditError) as exact:
                    audit_export.audit_exports([export], exact_output)
            self.assertEqual(exact.exception.code, "INSUFFICIENT_SPACE")
            self.assertFalse(exact_output.exists())
            self.assertEqual(self.stage_paths(exact_output.parent), [])

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

            output = self.stable_output()
            summary = audit_export.audit_exports([export], output)

            self.assertEqual(summary["status"], "incomplete")
            self.assertEqual(summary["html_missing_reference_count"], 1)
            self.assertEqual(summary["html_unreferenced_media_count"], 1)

    def test_report_write_failure_never_publishes_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            original = audit_export._write_exclusive_atomic

            for fail_at in range(1, 7):
                output = self.stable_output()
                calls = 0

                def fail_write(directory_fd, name, payload, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise OSError("synthetic write failure")
                    return original(directory_fd, name, payload, **kwargs)

                with self.subTest(fail_at=fail_at), mock.patch.object(
                    audit_export, "_write_exclusive_atomic", side_effect=fail_write
                ):
                    with self.assertRaises(OSError):
                        audit_export.audit_exports([export], output)

                self.assertFalse(output.exists())
                self.assertEqual(self.stage_paths(output.parent), [])

    def test_enospc_during_temporary_cleanup_leaves_no_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            output = self.stable_output()
            real_unlink = os.unlink
            failed_temporary = False

            def fail_first_temporary(name, *args, **kwargs):
                nonlocal failed_temporary
                if not failed_temporary and str(name).startswith(".tmp-"):
                    failed_temporary = True
                    raise OSError(errno.ENOSPC, "synthetic no space")
                return real_unlink(name, *args, **kwargs)

            with mock.patch.object(
                audit_export.os, "unlink", side_effect=fail_first_temporary
            ):
                with self.assertRaises(OSError) as failure:
                    audit_export.audit_exports([export], output)

            self.assertEqual(failure.exception.errno, errno.ENOSPC)
            self.assertFalse(output.exists())
            self.assertEqual(
                self.stage_paths(base),
                [],
            )

    def test_enospc_during_publication_never_leaves_partial_output(self):
        cases = (
            ("write", audit_export.os, "write"),
            ("fsync", audit_export.os, "fsync"),
            ("rename", audit_export, "_rename_directory_exclusive"),
        )
        for label, target, attribute in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                base = Path(os.path.realpath(directory))
                export = base / "export"
                make_export(export)
                output = self.stable_output()

                with mock.patch.object(
                    target,
                    attribute,
                    side_effect=OSError(errno.ENOSPC, "synthetic no space"),
                ):
                    with self.assertRaises(OSError) as failure:
                        audit_export.audit_exports([export], output)

                self.assertEqual(failure.exception.errno, errno.ENOSPC)
                self.assertEqual(
                    audit_export._safe_failure(failure.exception)[0:2],
                    (73, "SMS-E-OUTPUT"),
                )
                self.assertFalse(output.exists())
                self.assertEqual(self.stage_paths(output.parent), [])

    def test_no_fallible_operation_runs_after_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            output = self.stable_output()
            real_fsync = os.fsync

            def reject_post_publication_fsync(file_fd):
                if output.exists():
                    raise AssertionError("fsync ran after publication")
                return real_fsync(file_fd)

            with mock.patch.object(
                audit_export.os, "fsync", side_effect=reject_post_publication_fsync
            ):
                summary = audit_export.audit_exports([export], output)

            self.assertEqual(summary["status"], "complete")
            self.assertTrue(output.is_dir())

    def test_parent_close_failure_after_publication_does_not_report_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            output = self.stable_output()
            real_close = os.close

            def fail_close_after_publication(file_fd):
                if output.exists():
                    raise OSError(errno.EIO, "synthetic post-publication close failure")
                return real_close(file_fd)

            with mock.patch.object(
                audit_export.os,
                "close",
                side_effect=fail_close_after_publication,
            ):
                summary = audit_export.audit_exports([export], output)

            self.assertEqual(summary["status"], "complete")
            self.assertTrue(output.is_dir())

    def test_relocation_during_staging_is_rejected_before_any_report_write(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            output_parent = base / "output-parent"
            output_parent.mkdir()
            output = output_parent / "report"
            relocated = export / "relocated-output-parent"
            original_write = audit_export._write_exclusive_atomic
            original_stability = audit_export._assert_output_parent_not_movable
            saw_report_file = False
            stability_checks = 0

            def allow_only_initial_unstable_parent(parent_fd):
                nonlocal stability_checks
                stability_checks += 1
                if stability_checks > 1:
                    original_stability(parent_fd)

            def relocate_before_write(*args, **kwargs):
                nonlocal saw_report_file
                if not relocated.exists():
                    output_parent.rename(relocated)
                try:
                    return original_write(*args, **kwargs)
                finally:
                    saw_report_file = any(
                        path.is_file() and not path.name.startswith(".tmp-")
                        for path in relocated.rglob("*")
                    )

            with mock.patch.object(
                audit_export, "_write_exclusive_atomic", side_effect=relocate_before_write
            ), mock.patch.object(
                audit_export,
                "_assert_output_parent_not_movable",
                side_effect=allow_only_initial_unstable_parent,
            ):
                with self.assertRaises(audit_export.AuditError):
                    audit_export.audit_exports([export], output)

            report_files = [
                path
                for path in relocated.rglob("*")
                if path.is_file() and not path.name.startswith(".tmp-")
            ]
            self.assertFalse(saw_report_file)
            self.assertEqual(report_files, [])

    def test_relocation_at_publication_is_rejected_before_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            output_parent = base / "output-parent"
            output_parent.mkdir()
            output = output_parent / "report"
            relocated = export / "relocated-output-parent"
            original_rename = audit_export._rename_directory_exclusive
            original_stability = audit_export._assert_output_parent_not_movable
            stability_checks = 0

            def allow_only_initial_unstable_parent(parent_fd):
                nonlocal stability_checks
                stability_checks += 1
                if stability_checks > 1:
                    original_stability(parent_fd)

            def relocate_before_rename(*args, **kwargs):
                source = args[1]
                if source.startswith(".audit-stage-") and not relocated.exists():
                    output_parent.rename(relocated)
                return original_rename(*args, **kwargs)

            with mock.patch.object(
                audit_export,
                "_rename_directory_exclusive",
                side_effect=relocate_before_rename,
            ), mock.patch.object(
                audit_export,
                "_assert_output_parent_not_movable",
                side_effect=allow_only_initial_unstable_parent,
            ):
                with self.assertRaises(audit_export.AuditError):
                    audit_export.audit_exports([export], output)

            self.assertFalse((relocated / "report").exists())

    def test_output_permission_failure_uses_output_status(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            output = self.stable_output()

            with mock.patch.object(
                audit_export.os,
                "mkdir",
                side_effect=OSError(errno.EACCES, "synthetic permission failure"),
            ):
                with self.assertRaises(audit_export.AuditError) as failure:
                    audit_export.audit_exports([export], output)

            self.assertEqual(failure.exception.code, "OUTPUT_UNAVAILABLE")
            self.assertEqual(
                audit_export._safe_failure(failure.exception)[0:2],
                (73, "SMS-E-OUTPUT"),
            )
            self.assertFalse(output.exists())

    def test_unsupported_output_filesystem_uses_output_status(self):
        failure = OSError(errno.ENOTSUP, "synthetic unsupported filesystem")
        with self.assertRaises(audit_export.AuditError) as mapped:
            audit_export._raise_output_context(failure)

        self.assertEqual(mapped.exception.code, "OUTPUT_UNAVAILABLE")
        self.assertEqual(
            audit_export._safe_failure(mapped.exception)[0:2],
            (73, "SMS-E-OUTPUT"),
        )

    def test_cleanup_failure_does_not_replace_primary_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            primary = audit_export.AuditError(
                "synthetic output relocation",
                "OUTPUT_UNAVAILABLE",
            )
            output = self.stable_output()

            with mock.patch.object(
                audit_export,
                "_write_exclusive_atomic",
                side_effect=primary,
            ), mock.patch.object(
                audit_export,
                "_remove_report_stage",
                side_effect=OSError(errno.EIO, "synthetic cleanup failure"),
            ):
                with self.assertRaises(audit_export.AuditError) as failure:
                    audit_export.audit_exports([export], output)

            self.assertIs(failure.exception, primary)
            self.assertEqual(failure.exception.code, "OUTPUT_UNAVAILABLE")

    def test_persistent_cleanup_unlink_failure_scrubs_private_stage_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            output = self.stable_output()
            original_write = audit_export._write_exclusive_atomic
            original_unlink = os.unlink
            calls = 0

            def fail_second_write(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError(errno.EIO, "synthetic report write failure")
                return original_write(*args, **kwargs)

            def block_stage_unlink(name, *args, **kwargs):
                if not str(name).startswith(".tmp-"):
                    raise OSError(errno.ENOSPC, "synthetic persistent cleanup failure")
                return original_unlink(name, *args, **kwargs)

            with mock.patch.object(
                audit_export,
                "_write_exclusive_atomic",
                side_effect=fail_second_write,
            ), mock.patch.object(
                audit_export.os,
                "unlink",
                side_effect=block_stage_unlink,
            ):
                with self.assertRaises(OSError):
                    audit_export.audit_exports([export], output)

            stages = self.stage_paths(output.parent)
            self.assertFalse(output.exists())
            self.assertEqual(len(stages), 1)
            self.assertTrue(all(path.stat().st_size == 0 for path in stages[0].iterdir()))
            shutil.rmtree(stages[0])

    def test_keyboard_interrupt_during_staging_runs_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            output = self.stable_output()
            original_write = audit_export._write_exclusive_atomic
            calls = 0

            def interrupt_second_write(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise KeyboardInterrupt()
                return original_write(*args, **kwargs)

            with mock.patch.object(
                audit_export,
                "_write_exclusive_atomic",
                side_effect=interrupt_second_write,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    audit_export.audit_exports([export], output)

            self.assertFalse(output.exists())
            self.assertEqual(
                self.stage_paths(base),
                [],
            )

    def test_keyboard_interrupt_after_staged_file_close_preserves_interrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            output = self.stable_output()
            real_open = os.open
            real_close = os.close
            temporary_fds = []
            interrupted = False

            def capture_temporary_open(path, flags, *args, **kwargs):
                file_fd = real_open(path, flags, *args, **kwargs)
                if str(path).startswith(".tmp-"):
                    temporary_fds.append(file_fd)
                return file_fd

            def interrupt_after_kernel_close(file_fd):
                nonlocal interrupted
                if file_fd in temporary_fds and not interrupted:
                    interrupted = True
                    real_close(file_fd)
                    raise KeyboardInterrupt()
                return real_close(file_fd)

            with mock.patch.object(
                audit_export.os,
                "open",
                side_effect=capture_temporary_open,
            ), mock.patch.object(
                audit_export.os,
                "close",
                side_effect=interrupt_after_kernel_close,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    audit_export.audit_exports([export], output)

            self.assertTrue(interrupted)
            self.assertFalse(output.exists())
            self.assertEqual(self.stage_paths(output.parent), [])
            self.assertTrue(temporary_fds)
            for file_fd in temporary_fds:
                with self.assertRaises(OSError) as closed:
                    os.fstat(file_fd)
                self.assertEqual(closed.exception.errno, errno.EBADF)

    def test_keyboard_interrupt_during_cleanup_still_removes_private_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            stage_name = ".audit-stage-0123456789abcdef0123456789abcdef"
            stage = base / stage_name
            stage.mkdir(mode=0o700)
            (stage / "start_here.html").write_bytes(b"private-a")
            (stage / "report.txt").write_bytes(b"private-b")
            real_ftruncate = os.ftruncate
            interrupted = False

            def interrupt_once(file_fd, size):
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt()
                return real_ftruncate(file_fd, size)

            parent_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with mock.patch.object(
                    audit_export.os,
                    "ftruncate",
                    side_effect=interrupt_once,
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        audit_export._remove_report_stage(
                            parent_fd,
                            stage_name,
                            ("start_here.html", "report.txt"),
                        )
            finally:
                os.close(parent_fd)

            self.assertFalse(stage.exists())

    def test_keyboard_interrupt_before_cleanup_open_is_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            stage_name = ".audit-stage-0123456789abcdef0123456789abcdef"
            stage = base / stage_name
            stage.mkdir(mode=0o700)
            (stage / "start_here.html").write_bytes(b"private")
            real_open = os.open
            interrupted = False

            def interrupt_stage_open(path, flags, *args, **kwargs):
                nonlocal interrupted
                if path == stage_name and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt()
                return real_open(path, flags, *args, **kwargs)

            parent_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with mock.patch.object(
                    audit_export.os,
                    "open",
                    side_effect=interrupt_stage_open,
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        audit_export._remove_report_stage(
                            parent_fd,
                            stage_name,
                            ("start_here.html",),
                        )
            finally:
                os.close(parent_fd)

            self.assertFalse(stage.exists())

    def test_keyboard_interrupt_during_cleanup_close_does_not_leak_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            stage_name = ".audit-stage-0123456789abcdef0123456789abcdef"
            stage = base / stage_name
            stage.mkdir(mode=0o700)
            (stage / "start_here.html").write_bytes(b"private")
            real_open = os.open
            real_close = os.close
            report_fds = []
            interrupted = False

            def capture_report_open(path, flags, *args, **kwargs):
                file_fd = real_open(path, flags, *args, **kwargs)
                if path == "start_here.html":
                    report_fds.append(file_fd)
                return file_fd

            def interrupt_report_close(file_fd):
                nonlocal interrupted
                if report_fds and file_fd == report_fds[0] and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt()
                return real_close(file_fd)

            parent_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with mock.patch.object(
                    audit_export.os,
                    "open",
                    side_effect=capture_report_open,
                ), mock.patch.object(
                    audit_export.os,
                    "close",
                    side_effect=interrupt_report_close,
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        audit_export._remove_report_stage(
                            parent_fd,
                            stage_name,
                            ("start_here.html",),
                        )
            finally:
                os.close(parent_fd)

            self.assertFalse(stage.exists())
            self.assertTrue(report_fds)
            for report_fd in report_fds:
                with self.assertRaises(OSError) as closed:
                    os.fstat(report_fd)
                self.assertEqual(closed.exception.errno, errno.EBADF)

    def test_movable_output_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)

            with mock.patch.object(audit_export, "_scan_export") as scan_mock:
                with self.assertRaises(audit_export.AuditError) as failure:
                    audit_export.audit_exports([export], base / "report")

            self.assertEqual(failure.exception.code, "OUTPUT_UNAVAILABLE")
            scan_mock.assert_not_called()

    def test_user_owned_read_only_grandparent_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            container = base / "container"
            output_parent = container / "output-parent"
            output_parent.mkdir(parents=True)
            container.chmod(0o555)
            try:
                with self.assertRaises(audit_export.AuditError) as failure:
                    audit_export.audit_exports([export], output_parent / "report")
            finally:
                container.chmod(0o700)

            self.assertEqual(failure.exception.code, "OUTPUT_UNAVAILABLE")

    def test_output_parent_with_extended_acl_is_rejected(self):
        parent = SimpleNamespace(st_uid=0)
        grandparent = SimpleNamespace(st_uid=0)
        with mock.patch.object(audit_export.os, "open", return_value=456), mock.patch.object(
            audit_export.os,
            "fstat",
            side_effect=[parent, grandparent],
        ), mock.patch.object(
            audit_export.os,
            "access",
            return_value=False,
        ), mock.patch.object(
            audit_export,
            "_directory_has_extended_acl",
            side_effect=[True, False],
            create=True,
        ), mock.patch.object(audit_export.os, "close"):
            with self.assertRaises(audit_export.AuditError) as failure:
                audit_export._assert_output_parent_not_movable(123)

        self.assertEqual(failure.exception.code, "OUTPUT_UNAVAILABLE")

    def test_output_parent_owned_by_any_nonroot_user_is_rejected(self):
        parent = SimpleNamespace(st_uid=1234)
        grandparent = SimpleNamespace(st_uid=0)
        with mock.patch.object(audit_export.os, "open", return_value=456), mock.patch.object(
            audit_export.os,
            "fstat",
            side_effect=[parent, grandparent],
        ), mock.patch.object(
            audit_export.os,
            "access",
            return_value=False,
        ), mock.patch.object(
            audit_export,
            "_directory_has_extended_acl",
            return_value=False,
        ), mock.patch.object(audit_export.os, "close"):
            with self.assertRaises(audit_export.AuditError) as failure:
                audit_export._assert_output_parent_not_movable(123)

        self.assertEqual(failure.exception.code, "OUTPUT_UNAVAILABLE")

    def test_multiply_linked_input_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            external = base / "outside.bin"
            external.write_bytes(b"outside-selected-root")
            export = base / "export"
            make_export(export)
            target = export / "memories" / IMAGE_NAME
            target.unlink()
            os.link(external, target)
            output = self.stable_output()

            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], output)

            self.assertFalse(output.exists())

    def test_history_record_limit_is_enforced_during_parsing(self):
        text = '{"Saved Media":[' + ",".join("{}" for _ in range(10_000)) + "]}"
        with mock.patch.object(
            audit_export.json,
            "loads",
            side_effect=AssertionError("whole-document materialization is forbidden"),
        ):
            with self.assertRaises(audit_export.AuditError) as failure:
                audit_export._parse_history(text, max_records=1)

        self.assertEqual(failure.exception.code, "RESOURCE_LIMIT_REACHED")

    def test_html_reference_limit_is_enforced_during_parsing(self):
        relative = ("memories", "memories.html")
        scan = audit_export.ExportScan(
            1,
            [],
            [],
            [(relative, "export-1/memories/memories.html", '<img src="x">' * 10)],
            set(),
        )

        with self.assertRaises(audit_export.AuditError) as failure:
            audit_export._check_html([scan], [], max_html_references=2)

        self.assertEqual(failure.exception.code, "RESOURCE_LIMIT_REACHED")

    def test_empty_history_and_media_cannot_report_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export, rows=[], media={}, html_names=[])
            output = self.stable_output()

            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], output)

            self.assertFalse(output.exists())

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
                [history_part, media_part], self.stable_output()
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
            output = self.stable_output()

            summary = audit_export.audit_exports([export], output)

            self.assertEqual(summary["status"], "complete")
            self.assertTrue(summary["history_file_reconciliation_complete"])
            self.assertTrue(summary["html_evidence_complete"])
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {
                    "start_here.html",
                    "summary.json",
                    "manifest.csv",
                    "missing_history_records.csv",
                    "report.txt",
                    "support.txt",
                },
            )
            self.assertEqual(summary["tool_name"], "snapchat-memories-saver")
            self.assertEqual(summary["tool_version"], "0.2.0")
            self.assertEqual(summary["report_schema_name"], "snapchat-memories-audit")
            self.assertEqual(summary["report_schema_version"], 1)
            generated = dt.datetime.fromisoformat(summary["generated_utc"])
            self.assertEqual(generated.tzinfo, dt.timezone.utc)
            self.assertEqual(snapshot_tree(export), before)
            combined = "\n".join(
                path.read_text(encoding="utf-8") for path in output.iterdir()
            )
            self.assertNotIn(str(base), combined)
            self.assertNotIn(export.name, combined)
            self.assertNotIn("Private place", combined)
            self.assertNotIn("private.example", combined)
            self.assertIn("export-1/memories/", combined)
            self.assertIn("do not prove every image pixel or video frame decodes", combined)
            self.assertIn("ZIP integrity was not tested", combined)
            start_page = (output / "start_here.html").read_text(encoding="utf-8")
            self.assertIn("Your export check looks complete", start_page)
            self.assertIn("Snapchat Memories Saver 0.2.0", start_page)
            self.assertIn("only the supplied export evidence", start_page)
            self.assertIn("omitted from every supplied artifact", start_page)
            self.assertTrue(
                any(
                    "omitted from every supplied artifact" in limitation
                    for limitation in summary["limitations"]
                )
            )
            self.assertTrue(
                any(
                    "as read during the audit" in limitation
                    for limitation in summary["limitations"]
                )
            )
            self.assertIn("as read during the audit", start_page)
            self.assertNotIn("Request another export", start_page)
            self.assertNotIn("https://", start_page)
            self.assertNotIn("temporary and private", start_page)
            support = (output / "support.txt").read_text(encoding="utf-8")
            report = (output / "report.txt").read_text(encoding="utf-8")
            self.assertIn("Tool version: 0.2.0", support)
            self.assertIn("Report schema: 1", support)
            self.assertIn("Status: COMPLETE", support)
            self.assertIn("aggregate counts may still be sensitive", support)
            self.assertIn("review this file before sharing", support)
            self.assertIn("omitted from every supplied artifact", support)
            self.assertIn("omitted from every supplied artifact", report)
            self.assertIn("as read during the audit", support)
            self.assertIn("as read during the audit", report)
            self.assertNotIn("safe support summary", support.casefold())
            self.assertNotIn(IMAGE_NAME, support)
            self.assertNotIn("2024-01-01", support)
            self.assertNotIn("Generated UTC", support)
            self.assertNotIn("export-1", support)
            self.assertNotIn("Private place", support)
            self.assertNotIn("https://", support)
            self.assertNotIn(str(base), support)

    def test_cli_complete_message_states_supplied_evidence_limit(self):
        stdout = io.StringIO()
        with mock.patch.object(
            audit_export,
            "audit_exports",
            return_value={"status": "complete"},
        ), contextlib.redirect_stdout(stdout):
            status = audit_export.main(
                ["--export", "/private/export", "--output", "/private/report"]
            )

        self.assertEqual(status, 0)
        self.assertIn("only the supplied export evidence", stdout.getvalue())
        self.assertIn("omitted from every supplied artifact", stdout.getvalue())
        self.assertIn("as read during the audit", stdout.getvalue())

    def test_start_here_guides_missing_memory_recovery_without_leaking_private_links(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(
                export,
                rows=[
                    {
                        "Date": "<script>alert(1)</script>",
                        "Media Type": "Image",
                        "Location": "Private place",
                        "Download Link": "https://private.example/signed-token",
                    }
                ],
                media={},
                html_names=[],
            )
            output = self.stable_output()

            summary = audit_export.audit_exports([export], output)
            page = (output / "start_here.html").read_text(encoding="utf-8")

            self.assertEqual(summary["status"], "incomplete")
            self.assertIn("Some Memories may be missing", page)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
            self.assertNotIn("<script>alert(1)</script>", page)
            self.assertNotIn("Private place", page)
            self.assertNotIn("private.example", page)
            self.assertIn("https://accounts.snapchat.com/v2/download-my-data", page)
            self.assertIn("https://help.snapchat.com/", page)
            self.assertEqual(page.count("https://"), 2)
            self.assertIn("Run the saver again with the original and retry export folders", page)

    def test_start_here_explains_html_only_incomplete_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            (export / "memories" / "memories.html").write_text(
                f'<img src=".//does-not-exist/{IMAGE_NAME}">'
                f'<img src=".//{OVERLAY_NAME}">',
                encoding="utf-8",
            )
            output = self.stable_output()

            summary = audit_export.audit_exports([export], output)
            page = (output / "start_here.html").read_text(encoding="utf-8")

            self.assertEqual(summary["status"], "incomplete")
            self.assertEqual(summary["missing_history_record_count"], 0)
            self.assertIn("HTML references to missing files: 1", page)
            self.assertIn("Saved files not referenced by HTML: 1", page)
            self.assertNotIn("Items to look for", page)
            self.assertIn("Re-extract every original ZIP part", page)
            self.assertIn("history entries still match the saved media", page)
            self.assertNotIn("Use the dates above", page)

    def test_start_here_caps_missing_items_and_reports_remainder(self):
        summary = {
            "status": "incomplete",
            "history_record_count": 501,
            "missing_history_record_count": 501,
            "unmatched_main_file_count": 0,
            "empty_media_file_count": 0,
            "html_file_count": 1,
            "html_missing_reference_count": 0,
            "html_unexpected_reference_count": 0,
            "html_unreferenced_media_count": 0,
        }
        missing = [
            {"Date": f"2024-01-01 00:00:{index:02d} UTC", "Media Type": "Image"}
            for index in range(501)
        ]

        page = audit_export._start_here_payload(summary, missing).decode("utf-8")

        self.assertEqual(page.count("<tr><td>"), 500)
        self.assertIn("1 more missing items are listed", page)
    def test_retry_exports_use_multiset_max_and_deduplicate_identical_media(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            first = base / "first"
            second = base / "second"
            make_export(first)
            make_export(second)
            output = self.stable_output()

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
            output = self.stable_output()

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
                audit_export.audit_exports([first, second], self.stable_output())

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
                output = self.stable_output()
                with self.assertRaises(audit_export.AuditError):
                    audit_export.audit_exports([export], output)
                self.assertFalse(output.exists())

        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "json-limit"
            make_export(export)
            output = self.stable_output()
            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], output, max_json_bytes=8)
            self.assertFalse(output.exists())

        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "html-limit"
            make_export(export)
            output = self.stable_output()
            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], output, max_html_bytes=8)
            self.assertFalse(output.exists())

    def test_file_count_depth_symlink_and_special_file_limits_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "count"
            make_export(export)
            count_output = self.stable_output()
            depth_output = self.stable_output()
            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], count_output, max_files=2)
            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], depth_output, max_depth=1)
            self.assertFalse(count_output.exists())
            self.assertFalse(depth_output.exists())

        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "symlink"
            make_export(export)
            (export / "link").symlink_to(export / "memories" / IMAGE_NAME)
            output = self.stable_output()
            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], output)
            self.assertFalse(output.exists())

        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "fifo"
            make_export(export)
            os.mkfifo(export / "named-pipe")
            output = self.stable_output()
            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], output)
            self.assertFalse(output.exists())

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

            with self.assertRaises(audit_export.AuditError) as inside_failure:
                audit_export.audit_exports([export], export / "report")

            self.assertEqual(inside_failure.exception.code, "OUTPUT_UNAVAILABLE")
            self.assertEqual(
                audit_export._safe_failure(inside_failure.exception)[0:2],
                (73, "SMS-E-OUTPUT"),
            )

            self.assertEqual(list(target.iterdir()), [])
            self.assertEqual(list(real_parent.iterdir()), [])
            self.assertFalse((export / "report").exists())

    def test_output_case_alias_inside_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "CaseExport"
            make_export(export)
            alias_parent = base / "caseexport"
            try:
                aliases_export = os.path.samefile(export, alias_parent)
            except FileNotFoundError:
                aliases_export = False
            if not aliases_export:
                self.skipTest("filesystem is case-sensitive")
            before = snapshot_tree(export)

            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], alias_parent / "report")

            self.assertEqual(snapshot_tree(export), before)
            self.assertFalse((export / "report").exists())

    def test_output_unicode_normalization_alias_inside_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "Caf\u00e9Export"
            make_export(export)
            alias_parent = base / "Cafe\u0301Export"
            try:
                aliases_export = os.path.samefile(export, alias_parent)
            except FileNotFoundError:
                aliases_export = False
            if not aliases_export:
                self.skipTest("filesystem does not normalize these Unicode spellings")
            before = snapshot_tree(export)

            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], alias_parent / "report")

            self.assertEqual(snapshot_tree(export), before)
            self.assertFalse((export / "report").exists())

    def test_output_parent_relocated_inside_export_during_scan_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            output_parent = base / "output-parent"
            output_parent.mkdir()
            output = output_parent / "report"
            relocated_parent = export / "relocated-output-parent"
            original_scan = audit_export._scan_export
            original_stability = audit_export._assert_output_parent_not_movable
            moved = False
            stability_checks = 0

            def allow_only_initial_unstable_parent(parent_fd):
                nonlocal stability_checks
                stability_checks += 1
                if stability_checks > 1:
                    original_stability(parent_fd)

            def scan_then_relocate(*args, **kwargs):
                nonlocal moved
                result = original_scan(*args, **kwargs)
                if not moved:
                    output_parent.rename(relocated_parent)
                    moved = True
                return result

            with mock.patch.object(
                audit_export, "_scan_export", side_effect=scan_then_relocate
            ), mock.patch.object(
                audit_export,
                "_assert_output_parent_not_movable",
                side_effect=allow_only_initial_unstable_parent,
            ):
                with self.assertRaises(audit_export.AuditError):
                    audit_export.audit_exports([export], output)

            self.assertFalse(output.exists())
            self.assertFalse((relocated_parent / "report").exists())

    def test_location_is_opt_in_and_url_like_location_is_never_written(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            output = self.stable_output()

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
            output = self.stable_output()
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
            output = self.stable_output()

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

    def test_html_query_values_are_rejected_without_entering_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            token = "PRIVATE_QUERY_TOKEN_7f4a"
            (export / "memories" / "memories.html").write_text(
                '<img src=".//{}?token={}"><img src=".//{}">'.format(
                    IMAGE_NAME,
                    token,
                    OVERLAY_NAME,
                ),
                encoding="utf-8",
            )
            output = self.stable_output()

            summary = audit_export.audit_exports([export], output)

            self.assertEqual(summary["html_unexpected_reference_count"], 1)
            self.assertEqual(summary["html_missing_reference_count"], 0)
            combined = "\n".join(
                path.read_text(encoding="utf-8") for path in output.iterdir()
            )
            self.assertNotIn(token, combined)

    def test_media_hashing_streams_without_path_read_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            output = self.stable_output()

            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read_bytes used")):
                summary = audit_export.audit_exports([export], output)

            self.assertEqual(summary["status"], "complete")

    def test_media_growth_during_hashing_is_rejected_without_bypassing_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            media = base / "media.jpg"
            media.write_bytes(b"x")
            directory_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
            budget = audit_export.ResourceBudget(10, 10, 1, 10)
            real_read = os.read
            grew = False

            def grow_before_read(file_fd, count):
                nonlocal grew
                if not grew:
                    with media.open("ab") as handle:
                        handle.write(b"y" * (2 * 1024 * 1024))
                    grew = True
                return real_read(file_fd, count)

            try:
                with mock.patch.object(audit_export.os, "read", side_effect=grow_before_read):
                    with self.assertRaises(audit_export.AuditError):
                        audit_export._hash_at(directory_fd, media.name, budget)
            finally:
                os.close(directory_fd)

            self.assertEqual(budget.media_bytes_remaining, 1)

    def test_media_mutation_after_scan_is_rejected_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            media = export / "memories" / IMAGE_NAME
            output = self.stable_output()
            original_space_check = audit_export._require_payload_space
            mutated = False

            def mutate_after_payloads(directory_fd, payloads):
                nonlocal mutated
                original_space_check(directory_fd, payloads)
                if not mutated:
                    media.write_bytes(b"changed-after-scan")
                    mutated = True

            with mock.patch.object(
                audit_export,
                "_require_payload_space",
                side_effect=mutate_after_payloads,
            ):
                with self.assertRaises(audit_export.AuditError):
                    audit_export.audit_exports([export], output)

            self.assertFalse(output.exists())

    def test_media_mutation_during_final_parent_sync_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            media = export / "memories" / IMAGE_NAME
            output = self.stable_output()
            original_prepare = audit_export._prepare_output_parent
            original_fsync = os.fsync
            parent_fd = None
            mutated = False

            def capture_parent(*args, **kwargs):
                nonlocal parent_fd
                prepared = original_prepare(*args, **kwargs)
                parent_fd = prepared[0]
                return prepared

            def mutate_during_parent_sync(file_fd):
                nonlocal mutated
                result = original_fsync(file_fd)
                if file_fd == parent_fd and not mutated:
                    media.write_bytes(b"changed-during-final-parent-sync")
                    mutated = True
                return result

            with mock.patch.object(
                audit_export,
                "_prepare_output_parent",
                side_effect=capture_parent,
            ), mock.patch.object(
                audit_export.os,
                "fsync",
                side_effect=mutate_during_parent_sync,
            ):
                with self.assertRaises(audit_export.AuditError):
                    audit_export.audit_exports([export], output)

            self.assertTrue(mutated)
            self.assertFalse(output.exists())

    def test_media_mutation_during_staging_is_rejected_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            media = export / "memories" / IMAGE_NAME
            output = self.stable_output()
            original_write = audit_export._write_exclusive_atomic
            mutated = False

            def write_then_mutate(directory_fd, name, payload, **kwargs):
                nonlocal mutated
                original_write(directory_fd, name, payload, **kwargs)
                if not mutated:
                    media.write_bytes(b"changed-during-staging")
                    mutated = True

            with mock.patch.object(
                audit_export,
                "_write_exclusive_atomic",
                side_effect=write_then_mutate,
            ):
                with self.assertRaises(audit_export.AuditError):
                    audit_export.audit_exports([export], output)

            self.assertFalse(output.exists())
            self.assertEqual(
                self.stage_paths(base),
                [],
            )

    def test_publication_linearizes_at_exclusive_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            make_export(export)
            media = export / "memories" / IMAGE_NAME
            output = self.stable_output()
            original_rename = audit_export._rename_directory_exclusive
            mutated = False

            def mutate_after_publish(*args, **kwargs):
                nonlocal mutated
                result = original_rename(*args, **kwargs)
                if not mutated and args[1].startswith(".audit-stage-"):
                    media.write_bytes(b"changed-after-rename")
                    mutated = True
                return result

            with mock.patch.object(
                audit_export,
                "_rename_directory_exclusive",
                side_effect=mutate_after_publish,
            ):
                summary = audit_export.audit_exports([export], output)

            self.assertTrue(mutated)
            self.assertEqual(summary["status"], "complete")
            self.assertTrue(output.is_dir())

    def test_metadata_growth_during_read_is_rejected_without_bypassing_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            metadata = base / "history.json"
            metadata.write_bytes(b"x")
            directory_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
            budget = audit_export.ResourceBudget(10, 1, 10, 10)
            real_read = os.read
            grew = False

            def grow_before_read(file_fd, count):
                nonlocal grew
                if not grew:
                    with metadata.open("ab") as handle:
                        handle.write(b"y" * (2 * 1024 * 1024))
                    grew = True
                return real_read(file_fd, count)

            try:
                with mock.patch.object(audit_export.os, "read", side_effect=grow_before_read):
                    with self.assertRaises(audit_export.AuditError):
                        audit_export._read_bounded_at(
                            directory_fd,
                            metadata.name,
                            3 * 1024 * 1024,
                            budget,
                        )
            finally:
                os.close(directory_fd)

            self.assertEqual(budget.metadata_bytes_remaining, 1)

    def test_cli_returns_controlled_exit_codes_without_sensitive_error_values(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            complete_export = base / "complete-secret"
            make_export(complete_export)
            complete_output = self.stable_output()
            complete = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "audit_export.py"),
                    "--export",
                    str(complete_export),
                    "--output",
                    str(complete_output),
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
            incomplete_output = self.stable_output()
            incomplete = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "audit_export.py"),
                    "--export",
                    str(incomplete_export),
                    "--output",
                    str(incomplete_output),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(incomplete.returncode, 3, incomplete.stdout + incomplete.stderr)

            sensitive = base / "sensitive-do-not-echo"
            failed_output = self.stable_output()
            failed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "audit_export.py"),
                    "--export",
                    str(sensitive),
                    "--output",
                    str(failed_output),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(failed.returncode, 65)
            self.assertEqual(
                failed.stderr,
                "audit failed [SMS-E-INPUT]: the selected files or folders were rejected safely\n",
            )
            self.assertNotIn(str(sensitive), failed.stdout + failed.stderr)
            self.assertFalse(failed_output.exists())

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
            output = self.stable_output()

            with self.assertRaises(audit_export.AuditError):
                audit_export.audit_exports([export], output, include_location=True)

            self.assertFalse(output.exists())

        for scheme_value in (
            "x://private.example/location",
            "file:/private/location",
            "javascript:privateLocation()",
        ):
            with self.subTest(scheme_value=scheme_value), tempfile.TemporaryDirectory() as directory:
                base = Path(os.path.realpath(directory))
                export = base / "scheme-location"
                make_export(
                    export,
                    rows=[
                        {
                            "Date": "2024-01-01 00:00:00 UTC",
                            "Media Type": "Image",
                            "Location": scheme_value,
                        }
                    ],
                )
                output = self.stable_output()

                with self.assertRaises(audit_export.AuditError):
                    audit_export.audit_exports(
                        [export], output, include_location=True
                    )

                self.assertFalse(output.exists())

    def test_report_value_rejects_whitespace_prefixed_absolute_path(self):
        with self.assertRaises(audit_export.AuditError):
            audit_export._validate_report_value("   /Users/example/private-history-path")

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
            output = self.stable_output()

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
