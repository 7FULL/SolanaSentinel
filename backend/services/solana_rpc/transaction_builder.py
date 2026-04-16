"""
Solana Transaction Builder
Handles construction, simulation, and execution of Solana transactions.
Supports SOL transfers, SPL token transfers, and token swaps via Jupiter.
"""

import base64
import logging
import requests
from typing import Optional, Dict, List

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction, VersionedTransaction
from solders.message import Message
from solana.rpc.types import TxOpts


# SPL token program ID constant
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1bB8"
)

# Jupiter Aggregator v6 endpoints
JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL  = "https://quote-api.jup.ag/v6/swap"


class TransactionBuilder:
    """
    Builds and executes Solana transactions.
    Supports SOL transfers, SPL token transfers, and swaps via Jupiter.
    """

    def __init__(self, rpc_client):
        """
        Initialize transaction builder.

        Args:
            rpc_client: SolanaRPCClient instance
        """
        self.rpc = rpc_client
        self.logger = logging.getLogger(__name__)

    # ------------------------------------------------------------------
    # SOL Transfer
    # ------------------------------------------------------------------

    def transfer_sol(
        self,
        from_keypair: Keypair,
        to_address: str,
        amount_sol: float,
        simulate_only: bool = False,
    ) -> Dict:
        """
        Transfer SOL from one wallet to another.

        Args:
            from_keypair: Sender's keypair
            to_address: Recipient's public key string
            amount_sol: Amount in SOL
            simulate_only: If True, only simulate the transaction

        Returns:
            Dict with transaction result
        """
        try:
            to_pubkey = Pubkey.from_string(to_address)
            lamports = int(amount_sol * 1_000_000_000)

            # Build transfer instruction
            transfer_ix = transfer(
                TransferParams(
                    from_pubkey=from_keypair.pubkey(),
                    to_pubkey=to_pubkey,
                    lamports=lamports,
                )
            )

            recent_blockhash = self.rpc.client.get_latest_blockhash().value.blockhash

            message = Message.new_with_blockhash(
                [transfer_ix],
                from_keypair.pubkey(),
                recent_blockhash,
            )
            transaction = Transaction([from_keypair], message, recent_blockhash)

            if simulate_only:
                result = self.rpc.simulate_transaction(
                    transaction, [from_keypair], recent_blockhash
                )
                return {
                    "success": True,
                    "simulated": True,
                    "result": result,
                    "amount": amount_sol,
                    "from": str(from_keypair.pubkey()),
                    "to": to_address,
                }

            signature = self.rpc.send_transaction(
                transaction, [from_keypair], recent_blockhash=recent_blockhash
            )
            if signature:
                self.logger.info(f"SOL transfer successful: {signature}")
                return {
                    "success": True,
                    "signature": signature,
                    "amount": amount_sol,
                    "from": str(from_keypair.pubkey()),
                    "to": to_address,
                }
            return {"success": False, "error": "Transaction failed to send"}

        except Exception as e:
            self.logger.error(f"Failed to transfer SOL: {e}")
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # SPL Token Transfer
    # ------------------------------------------------------------------

    def transfer_token(
        self,
        from_keypair: Keypair,
        to_address: str,
        token_mint: str,
        amount: float,
        decimals: int = 9,
        simulate_only: bool = False,
    ) -> Dict:
        """
        Transfer SPL tokens between wallets.

        Derives both the source and destination associated token accounts
        (ATAs).  If the destination ATA does not exist it is created as
        part of the same transaction.

        Args:
            from_keypair: Sender's keypair
            to_address: Recipient's wallet public key string
            token_mint: SPL token mint address
            amount: Human-readable token amount
            decimals: Token decimals (default 9)
            simulate_only: If True, only simulate

        Returns:
            Dict with transaction result
        """
        try:
            from solders.instruction import Instruction, AccountMeta
            import struct

            mint_pubkey = Pubkey.from_string(token_mint)
            to_pubkey = Pubkey.from_string(to_address)

            # Derive ATAs for sender and recipient
            source_ata = self._get_associated_token_address(
                from_keypair.pubkey(), mint_pubkey
            )
            dest_ata = self._get_associated_token_address(to_pubkey, mint_pubkey)

            instructions = []

            # Create destination ATA if it does not exist
            dest_acct = self.rpc.get_account_info(dest_ata)
            if dest_acct is None:
                create_ata_ix = self._build_create_ata_instruction(
                    payer=from_keypair.pubkey(),
                    wallet=to_pubkey,
                    mint=mint_pubkey,
                    ata=dest_ata,
                )
                instructions.append(create_ata_ix)

            # Build transfer_checked instruction (discriminator 12)
            raw_amount = int(amount * (10 ** decimals))
            # Layout: u8 instruction(12), u64 amount, u8 decimals
            data = struct.pack("<BQB", 12, raw_amount, decimals)

            transfer_ix = Instruction(
                program_id=TOKEN_PROGRAM_ID,
                accounts=[
                    AccountMeta(pubkey=source_ata,         is_signer=False, is_writable=True),
                    AccountMeta(pubkey=mint_pubkey,         is_signer=False, is_writable=False),
                    AccountMeta(pubkey=dest_ata,            is_signer=False, is_writable=True),
                    AccountMeta(pubkey=from_keypair.pubkey(), is_signer=True, is_writable=False),
                ],
                data=bytes(data),
            )
            instructions.append(transfer_ix)

            recent_blockhash = self.rpc.client.get_latest_blockhash().value.blockhash
            message = Message.new_with_blockhash(
                instructions, from_keypair.pubkey(), recent_blockhash
            )
            transaction = Transaction([from_keypair], message, recent_blockhash)

            if simulate_only:
                result = self.rpc.simulate_transaction(
                    transaction, [from_keypair], recent_blockhash
                )
                return {
                    "success": True,
                    "simulated": True,
                    "result": result,
                    "token_mint": token_mint,
                    "amount": amount,
                    "from": str(from_keypair.pubkey()),
                    "to": to_address,
                }

            signature = self.rpc.send_transaction(
                transaction, [from_keypair], recent_blockhash=recent_blockhash
            )
            if signature:
                self.logger.info(f"Token transfer successful: {signature}")
                return {
                    "success": True,
                    "signature": signature,
                    "token_mint": token_mint,
                    "amount": amount,
                    "from": str(from_keypair.pubkey()),
                    "to": to_address,
                }
            return {"success": False, "error": "Token transfer failed to send"}

        except Exception as e:
            self.logger.error(f"Failed to transfer token: {e}")
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Token Swap via Jupiter
    # ------------------------------------------------------------------

    def swap_tokens(
        self,
        from_keypair: Keypair,
        input_mint: str,
        output_mint: str,
        amount: float,
        slippage_bps: int = 50,
        simulate_only: bool = False,
    ) -> Dict:
        """
        Swap tokens using Jupiter Aggregator v6.

        Fetches a quote, retrieves the pre-built VersionedTransaction from
        Jupiter, deserialises it, signs with the user keypair, and sends
        via the RPC.

        Args:
            from_keypair: Trader's keypair
            input_mint: Input token mint address (use SOL mint for SOL)
            output_mint: Output token mint address
            amount: Amount of input tokens (human-readable)
            slippage_bps: Slippage tolerance in basis points (50 = 0.5 %)
            simulate_only: If True, return the quote without executing

        Returns:
            Dict with swap result
        """
        try:
            # 1. Get quote from Jupiter
            quote = self._get_jupiter_quote(input_mint, output_mint, amount, slippage_bps)
            if not quote:
                return {"success": False, "error": "Failed to get swap quote from Jupiter"}

            if simulate_only:
                return {
                    "success": True,
                    "simulated": True,
                    "quote": quote,
                    "input_amount": amount,
                    "input_mint": input_mint,
                    "output_mint": output_mint,
                    "estimated_output": int(quote.get("outAmount", 0)),
                    "price_impact_pct": quote.get("priceImpactPct", "unknown"),
                }

            # 2. Fetch pre-built VersionedTransaction from Jupiter
            swap_data = self._get_jupiter_swap_transaction(
                quote, str(from_keypair.pubkey())
            )
            if not swap_data or "swapTransaction" not in swap_data:
                return {"success": False, "error": "Failed to get swap transaction from Jupiter"}

            # 3. Deserialise the base64-encoded VersionedTransaction
            raw_tx_bytes = base64.b64decode(swap_data["swapTransaction"])
            versioned_tx = VersionedTransaction.from_bytes(raw_tx_bytes)

            # 4. Sign: Jupiter returns an unsigned tx that needs the user's signature
            # VersionedTransaction.sign() replaces placeholder signatures
            versioned_tx.sign([from_keypair])

            # 5. Serialise and send
            serialised = bytes(versioned_tx)
            response = self.rpc.client.send_raw_transaction(
                serialised,
                opts=TxOpts(skip_preflight=False, preflight_commitment=self.rpc.commitment),
            )

            if response.value:
                signature = str(response.value)
                self.logger.info(f"Jupiter swap sent: {signature}")
                return {
                    "success": True,
                    "signature": signature,
                    "input_amount": amount,
                    "input_mint": input_mint,
                    "output_mint": output_mint,
                    "estimated_output": int(quote.get("outAmount", 0)),
                    "price_impact_pct": quote.get("priceImpactPct", "unknown"),
                }

            return {"success": False, "error": "Swap transaction returned no signature"}

        except Exception as e:
            self.logger.error(f"Failed to swap tokens: {e}")
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Jupiter helpers
    # ------------------------------------------------------------------

    def _get_jupiter_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: float,
        slippage_bps: int,
    ) -> Optional[Dict]:
        """Fetch a swap quote from Jupiter v6."""
        try:
            amount_lamports = int(amount * 1_000_000_000)
            params = {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": amount_lamports,
                "slippageBps": slippage_bps,
            }
            resp = requests.get(JUPITER_QUOTE_URL, params=params, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            self.logger.error(f"Jupiter quote HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        except Exception as e:
            self.logger.error(f"Jupiter quote request failed: {e}")
            return None

    def _get_jupiter_swap_transaction(
        self, quote: Dict, user_pubkey: str
    ) -> Optional[Dict]:
        """Fetch the pre-built swap transaction from Jupiter v6."""
        try:
            payload = {
                "quoteResponse": quote,
                "userPublicKey": user_pubkey,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto",
            }
            resp = requests.post(JUPITER_SWAP_URL, json=payload, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            self.logger.error(f"Jupiter swap tx HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        except Exception as e:
            self.logger.error(f"Jupiter swap transaction request failed: {e}")
            return None

    # ------------------------------------------------------------------
    # ATA helpers
    # ------------------------------------------------------------------

    def _get_associated_token_address(self, owner: Pubkey, mint: Pubkey) -> Pubkey:
        """
        Derive the associated token account (ATA) address for (owner, mint).
        Uses find_program_address with the standard ATA seeds.
        """
        seeds = [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)]
        ata, _ = Pubkey.find_program_address(seeds, ASSOCIATED_TOKEN_PROGRAM_ID)
        return ata

    def _build_create_ata_instruction(
        self, payer: Pubkey, wallet: Pubkey, mint: Pubkey, ata: Pubkey
    ):
        """
        Build an instruction to create an associated token account.
        Uses the ATA program's create instruction (no data, specific accounts).
        """
        from solders.instruction import Instruction, AccountMeta
        from solders.system_program import ID as SYS_PROGRAM_ID
        from solders.sysvar import RENT

        accounts = [
            AccountMeta(pubkey=payer,                     is_signer=True,  is_writable=True),
            AccountMeta(pubkey=ata,                       is_signer=False, is_writable=True),
            AccountMeta(pubkey=wallet,                    is_signer=False, is_writable=False),
            AccountMeta(pubkey=mint,                      is_signer=False, is_writable=False),
            AccountMeta(pubkey=SYS_PROGRAM_ID,            is_signer=False, is_writable=False),
            AccountMeta(pubkey=TOKEN_PROGRAM_ID,          is_signer=False, is_writable=False),
            AccountMeta(pubkey=RENT,                      is_signer=False, is_writable=False),
        ]
        # ATA program create instruction has no data
        return Instruction(
            program_id=ASSOCIATED_TOKEN_PROGRAM_ID,
            accounts=accounts,
            data=b"",
        )

    # ------------------------------------------------------------------
    # Generic builder
    # ------------------------------------------------------------------

    def build_transaction_from_instructions(
        self, instructions: List, payer: Pubkey, signers: List[Keypair]
    ) -> Optional[Transaction]:
        """
        Build a signed transaction from a list of instructions.

        Args:
            instructions: List of transaction instructions
            payer: Fee payer public key
            signers: List of keypairs to sign the transaction

        Returns:
            Built and signed Transaction or None
        """
        try:
            recent_blockhash = self.rpc.client.get_latest_blockhash().value.blockhash
            message = Message.new_with_blockhash(instructions, payer, recent_blockhash)
            return Transaction(signers, message, recent_blockhash)
        except Exception as e:
            self.logger.error(f"Failed to build transaction: {e}")
            return None
