"""Pytest bootstrap.

Puts the src-layout package on the import path, and sets dummy IDMC env vars so
modules that read env at *import time* (e.g. governance_engine) load without real
credentials. The unit tests mock all API calls, so dummy values are sufficient.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

os.environ.setdefault("IDMC_FRS_HOST", "test.dmp-us.informaticacloud.com")
os.environ.setdefault("IDMC_DQ_HOST", "test-dq.dmp-us.informaticacloud.com")
os.environ.setdefault("IDMC_IDENTITY_HOST", "test.dmp-us.informaticacloud.com")
os.environ.setdefault("CDGC_API_BASE", "https://test-cdgc.dmp-us.informaticacloud.com")
os.environ.setdefault("IDMC_ORG_ID", "testOrg")
