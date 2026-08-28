# MSC00C1: State Resolution V2.1.1

This proposal documents Rezzy's V2.1.1 (`room_version` `12.1`), a rezzy-internal
hardening of V2.1/MSC4297 state resolution, non-normatively: memoized transitive
`auth_events` traversal and a resolved-state banned-sender screening pass. It is
not a wire-facing room version proposal for the spec — no PDU format changes, no
new endpoints — it is a resolver-behavior variant a server MAY implement locally
and advertise via `12.1`. This document also records a real gap in that
hardening — silent tolerance of an unreachable auth ancestor — that neither
V2.1.1 nor MSC4242 (State DAGs, V2.2 in Rezzy's scheme) closes, and proposes
closing it.

## Background

V2.1 (Rezzy's MSC4297 implementation) authorizes an event by resolving the state
at its immediate `auth_events` citations. Two related problems surfaced in
adversarial/concurrent-DAG testing (see `docs/spec_audit.md` and
`tests/unit/test_traversal.rs`):

1. **Shallow auth lookup.** V2.1's local-auth construction only walks the
   event's _directly cited_ `auth_events`, one hop. If a required tuple (e.g.
   `m.room.power_levels`) is reachable only transitively — cited by something
   the event cites, rather than by the event itself — V2.1 doesn't see it and
   the corresponding auth rule falls back to a conservative default (e.g.
   invite-only join rules with no `m.room.join_rules` in local auth). This is
   _safe_ (fails closed) but _stricter_ than the room's real, valid history
   sometimes warrants — see `test_v2_1_strictness_future_v2_2_should_pass` for
   the canonical fixture and `docs/rezzy_msc4242_security_comparison.md` for why
   V2.2 (MSC4242 State DAGs), not V2.1.1, is the actual fix for that class.
2. **Ban-evasion via power-phase local auth.** During conflict resolution's
   power-phase, an event whose sender is provably banned in the fully resolved
   state could still be admitted if the power phase's local-auth fallback didn't
   yet reflect that ban. An earlier fix attempt — the Causal Domination Operator
   (CDO), `src/resolve/cdo.rs` — approximated this by dropping events an
   independent-branch admin action "causally dominated." CDO was **unsound** and
   is retired: an auth-invalid forged admin event could erase legitimate
   concurrent events, causing federation forks against non-CDO servers (see
   `docs/cdo_soundness_anomalies.md`, anomalies 17–20). It is dead code kept
   only as design history — nothing in the live path calls it.

V2.1.1 (`StateResVersion::V2_1_1`, room version string `12.1`) is Rezzy's sound
replacement for both, gated by two independent flags on `StateResVersion`
(`is_v2_1_plus()` and `has_ban_evasion_hardening()` — see
`src/basespec/rezzy_types.rs`):

- **Transitive, memoized auth-chain traversal.** `compute_local_auth` in
  `src/state/at.rs` replaced V2.1's 1-hop walk with a pure memoized
  breadth-first traversal over the full `auth_events` ancestry, for both
  `V2_1_1` and `V2_2`. This is the mechanism
  `test_v2_1_vs_v2_1_1_recursive_auth_lookup` exercises: V2.1.1 finds an auth
  tuple V2.1 cannot see.
- **Resolved-state ban screening.** `src/resolve/iterative.rs` applies
  `is_sender_banned` against the fully resolved membership state as a
  post-power-phase predicate, replacing CDO's unsound domination heuristic with
  a check against the room's actual resolved state rather than an approximation
  of it.
- **Narrowed power-phase local-auth fallback**, gated by
  `has_ban_evasion_hardening()`, which restricts what the power phase is allowed
  to fall back to when local auth is incomplete.

`docs/rezzy_msc4242_security_comparison.md` has the full fixture-by-fixture
comparison against `test_traversal.rs` and the local MSC4242 Complement suite
(`../complement/tests/msc4242`); this proposal focuses on the resolver mechanism
itself, not the comparison.

## Proposal

Adopt V2.1.1 as described above: memoized transitive auth traversal plus
resolved-state ban screening, as an implementation MAY-level hardening on top of
V2.1/MSC4297, advertised via room version `12.1`. This is largely already
implemented in Rezzy and is being written up for record and review, not proposed
as new work.

The one piece of _new_ work this proposal asks for: **make the transitive
traversal in `compute_local_auth` fail closed on an unreachable ancestor,
instead of silently truncating.**

### The gap

`compute_local_auth`'s BFS (`src/state/at.rs`) walks `auth_events` transitively
across two caller-supplied maps, `auth_context` and `conflicted_events`:

```rust
if let Some(aev) = auth_context
    .get(aid)
    .or_else(|| conflicted_events.get(aid))
{
    update_local_auth(&mut local_auth, aev, current_depth);
    // ... enqueue aev's own auth_events for further traversal
}
```

If `aid` is absent from both maps, the loop simply doesn't enqueue it and moves
on. `compute_local_auth` returns a plain `BTreeMap`, not a `Result` — there is
no signal, anywhere, that a cited ancestor was unreachable. A caller who handed
in an incomplete auth context (a fetch that silently dropped an event, an
off-by-one in chain-diff computation, a bug) gets a `local_auth` that is quietly
smaller than what the event actually cites, with no way to tell that apart from
"the room genuinely has no such tuple yet."

This does not currently produce a known wrong _answer_, because the auth rules
that consume `local_auth` (via `check_auth`) default-deny when a required tuple
is absent — e.g. missing `m.room.join_rules` defaults to invite-only.
`test_v2_1_1_xfail_disconnected_auth` is the fixture demonstrating this: it
passes today, but it passes because that _specific_ rule's default happens to be
safe, not because the resolver detected and rejected an incomplete walk. That is
a property of individual auth rules, not a guarantee `compute_local_auth`
provides. A future auth rule without a safe default-deny for an absent tuple —
or a change to an existing one — would silently fail open instead of closed, and
nothing in this code path would catch it.

Compare MSC4242's State-DAG path (V2.2 in Rezzy's scheme):
`compute_state_before_from_dag` calls `validate_state_dag_ancestors`, which
walks the _entire_ reachable `prev_state_events` closure and returns
`StateDagError::IncompleteDag { missing_event_ids }` the instant any ancestor,
however deep, is missing — before any auth is derived. V2.1.1's transitive walk
has no equivalent.

