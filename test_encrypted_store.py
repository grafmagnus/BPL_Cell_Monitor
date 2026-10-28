import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from encrypted_store import EncryptedFileBackend, restrict_new_file
from password_auth import configured_password, _file_password
import credentials


class EncryptedStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault = self.root / "data" / "password.enc"
        self.key = self.root / "secrets" / "password.key"
        self.backend = EncryptedFileBackend(self.vault, self.key)
        _file_password.cache_clear()

    def tearDown(self):
        _file_password.cache_clear()
        self.tmp.cleanup()

    def test_authenticated_encrypted_roundtrip_and_no_plaintext_on_disk(self):
        self.backend.set_password("bpl", "root@*", "SYNTHETIC-PASSWORD-äöü")
        self.assertEqual(self.backend.get_password("bpl", "root@*"), "SYNTHETIC-PASSWORD-äöü")
        self.assertNotIn(b"SYNTHETIC", self.vault.read_bytes())
        self.assertNotIn(b"root@*", self.vault.read_bytes())
        self.assertNotIn(b"SYNTHETIC", self.key.read_bytes())
        if os.name != "nt":
            self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.vault.stat().st_mode & 0o777, 0o600)

    def test_updates_preserve_existing_key_and_other_accounts(self):
        self.backend.set_password("bpl", "root@*", "one")
        original_key = self.key.read_bytes()
        self.backend.set_password("bpl", "root@he", "specific")
        self.backend.set_password("bpl", "root@*", "two")
        self.assertEqual(original_key, self.key.read_bytes())
        self.assertEqual(self.backend.get_password("bpl", "root@he"), "specific")
        self.assertEqual(self.backend.get_password("bpl", "root@*"), "two")

    def test_tampering_is_rejected(self):
        self.backend.set_password("bpl", "root@*", "one")
        content = bytearray(self.vault.read_bytes())
        content[len(content)//2] ^= 1
        self.vault.write_bytes(content)
        with self.assertRaisesRegex(RuntimeError, "Cannot decrypt"):
            self.backend.get_password("bpl", "root@*")

    def test_missing_key_is_never_recreated_for_existing_vault(self):
        self.backend.set_password("bpl", "root@*", "one")
        self.key.unlink()
        before = self.vault.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "key is missing"):
            self.backend.set_password("bpl", "root@*", "two")
        self.assertFalse(self.key.exists())
        self.assertEqual(before, self.vault.read_bytes())

    def test_wrong_key_is_rejected_without_overwriting_vault(self):
        self.backend.set_password("bpl", "root@*", "one")
        before = self.vault.read_bytes()
        self.key.write_bytes(Fernet.generate_key())
        with self.assertRaisesRegex(RuntimeError, "Cannot decrypt"):
            self.backend.set_password("bpl", "root@*", "two")
        self.assertEqual(before, self.vault.read_bytes())

    def test_shared_password_used_for_every_headend_without_native_store(self):
        self.backend.set_password("bpl", "root@*", "shared")
        cfg = {"credential_storage": "encrypted-file", "credential_file": str(self.vault),
               "credential_key_file": str(self.key), "credential_service": "bpl"}
        with patch("password_auth.secure_backend", side_effect=AssertionError("Native store must not be used")):
            self.assertEqual(configured_password(cfg, "root", "he-one"), "shared")
            self.assertEqual(configured_password(cfg, "root", "he-two"), "shared")

    def test_delete_and_missing_status_do_not_create_new_key(self):
        self.assertIsNone(self.backend.get_password("bpl", "root@*"))
        self.assertFalse(self.key.exists())
        self.backend.set_password("bpl", "root@*", "one")
        self.backend.delete_password("bpl", "root@*")
        self.assertIsNone(self.backend.get_password("bpl", "root@*"))
        with self.assertRaises(RuntimeError):
            self.backend.delete_password("bpl", "root@*")

    def test_same_key_and_vault_path_refused(self):
        with self.assertRaises(RuntimeError):
            EncryptedFileBackend(self.vault, self.vault)

    def test_acl_setup_failure_never_writes_key_or_password(self):
        with patch("encrypted_store.restrict_new_file", side_effect=RuntimeError("ACL denied")), self.assertRaises(RuntimeError):
            self.backend.set_password("bpl", "root@*", "synthetic")
        self.assertFalse(self.key.exists())
        self.assertFalse(self.vault.exists())

    def test_cli_set_without_native_keyring(self):
        cfg = {"ssh_user": "root", "headends": [{"ssh_host": "he"}],
               "credential_storage": "encrypted-file", "credential_file": str(self.vault),
               "credential_key_file": str(self.key), "credential_service": "bpl"}
        output = io.StringIO()
        with patch("sys.argv", ["credentials.py", "set"]), patch("credentials.load_config", return_value=cfg), patch(
                "credentials.secure_backend", side_effect=AssertionError("Native store must not be used")), patch(
                "sys.stdin.isatty", return_value=True), patch(
                "credentials.getpass.getpass", side_effect=["SYNTHETIC", "SYNTHETIC"]), patch("sys.stdout", output):
            credentials.main()
        self.assertEqual(self.backend.get_password("bpl", "root@*"), "SYNTHETIC")
        self.assertNotIn("SYNTHETIC", output.getvalue())


if __name__ == "__main__":
    unittest.main()
