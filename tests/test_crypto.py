import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet

from src.crypto import decrypt_token, encrypt_token

KEY_1 = Fernet.generate_key().decode()
KEY_2 = Fernet.generate_key().decode()


class TestEncryptDecryptRoundTrip(unittest.TestCase):
    def test_round_trips_a_refresh_token(self):
        with patch("src.crypto.env", return_value=KEY_1):
            ciphertext = encrypt_token("1//0abc-fake-refresh-token")
            self.assertNotEqual(ciphertext, "1//0abc-fake-refresh-token")
            self.assertEqual(decrypt_token(ciphertext), "1//0abc-fake-refresh-token")

    def test_ciphertext_is_url_safe_ascii(self):
        with patch("src.crypto.env", return_value=KEY_1):
            ciphertext = encrypt_token("some-token-value")
        # Fernet output is URL-safe base64 — plain ASCII, storable in a TEXT column.
        ciphertext.encode("ascii")  # raises if not

    def test_different_calls_produce_different_ciphertext(self):
        # Fernet includes a random IV/nonce — same plaintext, different ciphertext.
        with patch("src.crypto.env", return_value=KEY_1):
            a = encrypt_token("same-value")
            b = encrypt_token("same-value")
        self.assertNotEqual(a, b)


class TestKeyRotation(unittest.TestCase):
    def test_decrypts_with_any_key_in_a_multi_key_list(self):
        # Encrypted under the old (now second) key...
        with patch("src.crypto.env", return_value=KEY_1):
            ciphertext = encrypt_token("old-token")
        # ...still decrypts once a new key is added to the front of the list.
        with patch("src.crypto.env", return_value=f"{KEY_2},{KEY_1}"):
            self.assertEqual(decrypt_token(ciphertext), "old-token")

    def test_new_encryptions_use_the_first_key(self):
        with patch("src.crypto.env", return_value=f"{KEY_2},{KEY_1}"):
            ciphertext = encrypt_token("new-token")
        # Decryptable by KEY_2 alone confirms it was encrypted with the first key.
        with patch("src.crypto.env", return_value=KEY_2):
            self.assertEqual(decrypt_token(ciphertext), "new-token")

    def test_wrong_key_fails_to_decrypt(self):
        with patch("src.crypto.env", return_value=KEY_1):
            ciphertext = encrypt_token("secret")
        with patch("src.crypto.env", return_value=KEY_2):
            with self.assertRaises(Exception):
                decrypt_token(ciphertext)


if __name__ == "__main__":
    unittest.main()