### Spec grounding for failing closed

Neither `1442-state-resolution.md` nor `4297-state-resolution-v2_1.md` states an
explicit fallback for "the auth event isn't available locally," but the
surrounding spec text treats a complete auth chain as a _precondition_ of
running resolution, not a condition resolution itself must tolerate:

- Server-Server API "Checks performed on receipt of a PDU," rule 4: _"Passes
  authorisation rules based on the event's auth events, otherwise it is
  rejected"_ — stated as something the server just does, presupposing the cited
  auth events are already in hand.
- `GET /event_auth/{roomId}/{eventId}` is introduced with: _"The homeserver may
  be missing event authorisation information... These APIs give the homeserver
  an avenue for getting the information it needs"_ — the spec's answer to a
  missing auth event is fetch it (via this endpoint, or `/get_missing_events`,
  or backfill), not resolve without it.
- The iterative-auth-checks Step 4 supplementation clause — _"the appropriate
  state event from the event's `auth_events` is used if the auth event is not
  rejected"_ — presupposes the auth event is known and was itself already
  processed; it has no clause for "the auth event doesn't exist locally."

So a resolver that silently proceeds on an unreachable ancestor isn't reading an
ambiguous spec loosely — it is tolerating a state the spec's fetch-first model
doesn't anticipate resolution ever seeing. Failing closed here brings V2.1.1 in
line with that model, rather than introducing a new, stricter behavior beyond
what the spec assumes.

### The fix

The target invariant:

> Never authorize or resolve using an incomplete reachable auth ancestry.
> Missing cited ancestors produce an explicit incomplete-context signal; they
> are never silently omitted, locally substituted, or treated as authorization
> success.

Crucially, "fail closed" here must **not** mean `compute_local_auth` itself
decides the PDU is rejected. A missing ancestor is evidence of incomplete
_local_ knowledge — resolvable by fetching (`/event_auth`,
`/get_missing_events`, backfill) and retrying — not evidence the event is
invalid. Conflating the two would turn ordinary replication lag into a
premature, and potentially permanent-looking, semantic rejection. Per the spec
grounding above, this mirrors how the wider protocol already treats the
distinction: "checks performed on receipt of a PDU" rule 4 (auth failure) is a
different outcome from "the homeserver may be missing event authorisation
information" (§ `/event_auth`, a fetch problem) — the two are never conflated in
the spec text either.

Concretely, change `compute_local_auth` to surface unreachable ancestors as a
distinct condition instead of dropping them, e.g.:

```rust
pub(crate) fn compute_local_auth<Id, C, S1, S2, K>(
    /* ...unchanged params... */
) -> Result<BTreeMap<(EventType, K), LeanEvent<Id, C, K>>, IncompleteAuthContext<Id>>
```

where `IncompleteAuthContext { missing_event_ids: Vec<Id> }` mirrors
`StateDagCompleteness::Incomplete`'s field of the same name and is a distinct
type from `AuthError` — it must not be constructible as, or silently mapped to,
an auth rejection. Thread that signal through `iterative_auth_ok` and
`resolve_iterative_sort`'s call sites so a missing ancestor becomes an explicit
"cannot resolve yet, go fetch `missing_event_ids`" outcome the caller must act
on. Only a federation/validation boundary that has already tried to fetch the
missing ancestors and still can't obtain them gets to turn that into a final
rejection (or, per normal Matrix operation, hold the event as
unresolvable/outlier pending backfill) — `compute_local_auth` itself never makes
that call.

