# AI Peer Review Dispatcher

Autonomous infrastructure that runs AI peer review on every pull request to this repository. Implementation of the design specified in `docs/tradewatcher_dispatcher_design.md`.

> **Status:** Live as of 2026-06-09. First confirmed autonomous review on PR #80 (Claude + GPT signed verdicts, no leaks). This high-stakes-tier change additionally exercises the third reviewer (Gemini) and the auto-convergence path.

## What it does

When you open a PR, the dispatcher:

1. **Classifies** the PR into one of three tiers (routine / backend / high-stakes) based on file paths and diff content.
2. **Calls AI reviewers** (Claude, GPT, Gemini per tier) with the PR's title, body, and diff.
3. **Parses each reviewer's verdict** (approve / request_changes / abstain) and posts it as a PR comment with a structured verdict block.
4. **Checks convergence** — every required reviewer must approve the current head SHA before the PR is marked "ready for operator merge."
5. **Escalates by email** when reviewers disagree, when high-stakes files are touched, when the round budget is exceeded, or when costs spike.

The dispatcher **never merges**. The operator clicks merge manually on GitHub.

## What it does not do

By design (Section 10 of the design doc):

- Cannot merge any pull request, ever — including on operator command.
- Cannot place broker orders.
- Cannot enable live trading.
- Cannot change safety flags.
- Cannot force-push or rewrite history.
- Cannot run dispatcher code from a PR head branch (the workflow loads dispatcher code only from `main`).
- Cannot delete branches (no `contents: write` permission).

These are not policy — they are absent code paths or absent permissions.

## Operator commands

Post a PR comment with one of these to drive the dispatcher:

| Command | Effect |
|---|---|
| `OPERATOR APPROVE` | Add approving review, mark "ready for operator merge." You merge manually. |
| `OPERATOR BLOCK <reason>` | Add changes-requested review with reason. Pauses dispatcher loop. |
| `OPERATOR INVESTIGATE <note>` | Send PR back to reviewers for another round with note as context. |
| `OPERATOR DISCUSS <text>` | Post text as a PR comment, triggers a new review round. |
| `OPERATOR PAUSE` | Dispatcher stops touching this PR until `OPERATOR RESUME`. |
| `OPERATOR RESUME` | Re-enable dispatcher on a paused PR. |
| `OPERATOR KILL` | Close the PR. Branch is NOT deleted. |

Typos like `OPERATOR APROVE` get an explicit no-op reply listing valid verbs. Silent ignore is never the behavior.

## How to debug

If something looks wrong:

1. Open the PR. Look at the labels — there should be `dispatcher:tier-<tier>` and `dispatcher:round-<n>`.
2. Look for verdict comments. Each AI review ends in a fenced `tradewatcher-verdict` block.
3. Check the workflow runs at `https://github.com/NFS-247/StockTrader/actions`.

If the dispatcher seems stuck, post `OPERATOR PAUSE` to halt it, then investigate.

## Tests

```bash
pytest tests/test_dispatcher_*.py
```

All pure functions (`classify`, `verdict`, `parse_reply`, `converge`, `escalation`) have unit tests. API clients are not unit-tested here because they hit external services — they're exercised in production.

## Where the rules live

Section 4 of the design doc is canonical for tier classification. Section 7 is canonical for operator commands. Section 12.5 is canonical for verdict format. If the design doc and the code disagree, that's a bug in the code.

## Marker

`TRADEWATCHER_AI_PEER_REVIEW_DISPATCHER_V1`
