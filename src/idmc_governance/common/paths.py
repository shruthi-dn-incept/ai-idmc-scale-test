"""Repo-root + shared path resolution.

Modules read/write a repo-root `.env` and a `.scan_cache/` dir. After the
src-layout move, a module's own directory is no longer the repo root, so we
resolve the root robustly (env override -> walk up for a marker -> fixed depth).
"""
import os
from pathlib import Path


def _find_repo_root() -> Path:
    override = os.getenv("IDMC_REPO_ROOT")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").exists() or (parent / ".env").exists():
            return parent
    # Fallback: src/idmc_governance/common/paths.py -> repo root is 3 levels up
    return here.parents[3]


REPO_ROOT = _find_repo_root()
ENV_PATH = REPO_ROOT / ".env"
SCAN_CACHE_DIR = Path(os.getenv("SCAN_CACHE_DIR", str(REPO_ROOT / ".scan_cache")))

# Runtime-generated pipeline state (gitignored): taxonomy, maps, stats, job ids.
STATE_DIR = Path(os.getenv("IDMC_STATE_DIR", str(REPO_ROOT / "state")))
STATE_DIR.mkdir(parents=True, exist_ok=True)
