# PR update discovery — 2026-08-15

> **Historical snapshot.** Captured at `local_head 2308d7818`, before the
> prior-art-reference and PR-feedback commits landed. The dangling-reference
> findings below (MSC0501/0502/0F04/00E4) have since been resolved in the
> proposal files; this note is kept for context, not as a current status report.

Scope: decide what to push to the published upstream
(matrix-org/matrix-spec-proposals) PRs for MSC4499, MSC4500, MSC4511, MSC4521.
Plan-mode only — nothing committed/pushed.

Local repo is at `guru/working` (v1.2-883-g6ff3b303a), 11 commits ahead of
`origin/guru/working`. All four PR branches below are local `origin/*` refs from
the last fetch — not re-verified live (fetch was declined mid-session), but
user-confirmed via `git log --graph` to match:

```text
21969b5bd (origin/gitlab/guru/4499-strict-key-caching) initial draft of strict unique signing keyIDs/notary caching rules — 6 weeks ago
e4d3d70f9 (origin/gitlab/guru/4500-state-accumulators)  initial draft of state accumulators                               — 6 weeks ago
8c92859c1 (origin/guru/4510-merkleized-topo-api)        chore: rename 4500 -> 4511                                        — 4 weeks ago
bb76dade3 (origin/gitlab/guru/4521-algebraic-set-reconciliation) initial commit of algebraic set reconciliation           — 3 weeks ago
```

Every PR branch is frozen at its **initial-draft commit** — none of the
subsequent tightening/review work in `guru/working` has been pushed yet. This is
a content refresh for each, not a merge negotiation.

## Summary table

