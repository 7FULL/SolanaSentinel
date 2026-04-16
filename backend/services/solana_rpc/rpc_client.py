"""
Solana RPC Client
Handles all communication with Solana blockchain via RPC.
"""

from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed, Finalized, Processed
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import Transaction
from typing import Optional, Dict, List
import logging


class SolanaRPCClient:
    """
    Wrapper for Solana RPC client with helper methods.
    Handles connection to Solana blockchain and provides common operations.
    """

    def __init__(self, rpc_url: str, commitment: str = 'confirmed'):
        """
        Initialize Solana RPC client.

        Args:
            rpc_url: Solana RPC endpoint URL
            commitment: Commitment level (processed, confirmed, finalized)
        """
        self.rpc_url = rpc_url
        self.client  = self._create_client(rpc_url)

        # Map commitment string to commitment object
        commitment_map = {
            'processed': Processed,
            'confirmed': Confirmed,
            'finalized': Finalized
        }
        self.commitment = commitment_map.get(commitment.lower(), Confirmed)

        self.logger = logging.getLogger(__name__)

    @staticmethod
    def _create_client(rpc_url: str) -> Client:
        """Create a new solana-py HTTP client for the given RPC URL."""
        return Client(rpc_url)
        self.logger.info(f"Solana RPC client initialized: {rpc_url}")

    def is_connected(self) -> bool:
        """
        Check if RPC connection is working.

        Returns:
            True if connected, False otherwise
        """
        try:
            response = self.client.get_health()
            return response.value == "ok"
        except Exception as e:
            self.logger.error(f"RPC connection check failed: {e}")
            return False

    def get_balance(self, pubkey: Pubkey) -> Optional[float]:
        """
        Get SOL balance for a public key.

        Args:
            pubkey: Public key to check balance for

        Returns:
            Balance in SOL, or None if error
        """
        try:
            response = self.client.get_balance(pubkey, commitment=self.commitment)

            if response.value is not None:
                # Convert lamports to SOL (1 SOL = 1_000_000_000 lamports)
                balance_sol = response.value / 1_000_000_000
                return balance_sol
            return None
        except Exception as e:
            self.logger.error(f"Failed to get balance for {pubkey}: {e}")
            return None

    def get_token_accounts_by_owner(self, owner: Pubkey) -> List[Dict]:
        """
        Get all token accounts owned by a wallet.

        Args:
            owner: Owner public key

        Returns:
            List of token account information
        """
        try:
            from solders.rpc.config import RpcTokenAccountsFilterMint
            from solana.rpc.types import TokenAccountOpts

            # Get all token accounts (SPL tokens)
            response = self.client.get_token_accounts_by_owner(
                owner,
                TokenAccountOpts(program_id=Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")),
                commitment=self.commitment
            )

            token_accounts = []
            if response.value:
                for account_info in response.value:
                    token_accounts.append({
                        'pubkey': str(account_info.pubkey),
                        'account': account_info.account
                    })

            return token_accounts
        except Exception as e:
            self.logger.error(f"Failed to get token accounts for {owner}: {e}")
            return []

    def get_account_info(self, pubkey: Pubkey) -> Optional[Dict]:
        """
        Get account information.

        Args:
            pubkey: Account public key

        Returns:
            Account information or None
        """
        try:
            response = self.client.get_account_info(pubkey, commitment=self.commitment)

            if response.value:
                return {
                    'lamports': response.value.lamports,
                    'owner': str(response.value.owner),
                    'executable': response.value.executable,
                    'rent_epoch': response.value.rent_epoch,
                    'data': response.value.data
                }
            return None
        except Exception as e:
            self.logger.error(f"Failed to get account info for {pubkey}: {e}")
            return None

    def get_recent_blockhash(self) -> Optional[str]:
        """
        Get recent blockhash for transactions.

        Returns:
            Recent blockhash string or None
        """
        try:
            response = self.client.get_latest_blockhash(commitment=self.commitment)

            if response.value:
                return str(response.value.blockhash)
            return None
        except Exception as e:
            self.logger.error(f"Failed to get recent blockhash: {e}")
            return None

    def send_transaction(self, transaction: Transaction, signers: List[Keypair],
                        skip_preflight: bool = False, recent_blockhash=None) -> Optional[str]:
        """
        Send a transaction to the blockchain.

        Args:
            transaction: Transaction to send
            signers: List of keypairs to sign with
            skip_preflight: Skip preflight transaction checks
            recent_blockhash: Recent blockhash for signing

        Returns:
            Transaction signature or None if failed
        """
        try:
            # Get recent blockhash if not provided
            if recent_blockhash is None:
                recent_blockhash = self.client.get_latest_blockhash().value.blockhash

            # Sign transaction
            transaction.sign(signers, recent_blockhash)

            # Serialize and send transaction
            from solana.rpc.types import TxOpts
            serialized_tx = bytes(transaction)

            response = self.client.send_raw_transaction(
                serialized_tx,
                opts=TxOpts(skip_preflight=skip_preflight, preflight_commitment=self.commitment)
            )

            if response.value:
                signature = str(response.value)
                self.logger.info(f"Transaction sent: {signature}")
                return signature
            return None
        except Exception as e:
            self.logger.error(f"Failed to send transaction: {e}")
            return None

    def confirm_transaction(self, signature: str, timeout: int = 60) -> bool:
        """
        Poll for transaction confirmation, respecting a hard timeout.

        Uses get_signature_statuses polling instead of the blocking
        confirm_transaction RPC call, which can hang indefinitely on devnet.

        Args:
            signature: Transaction signature to confirm
            timeout: Maximum seconds to wait before giving up

        Returns:
            True if confirmed/finalized within timeout, False otherwise
        """
        import time
        from solders.signature import Signature

        try:
            sig = Signature.from_string(signature)
            deadline = time.monotonic() + timeout
            poll_interval = 2  # seconds between polls

            while time.monotonic() < deadline:
                try:
                    response = self.client.get_signature_statuses([sig])
                    if response.value and response.value[0] is not None:
                        status = response.value[0]
                        # err is None means the transaction succeeded
                        if status.err is None:
                            self.logger.debug(f"Transaction confirmed: {signature}")
                            return True
                        else:
                            # Transaction was processed but failed on-chain
                            self.logger.error(
                                f"Transaction {signature} failed on-chain: {status.err}"
                            )
                            return False
                except Exception as poll_err:
                    self.logger.debug(f"Polling error (will retry): {poll_err}")

                time.sleep(poll_interval)

            self.logger.warning(
                f"confirm_transaction timed out after {timeout}s for {signature}"
            )
            return False

        except Exception as e:
            self.logger.error(f"Failed to confirm transaction {signature}: {e}")
            return False

    def simulate_transaction(self, transaction: Transaction, signers: List[Keypair], recent_blockhash=None) -> Dict:
        """
        Simulate a transaction without sending it.

        Args:
            transaction: Transaction to simulate
            signers: Signers for the transaction
            recent_blockhash: Recent blockhash for signing

        Returns:
            Simulation result
        """
        try:
            # Get recent blockhash if not provided
            if recent_blockhash is None:
                recent_blockhash = self.client.get_latest_blockhash().value.blockhash

            # Sign transaction
            transaction.sign(signers, recent_blockhash)

            response = self.client.simulate_transaction(transaction, commitment=self.commitment)

            return {
                'success': response.value.err is None,
                'logs': response.value.logs if response.value else [],
                'error': str(response.value.err) if response.value and response.value.err else None
            }
        except Exception as e:
            self.logger.error(f"Failed to simulate transaction: {e}")
            return {
                'success': False,
                'logs': [],
                'error': str(e)
            }

    def get_transaction(self, signature: str) -> Optional[Dict]:
        """
        Get transaction details.

        Args:
            signature: Transaction signature

        Returns:
            Transaction details or None
        """
        try:
            from solders.signature import Signature

            sig = Signature.from_string(signature)
            response = self.client.get_transaction(
                sig,
                commitment=self.commitment,
                max_supported_transaction_version=0,
            )

            if not response.value:
                return None

            val = response.value

            # With max_supported_transaction_version=0 solders returns an
            # EncodedConfirmedTransactionWithStatusMeta where the meta lives
            # inside .transaction (an EncodedTransactionWithStatusMeta),
            # not directly on the top-level object.
            tx_with_meta = getattr(val, 'transaction', None)
            meta = (
                getattr(tx_with_meta, 'meta', None)   # versioned path
                if tx_with_meta is not None
                else getattr(val, 'meta', None)        # legacy path
            )

            return {
                'slot':        getattr(val, 'slot',       None),
                'transaction': tx_with_meta,
                'meta':        meta,
                'block_time':  getattr(val, 'block_time', None),
            }
        except Exception as e:
            self.logger.error(f"Failed to get transaction {signature}: {e}")
            return None

    def get_transaction_accounts(self, signature: str) -> Optional[List[str]]:
        """
        Fetch the ordered list of account public keys involved in a transaction.

        Uses a direct JSON-RPC HTTP call with json encoding so we get
        base58 account keys without needing to deserialize binary data.

        Args:
            signature: Transaction signature (base58 string)

        Returns:
            List of account address strings in transaction order, or None on error.
        """
        import requests as _requests

        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [
                    signature,
                    {
                        "encoding": "jsonParsed",
                        "maxSupportedTransactionVersion": 0,
                        "commitment": "confirmed",
                    },
                ],
            }
            resp = _requests.post(self.rpc_url, json=payload, timeout=8)
            resp.raise_for_status()
            result = resp.json().get("result")
            if not result:
                return None

            msg = result.get("transaction", {}).get("message", {})
            raw_keys = msg.get("accountKeys", [])
            if not raw_keys:
                return None

            # jsonParsed returns objects: {pubkey, signer, writable, source}
            # Normalise to a list of dicts with at least 'pubkey'
            accounts = []
            for entry in raw_keys:
                if isinstance(entry, dict):
                    accounts.append({
                        "pubkey":   entry.get("pubkey", ""),
                        "signer":   entry.get("signer", False),
                        "writable": entry.get("writable", False),
                    })
                else:
                    # Fallback: plain string
                    accounts.append({"pubkey": str(entry), "signer": False, "writable": False})

            return accounts if accounts else None

        except Exception as e:
            self.logger.debug(f"get_transaction_accounts failed for {signature[:20]}: {e}")
            return None

    def get_token_metadata(self, mint_address: str) -> Optional[Dict[str, str]]:
        """
        Fetch token metadata (name, symbol) from on-chain sources.

        Tries two strategies in order:
          1. Token-2022 metadata extension — pump.fun (and many modern tokens)
             store metadata directly on the mint account as a Token-2022
             `tokenMetadata` extension.  We fetch the mint with jsonParsed
             encoding; the RPC parses all extensions automatically.
          2. Metaplex metadata PDA — older SPL tokens and some Raydium tokens
             use a separate account derived from the mint address.

        Args:
            mint_address: Token mint address (base58 string)

        Returns:
            Dict with 'name' and 'symbol' strings, or None if not found.
        """
        import requests as _requests
        import base64
        import struct

        try:
            # ── Strategy 1: Token-2022 metadata extension (jsonParsed) ──────────
            # The RPC returns all extensions decoded; we just look for
            # the 'tokenMetadata' entry in the extensions array.
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAccountInfo",
                "params": [
                    mint_address,
                    {"encoding": "jsonParsed", "commitment": "confirmed"},
                ],
            }
            resp = _requests.post(self.rpc_url, json=payload, timeout=8)
            resp.raise_for_status()
            value = resp.json().get("result", {}).get("value") or {}
            parsed_data = (value.get("data") or {}).get("parsed") or {}
            info = parsed_data.get("info") or {}
            extensions = info.get("extensions") or []

            for ext in extensions:
                if ext.get("extension") == "tokenMetadata":
                    state = ext.get("state") or {}
                    name   = (state.get("name")   or "").strip()
                    symbol = (state.get("symbol") or "").strip()
                    if name or symbol:
                        self.logger.debug(
                            f"Token-2022 metadata for {mint_address[:12]}…: "
                            f"{symbol} / {name}"
                        )
                        return {"name": name or "Unknown", "symbol": symbol or "???"}

        except Exception as e:
            self.logger.debug(
                f"Token-2022 metadata lookup failed for {mint_address[:12]}: {e}"
            )

        # ── Strategy 2: Metaplex metadata PDA ───────────────────────────────────
        # Used by older SPL tokens and Raydium-listed tokens.
        METADATA_PROGRAM_ID = Pubkey.from_string(
            "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"
        )

        try:
            mint_pubkey = Pubkey.from_string(mint_address)

            seeds = [
                b"metadata",
                bytes(METADATA_PROGRAM_ID),
                bytes(mint_pubkey),
            ]
            metadata_pda, _ = Pubkey.find_program_address(seeds, METADATA_PROGRAM_ID)

            payload = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "getAccountInfo",
                "params": [
                    str(metadata_pda),
                    {"encoding": "base64", "commitment": "confirmed"},
                ],
            }
            resp = _requests.post(self.rpc_url, json=payload, timeout=8)
            resp.raise_for_status()
            account = resp.json().get("result", {}).get("value")
            if not account:
                self.logger.debug(f"No Metaplex metadata for {mint_address[:12]}…")
                return None

            raw = base64.b64decode(account["data"][0])

            # Borsh layout: key(1) + update_authority(32) + mint(32) + name + symbol
            offset = 1 + 32 + 32

            def read_string(data: bytes, pos: int):
                """Read a Borsh-encoded string (u32 len + utf8 bytes)."""
                if pos + 4 > len(data):
                    return "", pos
                length = struct.unpack_from("<I", data, pos)[0]
                pos += 4
                if length > 200 or pos + length > len(data):
                    return "", pos
                text = data[pos:pos + length].decode("utf-8", errors="replace")
                text = text.rstrip("\x00").strip()
                return text, pos + length

            name,   offset = read_string(raw, offset)
            symbol, offset = read_string(raw, offset)

            if not name and not symbol:
                return None

            self.logger.debug(
                f"Metaplex metadata for {mint_address[:12]}…: {symbol} / {name}"
            )
            return {"name": name or "Unknown", "symbol": symbol or "???"}

        except Exception as e:
            self.logger.debug(
                f"get_token_metadata (Metaplex) failed for {mint_address[:12]}: {e}"
            )
            return None

    def get_pumpfun_bonding_curve(self, mint_address: str) -> Optional[Dict]:
        """
        Read a Pump.fun bonding curve account and return token price/MC in SOL.

        Pump.fun tokens live on an automated bonding curve until they reach
        ~$69k market cap, at which point they migrate to Raydium.  The bonding
        curve account is a PDA derived from the mint and holds the current
        virtual reserves used to calculate price and market cap.

        Pump.fun BondingCurve Anchor account layout (after 8-byte discriminator):
          virtualTokenReserves : u64  (raw token units, 6 decimals)
          virtualSolReserves   : u64  (lamports, 9 decimals)
          realTokenReserves    : u64
          realSolReserves      : u64
          tokenTotalSupply     : u64
          complete             : bool  (True → migrated to Raydium)

        Args:
            mint_address: Token mint address (base58)

        Returns:
            Dict with price_sol, market_cap_sol, real_sol_reserves, complete flag,
            or None if the account doesn't exist or cannot be decoded.
        """
        import requests as _requests
        import base64
        import struct

        PUMP_FUN_PROGRAM = Pubkey.from_string(
            "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
        )
        TOKEN_DECIMALS = 6
        SOL_DECIMALS   = 9

        try:
            mint_pubkey = Pubkey.from_string(mint_address)
            seeds = [b"bonding-curve", bytes(mint_pubkey)]
            curve_pda, _ = Pubkey.find_program_address(seeds, PUMP_FUN_PROGRAM)

            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAccountInfo",
                "params": [
                    str(curve_pda),
                    {"encoding": "base64", "commitment": "confirmed"},
                ],
            }
            resp = _requests.post(self.rpc_url, json=payload, timeout=8)
            resp.raise_for_status()
            account = resp.json().get("result", {}).get("value")
            if not account:
                self.logger.debug(f"No bonding curve account for {mint_address[:12]}…")
                return None

            raw = base64.b64decode(account["data"][0])
            # Need at least: discriminator(8) + 5×u64(40) + bool(1) = 49 bytes
            if len(raw) < 49:
                return None

            offset = 8  # skip Anchor discriminator
            vtr = struct.unpack_from("<Q", raw, offset)[0]; offset += 8  # virtualTokenReserves
            vsr = struct.unpack_from("<Q", raw, offset)[0]; offset += 8  # virtualSolReserves
            _   = struct.unpack_from("<Q", raw, offset)[0]; offset += 8  # realTokenReserves (unused)
            rsr = struct.unpack_from("<Q", raw, offset)[0]; offset += 8  # realSolReserves
            tts = struct.unpack_from("<Q", raw, offset)[0]; offset += 8  # tokenTotalSupply
            complete = raw[offset] != 0

            if vtr == 0:
                return None

            # Price per token in SOL
            price_sol = (vsr / 10 ** SOL_DECIMALS) / (vtr / 10 ** TOKEN_DECIMALS)
            # Market cap in SOL = price × total supply
            market_cap_sol = price_sol * (tts / 10 ** TOKEN_DECIMALS)
            # Real SOL raised = current actual SOL in the curve
            real_sol = rsr / 10 ** SOL_DECIMALS

            self.logger.debug(
                f"Bonding curve {mint_address[:12]}…: "
                f"price={price_sol:.8f} SOL, MC={market_cap_sol:.2f} SOL, "
                f"real_sol={real_sol:.4f}, complete={complete}"
            )
            return {
                "price_sol":       price_sol,
                "market_cap_sol":  market_cap_sol,
                "real_sol":        real_sol,
                "complete":        complete,
            }

        except Exception as e:
            self.logger.debug(f"get_pumpfun_bonding_curve failed for {mint_address[:12]}: {e}")
            return None

    def get_signatures_for_address(self, address, limit: int = 10) -> List[Dict]:
        """
        Get recent transaction signatures for an address.

        Args:
            address: Pubkey object or base58 address string
            limit:   Maximum number of signatures to return

        Returns:
            List of signature dicts with keys: signature, slot, err, block_time
        """
        import traceback

        # Accept both Pubkey objects and plain strings
        try:
            from solders.pubkey import Pubkey as _Pubkey
            if isinstance(address, str):
                address = _Pubkey.from_string(address)
        except Exception as conv_err:
            self.logger.error(f"get_signatures_for_address: invalid address '{address}': {conv_err}")
            return []

        # First attempt: with commitment
        try:
            response = self.client.get_signatures_for_address(
                address,
                limit=limit,
                commitment=self.commitment,
            )
            signatures = []
            if response.value:
                for sig_info in response.value:
                    signatures.append({
                        'signature': str(sig_info.signature),
                        'slot':       sig_info.slot,
                        'err':        sig_info.err,
                        'block_time': sig_info.block_time,
                    })
            return signatures

        except Exception as e:
            # Log the full traceback so we can see the real cause
            tb = traceback.format_exc()
            self.logger.error(
                f"Failed to get signatures for {address} "
                f"(type={type(e).__name__}, msg={e!r}):\n{tb}"
            )

            # 429 Too Many Requests — retrying immediately would just fail again.
            # Bail out early so we don't generate a second error log.
            if '429' in str(e) or '429' in tb:
                self.logger.warning(
                    "Rate-limited by RPC (429). Consider switching to a private/dedicated "
                    "endpoint (e.g. Helius, QuickNode) to avoid this."
                )
                return []

        # Second attempt: without commitment (some RPC nodes reject it for other reasons)
        try:
            response = self.client.get_signatures_for_address(address, limit=limit)
            signatures = []
            if response.value:
                for sig_info in response.value:
                    signatures.append({
                        'signature': str(sig_info.signature),
                        'slot':       sig_info.slot,
                        'err':        sig_info.err,
                        'block_time': sig_info.block_time,
                    })
            return signatures
        except Exception as e2:
            self.logger.error(
                f"get_signatures_for_address fallback also failed for {address}: "
                f"{type(e2).__name__}: {e2!r}"
            )
            return []

    def get_slot(self) -> Optional[int]:
        """
        Get current slot.

        Returns:
            Current slot number or None
        """
        try:
            response = self.client.get_slot(commitment=self.commitment)
            return response.value
        except Exception as e:
            self.logger.error(f"Failed to get slot: {e}")
            return None

    def request_airdrop(self, pubkey: Pubkey, lamports: int = 1_000_000_000) -> Optional[str]:
        """
        Request airdrop (only works on devnet/testnet).

        Args:
            pubkey: Public key to airdrop to
            lamports: Amount in lamports (default: 1 SOL)

        Returns:
            Transaction signature or None

        Raises:
            Exception: If airdrop request fails
        """
        try:
            from solana.rpc.core import SolanaRpcException
        except ImportError:
            # Fallback if import fails
            SolanaRpcException = type('SolanaRpcException', (Exception,), {})

        try:
            self.logger.info(f"Requesting airdrop for {pubkey}: {lamports} lamports")
            response = self.client.request_airdrop(pubkey, lamports)

            self.logger.debug(f"Airdrop response type: {type(response)}")
            self.logger.debug(f"Airdrop response: {response}")

            if response.value:
                signature = str(response.value)
                self.logger.info(f"Airdrop requested: {signature}")
                return signature

            # If no value but no error, it means the request was rejected
            self.logger.error(f"Airdrop response has no value. Full response: {response}")
            raise Exception("Airdrop request was rejected by the network (no signature returned)")
        except SolanaRpcException as e:
            # Handle Solana RPC-specific exceptions
            error_code = getattr(e, 'code', 'unknown')
            error_message = getattr(e, 'message', str(e))
            error_data = getattr(e, 'data', None)

            self.logger.error(f"Solana RPC Exception - Code: {error_code}, Message: {error_message}, Data: {error_data}")

            # Map common error codes to user-friendly messages
            if error_code == -32603:
                raise Exception("Solana devnet airdrop service is currently down (Internal error -32603)")
            elif error_code == 429 or error_code == -32005:
                raise Exception("Rate limited by devnet faucet. Please wait or use https://faucet.solana.com")
            else:
                raise Exception(f"Solana RPC error ({error_code}): {error_message or 'Unknown error'}")
        except Exception as e:
            error_msg = str(e)
            error_type = type(e).__name__

            # Try to extract RPC error details from any exception
            error_code = getattr(e, 'code', None)
            error_message = getattr(e, 'message', getattr(e, 'error_msg', None))
            error_data = getattr(e, 'data', None)
            args = getattr(e, 'args', ())

            self.logger.error(f"Failed to request airdrop ({error_type}): {error_msg}")
            self.logger.error(f"Exception attributes - code: {error_code}, message: {error_message}, data: {error_data}, args: {args}")

            # Check if it's already our custom exception
            if "Solana RPC error" in error_msg or "devnet airdrop service" in error_msg or "Rate limited" in error_msg:
                raise

            # Try to parse error from args if available
            if args and len(args) > 0 and isinstance(args[0], dict):
                rpc_error = args[0]
                if 'error' in rpc_error:
                    error_code = rpc_error['error'].get('code')
                    error_message = rpc_error['error'].get('message')
                    self.logger.error(f"Parsed RPC error - code: {error_code}, message: {error_message}")

            # Handle based on extracted attributes
            if error_code == -32603:
                raise Exception("Solana devnet airdrop service is currently down (Internal error -32603)")
            elif error_code == 429 or error_code == -32005:
                raise Exception("Rate limited by devnet faucet. Please wait or use https://faucet.solana.com")
            elif error_code:
                raise Exception(f"Solana RPC error ({error_code}): {error_message or 'Unknown error'}")

            # Re-raise with more context
            if error_message and error_message != "":
                raise Exception(f"Devnet airdrop failed: {error_message}")
            elif not error_msg or error_msg == "":
                raise Exception(f"Devnet airdrop failed: Solana devnet service unavailable")
            else:
                raise Exception(f"Devnet airdrop failed: {error_msg}")
