# MSC0501 architecture note: why federation reconciliation is shaped this way

This note accompanies MSC0501 (federation missed-PDU reconciliation) and MSC0503
(the `algebraic_v1` digest profile). It carries the material that explains the
design rather than specifying it: the algebraic argument for a group-valued
digest, the causal-closure constraint, the rejected alternatives, the security
reasoning behind specific constants, and the integration story with adjacent
MSCs.

Nothing here is normative. Where this note and either MSC disagree, the MSC
wins.

## 1. Why a group, not a filter

The digest choice is the core design decision, and it is not a performance
tuning question. It determines what the protocol is capable of expressing.

A Bloom filter is a homomorphism into an idempotent monoid. It supports
membership tests. It does not support subtraction. That single missing operation
has four consequences, each of which shows up as a protocol limitation rather
than a constant factor:

1. **Cost scales with population, not difference.** The responder must test its
   own event set element-by-element. There is no residual object to decode, so
   there is no way to make the work proportional to the number of missing
   events.

2. **False positives are silent in the reconciliation direction.** The responder
   concludes that the requester already has an event and does not send it. The
   requester has no signal that this happened. For example, a 1% false-positive
   filter over a 1-million event room requires ~1.2 MB. If the servers differ by
   750,000 events, that 1% rate silently masks ~7,500 missing events. A protocol
   whose failure mode is silence cannot be operated safely at federation scale,
   because those 7,500 holes become permanent.

3. **A larger filter restarts the exchange.** Re-sizing or re-salting produces
   an object that cannot be combined with the previous one. The work already
   done is discarded.

4. **A windowed filter creates absorbing holes.** Once a silently masked event
   falls outside the window, no later Bloom round can find it. Meanwhile a
   full-frame accumulator continues to detect the divergence forever. The
   difference is permanent, not eventual.

These are algebraic consequences of using an idempotent monoid rather than a
group. They are not tuning problems, and no parameter choice removes them.

Group-valued digests invert all four properties. Subtraction exists, so the
residual is an object in its own right; decoding it costs a function of the
difference size. Decode failure is loud, because the decoder either produces a
set that verifies against the 128-bit accumulator or reports failure. Increasing
capacity extends the exchange additively rather than restarting it. And the
accumulator over a whole frame never forgets.

### The truncation ladder

MSC0503 defines a coordinated algebraic ladder with separate layers:

```text
sigma_k(S) = (sum h(e), sum h(e)^3, ..., sum h(e)^(2k-1))
```

The resident bucket syndromes, strata estimator, and extraction sketch are views
of the syndrome construction. The wire bucket summary instead exposes
coordinated per-bucket `h_128` XOR accumulators and counts. The 128-bit room
accumulator is also a separate XOR accumulator over `h_128`, coordinated with
the syndrome layer as an integrity anchor. These are not literally one map
evaluated at different widths. This is why the resident structure is 23 KiB
rather than four separate indices, and why a peer can escalate from "are we
different?" through "how different?" to "which elements differ?" without
recomputing the population from storage.

Even powers are omitted because the Frobenius endomorphism makes them redundant
in characteristic 2: `s_{2i} = s_i^2`. This halves the wire cost for free.

The 128-bit accumulator sits above the 64-bit syndrome layer as an integrity
anchor. Sixty-four bits is enough for extraction over the population sizes in
scope, but not enough to be confident that a decoded set is the right one. The
separation lets the fast path be cheap and the verification step be strict.

## 2. Causal closure: why truncation is reported, not hidden

The second constraint is graph-theoretic rather than algebraic.

A set of recovered events can be integrated into a Matrix DAG only if it is
downward-closed relative to the recipient's store. Every returned event's
`prev_events` and `auth_events` must resolve either to something the recipient
already has, or to something else in the returned set.

Exact recovery of `K_B \ K_A` satisfies this condition automatically, because
the set difference contains everything the requester lacks within the frame.

A truncated backward walk from the frontier does not. It discovers descendants
before ancestors. When the walk hits its bound, the minimal events it returns
may have parents in neither the requester's store nor the returned set. The
transfer is not useless — the bytes are real events and persisting them makes
progress — but the DAG is not repaired until the walk reaches known ancestry.

