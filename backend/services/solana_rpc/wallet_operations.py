"""
Solana Wallet Operations
Real wallet operations using Solana SDK.
"""

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from base58 import b58encode, b58decode
from typing import Dict, Optional, Tuple
import logging
from .transaction_builder import TransactionBuilder


class WalletOperations:
    """
    Handles Solana wallet operations.
    """

    def __init__(self, rpc_client):
        """
        Initialize wallet operations.

        Args:
            rpc_client: SolanaRPCClient instance
        """
        self.rpc = rpc_client
        self.tx_builder = TransactionBuilder(rpc_client)
        self.logger = logging.getLogger(__name__)

    def create_wallet(self) -> Tuple[str, str]:
        """
        Create a new Solana wallet.

        Returns:
            Tuple of (public_key_str, private_key_str)
        """
        try:
            # Generate new keypair
            keypair = Keypair()

            # Get public key
            public_key = str(keypair.pubkey())

            # Get private key as base58 string
            private_key = b58encode(bytes(keypair)).decode('utf-8')

            self.logger.info(f"Created new wallet: {public_key}")

            return public_key, private_key

        except Exception as e:
            self.logger.error(f"Failed to create wallet: {e}")
            raise

    def import_wallet_from_private_key(self, private_key_str: str) -> str:
        """
        Import wallet from private key.

        Args:
            private_key_str: Base58 encoded private key

        Returns:
            Public key string

        Raises:
            ValueError: If private key is invalid
        """
        try:
            # Decode private key
            private_key_bytes = b58decode(private_key_str)

            # Create keypair from private key
            keypair = Keypair.from_bytes(private_key_bytes)

            # Get public key
            public_key = str(keypair.pubkey())

            self.logger.info(f"Imported wallet: {public_key}")

            return public_key

        except Exception as e:
            self.logger.error(f"Failed to import wallet: {e}")
            raise ValueError(f"Invalid private key: {e}")

    def import_wallet_from_seed(self, seed_phrase: str) -> Tuple[str, str]:
        """
        Import wallet from seed phrase (mnemonic).

        Args:
            seed_phrase: BIP39 mnemonic phrase

        Returns:
            Tuple of (public_key_str, private_key_str)

        Raises:
            ValueError: If seed phrase is invalid
        """
        try:
            from mnemonic import Mnemonic

            # Validate and convert mnemonic to seed
            mnemo = Mnemonic("english")

            if not mnemo.check(seed_phrase):
                raise ValueError("Invalid mnemonic phrase")

            # Generate seed from mnemonic
            seed = mnemo.to_seed(seed_phrase)

            # Derive keypair from seed (using first 32 bytes)
            keypair = Keypair.from_seed(seed[:32])

            # Get public key
            public_key = str(keypair.pubkey())

            # Get private key
            private_key = b58encode(bytes(keypair)).decode('utf-8')

            self.logger.info(f"Imported wallet from seed: {public_key}")

            return public_key, private_key

        except Exception as e:
            self.logger.error(f"Failed to import wallet from seed: {e}")
            raise ValueError(f"Invalid seed phrase: {e}")

    def get_balance(self, public_key_str: str) -> Optional[float]:
        """
        Get SOL balance for a wallet.

        Args:
            public_key_str: Public key string

        Returns:
            Balance in SOL or None if error
        """
        try:
            import sys
            sys.stderr.write(f"[WALLET_OPS] Getting balance for: {public_key_str}\n")
            sys.stderr.flush()

            pubkey = Pubkey.from_string(public_key_str)

            sys.stderr.write(f"[WALLET_OPS] Pubkey object: {pubkey}\n")
            sys.stderr.flush()

            balance = self.rpc.get_balance(pubkey)

            sys.stderr.write(f"[WALLET_OPS] Balance from RPC: {balance}\n")
            sys.stderr.flush()

            if balance is not None:
                self.logger.debug(f"Balance for {public_key_str}: {balance} SOL")

            return balance

        except Exception as e:
            sys.stderr.write(f"[WALLET_OPS ERROR] {e}\n")
            sys.stderr.flush()
            import traceback
            traceback.print_exc()
            self.logger.error(f"Failed to get balance for {public_key_str}: {e}")
            return None

    def get_token_balances(self, public_key_str: str) -> list:
        """
        Get all SPL token balances for a wallet.

        Parses SPL Token Account data (165-byte layout) to extract mint,
        raw amount and decimals, then fetches the mint account to read
        the correct decimal precision.

        SPL Token Account layout:
          0-31  : mint pubkey
          32-63 : owner pubkey
          64-71 : amount (u64 LE)
          108   : state (0=uninit, 1=init, 2=frozen)
          ... (total 165 bytes)

        SPL Mint layout:
          48    : decimals (u8)

        Args:
            public_key_str: Owner wallet public key string

        Returns:
            List of token balance dicts with mint, amount, decimals, ui_amount
        """
        import struct

        try:
            pubkey = Pubkey.from_string(public_key_str)
            token_accounts = self.rpc.get_token_accounts_by_owner(pubkey)

            balances = []
            mint_decimals_cache: dict = {}

            for account in token_accounts:
                try:
                    acct_info = self.rpc.get_account_info(
                        Pubkey.from_string(account['pubkey'])
                    )
                    if acct_info is None:
                        continue

                    raw: bytes = acct_info.get('data', b'')
                    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 72:
                        continue

                    # Extract mint pubkey (bytes 0-31)
                    mint_bytes = raw[0:32]
                    from base58 import b58encode
                    mint_str = b58encode(mint_bytes).decode('utf-8')

                    # Extract amount (u64 LE at bytes 64-71)
                    raw_amount = struct.unpack_from('<Q', raw, 64)[0]

                    # Extract account state (byte 108): 1 = initialized
                    state = raw[108] if len(raw) > 108 else 0
                    if state == 0:
                        continue  # skip uninitialized accounts

                    # Look up decimals from mint account (cached)
                    if mint_str not in mint_decimals_cache:
                        mint_info = self.rpc.get_account_info(
                            Pubkey.from_string(mint_str)
                        )
                        decimals = 9  # safe default
                        if mint_info:
                            mint_data = mint_info.get('data', b'')
                            if isinstance(mint_data, (bytes, bytearray)) and len(mint_data) > 48:
                                decimals = mint_data[44]  # decimals at byte 44
                        mint_decimals_cache[mint_str] = decimals

                    decimals = mint_decimals_cache[mint_str]
                    ui_amount = raw_amount / (10 ** decimals) if raw_amount > 0 else 0.0

                    if ui_amount <= 0:
                        continue  # skip zero-balance accounts

                    balances.append({
                        'account': account['pubkey'],
                        'mint': mint_str,
                        'symbol': mint_str[:6] + '...',  # placeholder — no on-chain symbol
                        'amount': raw_amount,
                        'decimals': decimals,
                        'ui_amount': ui_amount,
                        'usd_value': 0.0,  # populated by price_service in the endpoint
                    })

                except Exception as inner_e:
                    self.logger.debug(f"Skipping token account parse error: {inner_e}")
                    continue

            return balances

        except Exception as e:
            self.logger.error(f"Failed to get token balances for {public_key_str}: {e}")
            return []

    def validate_address(self, address: str) -> bool:
        """
        Validate a Solana address.

        Args:
            address: Address to validate

        Returns:
            True if valid, False otherwise
        """
        try:
            Pubkey.from_string(address)
            return True
        except Exception:
            return False

    def get_recent_transactions(self, public_key_str: str, limit: int = 10) -> list:
        """
        Get recent transactions for a wallet.

        Args:
            public_key_str: Public key string
            limit: Maximum number of transactions

        Returns:
            List of transaction information
        """
        try:
            pubkey = Pubkey.from_string(public_key_str)
            signatures = self.rpc.get_signatures_for_address(pubkey, limit=limit)

            transactions = []
            for sig_info in signatures:
                # Get full transaction details
                tx_details = self.rpc.get_transaction(sig_info['signature'])

                if tx_details:
                    transactions.append({
                        'signature': sig_info['signature'],
                        'slot': sig_info['slot'],
                        'block_time': sig_info['block_time'],
                        'success': sig_info['err'] is None,
                        'error': sig_info['err']
                    })

            return transactions

        except Exception as e:
            self.logger.error(f"Failed to get transactions for {public_key_str}: {e}")
            return []

    def request_airdrop_devnet(self, public_key_str: str, amount_sol: float = 1.0) -> str:
        """
        Request airdrop on devnet (for testing).

        Args:
            public_key_str: Public key string
            amount_sol: Amount of SOL to request

        Returns:
            Transaction signature

        Raises:
            Exception: If airdrop fails
        """
        try:
            pubkey = Pubkey.from_string(public_key_str)
            lamports = int(amount_sol * 1_000_000_000)

            signature = self.rpc.request_airdrop(pubkey, lamports)

            if signature:
                self.logger.info(
                    f"Airdrop requested for {public_key_str}: {amount_sol} SOL "
                    f"(signature: {signature})"
                )
                # Poll for confirmation with a short timeout — devnet can be slow.
                # Returns immediately if unconfirmed after the timeout so the UI
                # is never left hanging; the balance will update once confirmed.
                confirmed = self.rpc.confirm_transaction(signature, timeout=30)
                if not confirmed:
                    self.logger.warning(
                        f"Airdrop confirmation timed-out (sig: {signature}). "
                        "Balance will update once the network processes it."
                    )
                return signature

            raise Exception("Airdrop returned no signature")

        except Exception as e:
            self.logger.error(f"Failed to request airdrop: {e}")
            raise

    def send_sol(
        self,
        from_private_key: str,
        to_address: str,
        amount_sol: float,
        simulate_only: bool = False
    ) -> Dict:
        """
        Send SOL from one wallet to another.

        Args:
            from_private_key: Sender's private key (base58)
            to_address: Recipient's address
            amount_sol: Amount in SOL
            simulate_only: If True, only simulate

        Returns:
            Transaction result dict
        """
        try:
            # Decode private key
            private_key_bytes = b58decode(from_private_key)
            keypair = Keypair.from_bytes(private_key_bytes)

            # Execute transfer
            result = self.tx_builder.transfer_sol(
                keypair,
                to_address,
                amount_sol,
                simulate_only
            )

            return result

        except Exception as e:
            self.logger.error(f"Failed to send SOL: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def swap_tokens(
        self,
        from_private_key: str,
        input_mint: str,
        output_mint: str,
        amount: float,
        slippage_bps: int = 50,
        simulate_only: bool = False
    ) -> Dict:
        """
        Swap tokens using Jupiter Aggregator.

        Args:
            from_private_key: Trader's private key (base58)
            input_mint: Input token mint
            output_mint: Output token mint
            amount: Amount of input tokens
            slippage_bps: Slippage tolerance in basis points
            simulate_only: If True, only simulate

        Returns:
            Swap result dict
        """
        try:
            # Decode private key
            private_key_bytes = b58decode(from_private_key)
            keypair = Keypair.from_bytes(private_key_bytes)

            # Execute swap
            result = self.tx_builder.swap_tokens(
                keypair,
                input_mint,
                output_mint,
                amount,
                slippage_bps,
                simulate_only
            )

            return result

        except Exception as e:
            self.logger.error(f"Failed to swap tokens: {e}")
            return {
                'success': False,
                'error': str(e)
            }
