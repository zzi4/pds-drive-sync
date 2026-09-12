import tempfile
import unittest
from pathlib import Path

from pds_sync.config import ConfigError, load_settings


class ConfigTests(unittest.TestCase):
    def test_defaults_target_all_spaces_and_detected_domain(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            settings = load_settings(root)

            self.assertEqual(settings.domain_id, "bj39311")
            self.assertEqual(
                settings.spaces,
                ("personal", "team", "enterprise"),
            )
            self.assertEqual(settings.page_size, 100)

    def test_relative_runtime_paths_are_resolved_from_project_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            settings = load_settings(root)

            self.assertEqual(settings.snapshot_dir, root / "snapshots")
            self.assertEqual(settings.download_dir, root / "downloads")

    def test_toml_overrides_non_secret_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "custom.toml"
            config_path.write_text(
                "\n".join(
                    [
                        'domain_id = "sh12345"',
                        'spaces = ["personal"]',
                        "page_size = 25",
                        'snapshot_dir = "state"',
                        'download_dir = "/tmp/pds-download-test"',
                        'aliyun_cli_path = "bin/aliyun"',
                        "timeout_seconds = 90",
                        "read_retries = 3",
                    ]
                ),
                encoding="utf-8",
            )

            settings = load_settings(root, config_path)

            self.assertEqual(settings.domain_id, "sh12345")
            self.assertEqual(settings.spaces, ("personal",))
            self.assertEqual(settings.page_size, 25)
            self.assertEqual(settings.snapshot_dir, root / "state")
            self.assertEqual(
                settings.download_dir,
                Path("/tmp/pds-download-test"),
            )
            self.assertEqual(settings.aliyun_cli_path, root / "bin/aliyun")
            self.assertEqual(settings.timeout_seconds, 90)
            self.assertEqual(settings.read_retries, 3)

    def test_unknown_space_type_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.toml"
            config_path.write_text(
                'spaces = ["personal", "public"]\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "public"):
                load_settings(root, config_path)

    def test_api_key_is_not_a_supported_config_field(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.toml"
            config_path.write_text(
                'api_key = "must-not-be-accepted"\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "api_key"):
                load_settings(root, config_path)


if __name__ == "__main__":
    unittest.main()
