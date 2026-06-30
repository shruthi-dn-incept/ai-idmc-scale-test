"""Make repo-root modules (governance_engine_mcp, etc.) importable from tests."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
