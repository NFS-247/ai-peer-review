# Instructions for AI dev agents working in this repo

## What this project is

<!-- One or two short paragraphs: what this project does, who it's for, and anything
that must NOT break (e.g. "paper-only trading system — it must never place a real
broker order"). The AI reviewers read this on every PR and judge your changes
against it, so keep it accurate as the project changes. -->

<!-- automation-nation-instructions -->
## Always open a pull request (never ask, never skip)

Every code change in this repo ships as a **pull request** — every single time, no
matter how small, no exceptions. The Automation Nation AI review panel only sees
work that is in a PR; anything you commit without one is never reviewed and never
reaches the operator. A PR is not a step you choose — it is the only way work is
delivered here.

So the moment you have a code change:
- Work on a branch — **never commit directly to the default branch.**
- Push it and **open the PR yourself** with the tools you have.
- Then follow "After creating a PR" below (subscribe and handle the review).

**Never ask whether to open a PR.** It is not a question and it is not the
operator's call — opening the PR is simply how you ship. Do not write "should I
open a PR?", "want me to open a PR?", or "let me know if you'd like a PR." Just
open it. The only things you ever ask the operator about are genuine
product/business judgment calls — never the mechanics of shipping code.
## Operator steps (read this before opening a PR)

This repo's pull requests are reviewed by the Automation Nation AI peer-review panel,
and the review app merges on a unanimous panel or an `OPERATOR APPROVE`. So if a PR
needs a manual action BEFORE merge — run a SQL script or database
migration, run a one-off command, set/rotate an env var or secret, backfill data — list
those steps in the PR description under a heading titled exactly `## Operator steps`,
with expected results.

**When a step runs SQL or a script, never point to it by filename alone.** The
operator is non-technical and would have to hunt for the file — which is usually on a
Claude branch, not `main`. Do ONE of these so they can act in a single move:
- **Paste the full SQL / script** into the step in a fenced code block, ready to copy; or
- **Link the exact file** as a clickable GitHub URL pinned to THIS PR's branch or head
  commit (e.g.
  `https://github.com/OWNER/REPO/blob/BRANCH-OR-SHA/db/migrations/0007.sql`), not the
  bare path.

For example:

    ## Operator steps
    1. Run this in the SQL editor (also on the PR branch:
       https://github.com/acme/app/blob/claude/add-priority/db/migrations/0007.sql):
       ```sql
       alter table orders add column priority int not null default 0;
       ```
       Confirm the column exists.
    2. Merge.

The reviewer echoes that block verbatim onto the operator's ready-to-merge alert so
they run it before merging. Omit the section for pure-code PRs with no manual step.

**The capability test decides WHO runs a step — check it before you write the
section.** Look at the tools and connectors actually available in your session
(Supabase, Render, Vercel, GitHub, a shell — whatever is wired up). **If one of
them can perform the step, it is YOURS to run, not the operator's.** That
connector is in the session precisely so you use it; leaving it unused and
filing the work for a non-technical operator is the failure this rule exists to
stop. A step belongs to the operator when nothing in the session can do it —
there is no such tool, the credential or console is somewhere you cannot reach,
or it is an account/billing/physical action that is theirs alone — **and also
whenever the timing rule below sends it there.**

Capability decides WHO *can* run a step; timing decides WHERE it may be filed.
Ask both, in order:

1. **Can anything in this session do it?** No → `## Operator steps`.
2. **Yes — can you finish it before the merge happens?** A prerequisite the
   merged code depends on must be done while the PR is still OPEN. If you
   cannot complete it before the merge, it stays under `## Operator steps` **no
   matter who could run it** (see "Ordering is yours" below) — having the tool
   never licenses filing a deferred prerequisite under the automated heading,
   because that lets the code land against a migration that has not run. Steps
   you will genuinely complete — while the PR is open, or safely after merge —
   go under `## Automated steps (handled by this session)`, and then you run
   them.

So, concretely: a migration in a repo where you hold Supabase tools is an
**automated** step — applied while the PR is open, then said so in the section.
An env var in a repo where you hold the hosting connector is an **automated**
step. The same two, in a session with no such connector, are **operator**
steps. Never classify by the *kind* of step ("migrations are operator steps") —
classify by what THIS session can reach, then by when you can finish it. "I
could have run it, but filed it for the operator anyway" holds the PR and hands
the operator work the automation was there to absorb.

One case needs a human decision even though you *can* run it: a step that is
destructive and irreversible on production data (dropping a table, deleting
rows, rotating a live credential). Ask the operator in chat before running it.

**Asking gates nothing.** An unanswered question in chat holds no PR; the merge
will not wait for it. So the timing rule still applies to a destructive step,
unchanged: if the merged code depends on it, it goes under `## Operator steps`
while the decision is pending, and the gate holds the PR until it is settled.
It becomes an automated step only once you have the answer AND have run it
while the PR is open — recorded as done, per "Ordering is yours" below. What is
never right is filing it under `## Operator steps` and going quiet: the ask is
the point, not the filing.

**Only steps a HUMAN must perform belong under `## Operator steps`.** That
heading is a merge gate: the dispatcher will not auto-merge a PR whose
description carries actionable operator steps — it holds the PR for the
operator even on a unanimous panel. If YOU (the session) are going to run the
step yourself, it is NOT an operator step: put it under
`## Automated steps (handled by this session)` — a heading the gate does not
recognize — and the PR auto-merges on approval while you carry out the step.
Two hard rules make that safe:

- **Ordering is yours.** If the step must happen BEFORE the code lands (a
  migration the new code depends on — the exact outage class the gate exists
  for), run it while the PR is still OPEN and say so in the section
  ("applied and verified on <date>"). If you cannot run it before the merge
  happens, it is not an automated step — keep it under `## Operator steps`.
- **Confirm in the thread.** After running a post-merge step, post a short PR
  comment saying what ran and how you verified it. The operator's hand-merge
  used to be the only confirmation a step happened; your confirmation comment
  replaces it, so a missing confirmation is a visible red flag.

A held PR should always mean "the operator personally has something to do,"
never "the automation left a note to itself."

**A bare "None." is an answer; anything more is a hold.** The gate treats ANY
other content under a recognized heading as steps and fails closed — including
FYI notes ("worth knowing", "not blocking", context about the change). Advisory
content that requires no action goes under an unrecognized heading such as
`## Notes`, never under the steps heading. If there is truly nothing to run,
omit the section entirely — that is the cleanest signal there is.
## After creating a PR (always do this)

**Always subscribe to watch the PR** after you create it. PRs in this repo are
reviewed by an AI panel, and if you don't watch, reviewer comments and CI
failures just sit there with nobody responding.

After `create_pull_request` succeeds:
1. Call `subscribe_pr_activity` for the PR.
2. Handle every event that comes in — respond to reviewer comments, fix CI
   failures, push fixes. Don't wait for the operator to notice.
3. Only escalate to the operator (via `PushNotification` or `AskUserQuestion`)
   when the PR is ready to merge, or you're genuinely stuck on something that
   requires their judgment.
## Handling reviewer dissent (do this yourself)

When a reviewer requests changes or dissents (the panel isn't unanimous), **don't
wait for the operator** — evaluate it yourself:

1. Read the dissenting reviewer's concern carefully.
2. **If the concern is valid** — fix the issue, push the fix, and say what you
   fixed in a reply to the reviewer's comment.
3. **If the concern is not valid** (misunderstanding the code, wrong about the
   behavior, stylistic nitpick that doesn't matter) — reply to the reviewer
   explaining why, then **post `OPERATOR APPROVE` as a PR comment yourself** to
   override the dissent and ship it. Do NOT ask the operator to type it — you
   have the tools to post the comment, so do it.

   **Required format when YOU post `OPERATOR APPROVE`**: always include a
   one-line "why" so the operator can scan the comment and understand what
   happened without reading the whole thread. Examples:
   - `OPERATOR APPROVE — gpt's "journal 0045 missing" claim is wrong (46 entries, last is 0045_lying_anita_blake); gemini's searchParams typing claim would break the Next 15 fork.`
   - `OPERATOR APPROVE — diff truncation, not missing code; all flagged paths exist on the branch (paths X, Y, Z).`
   - `OPERATOR APPROVE — only valid finding (X) was fixed in commit abc1234; remaining concerns are pre-existing/style nits.`

   Don't post a bare `OPERATOR APPROVE` with no reason. The operator should
   be able to read your single line and immediately know "agent decided
   correctly" or "wait, I disagree" without context-switching into the PR.

4. Only escalate to the operator if the dissent raises a **genuine ambiguity you
   can't resolve** — a product/business decision, not a code question.

**Hard rule — do not ask the operator to approve:** if you've read the
reviewer comments, evaluated them, and concluded "this is a false positive
and we should ship", that is YOUR decision to execute. Posting `OPERATOR
APPROVE` yourself IS the decision. Telling the operator "you should
approve this" or "please post OPERATOR APPROVE" is a violation of this
rule — it forces the operator to either trust you blindly or re-do the
analysis you already did. Just post the approval yourself with your
reasoning. If you genuinely cannot decide, escalate (per #4); but
"I think it's fine, but I want a human signoff" is not a real ambiguity —
just sign off.

The operator should never have to evaluate whether reviewer feedback is valid.
That's your job. Read the code, understand the concern, decide, and act.

**The red "Automation Nation review" check is NOT a CI failure.** It's the
panel's own status — "action_required" / "Needs the operator" means the
panel split and a human (operator or you) must adjudicate. Treat it like
the dissent it represents (step 3 above), not like a build failure. The
real CI checks (pytest, lint, your own GitHub Actions) are what matter
for "is the build broken" — if THOSE are green, the only thing in the
way is the panel itself, and OPERATOR APPROVE clears it.
## Keep a session journal (so work isn't lost across chats)

This project is built across many separate chats/sessions that can't see each
other. To stop them from duplicating or losing each other's work, every session
records what it did IN THE REPO — a shared memory the other chats can read.

When you finish a meaningful chunk of work, add a short entry under `docs/journal/`
**as part of the same PR** (it's a code change, so it ships in the PR like anything
else — never a separate step):

- **One file per session/topic** so chats never clash: name it
  `docs/journal/YYYY-MM-DD-short-slug.md` (a NEW file — do not edit another
  session's entry).
- Keep it brief — a memory aid, not a report:
  - **What I did** — the change, in a sentence or two.
  - **Why / decisions** — anything non-obvious a future chat should know (chose X
    over Y because…), so nobody re-litigates it.
  - **Open / next** — what's still TODO or was deliberately left out.
  - **Touches** — the main files/modules involved.

Before starting new work, **skim `docs/journal/`** to see what other sessions
already did or decided — so you build on them instead of rebuilding what exists.
<!-- /automation-nation-instructions -->
