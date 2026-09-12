import tempfile
import unittest
from pathlib import Path
import subprocess
from unittest.mock import patch

from pds_sync.core import (
    PdsError,
    build_pds_argv,
    inventory_drive,
    list_drives,
    redact,
    safe_destination,
    verify_versions,
)


class CoreSafetyTests(unittest.TestCase):
    def test_pds_command_has_required_stable_user_agent(self):
        argv = build_pds_argv(
            Path("/opt/aliyun"),
            ["get-user"],
            "0123456789abcdef0123456789abcdef",
        )

        self.assertEqual(argv[:3], ["/opt/aliyun", "pds", "get-user"])
        self.assertEqual(argv[-2], "--user-agent")
        self.assertEqual(
            argv[-1],
            "AlibabaCloud-Agent-Skills/alibabacloud-pds-intelligent-workspace/"
            "0123456789abcdef0123456789abcdef",
        )

    def test_redaction_removes_secret_from_diagnostics(self):
        secret = "FAKE_SECRET_VALUE"

        message = redact(f"authentication failed: {secret}", (secret,))

        self.assertEqual(message, "authentication failed: [REDACTED]")

    def test_api_key_setup_uses_explicit_pds_endpoint(self):
        calls = []

        def execute(argv, **kwargs):
            calls.append((argv, kwargs))
            output = "success" if "config" in argv else '{"user_id":"user-1"}'
            return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

        from pds_sync.core import PdsCli

        cli = PdsCli(
            Path("/opt/aliyun"),
            session_id="0123456789abcdef0123456789abcdef",
            executor=execute,
        )

        with patch.dict("os.environ", {"HTTPS_PROXY": "http://proxy.invalid:8080"}):
            cli.configure_api_key(
                "bj39311",
                "FAKE_SECRET_VALUE",
                "https://bj39311.api.aliyunfile.com",
            )

        config_argv, config_kwargs = calls[0]
        self.assertIn("--pds-endpoint", config_argv)
        endpoint_index = config_argv.index("--pds-endpoint")
        self.assertEqual(
            config_argv[endpoint_index + 1],
            "https://bj39311.api.aliyunfile.com",
        )
        self.assertNotIn("HTTPS_PROXY", config_kwargs["env"])

    def test_cloud_path_cannot_escape_download_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            with self.assertRaises(ValueError):
                safe_destination(root, "personal", "12", "/../../outside.txt")

    def test_cloud_path_is_namespaced_by_space_and_drive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            result = safe_destination(
                root,
                "team",
                "42",
                "/资料/报告.pdf",
            )

            self.assertEqual(result, root / "team-42" / "资料" / "报告.pdf")

    def test_personal_drive_fallback_has_only_one_marker_per_page(self):
        class FallbackCli:
            def __init__(self):
                self.calls = []

            def pds(self, args):
                self.calls.append(list(args))
                if args[0] == "list-all-drives":
                    raise PdsError("403 Forbidden")
                if args[0] == "list-my-drives":
                    if "next-page" in args:
                        return {"items": [], "next_marker": ""}
                    return {
                        "items": [{"drive_id": "1", "drive_name": "Mine"}],
                        "next_marker": "next-page",
                    }
                return {"items": [], "next_marker": ""}

        cli = FallbackCli()

        drives = list_drives(cli, ("personal",))

        personal_calls = [call for call in cli.calls if call[0] == "list-my-drives"]
        self.assertEqual(drives[0]["drive_id"], "1")
        self.assertEqual(personal_calls[0].count("--marker"), 0)
        self.assertEqual(personal_calls[1].count("--marker"), 1)
        self.assertEqual(personal_calls[1][-2:], ["--marker", "next-page"])

    def test_inventory_walks_root_and_nested_folders_with_list_file(self):
        class TreeCli:
            def __init__(self):
                self.parents = []

            def pds(self, args):
                self.assert_list_file(args)
                parent = args[args.index("--parent-file-id") + 1]
                self.parents.append(parent)
                if parent == "root":
                    return {
                        "items": [
                            {"file_id": "folder-1", "name": "docs", "type": "folder"},
                            {"file_id": "file-1", "name": "root.txt", "type": "file"},
                        ],
                        "next_marker": "",
                    }
                return {
                    "items": [
                        {"file_id": "file-2", "name": "nested.txt", "type": "file"}
                    ],
                    "next_marker": "",
                }

            def assert_list_file(self, args):
                if args[0] != "list-file":
                    raise AssertionError(f"unexpected command: {args}")

        cli = TreeCli()

        items = inventory_drive(
            cli,
            {"drive_id": "2", "drive_name": "DRIVEResearch", "space_type": "enterprise"},
            100,
        )

        self.assertEqual(cli.parents, ["root", "folder-1"])
        self.assertEqual([item["file_id"] for item in items], ["folder-1", "file-1", "file-2"])
        self.assertTrue(all(item["drive_id"] == "2" for item in items))

    def test_existing_pds_plugin_does_not_require_ram_profile_configuration(self):
        class VersionCli:
            def __init__(self):
                self.calls = []

            def run_text(self, args, **kwargs):
                self.calls.append(("cli", list(args)))
                if args == ["version"]:
                    return "3.5.0"
                raise AssertionError(f"unexpected command: {args}")

            def pds(self, args, **kwargs):
                self.calls.append(("pds", list(args)))
                return "aliyun-cli-pds 0.9.0"

        cli = VersionCli()

        versions = verify_versions(cli)

        self.assertEqual(versions, ("3.5.0", "aliyun-cli-pds 0.9.0"))
        self.assertEqual(cli.calls, [("cli", ["version"]), ("pds", ["version"])])


if __name__ == "__main__":
    unittest.main()