This is the entire reason MSC0501 distinguishes `truncated: true` from a
complete result, and the reason requesters are forbidden from treating a
truncated walk as resolving the gap. Conflating the two produces a protocol that
reports success while leaving permanent holes, which is precisely the failure
mode the MSC exists to eliminate.

It is also why both modes exist. Frontier lag and interior gaps are genuinely
different failure classes:

<!-- markdownlint-disable MD013 -->

|              | Frontier lag                       | Interior gaps                           |
| ------------ | ---------------------------------- | --------------------------------------- |
| Cause        | downtime, rate limiting, partition | rejection cascades, auth-chain timeouts |
| Extremities  | stale                              | may match exactly                       |
| Interior     | intact                             | holed                                   |
| Git analogue | branch behind upstream             | none                                    |
| Right tool   | merge-base walk                    | set reconciliation                      |

<!-- markdownlint-enable MD013 -->

Git has no analogue for the second row because content-addressable storage
guarantees that possessing a commit implies possessing all ancestors. Matrix
makes no such guarantee: a server can accept an event whose parents it never
received, and does so routinely under load. A protocol that only implements the
Git-shaped half of this table cannot repair the half that motivated the work.

## 3. Rejected alternatives

### 3.1 `/make_join` as a reconciliation probe

Calling `/make_join` with a throwaway user ID yields the remote server's current
`prev_events` and `auth_events` without mutation. It requires no spec changes at
all, which makes it worth taking seriously.

It fails on four counts. It reveals only extremity divergence, never interior
gaps — the harder and more damaging case. It creates spurious `make_join`
traffic that obscures real join attempts in operational logs. It has no
conditional-request support and returns a full PDU template that must be
serialized and discarded, so it cannot be polled at the cadence anti-entropy
needs. And it abuses an endpoint designed for a different purpose, which creates
lasting confusion about intent for anyone reading the traffic later.

Latency is the practical killer: requests to a lagging server can time out
because an unknown event triggers a full index scan, and the lagging server is
exactly the one worth probing.

### 3.2 Full Merkle tree synchronization

A persistent Merkle tree over the event ID space, with recursive descent into
divergent subtrees, is the textbook anti-entropy design and is used by several
distributed databases.

The decisive objection is that Matrix room reconciliation is a graph repair
problem, not a set-membership problem. A Merkle tree can tell peers which event
IDs differ. It cannot tell them how a missing event attaches to room history,
what its `prev_events` are, or which auth-chain and state-resolution inputs are
relevant. After the comparison identifies a missing event, a graph walk is still
required — so the Merkle tree is an addition to the protocol, not a replacement
for part of it.

Supporting objections: it requires persistent auxiliary state that must be
maintained, indexed, and recovered across restarts; every new event updates the
tree, adding write amplification to already I/O-bound homeservers; and the
protocol becomes interactive and multi-round even for the small divergences that
dominate.

The algebraic sketch captures the common-case benefit — fast extraction of small
differences — without mandating a room-level Merkle structure. If a strong need
emerges, Merkle reconciliation can be introduced later as a separate
`digest_type` without disturbing the endpoints.

### 3.3 Bloom filters

Rejected for the baseline for the reasons in §1: not group-valued, so no
subtraction, false positives silent in the reconciliation direction, and a
resized filter restarts rather than extends an exchange.

Accepted as `bloom_v1`, a separately negotiated `digest_type` used only when
`algebraic_v1` reports `capacity_exceeded`. Three changes make that tradeoff
acceptable there in a way it is not for the baseline:

1. **Extremity convergence is a precondition, not an assumption.** `bloom_v1`
   MUST NOT be negotiated until both peers' forward extremities already match,
   enforced per-request by the responder rather than inferred from a prior call.
   This guarantees both peers digest the same "interior" population — without
   it, the filters would silently cover different sets.
2. **Causal closure recovers most, not all, silent misses.** If a masked
   interior event has a live descendant, admitting that descendant fails closure
   and forces an explicit fetch of the missing ancestor. This does not apply to
   events with no descendant: forward extremities are excluded by the
   precondition above, but rejected or superseded fork tips remain in `K` and
   can still be silently missed, with no backstop from the graph.
