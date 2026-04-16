"""
Unit tests for CryptoManager (utils/crypto_utils.py).
Tests encryption round-trips, bad-password failures, and salt persistence.
"""

import os
import sys
import pytest
import tempfile

# Ensure backend is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from utils.crypto_utils import CryptoManager


class TestCryptoManager:
    """Tests for the Fernet-based encryption utility."""

    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    @pytest.fixture
    def crypto(self, tmp_dir):
        return CryptoManager(password="test_password_123", data_dir=tmp_dir)

    def test_encrypt_decrypt_round_trip(self, crypto):
        """Encrypting then decrypting should return the original plaintext."""
        plaintext = "super_secret_private_key_abc123"
        token = crypto.encrypt(plaintext)
        assert token != plaintext
        assert crypto.decrypt(token) == plaintext

    def test_encrypt_produces_different_tokens(self, crypto):
        """Each encryption call should produce a unique ciphertext (nonce)."""
        plaintext = "same_value"
        token1 = crypto.encrypt(plaintext)
        token2 = crypto.encrypt(plaintext)
        assert token1 != token2

    def test_decrypt_wrong_password_raises(self, tmp_dir):
        """Decrypting with a different password should raise an exception."""
        crypto_a = CryptoManager(password="password_a", data_dir=tmp_dir)
        token = crypto_a.encrypt("secret_data")

        # Re-instantiate with different password but same dir to reuse the salt
        crypto_b = CryptoManager(password="password_b", data_dir=tmp_dir)
        with pytest.raises(Exception):
            crypto_b.decrypt(token)

    def test_salt_file_created(self, tmp_dir):
        """Salt file should be created on first instantiation."""
        CryptoManager(password="pw", data_dir=tmp_dir)
        assert os.path.exists(os.path.join(tmp_dir, '.salt'))

    def test_salt_file_reused(self, tmp_dir):
        """Second instantiation with same dir should reuse existing salt."""
        crypto1 = CryptoManager(password="pw", data_dir=tmp_dir)
        token = crypto1.encrypt("data")

        crypto2 = CryptoManager(password="pw", data_dir=tmp_dir)
        assert crypto2.decrypt(token) == "data"

    def test_encrypt_empty_string(self, crypto):
        assert crypto.decrypt(crypto.encrypt("")) == ""

    def test_encrypt_unicode(self, crypto):
        text = "clave_privada_🔑_ñoño"
        assert crypto.decrypt(crypto.encrypt(text)) == text
