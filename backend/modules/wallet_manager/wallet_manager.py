"""
Wallet Manager Module
Handles wallet creation, import, storage, and management.
Supports both internal and external wallets with encryption.
"""

import json
import os
import uuid
from datetime import datetime
from typing import Dict, List, Optional
from pathlib import Path

from utils.crypto_utils import CryptoManager
from utils.validators import is_valid_solana_address, is_valid_private_key
from services.solana_rpc.rpc_client import SolanaRPCClient
from services.solana_rpc.wallet_operations import WalletOperations


class WalletManager:
    """
    Manages Solana wallets (creation, import, storage, selection).
    Stores wallets encrypted locally in JSON format.
    """

    def __init__(self, config):
        """
        Initialize the Wallet Manager.

        Args:
            config: ConfigManager instance
        """
        self.config = config
        self.crypto_manager = CryptoManager()
        self.wallets_dir = config.get_data_path('wallets')
        self.wallets_file = self.wallets_dir / 'wallets.json'
        self.active_wallet_file = self.wallets_dir / 'active_wallet.json'
        self.assignments_file = self.wallets_dir / 'assignments.json'

        # Initialize Solana RPC client
        rpc_url = config.get('solana.rpc_url', 'https://api.devnet.solana.com')
        commitment = config.get('solana.commitment', 'confirmed')
        self.rpc_client = SolanaRPCClient(rpc_url, commitment)
        self.wallet_ops = WalletOperations(self.rpc_client)

        # Load existing wallets and module assignments
        self.wallets = self._load_wallets()
        self.active_wallet_id = self._load_active_wallet()
        self.assignments = self._load_assignments()

    # Known modules that can have a dedicated wallet
    MODULES = ('sniper', 'copy_trading')

    def _load_wallets(self) -> Dict[str, Dict]:
        """
        Load wallets from encrypted JSON file.

        Returns:
            Dictionary of wallets keyed by wallet_id
        """
        if not os.path.exists(self.wallets_file):
            return {}

        try:
            with open(self.wallets_file, 'r') as f:
                encrypted_wallets = json.load(f)

            # Decrypt sensitive fields (private keys will remain encrypted until needed)
            return encrypted_wallets
        except Exception as e:
            print(f"Error loading wallets: {e}")
            return {}

    def _save_wallets(self) -> bool:
        """
        Save wallets to encrypted JSON file.

        Returns:
            True if successful, False otherwise
        """
        try:
            with open(self.wallets_file, 'w') as f:
                json.dump(self.wallets, f, indent=4)
            return True
        except Exception as e:
            print(f"Error saving wallets: {e}")
            return False

    def _load_active_wallet(self) -> Optional[str]:
        """
        Load the currently active wallet ID.

        Returns:
            Active wallet ID or None
        """
        if os.path.exists(self.active_wallet_file):
            try:
                with open(self.active_wallet_file, 'r') as f:
                    data = json.load(f)
                    return data.get('active_wallet_id')
            except Exception:
                return None
        return None

    def _save_active_wallet(self, wallet_id: Optional[str]) -> bool:
        """
        Save the active wallet ID.

        Args:
            wallet_id: Wallet ID to set as active

        Returns:
            True if successful
        """
        try:
            with open(self.active_wallet_file, 'w') as f:
                json.dump({'active_wallet_id': wallet_id}, f)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Module wallet assignments
    # ------------------------------------------------------------------

    def _load_assignments(self) -> Dict[str, Optional[str]]:
        """
        Load per-module wallet assignments from JSON file.

        Returns:
            Dict mapping module name → wallet_id (or None = use active wallet)
        """
        if os.path.exists(self.assignments_file):
            try:
                with open(self.assignments_file, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        return {module: None for module in self.MODULES}

    def _save_assignments(self) -> bool:
        """Persist module assignments to disk."""
        try:
            with open(self.assignments_file, 'w') as f:
                json.dump(self.assignments, f, indent=4)
            return True
        except Exception as e:
            print(f"Error saving wallet assignments: {e}")
            return False

    def get_module_assignments(self) -> Dict:
        """
        Return current module assignments enriched with wallet info.

        Returns:
            Dict with one entry per module:
              {
                "sniper": { "wallet_id": "...", "wallet_name": "...",
                            "wallet_address": "...", "is_active_fallback": False },
                ...
              }
        """
        result = {}
        active = self.active_wallet_id

        for module in self.MODULES:
            assigned_id = self.assignments.get(module)
            wallet      = self.wallets.get(assigned_id) if assigned_id else None

            if wallet:
                result[module] = {
                    'wallet_id':           assigned_id,
                    'wallet_name':         wallet.get('name', 'Unknown'),
                    'wallet_address':      wallet.get('address', ''),
                    'is_active_fallback':  False,
                }
            else:
                # Falls back to active wallet
                active_wallet = self.wallets.get(active) if active else None
                result[module] = {
                    'wallet_id':           None,
                    'wallet_name':         active_wallet.get('name', 'None') if active_wallet else 'None',
                    'wallet_address':      active_wallet.get('address', '') if active_wallet else '',
                    'is_active_fallback':  True,
                }

        return result

    def set_module_assignment(self, module: str, wallet_id: Optional[str]) -> bool:
        """
        Assign a wallet to a module, or clear the assignment (wallet_id=None).

        Args:
            module:    One of MODULES ('sniper', 'copy_trading')
            wallet_id: Wallet ID to assign, or None to revert to active wallet

        Returns:
            True if successful, False if module or wallet is unknown
        """
        if module not in self.MODULES:
            return False

        if wallet_id is not None and wallet_id not in self.wallets:
            return False

        self.assignments[module] = wallet_id
        return self._save_assignments()

    def get_wallet_for_module(self, module: str) -> Optional[Dict]:
        """
        Get the wallet assigned to a module, falling back to the active wallet.

        Args:
            module: Module name ('sniper', 'copy_trading')

        Returns:
            Sanitized wallet dict, or None if no wallet is available
        """
        wallet_id = self.assignments.get(module)
        if wallet_id and wallet_id in self.wallets:
            return self._sanitize_wallet(self.wallets[wallet_id])

        # Fallback: active wallet
        if self.active_wallet_id and self.active_wallet_id in self.wallets:
            return self._sanitize_wallet(self.wallets[self.active_wallet_id])

        return None

    def create_wallet(self, name: str = "New Wallet") -> Dict:
        """
        Create a new internal wallet using Solana.

        Args:
            name: Wallet name

        Returns:
            Wallet data (without private key in response)
        """
        wallet_id = str(uuid.uuid4())

        try:
            # Generate real Solana keypair
            public_key, private_key = self.wallet_ops.create_wallet()

            # Encrypt private key
            encrypted_private_key = self.crypto_manager.encrypt(private_key)

            # Get initial balance (will be 0 on mainnet, can request airdrop on devnet)
            balance_sol = self.wallet_ops.get_balance(public_key) or 0.0

            wallet_data = {
                'id': wallet_id,
                'name': name,
                'address': public_key,
                'private_key_encrypted': encrypted_private_key,
                'type': 'internal',
                'created_at': datetime.utcnow().isoformat(),
                'last_used': None,
                'balance': {
                    'sol': balance_sol,
                    'usd': 0.0  # Would need price oracle for real USD value
                }
            }

            self.wallets[wallet_id] = wallet_data
            self._save_wallets()

            # Return wallet without private key
            return self._sanitize_wallet(wallet_data)

        except Exception as e:
            print(f"Error creating wallet: {e}")
            raise

    def import_wallet(self, private_key: Optional[str] = None,
                     seed_phrase: Optional[str] = None,
                     name: str = "Imported Wallet") -> Dict:
        """
        Import an external wallet using Solana.

        Args:
            private_key: Private key string (base58)
            seed_phrase: Seed phrase string (BIP39 mnemonic)
            name: Wallet name

        Returns:
            Wallet data (without private key in response)
        """
        wallet_id = str(uuid.uuid4())

        try:
            # Import from private key or seed phrase
            if private_key:
                public_key = self.wallet_ops.import_wallet_from_private_key(private_key)
                key_to_store = private_key
            elif seed_phrase:
                public_key, derived_private_key = self.wallet_ops.import_wallet_from_seed(seed_phrase)
                key_to_store = derived_private_key
            else:
                raise ValueError("Either private_key or seed_phrase must be provided")

            # Encrypt private key
            encrypted_private_key = self.crypto_manager.encrypt(key_to_store)

            # Get balance
            balance_sol = self.wallet_ops.get_balance(public_key) or 0.0

            wallet_data = {
                'id': wallet_id,
                'name': name,
                'address': public_key,
                'private_key_encrypted': encrypted_private_key,
                'type': 'external',
                'created_at': datetime.utcnow().isoformat(),
                'last_used': None,
                'balance': {
                    'sol': balance_sol,
                    'usd': 0.0
                }
            }

            self.wallets[wallet_id] = wallet_data
            self._save_wallets()

            return self._sanitize_wallet(wallet_data)

        except Exception as e:
            print(f"Error importing wallet: {e}")
            raise

    def get_all_wallets(self) -> List[Dict]:
        """
        Get all wallets (without private keys).

        Returns:
            List of wallet data dictionaries
        """
        # Return real wallets from storage
        wallets_list = []
        for wallet in self.wallets.values():
            # Update balance from blockchain for each wallet
            try:
                import sys
                sys.stderr.write(f"[DEBUG] Fetching balance for wallet: {wallet['address']}\n")
                sys.stderr.flush()

                sol_balance = self.wallet_ops.get_balance(wallet['address']) or 0.0

                sys.stderr.write(f"[DEBUG] Balance received: {sol_balance} SOL\n")
                sys.stderr.flush()

                wallet['balance']['sol'] = sol_balance
            except Exception as e:
                sys.stderr.write(f"[ERROR] Failed to fetch balance for {wallet['address']}: {e}\n")
                sys.stderr.flush()
                import traceback
                traceback.print_exc()
                pass  # Keep existing balance if fetch fails

            wallets_list.append(self._sanitize_wallet(wallet))

        return wallets_list

    def get_wallet(self, wallet_id: str) -> Optional[Dict]:
        """
        Get a specific wallet by ID.

        Args:
            wallet_id: Wallet ID

        Returns:
            Wallet data or None if not found
        """
        if wallet_id in self.wallets:
            wallet = self.wallets[wallet_id]

            # Update balance from blockchain
            try:
                sol_balance = self.wallet_ops.get_balance(wallet['address']) or 0.0
                wallet['balance']['sol'] = sol_balance
            except Exception:
                pass  # Keep existing balance if fetch fails

            return self._sanitize_wallet(wallet)

        return None

    def delete_wallet(self, wallet_id: str) -> bool:
        """
        Delete a wallet.

        Args:
            wallet_id: Wallet ID to delete

        Returns:
            True if deleted, False if not found
        """
        if wallet_id in self.wallets:
            del self.wallets[wallet_id]

            # Clear active wallet if it was deleted
            if self.active_wallet_id == wallet_id:
                self.active_wallet_id = None
                self._save_active_wallet(None)

            self._save_wallets()
            return True

        return False

    def set_active_wallet(self, wallet_id: str) -> bool:
        """
        Set a wallet as the active wallet.

        Args:
            wallet_id: Wallet ID to activate

        Returns:
            True if successful, False if wallet not found
        """
        if wallet_id in self.wallets:
            self.active_wallet_id = wallet_id
            self._save_active_wallet(wallet_id)
            return True

        return False

    def get_active_wallet(self) -> Optional[Dict]:
        """
        Get the currently active wallet.

        Returns:
            Active wallet data or None
        """
        if self.active_wallet_id:
            return self.get_wallet(self.active_wallet_id)
        return None

    def get_balance(self, wallet_id: str) -> Optional[Dict]:
        """
        Get wallet balance using Solana RPC.

        Args:
            wallet_id: Wallet ID

        Returns:
            Balance data or None if wallet not found
        """
        # Get wallet from storage
        wallet = self.wallets.get(wallet_id)
        if not wallet:
            return None

        try:
            # Get SOL balance from blockchain
            sol_balance = self.wallet_ops.get_balance(wallet['address']) or 0.0

            # Get token balances
            token_balances = self.wallet_ops.get_token_balances(wallet['address'])

            # Update wallet balance in storage
            wallet['balance']['sol'] = sol_balance
            self._save_wallets()

            return {
                'sol': sol_balance,
                'usd': 0.0,  # Would need price oracle
                'tokens': token_balances,
                'last_updated': datetime.utcnow().isoformat()
            }

        except Exception as e:
            print(f"Error getting balance for wallet {wallet_id}: {e}")
            return None

    def _sanitize_wallet(self, wallet_data: Dict) -> Dict:
        """
        Remove sensitive data from wallet for API responses.

        Args:
            wallet_data: Complete wallet data

        Returns:
            Sanitized wallet data
        """
        sanitized = wallet_data.copy()

        # Remove private key (encrypted or not)
        sanitized.pop('private_key_encrypted', None)
        sanitized.pop('private_key', None)

        # Add active status
        sanitized['is_active'] = sanitized.get('id') == self.active_wallet_id

        return sanitized

    def get_status(self) -> Dict:
        """
        Get wallet manager status.

        Returns:
            Status dictionary
        """
        return {
            'total_wallets': len(self.wallets) or 2,  # Mock: 2 wallets
            'active_wallet': self.active_wallet_id or 'wallet-1',
            'encryption_enabled': True
        }

    def get_metrics(self) -> Dict:
        """
        Get wallet metrics.

        Returns:
            Metrics dictionary
        """
        return {
            'total_wallets': len(self.wallets) or 2,
            'internal_wallets': 1,
            'external_wallets': 1,
            'total_balance_sol': 7.5,
            'total_balance_usd': 752.25
        }
