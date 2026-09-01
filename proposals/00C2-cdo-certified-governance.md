# MSC00C2: Certified causal governance

This proposal defines an **experimental, non-standard Matrix room version**:

```text
room_version: "tk.nutra.cdo.12"
state resolution: Rezzy V3.0.0
```

It MUST NOT be advertised as Matrix room version `12` or any existing MSC room
version. A server which does not implement this room version MUST reject or
remain unable to join the room; it MUST NOT resolve the room with ordinary State
Resolution v2.

## Motivation

State Resolution v2 performs iterative authorization after sorting divergent
auth chains into a synthetic order. A concurrently injected kick can therefore
be considered before a creator's promotion and cause later, independently
authorized actions by the promoted user to fail the sender-membership rule.

This proposal introduces a narrow, typed rule for that governance conflict. It
does not recover wall-clock order. It instead gives a certified creator grant
explicit precedence over a concurrent, lower-or-equal-authority kick, and
authorizes affected downstream actions against their own certified causal
branch.

## Terminology

An event is _causally before_ another event when it is reachable through the
room version's defined DAG edges. Events are _concurrent_ when neither is
causally before the other. Timestamps and received order MUST NOT be used.

`grant_admin(B)` is not an inferred interpretation of an arbitrary power-level
event. It is a compound, certified operation defined below.

## Wire format

A creator-issued `m.room.power_levels` event MAY carry:

```json
"tk.nutra.cdo": {
  "active_member": "$event_id_of_B_join"
}
```

The field is part of the signed PDU and is preserved wherever this room version
preserves power-level content. `active_member` MUST be a single event ID; an
array, absent value, or any other JSON type makes the event ineligible for CDO
certification. It does not by itself make the underlying power-level event
invalid under ordinary Matrix authorization.

## Certified creator grants

A server MAY create

```text
CertifiedCreatorGrant {
   grant_id, target, target_pl, prior_pl_id, active_witness
}
```

only if all conditions below hold:

1. The grant passed signature, event-ID/hash, and admission verification.
2. It is a non-rejected `m.room.power_levels` state event sent by the room
   creator.
3. Its signed witness names a locally available, verified `m.room.member` event
   for `target`, with `membership: "join"`.
4. The witness is an explicitly required member-state reference in the grant's
   canonical, room-version-defined branch-auth snapshot.
5. The canonical snapshot contains exactly one authoritative membership and
   power-level event for each required auth key. Missing, duplicate, rejected,
   foreign-room, or unverified dependencies fail certification.
6. Ordinary authorization succeeds when the grant is checked exclusively against
   that canonical branch-auth snapshot.
7. The grant raises `target`'s power level relative to the authenticated prior
   power-level event. The new level is at least the concurrent kicker's level.

Certification is recursive and cached by event ID. Missing dependencies are an
incomplete-context/fetch condition, not proof that the PDU is invalid. Only a
verified certificate participates in the rules below.

## Resolution rules

Before iterative resolution, implementations derive immutable certificates and
the relevant cached causal relations. For a certified `grant_admin(B)` and a
kick of `B` by `A`:

- If the kick is causally before the grant, ordinary descendant semantics apply;
  the later creator grant may restore B according to its own auth.
- If the grant is causally before the kick, ordinary authorization applies; this
  proposal grants B no tenure.
- If they are concurrent and A's certified authority is no greater than B's
  newly certified authority, the grant dominates the kick.

In the concurrent case the resolver MUST retain the certificate's membership
witness and grant as the cross-key governance result and MUST NOT admit the
dominated kick. It MUST evaluate an event authored by B that cites the
certificate's canonical branch-auth state against that branch-auth state, not
against the dominated kick's synthetic position.

This rule is intentionally limited to certified creator grants versus a kick. It
does not define peer-admin arbitration, bans, self-leaves, invitations,
join-rule conflicts, or arbitrary power-level edits. Those cases use ordinary
room-version resolution until separately specified.

## Canonical repair schedule

V3 selection is synchronous. Implementations MUST NOT mutate a provisional
resolved state while authorizing another event in the same round.

Let `D0` be the complete admitted event set. At round `i`:

1. For every state key, derive causally maximal writers in `Di` and select the
   maximum under V3's total rank. These selections form `sigma_i`.
2. Evaluate every selected event against the same immutable `sigma_i`, including
   the V3 cross-branch-reach predicate.
3. Let `Fi` contain every selected event that fails that predicate.
4. If `Fi` is empty, `sigma_i` is the result. Otherwise set
   `D(i+1) = Di minus Fi` and begin the next round.

Candidate discovery, rank comparison, and diagnostics MUST use canonical event
ID order. That order cannot affect a round's outcome; it only makes work and
diagnostics reproducible. Removals are simultaneous. After removal, a causally
dominated writer may become maximal and is considered in the next round. A key
with no remaining writer is absent.

The schedule terminates in at most `|D0|` non-final rounds: every non-final
round removes at least one event. This schedule is normative even if the
associated repair operator is not monotone in the lattice-theoretic sense.

## Safety and interoperability

Implementations MUST NOT use a traversal budget whose exhaustion is interpreted
as concurrency. A bounded signed witness avoids an unbounded witness search;
causal-relation work MUST be exact, cached, and never infer chronology from a
timestamp or traversal failure.

The feature changes resolved state. It is therefore safe only in this dedicated
room version. Existing Matrix room versions retain their existing state
resolution semantics.

## Security considerations

The certificate is a protocol security boundary. `sender == creator` or
`rejected == false` is insufficient. Implementations must require verified PDU
admission, canonical dependency selection, recursive branch-auth validation, and
a signed witness before seeding or suppressing any event.

The proposal does not solve availability of missing history, malicious omission
of concurrent branches, or consensus over peer administrators. Those remain
separate federation and governance problems.

## Open questions

1. Define the exact canonical branch-auth snapshot encoding and dependency
   closure.
2. Specify certificate invalidation/retry when a missing dependency arrives.
3. Define complete typed algebra for peer administrators, bans, leaves,
   re-joins, and access-control state.
4. Publish adversarial vectors and a multi-event model, not only pairwise
   examples.
