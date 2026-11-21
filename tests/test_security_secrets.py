"""Vault regressions use temporary files and generated test keys only."""
import json
import os
import stat
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import security_secrets as vault


class SecretVaultTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "credentials.json"
        self.key = os.urandom(32)
        stub = patch.object(vault, "_backend", side_effect=lambda _: vault._AESGCM(self.key))
        stub.start()
        self.addCleanup(stub.stop)

    @staticmethod
    def model_shape(data):
        return all(isinstance(value, str) for value in data.values())

    def test_authenticated_roundtrip_and_ciphertext_only_at_rest(self):
        data = {"model": "test-secret-do-not-disclose"}
        vault.save_json(self.path, data)
        first = self.path.read_bytes()
        self.assertTrue(first.startswith(vault.MAGIC))
        self.assertNotIn(data["model"].encode(), first)
        self.assertEqual(vault.load_json(self.path), data)
        vault.save_json(self.path, data)
        self.assertNotEqual(first, self.path.read_bytes(), "Every write needs a fresh nonce")
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_legacy_plaintext_is_validated_and_migrated_without_backup(self):
        original = {"one": "first-test-key", "two": "second-test-key"}
        self.path.write_text(json.dumps(original), encoding="utf-8")
        self.assertEqual(vault.load_json(self.path, self.model_shape), original)
        self.assertTrue(self.path.read_bytes().startswith(vault.MAGIC))
        self.assertNotIn(b"first-test-key", self.path.read_bytes())
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_migration_failure_returns_no_secret_and_preserves_legacy_file(self):
        original = b'{"one":"legacy-test-key"}'
        self.path.write_bytes(original)
        with patch.object(vault.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(vault.SecretStorageError) as error:
                vault.load_json(self.path)
        self.assertNotIn("legacy-test-key", str(error.exception))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_invalid_legacy_schema_is_never_migrated_or_replaced(self):
        for original in [b'{"one":null}', b"[]", b"{broken-test-secret"]:
            with self.subTest(original=original[:8]):
                self.path.write_bytes(original)
                with self.assertRaises(vault.SecretStorageError):
                    vault.load_json(self.path, self.model_shape)
                with self.assertRaises(vault.SecretStorageError):
                    vault.save_json(self.path, {"one": "replacement"}, self.model_shape)
                self.assertEqual(self.path.read_bytes(), original)

    def test_tampered_ciphertext_fails_closed_on_read_and_write(self):
        vault.save_json(self.path, {"one": "original-test-key"})
        corrupted = bytearray(self.path.read_bytes())
        corrupted[-1] ^= 1
        self.path.write_bytes(corrupted)
        with self.assertRaises(vault.SecretStorageError):
            vault.load_json(self.path)
        with self.assertRaises(vault.SecretStorageError):
            vault.save_json(self.path, {"one": "replacement"})
        self.assertEqual(self.path.read_bytes(), corrupted)

    def test_wrong_key_or_store_name_cannot_decrypt(self):
        vault.save_json(self.path, {"one": "original-test-key"})
        original = self.path.read_bytes()
        self.key = os.urandom(32)
        with self.assertRaises(vault.SecretStorageError):
            vault.load_json(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        alternate = self.root / "security_users.enc"
        alternate.write_bytes(original)
        with self.assertRaises(vault.SecretStorageError):
            vault.load_json(alternate)

    def test_file_name_is_authenticated_even_with_correct_key(self):
        vault.save_json(self.path, {"one": "original-test-key"})
        alternate = self.root / "security_users.enc"
        alternate.write_bytes(self.path.read_bytes())
        with self.assertRaises(vault.SecretStorageError):
            vault.load_json(alternate)

    def test_atomic_failure_preserves_prior_ciphertext(self):
        vault.save_json(self.path, {"one": "original-test-key"})
        original = self.path.read_bytes()
        with patch.object(vault.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(vault.SecretStorageError):
                vault.save_json(self.path, {"two": "replacement-test-key"})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_rollback_accepts_only_authenticated_ciphertext(self):
        vault.save_json(self.path, {"one": "old-key"})
        previous = self.path.read_bytes()
        vault.save_json(self.path, {"one": "new-key"})
        vault.write_encrypted_bytes(self.path, previous)
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(vault.load_json(self.path), {"one": "old-key"})
        with self.assertRaises(vault.SecretStorageError):
            vault.write_encrypted_bytes(self.path, b'{"one":"plaintext"}')
        self.assertEqual(self.path.read_bytes(), previous)

    def test_missing_store_does_not_create_plaintext_or_require_secrets(self):
        with patch.object(vault, "_backend", side_effect=vault.SecretStorageError("not configured")):
            self.assertEqual(vault.load_json(self.path), {})
            with self.assertRaises(vault.SecretStorageError):
                vault.save_json(self.path, {"one": "new-key"})
        self.assertFalse(self.path.exists())

    def test_readiness_only_reads_header_without_decrypting_or_migrating(self):
        plaintext = b'{"one":"legacy-test-key"}'
        self.path.write_bytes(plaintext)
        with patch.object(vault, "load_json", side_effect=AssertionError("must not migrate")):
            report = vault.inspect_file(self.path)
        self.assertTrue(report["configured"])
        self.assertFalse(report["encrypted"])
        self.assertNotIn("legacy-test-key", json.dumps(report))
        self.assertEqual(self.path.read_bytes(), plaintext)
        vault.save_json(self.path, {"one": "current-test-key"})
        with patch.object(vault._AESGCM, "decrypt", side_effect=AssertionError("must not decrypt")):
            report = vault.inspect_file(self.path)
        self.assertTrue(report["encrypted"])
        self.assertFalse(report["verified"])
        self.assertNotIn("current-test-key", json.dumps(report))


class PosixKeyPolicyTests(unittest.TestCase):
    def test_owner_only_permissions_reject_shared_files_and_symlinks(self):
        with patch.object(vault.os, "geteuid", return_value=123, create=True):
            for mode, uid in [(stat.S_IFREG | 0o644, 123), (stat.S_IFREG | 0o600, 456),
                              (stat.S_IFLNK | 0o600, 123)]:
                with self.subTest(mode=mode, uid=uid):
                    path = Mock()
                    path.lstat.return_value = SimpleNamespace(st_mode=mode, st_uid=uid)
                    with self.assertRaises(vault.SecretStorageError):
                        vault._private_path(path)
            path = Mock()
            path.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=123)
            vault._private_path(path)

    def test_posix_key_must_be_configured_and_separate_from_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(vault.SecretStorageError):
                    vault._key_material(root / "credentials.json")
            for key_name in (root / "master.key", root / "nested" / "master.key"):
                with patch.dict(os.environ, {"SHOP_SECRET_KEY_FILE": str(key_name)}):
                    with self.assertRaises(vault.SecretStorageError):
                        vault._key_material(root / "credentials.json")


class CredentialIntegrationTests(unittest.TestCase):
    setUp = SecretVaultTests.setUp

    def test_model_transaction_migrates_legacy_then_rolls_back_to_ciphertext(self):
        from app import main
        original = {"1": "retained-test-key"}
        self.path.write_text(json.dumps(original), encoding="utf-8")
        with patch.object(main, "CREDENTIALS_PATH", self.path), \
                patch.object(main, "db", side_effect=lambda: nullcontext()):
            with self.assertRaisesRegex(RuntimeError, "failed commit"):
                with main.configuration_transaction("models"):
                    main.save_credential(2, "new-test-key")
                    raise RuntimeError("failed commit")
            self.assertEqual(main.read_credentials(), original)
        self.assertTrue(self.path.read_bytes().startswith(vault.MAGIC))
        self.assertNotIn(b"retained-test-key", self.path.read_bytes())

    def test_model_corruption_raises_even_for_non_strict_status_reader(self):
        from fastapi import HTTPException
        from app import main
        self.path.write_bytes(b"{broken-test-key")
        with patch.object(main, "CREDENTIALS_PATH", self.path):
            with self.assertRaises(HTTPException) as error:
                main.read_credentials(strict=False)
        self.assertEqual(error.exception.status_code, 503)
        self.assertNotIn("broken-test-key", error.exception.detail)

    def test_amazon_partial_save_migrates_and_preserves_other_accounts(self):
        from app.commerce import amazon
        fake_main = SimpleNamespace(DATA_ROOT=self.root)
        credentials = {"1": {"client_id": "first-client", "refresh_token": "first-refresh"},
                       "2": {"client_id": "second-client", "client_secret": "second-secret"}}
        path = self.root / "commerce_credentials.json"
        path.write_text(json.dumps(credentials), encoding="utf-8")
        with patch.object(amazon.s, "main", return_value=fake_main):
            amazon.save_credentials(1, {"client_secret": "new-first-secret"})
        credentials["1"]["client_secret"] = "new-first-secret"
        self.assertEqual(vault.load_json(path, amazon._valid_credentials), credentials)
        for row in credentials.values():
            for secret in row.values():
                self.assertNotIn(secret.encode(), path.read_bytes())


@unittest.skipUnless(os.name == "nt", "Real DPAPI verification requires Windows")
class WindowsDPAPITests(unittest.TestCase):
    def test_actual_dpapi_roundtrip_and_tamper_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "credentials.json"
            vault.save_json(path, {"key": "dpapi-test-key"})
            self.assertEqual(vault.load_json(path), {"key": "dpapi-test-key"})
            self.assertNotIn(b"dpapi-test-key", path.read_bytes())
            corrupted = bytearray(path.read_bytes())
            corrupted[-1] ^= 1
            path.write_bytes(corrupted)
            with self.assertRaises(vault.SecretStorageError):
                vault.load_json(path)
            self.assertEqual(path.read_bytes(), corrupted)


if __name__ == "__main__":
    unittest.main()
