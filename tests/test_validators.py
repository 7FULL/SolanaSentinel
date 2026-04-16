"""
Unit tests for validators (utils/validators.py).
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from utils.validators import (
    validate_solana_address,
    validate_amount,
    validate_percentage,
    sanitize_string,
)


class TestSolanaAddressValidator:
    """Tests for Solana address validation."""

    VALID_ADDRESSES = [
        "So11111111111111111111111111111111111111112",     # SOL mint (44 chars)
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",    # Token program
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",   # Raydium
        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",    # Pump.fun
    ]

    INVALID_ADDRESSES = [
        "",                  # Empty string
        "short",             # Too short
        "0x1234567890abcdef",  # Ethereum address
        "not_base58_!!!!!",  # Invalid characters
        "a" * 100,           # Too long
    ]

    @pytest.mark.parametrize("address", VALID_ADDRESSES)
    def test_valid_addresses(self, address):
        assert validate_solana_address(address) is True

    @pytest.mark.parametrize("address", INVALID_ADDRESSES)
    def test_invalid_addresses(self, address):
        assert validate_solana_address(address) is False


class TestAmountValidator:
    """Tests for amount validation."""

    def test_positive_amount_valid(self):
        assert validate_amount(0.1) is True

    def test_zero_amount_invalid(self):
        assert validate_amount(0) is False

    def test_negative_amount_invalid(self):
        assert validate_amount(-1.5) is False

    def test_very_small_amount(self):
        assert validate_amount(0.000001) is True

    def test_large_amount_valid_by_default(self):
        assert validate_amount(1000.0) is True

    def test_amount_exceeds_max(self):
        assert validate_amount(200.0, max_amount=100.0) is False

    def test_amount_within_max(self):
        assert validate_amount(50.0, max_amount=100.0) is True

    def test_amount_below_min(self):
        assert validate_amount(0.0001, min_amount=0.01) is False


class TestPercentageValidator:
    """Tests for percentage validation."""

    def test_valid_percentages(self):
        for pct in [0, 0.5, 50.0, 100.0]:
            assert validate_percentage(pct) is True

    def test_negative_percentage(self):
        assert validate_percentage(-1) is False

    def test_over_100(self):
        assert validate_percentage(101) is False


class TestSanitizeString:
    """Tests for string sanitization."""

    def test_normal_string_unchanged(self):
        assert sanitize_string("hello world") == "hello world"

    def test_null_bytes_removed(self):
        result = sanitize_string("hello\x00world")
        assert "\x00" not in result

    def test_truncation(self):
        long_str = "a" * 500
        result = sanitize_string(long_str, max_length=100)
        assert len(result) <= 100

    def test_empty_string(self):
        assert sanitize_string("") == ""
