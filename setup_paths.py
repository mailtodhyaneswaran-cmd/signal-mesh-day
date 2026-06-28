"""
setup_paths.py — Project-wide sys.path bootstrap and path constants.

Import this as the FIRST project import in any executable script:

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))  # project root
    import setup_paths                                      # this file

After import, all flat names (import config, import ibkr_connector, etc.)
resolve correctly regardless of which sub-folder the calling script lives in.

Also exports canonical path constants so no module hardcodes "data/" etc.
"""
import sys
from pathlib import Path

# Project root = the directory that contains this file
PROJECT_ROOT = Path(__file__).parent.resolve()

# Add every sub-folder to sys.path (flat-import semantics for all modules)
for _d in ("bin", "inc", "lib", "tst", "dat"):
    _p = str(PROJECT_ROOT / _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Canonical path constants — use these instead of hardcoded strings
DATA_DIR       = PROJECT_ROOT / "dat" / "data"
WATCHLIST_DIR  = PROJECT_ROOT / "dat" / "watchlist"
RESULTS_DIR    = PROJECT_ROOT / "dat" / "results"
STATE_FILE     = PROJECT_ROOT / "dat" / "state.json"
RVOL_CACHE_DIR = PROJECT_ROOT / "dat" / "watchlist" / ".rvol_cache"
SP500_CACHE    = PROJECT_ROOT / "dat" / "data" / "sp500_tickers.json"
