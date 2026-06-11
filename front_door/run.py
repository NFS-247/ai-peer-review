"""Entrypoint: `python front_door/run.py` (or `python -m front_door.run`)."""

from __future__ import annotations

import os
import sys

# Allow running as a plain script (`python front_door/run.py`): put the repo root
# (the directory containing the front_door package) on the path so the absolute
# imports below resolve. Running via `-m front_door.run` already has it.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from front_door.app import config as config_mod
from front_door.app.server import serve


def main() -> int:
    cfg = config_mod.load()
    if not cfg.read_token:
        print("Set GITHUB_READ_TOKEN (a token that can read the board repos).", file=sys.stderr)
        return 1
    if not cfg.repos:
        print("Set FRONT_DOOR_REPOS=owner/repo,owner/repo2 (the projects to show).", file=sys.stderr)
        return 1
    serve(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
