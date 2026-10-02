from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "bundled" / "tools"
LIBS = ROOT / "bundled" / "libs"

if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
# Vendored deps (lsprotocol, pygls) — normally added as a side effect of
# importing server_context, but test modules must not depend on import order.
if str(LIBS) not in sys.path:
    sys.path.insert(0, str(LIBS))
