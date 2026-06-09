"""TradeWatcher AI Peer Review Dispatcher.

Implementation of the design specified in
docs/tradewatcher_dispatcher_design.md (PR #73, merged as commit 1f71a21).

This package contains autonomous infrastructure that runs AI peer review on
pull requests. It does not:
- Place broker orders.
- Enable live trading.
- Change safety flags.
- Merge any pull request, ever.
- Force-push, rewrite history, or bypass branch protection.

See the design doc for the full specification.
"""

__version__ = "1.0.0"
__marker__ = "TRADEWATCHER_AI_PEER_REVIEW_DISPATCHER_V1"
