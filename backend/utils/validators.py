"""
Validation Utilities
Provides validation functions for various inputs (wallet addresses, amounts, etc.)
"""

import re
from typing import Optional


def is_valid_solana_address(address: str) -> bool:
    """
    Validate a Solana wallet/token address.

    Args:
        address: Address to validate

    Returns:
        True if valid, False otherwise
    """
    if not address or not isinstance(address, str):
        return False

    # Solana addresses are base58 encoded, typically 32-44 characters
    if len(address) < 32 or len(address) > 44:
        return False

    # Check for valid base58 characters
    base58_pattern = re.compile(r'^[1-9A-HJ-NP-Za-km-z]+$')
    return bool(base58_pattern.match(address))


def is_valid_private_key(private_key: str) -> bool:
    """
    Validate a Solana private key format.

    Args:
        private_key: Private key to validate

    Returns:
        True if valid format, False otherwise
    """
    if not private_key or not isinstance(private_key, str):
        return False

    # Private keys can be in multiple formats
    # This is a basic check - actual validation would need to try parsing
    return len(private_key) > 20


def is_valid_amount(amount: float, min_amount: float = 0.0, max_amount: Optional[float] = None) -> bool:
    """
    Validate a transaction amount.

    Args:
        amount: Amount to validate
        min_amount: Minimum allowed amount
        max_amount: Optional maximum allowed amount

    Returns:
        True if valid, False otherwise
    """
    if not isinstance(amount, (int, float)):
        return False

    if amount < min_amount:
        return False

    if max_amount is not None and amount > max_amount:
        return False

    return True


def is_valid_percentage(percentage: float) -> bool:
    """
    Validate a percentage value (0-100).

    Args:
        percentage: Percentage to validate

    Returns:
        True if valid (0-100), False otherwise
    """
    if not isinstance(percentage, (int, float)):
        return False

    return 0 <= percentage <= 100


def is_valid_slippage(slippage: float) -> bool:
    """
    Validate slippage percentage (typically 0-100, but can be higher).

    Args:
        slippage: Slippage percentage to validate

    Returns:
        True if valid, False otherwise
    """
    if not isinstance(slippage, (int, float)):
        return False

    return 0 <= slippage <= 100


def sanitize_string(input_string: str, max_length: int = 1000) -> str:
    """
    Sanitize a string input.

    Args:
        input_string: String to sanitize
        max_length: Maximum allowed length

    Returns:
        Sanitized string
    """
    if not isinstance(input_string, str):
        return ""

    # Remove null bytes
    sanitized = input_string.replace('\x00', '')

    # Truncate to max length
    sanitized = sanitized[:max_length]

    # Strip whitespace
    sanitized = sanitized.strip()

    return sanitized


def validate_wallet_data(data: dict) -> tuple[bool, Optional[str]]:
    """
    Validate wallet creation/import data.

    Args:
        data: Dictionary with wallet data

    Returns:
        Tuple of (is_valid, error_message)
    """
    # Check for required fields based on operation type
    if 'private_key' in data:
        if not is_valid_private_key(data['private_key']):
            return False, "Invalid private key format"

    if 'address' in data:
        if not is_valid_solana_address(data['address']):
            return False, "Invalid Solana address"

    if 'name' in data:
        if not data['name'] or len(data['name']) > 100:
            return False, "Wallet name must be between 1-100 characters"

    return True, None


def validate_copy_trading_rule(data: dict) -> tuple[bool, Optional[str]]:
    """
    Validate copy trading rule data.

    Args:
        data: Dictionary with rule data

    Returns:
        Tuple of (is_valid, error_message)
    """
    required_fields = ['name', 'mode']

    for field in required_fields:
        if field not in data:
            return False, f"Missing required field: {field}"

    # Validate mode
    valid_modes = ['notification', 'simulation', 'auto_execution', 'precise_execution']
    if data['mode'] not in valid_modes:
        return False, f"Invalid mode. Must be one of: {', '.join(valid_modes)}"

    # Validate amounts if present
    if 'amount' in data:
        if not is_valid_amount(data['amount'], min_amount=0):
            return False, "Invalid amount"

    # Validate slippage if present
    if 'max_slippage' in data:
        if not is_valid_slippage(data['max_slippage']):
            return False, "Invalid slippage percentage"

    return True, None


def validate_token_address(address: str) -> tuple[bool, Optional[str]]:
    """
    Validate a token address for analysis.

    Args:
        address: Token address

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not address:
        return False, "Token address is required"

    if not is_valid_solana_address(address):
        return False, "Invalid token address format"

    return True, None
