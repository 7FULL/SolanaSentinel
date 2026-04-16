"""
Logging Engine Module
Centralized logging system for all application events, errors, and operations.
Supports multiple log levels, persistent daily storage, and automatic archiving.

Rotation policy:
  - One log file per calendar day: logs/app_YYYYMMDD.json
  - At midnight (detected on the next log() call) the previous day's file is moved
    atomically to logs/archive/YYYY/MM/DD/app.json
  - Archives older than `archive_retention_days` (default 30) are auto-deleted
  - The frontend only reads today's file — older data is accessed manually in the
    archive folders
"""

import json
import os
import shutil
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional
from pathlib import Path


class LoggingEngine:
    """
    Manages application-wide logging with multiple levels and persistent storage.
    Provides query capabilities for log analysis.
    """

    # Log levels
    LEVEL_DEBUG    = 'DEBUG'
    LEVEL_INFO     = 'INFO'
    LEVEL_WARNING  = 'WARNING'
    LEVEL_ERROR    = 'ERROR'
    LEVEL_CRITICAL = 'CRITICAL'

    def __init__(self, config):
        """
        Initialize the Logging Engine.

        Args:
            config: ConfigManager instance
        """
        self.config        = config
        self.logs_dir      = config.get_data_path('logs')
        self.console_output = config.get('logging.console_output', True)
        self.archive_retention_days = config.get('logging.archive_retention_days', 30)

        # Track current day so we can detect midnight rollover on any log() call
        self._current_day  = self._today_str()

        # In-memory log buffer (for quick access, reset on day rollover)
        self.log_buffer = []
        self.buffer_size = 1000

        # Real-time push callbacks — called with the log_entry dict after every log() call.
        # Used by app.py to forward entries to connected WebSocket clients.
        self.on_log_callbacks: List[Callable] = []

        # On startup: archive any orphan files from previous days and prune old archives
        self._archive_orphan_files()
        self._cleanup_old_archives()

    # ------------------------------------------------------------------
    # Date helpers
    # ------------------------------------------------------------------

    def _today_str(self) -> str:
        """Return today's date as YYYYMMDD string (local time)."""
        return datetime.now().strftime('%Y%m%d')

    def _todays_log_file(self) -> Path:
        """Return the Path for today's active log file."""
        return self.logs_dir / f"app_{self._today_str()}.json"

    # ------------------------------------------------------------------
    # Day-rollover detection
    # ------------------------------------------------------------------

    def _check_day_rollover(self) -> None:
        """
        Detect if the calendar day has changed since the last log entry.
        If so, archive yesterday's file, reset the buffer, and update
        the tracked day. Called at the start of every log() invocation.
        """
        today = self._today_str()
        if today == self._current_day:
            return

        # Archive the completed day's file
        old_file = self.logs_dir / f"app_{self._current_day}.json"
        if old_file.exists():
            self._archive_old_file(old_file)

        # Advance to the new day
        self._current_day = today
        self.log_buffer   = []

        # Prune archives that have aged out
        self._cleanup_old_archives()

    # ------------------------------------------------------------------
    # Archive helpers
    # ------------------------------------------------------------------

    def _archive_old_file(self, source: Path) -> None:
        """
        Move a completed day's log file into the archive tree.

        Destination: logs/archive/YYYY/MM/DD/app.json

        The move uses shutil.move which reduces to os.rename on the same
        filesystem — an atomic OS operation. If the destination already
        exists (crash-recovery path), the source is removed and the
        existing archive is kept as the authoritative copy.

        Args:
            source: Path to the app_YYYYMMDD.json file to archive
        """
        try:
            # Parse date from filename: app_YYYYMMDD.json
            stem = source.stem          # e.g. "app_20260405"
            date_str = stem.split('_', 1)[1]   # "20260405"
            year  = date_str[0:4]
            month = date_str[4:6]
            day   = date_str[6:8]

            archive_dir = self.logs_dir / 'archive' / year / month / day
            os.makedirs(archive_dir, exist_ok=True)
            dest = archive_dir / 'app.json'

            if dest.exists():
                # Archive already exists (e.g. process crashed mid-rename last time).
                # The existing file is authoritative — just remove the orphan source.
                os.remove(source)
                return

            shutil.move(str(source), str(dest))

        except Exception as e:
            print(f"[LoggingEngine] Failed to archive {source}: {e}")

    def _archive_orphan_files(self) -> None:
        """
        On startup, move any app_YYYYMMDD.json files in the root logs directory
        (other than today's) into the archive tree.

        This handles the case where the backend was offline across midnight and
        also cleans up any pre-existing stale files.
        """
        today_file = self._todays_log_file()
        for f in self.logs_dir.glob('app_????????.json'):
            if f.resolve() != today_file.resolve():
                self._archive_old_file(f)

    def _cleanup_old_archives(self) -> None:
        """
        Delete archive directories (and their app.json) older than
        archive_retention_days. Called at startup and on day rollover.
        """
        archive_root = self.logs_dir / 'archive'
        if not archive_root.exists():
            return

        cutoff = datetime.now() - timedelta(days=self.archive_retention_days)

        for day_dir in archive_root.glob('*/*/*'):   # matches YYYY/MM/DD leaf dirs
            try:
                parts = day_dir.parts
                year, month, day = int(parts[-3]), int(parts[-2]), int(parts[-1])
                dir_date = datetime(year, month, day)

                if dir_date < cutoff:
                    app_file = day_dir / 'app.json'
                    if app_file.exists():
                        os.remove(app_file)
                    try:
                        os.rmdir(day_dir)     # only succeeds if empty (it will be)
                    except OSError:
                        pass
            except Exception:
                pass   # malformed directory name or other error — skip silently

    # ------------------------------------------------------------------
    # Core logging
    # ------------------------------------------------------------------

    def log(self, level: str, message: str, module: str = 'system', data: Optional[Dict] = None) -> None:
        """
        Log a message.

        Args:
            level:   Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            message: Log message
            module:  Module/component name
            data:    Optional additional data dict
        """
        # Detect midnight rollover before writing
        self._check_day_rollover()

        log_entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'level':     level.upper(),
            'module':    module,
            'message':   message,
            'data':      data or {}
        }

        # Add to in-memory buffer
        self.log_buffer.append(log_entry)
        if len(self.log_buffer) > self.buffer_size:
            self.log_buffer.pop(0)

        # Persist to file
        self._write_log(log_entry)

        # Console output
        if self.console_output:
            self._print_log(log_entry)

        # Push to real-time subscribers (e.g. SocketIO)
        for cb in self.on_log_callbacks:
            try:
                cb(log_entry)
            except Exception:
                pass

    def debug(self, message: str, module: str = 'system', data: Optional[Dict] = None) -> None:
        """Log a debug message."""
        self.log(self.LEVEL_DEBUG, message, module, data)

    def info(self, message: str, module: str = 'system', data: Optional[Dict] = None) -> None:
        """Log an info message."""
        self.log(self.LEVEL_INFO, message, module, data)

    def warning(self, message: str, module: str = 'system', data: Optional[Dict] = None) -> None:
        """Log a warning message."""
        self.log(self.LEVEL_WARNING, message, module, data)

    def error(self, message: str, module: str = 'system', data: Optional[Dict] = None) -> None:
        """Log an error message."""
        self.log(self.LEVEL_ERROR, message, module, data)

    def critical(self, message: str, module: str = 'system', data: Optional[Dict] = None) -> None:
        """Log a critical message."""
        self.log(self.LEVEL_CRITICAL, message, module, data)

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def _write_log(self, log_entry: Dict) -> None:
        """
        Append a log entry to today's log file (JSONL format).

        Args:
            log_entry: Log entry dictionary
        """
        try:
            with open(self._todays_log_file(), 'a', encoding='utf-8') as f:
                f.write(json.dumps(log_entry) + '\n')
        except Exception as e:
            print(f"[LoggingEngine] Error writing log: {e}")

    def _print_log(self, log_entry: Dict) -> None:
        """
        Print a log entry to console with ANSI color codes.

        Args:
            log_entry: Log entry dictionary
        """
        colors = {
            self.LEVEL_DEBUG:    '\033[36m',   # Cyan
            self.LEVEL_INFO:     '\033[32m',   # Green
            self.LEVEL_WARNING:  '\033[33m',   # Yellow
            self.LEVEL_ERROR:    '\033[31m',   # Red
            self.LEVEL_CRITICAL: '\033[35m'    # Magenta
        }
        reset = '\033[0m'
        level = log_entry['level']
        color = colors.get(level, '')
        print(f"{color}[{log_entry['timestamp']}] [{level}] [{log_entry['module']}] {log_entry['message']}{reset}")

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def _load_all_entries(self) -> List[Dict]:
        """
        Load all log entries for today from the log file and in-memory buffer.
        Only today's file is loaded — archives are not surfaced in the UI.

        Returns:
            List of all today's log entries (unordered)
        """
        entries = []
        seen    = set()

        # Primary source: today's log file (survives restarts)
        today_file = self._todays_log_file()
        if today_file.exists():
            try:
                with open(today_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            key   = (entry.get('timestamp', ''), entry.get('message', ''))
                            seen.add(key)
                            entries.append(entry)
                        except json.JSONDecodeError:
                            pass
            except Exception as e:
                print(f"[LoggingEngine] Error reading log file: {e}")

        # Secondary source: in-memory buffer (entries not yet flushed, e.g. after crash)
        for entry in self.log_buffer:
            key = (entry.get('timestamp', ''), entry.get('message', ''))
            if key not in seen:
                entries.append(entry)

        return entries

    def get_logs(self, level: str = 'all', limit: int = 100, module: Optional[str] = None) -> List[Dict]:
        """
        Get today's logs with optional filtering, newest first.

        Args:
            level:  Filter by log level ('all' for all levels)
            limit:  Maximum number of entries to return
            module: Filter by module name

        Returns:
            List of log entries, newest first
        """
        entries = self._load_all_entries()

        # Sort newest first
        entries.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

        # Apply level filter
        if level and level.lower() != 'all':
            entries = [e for e in entries if e.get('level', '').upper() == level.upper()]

        # Apply module filter
        if module:
            entries = [e for e in entries if e.get('module') == module]

        return entries[:limit]

    def clear_logs(self) -> bool:
        """
        Clear today's logs (buffer + today's file). Archives are NOT affected.

        Returns:
            True if successful
        """
        try:
            self.log_buffer = []
            today_file = self._todays_log_file()
            if today_file.exists():
                os.remove(today_file)
            return True
        except Exception as e:
            print(f"[LoggingEngine] Error clearing logs: {e}")
            return False

    def get_log_statistics(self) -> Dict:
        """
        Get statistics computed from today's log entries.

        Returns:
            Dictionary with counts by level/module and last error info
        """
        entries = self._load_all_entries()

        by_level:  Dict[str, int] = {}
        by_module: Dict[str, int] = {}
        last_error = None

        for entry in sorted(entries, key=lambda x: x.get('timestamp', ''), reverse=True):
            level = entry.get('level', 'INFO')
            mod   = entry.get('module', 'system')
            by_level[level]  = by_level.get(level, 0) + 1
            by_module[mod]   = by_module.get(mod, 0) + 1

            if last_error is None and level in ('ERROR', 'CRITICAL'):
                last_error = {
                    'timestamp': entry.get('timestamp'),
                    'module':    mod,
                    'message':   entry.get('message', ''),
                }

        return {
            'total_logs': len(entries),
            'by_level':   by_level,
            'by_module':  by_module,
            'last_error': last_error,
        }
