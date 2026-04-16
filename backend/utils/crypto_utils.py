"""
Cryptography Utilities
Handles encryption/decryption of sensitive data (private keys, seed phrases).
"""

import os
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from typing import Optional


class CryptoManager:
    """
    Manages encryption and decryption of sensitive data.
    Uses Fernet (symmetric encryption) with a key derived from a password.
    """

    def __init__(self, password: Optional[str] = None):
        """
        Initialize the crypto manager.

        Args:
            password: Optional password for encryption (uses env var if not provided)
        """
        self.password = password or os.getenv('ENCRYPTION_PASSWORD', 'default_password_change_me')
        self.salt = self._get_or_create_salt()
        self.key = self._derive_key()
        self.fernet = Fernet(self.key)

    def _get_or_create_salt(self) -> bytes:
        """
        Get existing salt or create a new one.

        Returns:
            Salt bytes
        """
        salt_file = os.path.join(os.path.dirname(__file__), '..', 'data', '.salt')
        os.makedirs(os.path.dirname(salt_file), exist_ok=True)

        if os.path.exists(salt_file):
            with open(salt_file, 'rb') as f:
                return f.read()
        else:
            # Generate new salt
            salt = os.urandom(16)
            with open(salt_file, 'wb') as f:
                f.write(salt)
            return salt

    def _derive_key(self) -> bytes:
        """
        Derive encryption key from password using PBKDF2.

        Returns:
            Derived key bytes
        """
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=self.salt,
            iterations=100000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(self.password.encode()))
        return key

    def encrypt(self, data: str) -> str:
        """
        Encrypt a string.

        Args:
            data: String to encrypt

        Returns:
            Encrypted string (base64 encoded)
        """
        encrypted_bytes = self.fernet.encrypt(data.encode())
        return base64.urlsafe_b64encode(encrypted_bytes).decode()

    def decrypt(self, encrypted_data: str) -> str:
        """
        Decrypt a string.

        Args:
            encrypted_data: Encrypted string (base64 encoded)

        Returns:
            Decrypted string

        Raises:
            Exception if decryption fails
        """
        try:
            encrypted_bytes = base64.urlsafe_b64decode(encrypted_data.encode())
            decrypted_bytes = self.fernet.decrypt(encrypted_bytes)
            return decrypted_bytes.decode()
        except Exception as e:
            raise Exception(f"Decryption failed: {str(e)}")

    def encrypt_dict(self, data: dict, fields_to_encrypt: list) -> dict:
        """
        Encrypt specific fields in a dictionary.

        Args:
            data: Dictionary containing data
            fields_to_encrypt: List of field names to encrypt

        Returns:
            Dictionary with encrypted fields
        """
        encrypted_data = data.copy()

        for field in fields_to_encrypt:
            if field in encrypted_data and encrypted_data[field]:
                encrypted_data[field] = self.encrypt(str(encrypted_data[field]))

        return encrypted_data

    def decrypt_dict(self, data: dict, fields_to_decrypt: list) -> dict:
        """
        Decrypt specific fields in a dictionary.

        Args:
            data: Dictionary containing encrypted data
            fields_to_decrypt: List of field names to decrypt

        Returns:
            Dictionary with decrypted fields
        """
        decrypted_data = data.copy()

        for field in fields_to_decrypt:
            if field in decrypted_data and decrypted_data[field]:
                try:
                    decrypted_data[field] = self.decrypt(decrypted_data[field])
                except Exception:
                    # If decryption fails, keep original value
                    pass

        return decrypted_data


def generate_random_key(length: int = 32) -> str:
    """
    Generate a random key for encryption.

    Args:
        length: Length of the key in bytes

    Returns:
        Random key as hex string
    """
    return os.urandom(length).hex()


def hash_data(data: str) -> str:
    """
    Create a SHA256 hash of data.

    Args:
        data: String to hash

    Returns:
        Hex string of hash
    """
    import hashlib
    return hashlib.sha256(data.encode()).hexdigest()