| MSC  | GH PR #                                                                | State | PR branch                                                                                           | PR head SHA | File(s) in `guru/working`                                     | Diff vs PR head                                                                                | Structural change?                               | Impl branch (reference)                                                                                                                        |
| ---- | ---------------------------------------------------------------------- | ----- | --------------------------------------------------------------------------------------------------- | ----------- | ------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| 4499 | [#4499](https://github.com/matrix-org/matrix-spec-proposals/pull/4499) | DRAFT | `gamesguru:guru/4499-strict-key-caching`                                                            | `21969b5bd` | `4499-key-caching.md`                                         | +843/-116 (959 total)                                                                          | No (same file, heavily expanded)                 | continuwuity2 `guru/feat/notary-endpoint` @ `40fbc3471`                                                                                        |
| 4500 | [#4500](https://github.com/matrix-org/matrix-spec-proposals/pull/4500) | OPEN  | `gamesguru:guru/4500-state-accumulators`                                                            | `e4d3d70f9` | `4500-state-accumulators.md`                                  | +390/-47 (437 total)                                                                           | No                                               | continuwuity2 `guru/feat/msc4500/state-accumulator` @ `fd4ce4212`                                                                              |
| 4511 | [#4511](https://github.com/matrix-org/matrix-spec-proposals/pull/4511) | DRAFT | `gamesguru:guru/4510-merkleized-topo-api` (⚠ branch says 4510, PR is 4511 — turt2live flagged this) | `8c92859c1` | `4511-part-a-...md`, `4511-part-b-...md`, `4511-part-c-...md` | +1901/-0 (all new — PR head has one file: `4511-topological-metadata-query-api.md`, 784 lines) | **Yes — 1 file → 3-part split, same MSC number** | continuwuity2 `guru/feat/msc4511/augmented-hamt` @ `47d91bede`; rezzy `pr-hamt` @ `fb39314`, `pr-reachability` @ `d6e8e9a`                     |
| 4521 | [#4521](https://github.com/matrix-org/matrix-spec-proposals/pull/4521) | OPEN  | `gamesguru:guru/4521-algebraic-set-reconciliation`                                                  | `bb76dade3` | `4521-algebraic-set-reconciliation.md`                        | +406/-154 (560 total)                                                                          | No                                               | continuwuity2 `guru/feat/pinsketch-algebraic-digest-set-reconciliation` @ `5c547d4de`; rezzy `pr-reconcile` @ `e50bbcd`, `pr-gf64` @ `4c94c96` |

Commits accumulated on `guru/working` since each PR's draft, touching that MSC's
file(s): 4499 = 74, 4500 = 22, 4511 = 44 (across old single-file + new split),
4521 = 31.

## Blocking issue #1 — dangling cross-references to unpublished MSC numbers

`grep -noE 'MSC ?(0[0-9A-F]{3}|45XX|00[A-Z][0-9A-Z]?)'` across all four docs
turns up only placeholder-style refs, no `45XX` literal survived to the current
text:

- **MSC0501** — referenced 4x in `4521-algebraic-set-reconciliation.md` (L345,
  596, 655, 922) and 7x in `4500-state-accumulators.md` (L473, 501, 503, 505,
  507, 519, 524). One of the 4500 hits is a **heading**, previously
  `## Synergy with MSCXXXX (event set reconciliation)` → now
  `## Synergy with MSC0501 (event set reconciliation)`.
- **MSC0502** — referenced once in `4521-algebraic-set-reconciliation.md`
  (L923).
- **MSC0F04** — referenced once in
  `4511-part-c-merkleized-room-version-upgrade.md` (L442).
- **MSC00E4** — referenced once in `4499-key-caching.md` (L812).

None of MSC0501, MSC0502, MSC0F04, or MSC00E4 are known to have upstream PR
numbers (they read as internal/gitlab-only placeholder identifiers — cf.
`guru/draft/msc00c1-topo-query-merkle-metadata`,
`guru/federation-gossip-reconciliation`, `guru/federation-key-hygiene` branch
names in this repo). **Each is a per-PR decision before pushing:** inline the
referenced content, drop the reference and soften to prose, or hold that PR
until the referenced MSC has a real number.

## Blocking issue #2 — MSC4511 one-file → three-file split

PR #4511 currently carries a single doc. `guru/working` has since split it into
Part A/B/C (topological query API / Merkle overlay backwards-compat / Merkleized
room-version upgrade), 1901 lines total vs. 784. The Matrix MSC process assigns
one number and one FCP per proposal — three docs under one PR is not a shape it
handles cleanly. This needs a user decision, not a mechanical push:

- (a) re-merge Part A/B/C back into one document under #4511, or
- (b) keep Part A as #4511 and open two **new** MSC PRs for Part B and Part C,
  or
- (c) some other split the user has already been steering toward and hasn't
  stated here.

Also cosmetic but worth doing in the same pass: rename branch
`guru/4510-merkleized-topo-api` → something `4511`-prefixed; turt2live already
flagged the number mismatch on the PR itself
(`.tmp/pr-comments-4511-travis-2026-07-06.txt`): _"Your MSC number is 4511, not
4510."_ — no other review feedback on that thread besides an unchecked
"Implementation requirements: Server (multiple)" checkbox.

## Housekeeping — do not push these paths

Repo-root files that must NOT land on any PR branch:

- `msc4499-draft2-guided-review.md` (internal review notes)
- `4521-algebraic-set-reconciliation.md.pdf` (rendered artifact)
- `.tmp/pr-comments-4511-travis-2026-07-06.{json,txt}` (scratch notes)
- this file, `.tmp/pr-update-discovery-2026-08-15.md`

Each PR update should touch **exactly** its MSC's `.md` file(s), nothing else.

## Suggested mechanical shape (once #1 and #2 are resolved) — not yet run

For each PR branch: `git checkout <pr-branch>`,
`git checkout guru/working -- <that MSC's file(s)>`, one squashed commit
describing the refresh, push under review. Do not replay the 22–74 intervening
`guru/working` commits individually — they're interleaved across unrelated MSCs.

## Open questions for the user

1. MSC0501/0502/0F04/00E4 refs (#1 above) — inline, drop, or
   hold-for-dependency, per PR?
2. MSC4511 split (#2 above) — pick (a)/(b)/(c).
3. Confirm impl-branch mapping above is right (esp. 4499 ↔
   `guru/feat/notary-endpoint`, inferred by content/timing, not by name).
4. PR body/description text — 4499 grew 116→959 lines; likely needs a rewritten
   PR description too, not just a file swap. Same question for the other three,
   lower priority.
5. Want a live re-fetch of `origin/*` before actually pushing, to confirm PR
   heads haven't moved upstream since last fetch?
