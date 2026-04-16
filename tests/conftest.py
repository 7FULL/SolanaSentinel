"""
Pytest configuration for SolanaSentinel tests.
Adds the backend directory to sys.path so all imports work correctly.
"""

import os
import sys

# Make backend importable from all test files
BACKEND_DIR = os.path.join(os.path.dirname(__file__), '..', 'backend')
sys.path.insert(0, os.path.abspath(BACKEND_DIR))
