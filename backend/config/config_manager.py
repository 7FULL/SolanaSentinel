"""
Configuration Manager
Handles loading and managing application configuration from environment variables and JSON files.
"""

import os
import json
from pathlib import Path
from typing import Any, Dict, Optional
from dotenv import load_dotenv

class ConfigManager:
    """
    Centralized configuration management system.
    Loads configuration from environment variables and JSON files.
    """

    def __init__(self, config_file: Optional[str] = None):
        """
        Initialize the configuration manager.

        Args:
            config_file: Optional path to JSON configuration file
        """
        self.base_dir = Path(__file__).parent.parent
        _env_path = self.base_dir.parent / '.env'
        _loaded = load_dotenv(_env_path, override=True)
        print(f"[CONFIG] .env path : {_env_path}")
        print(f"[CONFIG] .env found: {_env_path.exists()}  loaded={_loaded}")
        self.data_dir = self.base_dir / 'data'
        self.config_file = config_file or self.base_dir / 'config' / 'settings.json'
        print(f"[CONFIG] settings  : {self.config_file}  exists={Path(self.config_file).exists()}")
        print(f"[CONFIG] SOLANA_NETWORK env = {os.getenv('SOLANA_NETWORK', '(not set)')}")

        # Load configuration
        self.config = self._load_config()
        print(f"[CONFIG] Final network: {self.config.get('solana', {}).get('network', '?')}")

    def _load_config(self) -> Dict[str, Any]:
        """
        Load configuration from file or create default configuration.

        Returns:
            Configuration dictionary
        """
        # Default configuration
        default_config = {
            'app': {
                'name': 'SolanaSentinel',
                'version': '1.0.0',
                'environment': os.getenv('ENVIRONMENT', 'development')
            },
            'flask': {
                'host': os.getenv('FLASK_HOST', '127.0.0.1'),
                'port': int(os.getenv('FLASK_PORT', 5000)),
                'debug': os.getenv('FLASK_DEBUG', 'True').lower() == 'true'
            },
            'solana': {
                'rpc_url': os.getenv('SOLANA_RPC_URL', 'https://api.devnet.solana.com'),
                'ws_url': os.getenv('SOLANA_WS_URL', 'wss://api.devnet.solana.com'),
                'network': os.getenv('SOLANA_NETWORK', 'devnet'),
                'commitment': os.getenv('SOLANA_COMMITMENT', 'confirmed')
            },
            'security': {
                'encryption_enabled': True,
                'max_transaction_amount': float(os.getenv('MAX_TRANSACTION_AMOUNT', 10.0)),
                'require_confirmation': os.getenv('REQUIRE_CONFIRMATION', 'True').lower() == 'true'
            },
            'copy_trading': {
                'enabled': False,
                'default_mode': 'notification',
                'max_slippage': 0.05,
                'gas_buffer': 1.2
            },
            'sniper': {
                'enabled': False,
                'mode': 'simulation',
                'min_liquidity': 1000,
                'max_market_cap': 100000,
                'auto_buy_amount': 0.1
            },
            'anti_scam': {
                'enabled': True,
                'strict_mode': False,
                'min_risk_score': 50,
                'ai_analysis_enabled': False
            },
            'logging': {
                'level': os.getenv('LOG_LEVEL', 'INFO'),
                'max_file_size': 10485760,  # 10MB
                'backup_count': 5,
                'console_output': True
            }
        }

        # Try to load from file
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    file_config = json.load(f)
                    # Merge with defaults (file config overrides defaults)
                    self._deep_merge(default_config, file_config)
            except Exception as e:
                print(f"Warning: Could not load config file: {e}")

        # Save default config if file doesn't exist
        if not os.path.exists(self.config_file):
            self.save_config(default_config)

        return default_config

    def _deep_merge(self, base: Dict, override: Dict) -> None:
        """
        Deep merge override dictionary into base dictionary.

        Args:
            base: Base dictionary to merge into
            override: Dictionary with values to override
        """
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._deep_merge(base[key], value)
            else:
                base[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get a configuration value using dot notation.

        Args:
            key: Configuration key (e.g., 'solana.rpc_url')
            default: Default value if key not found

        Returns:
            Configuration value or default
        """
        keys = key.split('.')
        value = self.config

        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default

        return value

    def set(self, key: str, value: Any) -> None:
        """
        Set a configuration value using dot notation.

        Args:
            key: Configuration key (e.g., 'solana.rpc_url')
            value: Value to set
        """
        keys = key.split('.')
        config = self.config

        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]

        config[keys[-1]] = value

    def save_config(self, config: Optional[Dict] = None) -> bool:
        """
        Save configuration to file.

        Args:
            config: Optional configuration dictionary to save (uses self.config if None)

        Returns:
            True if successful, False otherwise
        """
        try:
            config_to_save = config or self.config

            # Ensure config directory exists
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)

            with open(self.config_file, 'w') as f:
                json.dump(config_to_save, f, indent=4)

            return True
        except Exception as e:
            print(f"Error saving config: {e}")
            return False

    def get_data_path(self, subdirectory: str) -> Path:
        """
        Get path to a data subdirectory.

        Args:
            subdirectory: Name of subdirectory (e.g., 'wallets', 'logs')

        Returns:
            Path object to the subdirectory
        """
        path = self.data_dir / subdirectory
        os.makedirs(path, exist_ok=True)
        return path

    def reload(self) -> None:
        """Reload configuration from file."""
        self.config = self._load_config()

    def get_all(self) -> Dict[str, Any]:
        """
        Get all configuration.

        Returns:
            Complete configuration dictionary
        """
        return self.config.copy()