3. **A mandatory termination rule, not the causal-closure property, bounds the
   residual risk.** Retries are capped, each round uses a fresh salt, and if the
   strata-estimated delta does not strictly decrease, the requester MUST fall
   back to `algebraic_v1` bucket localization or full backfill. `bloom_v1` is a
   scale optimization layered on an exact mechanism, not a standalone
   replacement for one.

The hash derivation reuses MSC0500's `D(e)` rather than introducing a new
primitive: `h_1, h_2` are the two halves of `SHA3-256(salt ‖ D(e))`, combined by
standard double hashing. No auxiliary hash function is required.

### 3.4 IBLT and RIBLT

Invertible Bloom Lookup Tables (IBLT) can recover missing IDs directly from the
digest exchange. Both fixed-capacity IBLT and its rateless variant (RIBLT) are
in the same group-valued family as PinSketch, making the fixed-capacity choice a
decision between siblings rather than a rejection on principle. MSC0501 keeps
BCH/PinSketch-style syndromes as the baseline because of density. An IBLT cell
requires a `count`, an `id_sum`, and a `hash_sum` checksum, and the table must
be provisioned at approximately 1.35x to 1.5x the expected difference. PinSketch
requires exactly one field element per unit of extraction capacity. Because
normal federation repair is dominated by small, one-sided differences, density
and resident-memory efficiency are more important than avoiding capacity limits.

