"""Entrypoint: `python front_door/run.py` (or `python -m front_door.run`)."""

from __future__ import annotations

import sys

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