This is scoped to `V2_1_1`/`V2_2`'s transitive traversal branch specifically.
V2.1's 1-hop walk isn't exposed to this problem the same way: it only ever reads
an event's own `auth_events` list directly, so "missing" there already means
"not cited," which existing rules already handle correctly — there is no
multi-hop closure to be silently incomplete over.

### Retry cost, and un-rejection

Treating a missing ancestor as deferrable rather than final only holds up if
re-resolution once the ancestor arrives is cheap enough to actually do,
including the case where a previously-rejected event should now be _un_-rejected
because the fetched ancestor changes its auth outcome. With naive delta-chain or
flat-map state storage, replaying resolution over an event's dependents after a
late-arriving auth event can be expensive enough that implementations are
tempted to just leave the earlier rejection standing. MSC00DC's augmented-HAMT
storage — structurally-shared, subtree-digest-cached state maps — is what makes
that replay cheap in practice: only the branches actually touched by the
newly-available ancestor need re-derivation, not an O(room size) re-walk. This
proposal's incomplete-context model assumes that or an equivalent cost profile;
without it, "defer and retry" degrades back into "reject and hope it doesn't
matter" under load.

## Potential issues

- **Behavior change under partial auth context.** Any caller (production or
  test) that currently relies on `compute_local_auth` tolerating a partial
  `auth_context`/`conflicted_events` map will start failing where it previously
  succeeded quietly. This is the point of the change, but it means auditing
  existing callers and tests for implicit reliance on the silent-drop behavior
  before landing it.
- **Performance.** The BFS already visits every node once (memoized); tracking
  missing ids adds a `Vec` push on the miss path only, which is the
  already-uncommon case. No expected regression on the happy path.
- **Does not fix DAG honesty.** Both this fix and MSC4242's existing
  `IncompleteDag` check only prove the _local_ map handed to the resolver is
  connected (or isn't). Neither proves a sender's claimed DAG is _exhaustive_: a
  malicious or buggy sender can omit a legitimate concurrent branch entirely,
  and the receiver's local view will look complete — connected back to
  `m.room.create`, no missing ancestors — while still being smaller than the
  room's real history. That is a DAG-_honesty_ problem, bounded by ordinary
  federation event-fetching/backfill (`/get_missing_events`,
  `get_missing_state_events`), not something any local completeness check — in
  V2.1.1 or V2.2 — can catch. This proposal closes the connectivity gap only;
  DAG honesty is out of scope and, as far as this proposal's author is aware,
  isn't closed by anything in MSC4242 either.

## Alternatives

- **Leave it as-is, relying on default-deny.** Rejected: it works only as long
  as every consuming auth rule happens to have a safe default for an absent
  required tuple, which is an emergent property of the current rule set, not an
  invariant the resolver enforces. New rules or refactors could silently break
  it.
- **Port MSC4242's full `StateDagCompleteness` machinery into V2.1.1.**
  Considered and rejected as overkill: V2.1.1's traversal is over `auth_events`
  (a flat historical citation list), not a validated `prev_state_events` state
  DAG. It doesn't need fanout limits, foreign-room checks, or rejected-ancestor
  validation the way MSC4242 does — it only needs to know when it couldn't
  finish the walk. A `Vec<Id>` of missing ids is proportionate; a parallel
  `StateDagCompleteness` enum is not.

## Security considerations

This proposal is itself a security hardening: it closes a fail-open exposure in
the multi-hop auth traversal that ban-evasion and progressive-privilege fixtures
already depend on (`test_v2_1_flaw_concurrent_ban_evasion`,
`test_v2_1_1_power_phase_ban_supplementation`, and siblings in
`tests/unit/test_traversal.rs`). Today those fixtures pass because the specific
rules involved default-deny; this proposal removes the dependency on that
coincidence for auth rules generally, present and future.

No new attack surface is introduced: the change only makes an existing
silent-degradation path observable and rejectable. Callers gain a new error case
to handle (a resolution attempt against a genuinely incomplete auth context),
which should map to the same recovery path already needed for V2.2's
`IncompleteDag` — fetch the missing event(s) and retry, or reject the event if
they cannot be obtained.

## Unstable prefix

Already shipping as `StateResVersion::V2_1_1` / room version string `12.1` in
Rezzy; this is rezzy-internal and not proposed for inclusion in the Matrix spec.
No unstable feature flag beyond the existing `12.1` room version string is
needed for the traversal hardening proposed here.

## Dependencies

This proposal builds on V2.1/MSC4297 (state resolution v2) and is documented
alongside, but does not depend on, MSC4242 (State DAGs) — see
`docs/rezzy_msc4242_security_comparison.md` for how the two relate. The
incomplete-context/defer-and-retry recovery model in this proposal assumes a
storage layer with MSC00DC's cost profile (cheap, subtree-scoped re-derivation);
it does not strictly require MSC00DC's specific augmented-HAMT design, only an
equivalent one.
