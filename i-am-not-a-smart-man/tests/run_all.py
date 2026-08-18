"""Run the standalone skill's dependency-free regression suite."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


TEST_ROOT = Path(__file__).resolve().parent


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(TEST_ROOT), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
