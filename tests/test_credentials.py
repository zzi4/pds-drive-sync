import os
import tempfile
import unittest
from pathlib import Path

from pds_sync.credentials import CredentialError, load_or_create_api_key


class CredentialTests(unittest.TestCase):
    def test_first_hidden_input_is_saved_and_reused_without_prompt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            key_file = Path(temp_dir) / "private" / "api_key"

            first = load_or_create_api_key(
                key_file,
                prompt=lambda _message: "FAKE_SECRET_VALUE",
            )
            second = load_or_create_api_key(
                key_file,
                prompt=lambda _message: self.fail("prompt should not be called"),
            )

            self.assertEqual(first, "FAKE_SECRET_VALUE")
            self.assertEqual(second, "FAKE_SECRET_VALUE")
            self.assertEqual(os.stat(key_file).st_mode & 0o777, 0o600)

    def test_existing_key_with_group_or_world_permissions_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            key_file = Path(temp_dir) / "api_key"
            key_file.write_text("FAKE_SECRET_VALUE\n", encoding="utf-8")
            key_file.chmod(0o644)

            with self.assertRaisesRegex(CredentialError, "permissions"):
                load_or_create_api_key(key_file)


if __name__ == "__main__":
    unittest.main()
