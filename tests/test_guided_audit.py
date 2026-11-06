import json
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import guided_audit  # noqa: E402
from test_audit_export import make_export  # noqa: E402


class GuidedAuditTests(unittest.TestCase):
    def stable_output(self) -> Path:
        output = Path("/private/tmp") / "sms-v02-guided-report-{}".format(uuid.uuid4().hex)

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

    def test_guided_usage_error_does_not_echo_sensitive_argument(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = guided_audit.main(["--unknown", "/private/sensitive-export"])
        self.assertEqual(status, 64)
        self.assertEqual(
            stderr.getvalue(),
            "audit failed [SMS-E-USAGE]: invalid command usage\n",
        )
        self.assertNotIn("sensitive", stderr.getvalue())

    def test_build_audit_command_preserves_each_path_as_one_argument(self):
        command = guided_audit.build_audit_command(
            [
                Path("/private/Export One"),
                Path("/private/export\nTwo"),
                Path('/private/$HOME;$(touch should-not-run);"quote";Caf\u00e9'),
            ],
            Path("/private/Report Folder"),
        )

        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1], str(SCRIPTS / "audit_export.py"))
        self.assertEqual(
            command[2:],
            [
                "--export",
                "/private/Export One",
                "--export",
                "/private/export\nTwo",
                "--export",
                '/private/$HOME;$(touch should-not-run);"quote";Caf\u00e9',
                "--output",
                "/private/Report Folder",
            ],
        )

    def test_failed_audit_never_opens_a_report(self):
        failed = guided_audit.subprocess.CompletedProcess([], 65)
        with mock.patch.object(
            guided_audit.subprocess, "run", return_value=failed
        ) as run_mock:
            status = guided_audit.run_audit(
                [Path("/private/export")], Path("/private/report"), open_result=True
            )

        self.assertEqual(status, 65)
        self.assertEqual(run_mock.call_count, 1)

    def test_unknown_audit_status_is_contained(self):
        completed = guided_audit.subprocess.CompletedProcess([], 42)
        with mock.patch.object(
            guided_audit.subprocess, "run", return_value=completed
        ):
            status = guided_audit.run_audit(
                [Path("/private/export")], Path("/private/report"), open_result=False
            )

        self.assertEqual(status, 70)

    def test_audit_process_launch_failure_is_privacy_safe(self):
        stderr = io.StringIO()
        with mock.patch.object(
            guided_audit.subprocess,
            "run",
            side_effect=OSError("private launch detail"),
        ), contextlib.redirect_stderr(stderr):
            status = guided_audit.main(
                ["--export", "/private/secret-export", "--output", "/private/report"]
            )

        self.assertEqual(status, 74)
        self.assertEqual(
            stderr.getvalue(),
            "audit failed [SMS-E-IO]: a local file operation failed safely\n",
        )
        self.assertNotIn("secret", stderr.getvalue())

    def test_result_open_failure_is_privacy_safe(self):
        stderr = io.StringIO()
        completed = guided_audit.subprocess.CompletedProcess([], 0)
        with mock.patch.object(
            guided_audit.subprocess,
            "run",
            side_effect=[completed, OSError("private open detail")],
        ), contextlib.redirect_stderr(stderr):
            status = guided_audit.main(
                ["--export", "/private/secret-export", "--output", "/private/report"]
            )

        self.assertEqual(status, 74)
        self.assertEqual(
            stderr.getvalue(),
            "audit failed [SMS-E-IO]: a local file operation failed safely\n",
        )
        self.assertNotIn("secret", stderr.getvalue())

    def test_chooser_failure_is_privacy_safe(self):
        stderr = io.StringIO()
        with mock.patch.object(
            guided_audit,
            "choose_export_folders",
            side_effect=RuntimeError("private chooser detail"),
        ), mock.patch.object(guided_audit, "_show_message") as message_mock, contextlib.redirect_stderr(
            stderr
        ):
            status = guided_audit.main([])

        self.assertEqual(status, 70)
        self.assertEqual(
            stderr.getvalue(),
            "audit failed [SMS-E-INTERNAL]: an unexpected internal error was contained\n",
        )
        self.assertNotIn("private", stderr.getvalue())
        message_mock.assert_called_once()

    def test_run_audit_noninteractive_creates_and_opens_complete_report(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(os.path.realpath(directory))
            export = base / "export"
            output = self.stable_output()
            make_export(export)
            real_run = guided_audit.subprocess.run

            def run_without_open(command, *args, **kwargs):
                if command[0] == "open":
                    return guided_audit.subprocess.CompletedProcess(command, 0)
                return real_run(command, *args, **kwargs)

            with mock.patch.object(
                guided_audit.subprocess, "run", side_effect=run_without_open
            ) as run_mock:
                result = guided_audit.run_audit([export], output, open_result=True)

            self.assertEqual(result, 0)
            self.assertTrue((output / "start_here.html").is_file())
            open_calls = [call for call in run_mock.call_args_list if call.args and call.args[0][0] == "open"]
            self.assertEqual(len(open_calls), 1)
            self.assertEqual(open_calls[0].args[0], ["open", str(output / "start_here.html")])

    def test_jxa_folder_selection_is_json_and_cancel_is_controlled(self):
        completed = mock.Mock(returncode=0, stdout=json.dumps(["/private/One", "/private/Two"]), stderr="")
        with mock.patch.object(guided_audit.subprocess, "run", return_value=completed):
            selected = guided_audit.choose_export_folders()
        self.assertEqual(selected, [Path("/private/One"), Path("/private/Two")])

        cancelled = mock.Mock(returncode=1, stdout="", stderr="User canceled. (-128)\n")
        with mock.patch.object(guided_audit.subprocess, "run", return_value=cancelled):
            with self.assertRaises(guided_audit.UserCancelled):
                guided_audit.choose_export_folders()

    def test_chooser_mode_uses_stable_shared_folder_and_forwards_no_open(self):
        exports = [Path("/private/export")]
        output = Path("/private/report")
        with mock.patch.object(
            guided_audit, "choose_export_folders", return_value=exports
        ), mock.patch.object(
            guided_audit, "default_output", return_value=output
        ) as default_mock, mock.patch.object(
            guided_audit, "run_audit", return_value=0
        ) as run_mock:
            status = guided_audit.main(["--no-open"])

        self.assertEqual(status, 0)
        default_mock.assert_called_once_with(Path("/Users/Shared"))
        run_mock.assert_called_once_with(exports, output, open_result=False)

    def test_chooser_failure_dialog_includes_safe_error_code(self):
        exports = [Path("/private/export")]
        output = Path("/private/report")
        with mock.patch.object(
            guided_audit, "choose_export_folders", return_value=exports
        ), mock.patch.object(
            guided_audit, "default_output", return_value=output
        ), mock.patch.object(
            guided_audit, "run_audit", return_value=65
        ), mock.patch.object(guided_audit, "_show_message") as message_mock:
            status = guided_audit.main([])

        self.assertEqual(status, 65)
        message = message_mock.call_args.args[0]
        self.assertIn("SMS-E-INPUT", message)
        self.assertNotIn(str(output), message)

    def test_output_failure_dialog_gives_safe_corrective_action(self):
        with mock.patch.object(
            guided_audit, "choose_export_folders", return_value=[Path("/private/export")]
        ), mock.patch.object(
            guided_audit, "default_output", return_value=Path("/private/report")
        ), mock.patch.object(
            guided_audit, "run_audit", return_value=73
        ), mock.patch.object(guided_audit, "_show_message") as message_mock:
            status = guided_audit.main([])

        self.assertEqual(status, 73)
        message = message_mock.call_args.args[0]
        self.assertIn("/Users/Shared", message)
        self.assertIn("free space", message)
        self.assertIn("compatible local Mac filesystem", message)
        self.assertIn("SMS-E-OUTPUT", message)

    def test_double_click_launcher_exists_and_is_executable(self):
        launcher = REPO / "Run Snapchat Memories Saver.command"
        self.assertTrue(launcher.is_file())
        self.assertTrue(os.access(launcher, os.X_OK))
        text = launcher.read_text(encoding="utf-8")
        self.assertIn("scripts/guided_audit.py", text)
        self.assertIn("command -v python3", text)
        self.assertIn("Python 3.9", text)
        self.assertIn("/usr/bin/osascript", text)
        self.assertNotIn("eval", text)

    def test_release_docs_keep_private_reports_out_of_ai_context(self):
        skill = (REPO / "SKILL.md").read_text(encoding="utf-8")
        project_skill_path = (
            REPO
            / ".hermes"
            / "skills"
            / "snapchat-memories-saver"
            / "SKILL.md"
        )
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        compatibility = (REPO / "COMPATIBILITY.md").read_text(encoding="utf-8")

        self.assertTrue(project_skill_path.is_file())
        self.assertEqual(
            project_skill_path.read_bytes(),
            (REPO / "SKILL.md").read_bytes(),
        )
        self.assertIn("From the trusted project root", skill)
        self.assertNotIn("From this skill's folder", skill)
        self.assertNotIn("](scripts/audit_export.py)", skill)
        self.assertNotIn("](references/export-format.md)", skill)
        self.assertIn("Keep detailed reports out of AI context by default", skill)
        self.assertIn("user-reviewed `support.txt`", skill)
        self.assertIn("directly in `/Users/Shared`", readme)
        self.assertIn("`69`", readme)
        self.assertIn(".hermes/skills/snapchat-memories-saver/SKILL.md", readme)
        self.assertIn("hermes skills trust .", readme)
        self.assertIn("hermes skills trust .", compatibility)
        self.assertIn("Do not install mutable `main`", compatibility)


if __name__ == "__main__":
    unittest.main()