RIBLT is rejected outright rather than deferred. It would remove the need to
choose capacity up front while preserving exact, group-valued recovery, which is
a real advantage over both PinSketch and Bloom filters. But making it safe
against an adversarial peer requires its own wire format — signed fixed-width
counts, overflow bounds, authenticated chunks, finite memory prefixes, and a
termination rule — on top of a second decoder implementation (peeling-cascade,
distinct from PinSketch's Galois-field decode). MSC0501 instead handles
heavy-tailed differences with `bloom_v1` (§3.3), which reuses PinSketch's
existing `D(e)` digest and needs no new decoder at all, at the cost of giving up
exact recovery for a probabilistic one bounded by an explicit termination rule.
Keeping the total spec surface to two decoders (PinSketch and a bit-array scan)
instead of three outweighs RIBLT's exactness advantage for this MSC.

### 3.5 Server-initiated push

Servers could proactively push digests to peers when their DAG advances
(rumor-mongering) rather than waiting to be asked.

Rejected because it creates O(servers²) traffic in active rooms; it forces all
servers to process incoming digests even when already synchronized; and it
forfeits the natural rate limiting of pull, where a server reconciles only when
it chooses to and only with peers it selects.

### 3.6 Range-based set reconciliation (Merkle Search Trees)

This is a narrower case than §3.2: not a persistent Merkle tree over the whole
event ID space, but range-based reconciliation (e.g., Merkle-Radix trees
dividing a dataset into segments by timestamp or lexicographical ID), which is
highly efficient in eventual-consistency systems but fails for a different
reason against Matrix's adversarial DAG.

If segments are bounded by timestamp or depth, an attacker can craft an event
with a spoofed timestamp or deeply spoofed `prev_events` to retroactively drop a
new event into a "finalized" historical segment. This would constantly
invalidate historical hashes and force peers to re-traverse old data.

Instead, MSC0501 relies on **causal bounding** via the Frame anchor. Because
Matrix is a cryptographic DAG, the causal past is sealed by hashes. By defining
a Frame mathematically as "all events that causally succeed the anchor," the
historical boundary is cryptographically locked. Pre-join events are naturally
filtered out, and spoofed outliers are quarantined to the active concurrent
frontier rather than invalidating historical segments.

## 4. Why the constants are what they are

### 4.1 Peer fanout and the uniform floor

The peer-selection rule in MSC0501 is:

```text
Pr(select i) = (1 - epsilon) * weight_i + epsilon / N
```

The weighted term prefers servers that originated recent events (likely ahead),
servers that previously returned divergent digests (known to differ), and
backbone servers with high availability (likely to have complete DAGs). All
three are good heuristics for finding data quickly.

All three are also concentrating heuristics, and concentration is an eclipse
surface. A server that only ever reconciles with hubs can be fed a consistent
false view by whoever controls those hubs.

The uniform floor is the fix, and the analysis is straightforward. If `p` is the
probability that one selected peer is adversarial, a round with fanout `f` is
eclipsed with probability `p^f`. The expected number of rounds before sampling
at least one honest peer is:

```text
E[T] = 1 / (1 - p^f)
Pr[T > tau] = p^(f * tau)
```

With `epsilon = 0.05`, adversarial weight near 1, Sybil fraction `mu = 0.1`, and
`f = 1`, there is roughly a 63% chance of exceeding 10 rounds. At `f = 3`, about
25%. That gap is the justification for `f = 3` as a floor for active rooms.

The affordability argument closes the loop: the level-0 digest is 16 bytes plus
a count, and a synchronized peer answers with a 304. Fanout 3 on the common path
costs almost nothing, which is what makes a security-motivated constant
acceptable as a default rather than an opt-in.

### 4.2 Polling intervals

Recommended anti-entropy intervals, subject to the mandatory ±15% jitter:

| Room activity                | Interval |
| ---------------------------- | -------- |
| events in the last 5 minutes | 60 s     |
| events in the last hour      | 300 s    |
| idle                         | 3600 s   |

Back-off: after 3 consecutive polls returning identical digests, back off
exponentially per peer/room pair to a maximum of 24 hours. Any new event in the
room resets it.

The jitter requirement is normative rather than advisory because the failure
mode it prevents is correlated and self-amplifying: a large homeserver restart
or a partition heal puts thousands of room/peer pairs on the same schedule, and
the resulting wave arrives at exactly the moment the cluster is least able to
absorb it.

MSC4500 interacts here. Rooms whose recent inbound transactions carry matching
state accumulator digests have already demonstrated agreement passively, and may
back off periodic polling accordingly. The two mechanisms compose: MSC4500
provides free continuous divergence detection for active rooms, MSC0501 provides
enumeration and healing when it fires.

### 4.3 Walk bounds

`max_depth_delta` (default 5000, max 50000), `max_events` (default 10000, max
50000), `limit` (default 1000, max 10000), a 256-entry cap on the `have` set,
and a recommended cumulative budget of 100,000 inspected events per peer per
room per minute.

The per-request bounds stop a single expensive walk. The cumulative budget stops
the same attack spread across many requests each sitting just under the
per-request limit. Both are needed; either alone is bypassable.

The pre-flight check — return empty with `truncated: true` if zero `have` events
are recognized — deserves specific mention, because without it a requester can
force a maximal walk simply by fabricating `have` event IDs. The check costs a
batch of point lookups and eliminates the cheapest denial-of-service path
against the endpoint.

The design principle behind all of these: reconciliation is allowed to be
incomplete, but it must never become unbounded. Incompleteness is recoverable on
the next round. Unboundedness is an outage.

### 4.4 Capacity caps

Unbucketed sketches cap at capacity 64; bucketed aggregate capacity caps
at 4096. These are sized for the small one-sided differences expected to
dominate. A deployment routinely hitting them is telling you something — either
its peers are diverging far more than expected, or it should be using bucket
localization earlier, or it wants a rateless profile.

## 5. Accumulator forgery, stated precisely

XOR accumulators are fault-detecting, not authenticators, and it is worth being
exact about the limit rather than hand-waving at it.

XOR is linear over `GF(2)`. Any set of 129 128-bit values is linearly dependent.
An adversary with freedom over which event IDs to include can therefore
construct a nonempty subset whose accumulator is zero, by linear algebra alone —
no hash break required.

Nothing in MSC0501 or MSC0503 relies on the accumulator being binding against
such a peer. Its jobs are to detect accidental decode failure and benign desync,
both of which it does well. Every returned PDU is still verified independently
by event ID, hashes, signatures, and authorization rules, which is where the
actual security guarantee lives.

Deployments needing stronger transferable evidence can use MSC4511's Ed25519-
signed overlay attestations. A future digest profile may define negotiated
per-link salting for transmitted extraction sketches, but `algebraic_v1` leaves
that out because its fixed `h_64` mapping is intentionally interoperable. An
LtHash-style binding accumulator remains available as a future `digest_type` for
deployments willing to pay its per-update cost.

The same reasoning applies to the ETag. A `304` means the responder's view has
not changed since the requester last observed it. It does not mean the two
servers agree, and it is not evidence against a peer that is lying about its
state.

## 6. Integration map

**MSC0503 (`algebraic_v1` profile).** The digest kernel, extracted so that
MSC0501 has one specification of the field, hash derivation, and decoder
contract. MSC0502 may adapt the same machinery for EDU entries, but its current
version/content-hash protocol is not a direct consumer of this event-ID profile.
The split also enforces a layering discipline: the kernel receives only inputs
already validated as belonging to the same population, and never performs
negotiation itself. Frame validation is a transport responsibility, and putting
it in a different document makes that boundary hard to blur.

**MSC00DB (bulk backfill).** The complementary boundary-extension mechanism.
MSC0501 repairs holes _inside_ a frame; MSC00DB moves the frame boundary
_downward_. Its `edges.oldest` response field is an antichain that becomes the
next frame anchor once the returned segment is validated and ingested. When
frame negotiation returns `frame_status: "none"`, the room is handed to MSC00DB
rather than being misdiagnosed as an interior hole — this is the single most
important interaction between the two, because the alternative is a server
grinding forever on a gap that reconciliation structurally cannot close.

**MSC4500 (state accumulators).** Passive per-transaction digests give active
rooms free divergence detection. On mismatch, enumeration and healing delegate
to `room_diff` and `room_events`. See §4.2.

**MSC4511 (signed overlay attestations).** Provides the responder accountability
that XOR accumulators structurally cannot. See §5.

**MSC3706 (partial state joins).** A server mid-resync has a legitimately
incomplete view and must not advertise a normal digest for the fully joined
frame, or it will report divergence that is really its own incompleteness. Three
escapes are permitted: omit the feature flag for that room, return 409 with
`M_PARTIAL_STATE`, or advertise a distinct partial-state frame with
`X-Matrix-Partial-State: true`.

**MSC4297 (state resolution v2.1).** Orthogonal and complementary.
Reconciliation repairs the _data_ gaps that no state resolution algorithm can
address, because a correct algorithm over a holed DAG still produces a wrong
answer. Improvements to state resolution and improvements to data completeness
are not substitutes for each other.

**MSC0502 (EDU state reconciliation).** The ephemeral-state counterpart, using
version-vector comparison rather than graph reconciliation. It may later reuse
the algebraic machinery with an EDU-specific identifier profile, but remains
wire-independent from MSC0503 as currently drafted.

## 7. Operational guidance

**Deploy the accumulator before the sketch.** The 16-byte level-0 digest plus
count, with ETag support, is most of the value at a fraction of the
implementation cost. It answers "are we diverged?" for every room continuously.
Extraction can follow.

**Resident state is optional but changes the rate limits.** An implementation
that computes sketches by scanning the event store is conforming, but its cost
per request scales with room size rather than difference size, and it should
apply stricter budgets accordingly. Advertise capability honestly; a server that
accepts sketch requests it cannot serve cheaply is a server that will be used as
an amplifier.

**Instrument truncation.** `truncated: true` rates per peer and per room are the
health signal for this protocol. A room that never converges will show as
persistent truncation long before users report missing history.

**Instrument decode failure separately from capacity exceeded.** They mean
different things. Capacity exceeded means provision more. Verification failure
at adequate capacity means either a 64-bit collision — rare but real, and not
misbehaviour — or a genuine population mismatch that frame validation should
have caught. Merging the two metrics makes both uninterpretable.

**Expect frame churn in rooms with leave/rejoin cycles.** The frame anchor is an
antichain, not a single event, and repeated cycles grow it. Resident state keyed
by frame ID needs eviction policy, and `frame_status: "none"` is a normal
outcome rather than an error condition.

## 8. References

- Demers et al., "Epidemic Algorithms for Replicated Database Maintenance"
  (1987) — anti-entropy, rumor-mongering, and the pull/push tradeoff
- Birman et al., "Bimodal Multicast" (1999) — gossip under partial failure
- Eppstein, Goodrich, Uyeda, Varghese, "What's the Difference? Efficient Set
  Reconciliation without Prior Context" — strata estimator and IBLT
- Dodis et al., PinSketch — BCH syndrome set reconciliation
- Pieter Wuille, libminisketch — the byte-compatibility reference for
  `algebraic_v1`
- Git `upload-pack` negotiation — the model for `extremity` mode
