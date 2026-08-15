# MSC4500: State accumulator endpoint and transaction digests

Matrix servers replicate a room as a DAG of events and rely on state resolution
to eventually converge on a shared state. When servers diverge, the result can
be a serious nuisance. Matrix lacks an out-of-band or real-time mechanism for
state verification or re-alignment; servers often only learn of
de-synchronization once they disagree on a much later authorization failure
(e.g., another user's join is incorrectly rejected).

The absence of early detection is not merely a theoretical nuisance. A gap or
omission in a server's `/get_missing_events` response — one that leaves a remote
peer's DAG still dangling after the intended single-round-trip healing path —
pushes that peer into progressively heavier fallback behavior: piecemeal
`/state_ids` and per-event `/event/{eventId}` polling in place of one batched
fetch. Because that stuck event blocks anything built on top of it, each
subsequent event referencing it can independently trigger its own fallback
cascade, and the resulting request volume lands back on the originating server
as self-inflicted load, not merely on the requester. This is the case that
actually motivated this proposal: once a receiver's view is resolvable, a
genuine split-brain is caught on the very next transaction via a cheap digest
comparison, instead of surfacing much later as a confusing downstream
authorization failure that then triggers exactly the kind of heavy,
ambiguity-driven fallback traffic described above.

I present an "early-warning system" which rapidly confirms incremental state
consensus, or signals that divergence exists, so servers know they share the
exact same view of a room at a given point in the DAG.

This proposal does not impose any verification requirements on PDU handling. It
seeks to act as a secondary state convergence mechanism, while simultaneously
**relegating state group transitions** and naive iterative BFS implementations
to storage/retrieval with a cheap, bitwise, commutative, subtractable (supports
element removal), collision-resistant 2048-byte `LtHash16` accumulator function
[^3], [^6]. Similar additive lattice accumulators are increasingly used in
production blockchain architectures to compute real-time, incremental
cryptographic state commitments under high transactional volume [^5], [^6].

Should this proposal be accepted, for the sake of federation clarity homeservers
must embed a canonical `BLAKE2b-256` digest (of their 2048-byte room state
accumulator) in the `PUT /_matrix/federation/v1/send/{txnId}` transaction body.

## Proposal

### Relationship to existing specification

This MSC introduces two primary mechanisms to the Matrix federation protocol:

1. **Transaction-level state hashes:** A new `state_hashes` dictionary in the
   `PUT /_matrix/federation/v1/send/{txnId}` payload, allowing servers to embed
   their local, resolved state view alongside the events they are transmitting.
2. **Federation reconciliation endpoint:** A new
   `GET /_matrix/federation/unstable/tk.nutra.msc4500/state_accumulator/{roomId}?event_id={eventId}`
   endpoint that allows an out-of-sync server to query historical accumulator
   points from a healthy peer and perform a "state bisect" path without a heavy
   `make_join` or `make_knock`.

These mechanisms are additive and do not alter existing room version consensus
rules, nor do they modify the canonical structure of the signed PDU itself.

Rather than attaching hashes to individual events (which are routinely stripped,
rewritten, or relayed by intermediate servers), this proposal places the hashes
in the body of the federation transaction.

When a homeserver sends or relays a federated transaction, it calculates the sum
accumulation of the room's state exactly at the DAG tip of each included PDU.

It then collapses each PDU's vectorized state into a standard 32-byte digest and
includes them in the transaction payload as a dictionary.

### Capability discovery

Servers advertise support for this MSC via `GET /_matrix/federation/v1/version`.
Support is advertised in `unstable_features` so that backports and other
pre-standard implementations can avoid probing unsupported peers.

```json
{
  "unstable_features": {
    "tk.nutra.msc4500.state_accumulator": true
  }
}
```

A server that does not advertise this flag SHOULD be treated as not supporting
the `/state_accumulator` endpoint for routine federation repair. Receivers
SHOULD avoid repeated probes to unsupported peers; a `501 Not Implemented`
response, or a `404` response with `M_UNRECOGNIZED` or a non-Matrix body, SHOULD
be cached as an unsupported signal for at least 24 hours unless an operator
explicitly overrides the cache. The cache MUST be invalidated on any observed
change to the peer's `/version` document.

### Algorithm specification

To guarantee interoperability and collision resistance, the algorithm MUST be
implemented as follows:

1. **Input encoding.** Each entry in the room's resolved state map is serialized
   as: `len(type) || type || len(state_key) || state_key || event_id` where each
   `len()` is an unsigned 16-bit little-endian byte count of the UTF-8 field
   that follows. The `event_id` is appended raw with no length prefix (it is the
   final field, so the two prefixes already make decoding unambiguous). Length
   prefixes make the encoding injective for arbitrary field contents — including
   embedded null bytes — with no rejection or escaping rules needed. Two bytes
   per length is sufficient since no field in a valid PDU can exceed the global
   65 KB event size limit.
2. **Input expansion.** The encoded element, prefixed with the domain separation
   tag `msc4500_lthash16\x00`, is expanded to exactly 2048 bytes using the
   `SHAKE256` extendable-output function (XOF) from NIST FIPS 202:
   `expansion = SHAKE256("msc4500_lthash16\x00" || element, 2048)`. A
   fixed-width hash cannot fill the lattice; this uniform XOF expansion is
   essential for identical lane distribution. `SHAKE256` is natively supported
   across virtually all cryptographic libraries without custom parameter block
   requirements.

3. **Accumulation.** The 2048-byte expansion is interpreted as 1024
   little-endian unsigned 16-bit lanes and combined into the local lattice with
   lane-wise wrapping addition.
4. **Removal and replacement.** Removing an element is lane-wise wrapping
   subtraction of its expansion. Replacing the event for a `(type, state_key)`
   pair is one subtraction (old element) followed by one addition (new element)
   — the `O(1)` update at the heart of this proposal. A replace operation MUST
   only be accepted when the removed and added entries refer to the same
   `(type, state_key)` tuple. If the tuples differ, implementations MUST fail
   closed with a panic, exception, or equivalent hard error, and MUST NOT
   reinterpret the call as a replace, add, or remove.
5. **Initial state.** The accumulator of the empty state set is 2048 zero bytes.
6. **Collapse.** Compute the final 32-byte digest $D$ by hashing the final
   2048-byte sum lattice $S$ using `BLAKE2b-256`, hex-encoded at 64 characters:
   $$D = \text{BLAKE2b-256}(S)$$

**NOTE:** elements bind the `event_id` only, never event content. Redacting an
event therefore has no effect on the accumulator (having no effect on event ID).

**NOTE:** It is the caller's responsibility to ensure the input is really a set
[^3]. The digest allows deducting elements which were never added, and it allows
adding the same element twice (producing different digests). Due to the wrapping
math of the 16-bit lanes, adding the exact same element $2^{16}$ ($65,536$)
times will roll the accumulator's lanes back to zero, returning to the starting
digest. This degenerate state is materially unattainable when the input domain
is a resolved state map (a set whose elements all have a multiplicity of 1). The
inbound accumulator is strictly a one-way _comparative_ tool; homeserver
databases MUST remain responsible for _managing_ actual set element membership.
Homeservers MUST therefore treat their local resolved state map — keyed by
`(type, state_key)` — as the authoritative source of state membership,
replacement, and deduplication. The accumulator is a cryptographic commitment of
that map's current `(type, state_key, event_id)` assignments, not a set manager
or delta-decoder.

Implementations MAY additionally maintain an auxiliary, order-independent
**shape checksum** over the occupied `(type, state_key)` slots only, computed as
a second `LtHash16` lattice under a distinct domain separation tag so it can be
meaningfully compared across servers (see
[Shape checksum wire format](#shape-checksum-wire-format) below). Such a
checksum remains advisory and non-authoritative — it MUST NOT be used for
anything other than classifying a mismatch already detected by the main
accumulator — but because it is comparable across servers, it can help classify
whether a mismatch reflects disagreement about which slots exist or only
disagreement about which `event_id` occupies an existing slot. A
`state_key`-only checksum is not useful, because `state_key` is not unique
without the event `type`.

#### Shape checksum wire format

The shape checksum reuses the same `LtHash16` machinery as the main accumulator,
with two differences:

1. **Input encoding.** Each occupied slot is serialized as
   `len(type) || type || len(state_key) || state_key` — the same two
   length-prefixed fields as the main encoding, with the `event_id` field
   omitted entirely. Two occupying events for the same `(type, state_key)`
   therefore expand to the identical element for shape purposes, which is the
   intended behavior: replacing the event in an existing slot changes the main
   accumulator but leaves the shape lattice unchanged.
2. **Domain separation.** The encoded element is expanded with `SHAKE256` using
   the distinct tag `msc4500_lthash16_shape\x00` instead of
   `msc4500_lthash16\x00`, so shape and main lattice values can never be
   confused or cross-contaminated even though the underlying accumulation and
   collapse steps (accumulation, removal/replacement, initial state, and
   `BLAKE2b-256` collapse) are otherwise identical to
   [Algorithm specification](#algorithm-specification).

The `/state_accumulator` response (see
[Endpoint definition](#endpoint-definition)) MAY include an additional `shape`
field: the 32-byte `BLAKE2b-256` collapse digest of the shape lattice at that
same DAG point, hex-encoded identically to `digest`. Adding `shape` costs
roughly 70 bytes of JSON overhead per response. The shape checksum is
intentionally not part of `state_hashes`: it is only useful once a
main-accumulator mismatch has already been detected and a receiver is bisecting
via `/state_accumulator`, so paying its cost on every transaction would be
waste. The multiplicity assumption in
[Parameter security](#security-considerations) also holds structurally for the
shape lattice: a resolved state map has exactly one occupied slot per
`(type, state_key)` key, so every shape element likewise has multiplicity 1
regardless of room size.

### Transaction payload

Servers implementing this MSC MUST embed a `state_hashes` dictionary at the root
of the `PUT /_matrix/federation/v1/send/{txnId}` request body. `state_hashes` is
this field's eventual stable name; until stabilization, implementations MUST use
the unstable key given in [Unstable prefix](#unstable-prefix) instead, with an
identical shape. The examples in this section use the stable name for
readability. It maps the IDs of the PDUs included in the transaction to their
respective `before` and `after` digests, plus one sibling meta-key, `algorithm`
(see below). Because PDU IDs are `$`-prefixed Matrix event IDs and `algorithm`
is not, receivers can distinguish the meta-key from per-PDU entries by key shape
and MUST skip it when iterating PDU results. The `state_hashes` values always
represent the transaction sender's local resolved state, not necessarily the
origin server's (meaning relays forward their own view).

Network overhead for duplicate digests (e.g. across multiple non-state PDUs in a
batch) is collapsed by standard federation HTTP compression (gzip/brotli).

When a PDU lists multiple `prev_events`, the `before` state is the output of
state resolution (v2/v2.1) applied across the states at each of those events —
i.e. the same resolved state the server would use to authorize the PDU. The
`after` state is `before` with the PDU applied, if it is an accepted state
event; otherwise `after` equals `before`. If a server does not know about a PDU
in the given `prev_events`, they shall omit it entirely from the dictionary.

- `algorithm`: A single top-level string identifying the digest algorithm used
  for every entry in this transaction's `state_hashes` (e.g. `lthash16`, see
  [Algorithm specification](#algorithm-specification)). One value governs the
  whole transaction; mixing algorithms within a single transaction serves no
  purpose and is not supported. A receiver that does not recognize the algorithm
  MUST silently skip hash validation for the entire transaction, the same as any
  other deferral case in the [Receiver contract](#receiver-contract) — this
  preserves forward compatibility if a future revision introduces a new digest
  family (e.g. a wider lattice or a different XOF) without causing receivers on
  the old algorithm to raise false mismatch alarms against upgraded senders.
- `before`: The 32-byte digest of the room state evaluated exactly at the given
  PDU's `prev_events`, excluding and preceding the given event.
- `after`: The 32-byte digest of the room state after the current PDU is
  applied. (For non-state events, this will be identical to `before`).
- `n_before`: An unsigned integer representing the exact number of elements in
  the room's resolved state map at the `before` DAG point.
- `n_after`: An unsigned integer representing the exact number of elements in
  the room's resolved state map at the `after` DAG point (identical to
  `n_before` for non-state events).

**Sender-side partial state.** A server MUST NOT emit a guessed or approximated
digest. If a sending or relaying server cannot compute the resolved state at a
given PDU's position — because it is itself operating under Partial State
(MSC3706), is missing ancestry, or holds an unpersisted accumulator it declines
to backfill on demand — it MUST omit that PDU's entry from `state_hashes`
entirely rather than emit a best-effort guess. An absent entry and an entry
omitted for this reason are indistinguishable to the receiver, which is
intentional: both mean "no assertion is made about this PDU's state," and the
receiver's deferral rules in the [Receiver contract](#receiver-contract) already
handle a PDU with no `state_hashes` entry. Transactions containing only
non-state-altering PDUs, or only PDUs a server declines to assert on, MAY
therefore carry an empty (or entirely absent) `state_hashes` dictionary; the two
are equivalent.

```json
{
  "origin": "example.com",
  "pdus": [
    {
      "type": "m.room.message",
      "event_id": "$sample_pduid_abc123def456",
      "sender": "@alice:example.com",
      "content": {
        "body": "Hello world",
        "msgtype": "m.text"
      }
    }
  ],
  "state_hashes": {
    "algorithm": "lthash16",
    "$sample_pduid_abc123def456": {
      "before": "a85dfe1d480705482f37d582ffa27611117b577f8734532a5a6379bc666b2104",
      "after": "a85dfe1d480705482f37d582ffa27611117b577f8734532a5a6379bc666b2104",
      "n_before": 2,
      "n_after": 2
    }
  }
}
```

### Network efficiency

To avoid event bloat, the full `LtHash16` lattice state (2048 bytes) is **never
explicitly transmitted over transactions.**

Transmitting only the collapsed 32-byte digest keeps payload footprints small.
Adding both `before` and `after` digests plus both cardinality counts consumes
approximately 200 bytes of JSON overhead per PDU in the transaction.

### Receiver contract

Each server independently maintains its own `LtHash16` lattice in local storage.

When a server catches a `/send` transaction containing the `state_hashes`
payload, it collapses its own local lattice at that exact DAG point using fast
bitmap operations, hashing it down to a canonical 32-byte `BLAKE2b-256` digest.
If the local digest matches the incoming one, all systems are nominal.

If digests mismatch, servers SHOULD log an error or warning message of the state
split. The receiver can automatically trigger a background `/get_missing_events`
or perform a state bisection (see
[Reconciliation (bisecting forks)](#reconciliation-bisecting-forks)) with
authoritative servers, while replying to the sender with the mismatched digest
embedded in a `state_hash_mismatch` dictionary as part of the PDU's processing
result and the `200 OK` response. Unknown keys in per-PDU result objects are
silently ignored by existing implementations, so adding `state_hash_mismatch` is
backwards-compatible. `state_hash_mismatch.algorithm` echoes back the algorithm
identifier from the triggering transaction's `state_hashes.algorithm`, so a
sender receiving the mismatch can tell which digest family the receiver
evaluated against.

```json
{
  "pdus": {
    "$sample_pduid_abc123def456": {
      "state_hash_mismatch": {
        "algorithm": "lthash16",
        "expected_after": "b85dfe1d480705482f37d582ffa27611117b577f8734532a5a6379bc666b2104",
        "received_after": "a85dfe1d480705482f37d582ffa27611117b577f8734532a5a6379bc666b2104"
      }
    }
  }
}
```

Mismatch handling SHOULD be deduplicated per room (i.e. the first detection
triggers logging/bisection, but subsequent mismatching transactions within a
reasonable cooldown period are deprioritized to limit logger output and network
activity). Note that if a receiving server **rejects** an incoming state event
due to auth/power-level rules, their `after` hash will instantly (and correctly)
mismatch the sender's `after` hash. This mechanism instantly detects split-brain
authorization failures.

Homeservers operating under Partial State (MSC3706) MUST silently defer hash
validation for that room. They cannot compare state to emit warnings or trigger
bisection (until the room state is fully synchronized).

The emphasis here is on agility: if a receiver cannot validate the `before` and
`after` hashes readily (e.g., from an in-memory LRU cache or a point database
lookup), they MUST defer the verification pipeline. This same deferral applies
whenever a receiver cannot yet resolve state at the relevant DAG point at all —
for example, while it is still mid-gap behind a `/get_missing_events` shortfall
(see the motivating case in the introduction): an unresolved gap MUST be
silently deferred like any other not-yet-resolvable point, never treated as a
positive signal either way. This proposal accordingly does not repair a broken
`/get_missing_events` implementation; what it changes is only the case where a
receiver's view _is_ resolvable, by catching a genuine split-brain on the very
next transaction instead of letting it surface later as a confusing downstream
authorization failure.

A mismatched or deferred hash does not block the PDU; it is still processed
under standard rules. Whether homeservers implements an automated healing
pipeline or merely log the divergence for admin intervention is left as an
implementation detail.

### Endpoint definition

`GET /_matrix/federation/v1/state_accumulator/{roomId}?event_id={eventId}`

This is the endpoint's eventual stable name. Until this MSC is stabilized,
implementations MUST serve it at the unstable path given in
[Unstable prefix](#unstable-prefix) instead; the request/response shapes below
apply identically to both paths.

Returns the raw lattice for the room state immediately **after** `eventId` is
applied (the `after` accumulator of that PDU).

**Response (200):**

```json
{
  "event_id": "$sample_pduid_abc123def456",
  "algorithm": "lthash16",
  "lattice": "<base64url, unpadded, 2048 raw bytes>",
  "n_state_events": 2,
  "digest": "99d3ed0ae604d2fb5849f7280062e27ecea4425b64b25190e067e3d6a755680c",
  "shape": "9aab4968674238606d7be6c20bf85c2ecd7e7ae19f5c63313ed3c456a91d432d"
}
```

`shape` is OPTIONAL and, when present, is the collapse digest of the auxiliary
shape lattice described in
[Shape checksum wire format](#shape-checksum-wire-format) at this same DAG
point. A server that does not maintain the shape lattice MUST omit the field
rather than fabricate a value.

The receiver MUST verify that `BLAKE2b-256(lattice)` equals `digest` before
using the lattice; a mismatch indicates the response is malformed or tampered
with, and MUST be discarded.

**Errors:** `404 M_NOT_FOUND` if the server does not hold resolved PDU state at
that event (unknown event, outlier, purged history, bug). `403 M_FORBIDDEN` if
the requesting server is denied by `m.room.server_acl`, or if the requesting
server was not a participant in the room at the queried event — this endpoint
MUST apply the same historical-visibility rule as
`GET /_matrix/federation/v1/state_ids/{roomId}`: current room membership alone
is not sufficient to authorize a query about an arbitrary historical point,
since a server that joined recently can otherwise use this endpoint to learn a
digest and cardinality count for epochs before it joined. Mirroring `/state_ids`
costs nothing here: divergence-healing only needs historical accumulator points
within the requester's own membership epochs, since a server cannot have locally
computed an accumulator for an epoch it was never present for in the first
place.

**Rate limiting:** Servers SHOULD rate-limit per peer per room. Bisection
requires `O(log ΔD)` sequential network calls, so a short burst allowance (e.g.
30 requests) with a sustained rate of ~1/second is a reasonable default. The
response is ~3 KB; amplification risk is negligible.

### Other affected endpoints

The introduction of a cryptographically verifiable state accumulator enables
several zero-cost optimizations across the existing Matrix Client-Server and
Server-Server APIs.

- **`GET /_matrix/federation/v1/state/{roomId}`**,
  **`GET /_matrix/federation/v1/state_ids/{roomId}`**, and
  **`/_matrix/client/v3/rooms/{roomId}/state`** Currently, homeservers must
  fully materialize the room state to serve these endpoints, which is an
  expensive $O(S)$ operation for large rooms. These endpoints become instantly
  cacheable via standard HTTP semantics. Servers SHOULD include the digest as an
  `ETag` header on `200 OK` responses so standard conditional-request semantics
  hold end-to-end. Requesters SHOULD include the 32-byte accumulator digest in
  the `If-None-Match` header. The receiving server simply compares this against
  its own local LRU cache of the requested state digest. If they match, the
  server immediately returns `304 Not Modified`, bypassing the legacy database
  traversal and JSON serialization of tens of thousands of state events.

## Reconciliation (bisecting forks)

When the 32-byte digest triggers a mismatch alarm, the receiving server knows at
least one party is desynchronized. The receiver performs homomorphic subtraction
against the sender's full accumulator lattice.

MSC4511 and MSC4521 complement this lookup primitive rather than replace it:
MSC4511 can provide graph metadata and ancestor hints for choosing candidate
repair points, and MSC4521 can reconcile known event sets after a gap has been
identified. Neither proposal exposes historical resolved-state accumulators —
that remains `/state_accumulator`'s role — but see
[Synergy with MSC4521](#synergy-with-msc4521-state-set-sketch-reconciliation)
below for an optional sketch-based accelerant to the bisection walk itself.

The delta lattice tells you _that_ you've diverged and lets you **bisect** to
_where_. Because both servers can produce digests at historical DAG points, the
receiver can query accumulators at $O(\log \Delta D)$ depth (topological
bisection—similar to `git bisect`—over the known `prev_events` graph or auth
chain) to find the earliest event where the digests diverged. This endpoint
defines the queryable primitive — the accumulator at a given event — and
deliberately leaves the traversal strategy to the implementation, since the DAG
is a partial order rather than a line: unlike `git bisect`'s single linear
history, a divergence between two forked branches that each contributed
independent drift may not reduce to one earliest event at all, but to a frontier
of events. The `O(log ΔD)` figure describes the linear-history case;
implementations bisecting across genuinely forked histories should expect the
earliest-divergence result to be a small set of candidate events rather than a
single one, and should treat this proposal as defining the lookup primitive, not
the search algorithm over it. For historical PDUs where a server has no stored
accumulator (and deems retroactive computation prohibitive), it responds
`404 M_NOT_FOUND`; the bisecting requester then treats the oldest event for
which both sides _can_ produce accumulators as a lower bound on the divergence
point and proceeds from there.

It is important to note that the delta lattice cannot name events you have never
seen—a lattice sum isn't invertible to its summands (the property that makes it
collision-resistant). Once the exact divergence point is isolated via bisection,
enumeration and healing are delegated to MSC0501 [Gossip-based federation room
reconciliation] and its `/room_diff` and `/room_events` endpoints. Attempting to
recover the missing `+12 / -18` events directly from the accumulator difference
is computationally intractable in the general case; the accumulator is for
verification, not reconciliation. Cheap delta discovery requires separate
set-reconciliation structures or timeline traversal, such as IBLT-style
state-set reconciliation, Merkle search trees over `(type, state_key)` slots, or
Matrix-native lowest-common-ancestor traversal across state-altering events.

If both servers maintain the shape checksum and exchange it via the `shape`
field of the `/state_accumulator` response (see
[Shape checksum wire format](#shape-checksum-wire-format)), a bisecting receiver
can compare its own locally-computed shape digest against the sender's `shape`
value at the same DAG point to classify a mismatch already detected by the main
accumulator:

- Matching shape checksum + mismatching main accumulator indicates mutation
  drift: both servers agree on the active `(type, state_key)` slots but disagree
  on one or more occupying `event_id`s.
- Mismatching shape checksum + mismatching main accumulator indicates structural
  drift: the servers disagree on which `(type, state_key)` slots exist at all.

This classification is only ever advisory context for an operator or an
automated bisection strategy; it does not change what the main accumulator
already proved (that a mismatch exists), and a server that omits `shape` simply
forgoes classification, not detection.

Furthermore, this MSC cannot detect omissions in messages, redactions, or other
non-state-altering events. For this capability, it fully defers to MSC0501.

## Synergy with MSC0501 (event set reconciliation)

This proposal and MSC0501 (`room_digest` / `room_diff`) solve fundamentally
different sets. MSC4500's accumulator covers the room's _current resolved state
set_ at arbitrary DAG positions. MSC0501's algebraic digest and bounded
extremity fallback cover the _known event set_ (accepted events and retained
rejection tombstones across the frame).

Because state divergence implies event-set divergence (with the converse _often_
also holding true), the two proposals nicely complement each other:

1. **Detect (MSC4500, passive, free):** Every `/send` carries before/after
   digests. Active rooms get continuous state-consistency checks with zero extra
   round trips.
2. **Bisect (MSC4500, active):** On mismatch, optional bisection via the
   `/state_accumulator` endpoint alerts to the divergence point.
3. **Reconcile (MSC0501):** `room_diff` identifies missing event IDs and
   `room_events` retrieves their PDUs and auth chains. The receiver admits
   verified events to its DAG, then recomputes its resolved state locally;
   remote state digests and state maps are never write targets.

Because MSC4500 gives active rooms free passive detection, MSC0501's periodic
polling can back off significantly for rooms with recent inbound transactions.

## Synergy with MSC4521 (state-set sketch reconciliation)

[Reconciliation (bisecting forks)](#reconciliation-bisecting-forks) above treats
`/state_accumulator` as the only lookup primitive: once a mismatch is known, the
receiver walks the DAG at `O(log ΔD)` depth to isolate a divergence point, then
hands enumeration off to MSC0501. Servers that also implement MSC4521's
State-map binding profile (see
[Element derivation](../4521-algebraic-set-reconciliation.md#element-derivation)
and
[State-map binding](../4521-algebraic-set-reconciliation.md#state-map-binding))
MAY skip or shorten that walk: instead of bisecting to a point and enumerating
from there, the two sides exchange a PinSketch syndrome sketch directly over the
resolved state maps at the already-known mismatched `before`/`after` DAG
position, and decode the symmetric difference in one round trip.

This is an accelerant to bisection, not a replacement for it, and it is
capability-gated the same way `/state_accumulator` itself is (see
[Capability discovery](#capability-discovery)): a receiver that does not
advertise MSC4521 support falls back to tree-walk bisection exactly as today.
This section defines nothing new about _when_ to reconcile, only a faster path
for servers that already support both proposals.

### Where the sketch travels

The sketch MUST NOT be attached to per-PDU `state_hashes` entries, and MUST NOT
be sent unconditionally with every transaction. A 32-entry strata estimator
alone is a fixed 2048 bytes — the same size as the raw `LtHash16` lattice this
proposal exists specifically to avoid transmitting (see
[Network efficiency](#network-efficiency)). Sending it per-PDU, or even once per
transaction, reproduces the exact cost this proposal was written to eliminate.

Instead, the sketch is requested only after a mismatch is already known, as an
optional addition to the existing escalation path:

- As an added field on `state_hash_mismatch` (see
  [Receiver contract](#receiver-contract)): a receiver that already knows it
  diverged MAY include a `strata_estimator` alongside the mismatch report,
  letting the sender decide in one extra round trip whether a full sketch
  exchange is worth provisioning.
- As a query option on `/state_accumulator`, e.g. `?sketch=true`, returning a
  PinSketch syndrome sketch of the resolved state map at that DAG point instead
  of, or alongside, the raw accumulator.

### Sizing and fallback

Sketch provisioning follows MSC4521's own strata-estimator rule: size the
initial extraction request from $\hat d$, escalate on `capacity_exceeded`, and
treat a `low_confidence` or `null` estimate as a signal to fall back to
tree-walk bisection or a bulk `/state_accumulator` fetch rather than
provisioning a large sketch speculatively. This mirrors the existing
[Reconciliation](#reconciliation-bisecting-forks) fallback for historical points
with no stored accumulator: both routes degrade to the same bulk-fetch floor,
they just differ in how cheaply they resolve the common case.

Because state-map divergence tends to cluster — a single bad state-resolution
outcome on one branch typically drags a run of `(type, state_key)` slots along
with it, rather than dropping independently at random the way missed PDUs often
do — implementations SHOULD NOT assume MSC4521's strata-estimator bounds,
calibrated primarily against event-set churn, transfer unchanged to
resolved-state divergence. Operators adopting this section are encouraged to
validate estimator tightness against their own state-map divergence patterns
before relying on `low_confidence` thresholds tuned for the event-set case.

### Non-goals

This section does not change what MSC4500 detects or when: the `before`/ `after`
`LtHash` digests remain the sole free, passive, per-transaction signal. Nothing
here is required to implement the base proposal; a server MAY implement full
`/state_accumulator` bisection and never implement this section at all.

## Implementation notes

The natural storage model is one 2048-byte lattice per state group. Creating a
new state group from a delta is one subtraction plus one addition against the
parent's lattice — `O(1)`, no chain walk and no full state materialization.
Historical `/state_accumulator` queries then reduce to the existing event (state
group lookup plus a single row or cache read).

Servers without persisted lattices can compute them on demand per-event during
naive delta chain traversals or iterative BFS sweeps (accumulating the already
materialized state in CPU cache and persisting the accumulator, thereby
obviating any need for traversals of that delta chain during future point
lookups or state group transitions).

### Fast local divergence lookup (optional)

Because state groups form an append-only forest in the common case (one delta
parent per group), implementations MAY maintain a binary-lifting ancestor index
over that forest — a jump-pointer table doubling in stride, populated
incrementally as each group is created — to compute the lowest common state
group between two DAG tips locally in $O(\log n)$, with no network round trip.
This is independent of the `LtHash16` accumulator: the accumulator detects
_that_ divergence exists; the jump table finds _where_, locally, before falling
back to the `/state_accumulator` bisection endpoint in
[Reconciliation (bisecting forks)](#reconciliation-bisecting-forks) for cases
where the common ancestor predates local retention.

An Euler tour over this same forest, combined with a sparse-table RMQ, would
give $O(1)$ instead of $O(\log n)$ queries, but requires the full tour to be
known in advance and is expensive to keep valid under continuous appends. Binary
lifting is the better fit here: each new group's jump-pointer row is computed in
$O(\log n)$ purely from its parent's row, with no rebuild of existing structure.

**Caveat: the storage tree is not always immutable or fully connected.** This
optimization assumes state-group parent pointers are stable once written. In
practice this does not always hold, and a jump-pointer table naively built on
top of it can go stale or silently report a wrong or non-existent answer:

- **Compaction/compression.** Background jobs that shorten long delta chains
  (used by implementations such as Synapse) can rewrite an existing group's
  parent pointer after creation. Ancestor-table entries downstream of a
  re-parented group become stale and MUST be invalidated or rebuilt, not trusted
  as-is.
- **Partial-state joins (MSC3706).** Provisional state groups built from partial
  state are replaced once full state resync completes. Ancestor tables built
  against provisional groups MUST be discarded, not merged into the post-resync
  tree.
- **Fork healing through state resolution.** A resolved state can be logically
  derived from two or more branches, even though storage typically records only
  one delta parent for compactness. An ancestor table built purely from
  delta-parent pointers reflects only that recorded lineage; it MAY report a
  lowest common state group that is a storage-layer simplification of the true
  derivation history, and MUST NOT be treated as an authoritative substitute for
  the accumulator/bisection outcome.
- **Local disconnection.** A server's stored state groups are not guaranteed to
  form one connected tree at all times — backfill gaps, rejoining after a long
  absence, or independent partial-state resyncs can leave disconnected
  components until intervening history arrives. A lookup between groups in
  different components MUST return "unknown," not "no common ancestor," and fall
  back to network-based bisection.

A related local-only technique — isolating _which_ `(type, state_key)` tuples
diverged between two locally-held state maps, in time proportional to the
divergence rather than to room size, using a Merkle-ized prefix trie — is purely
a storage-engine indexing choice with no wire-visible effect. In brief: the
local index stores structurally-shared trie nodes so identical subtrees can be
skipped wholesale and only differing prefixes are recursed into. It is kept out
of this MSC because it is a fork-local implementation note, not part of the wire
contract.

### State identifiers and local storage optimizations

Locally, an accumulator makes state identity path-independent instead of
path-dependent (cf. Solana's "Accounts Lattice Hash" [^5], which computes
rolling `O(1)` state-root identities the same way). Today's homeservers trade
read-time CPU against write-time I/O: Synapse's incrementing "state group" IDs
need cache-heavy comparisons or graph traversal to tell two groups apart, and
rely on background workers to deduplicate converging groups; Conduit-derived
implementations hash sorted state lists (`ShortStateHash`) for cheap reads but
must re-materialize, re-sort, and re-hash the full state vector on every write,
since `BLAKE2b-256` isn't homomorphic.

An `LtHash16` accumulator's 32-byte digest gives three optimizations instead:

1. **`O(1)` state progression.** A new state group's digest is the parent's
   cached lattice with one subtraction and one addition, collapsed — no delta
   walk, no re-sorted materialization, independent of room size or fork depth.
2. **Free deduplication.** Lattice addition is commutative, so `Base + X + Y`
   and `Base + Y + X` collapse to the same digest regardless of DAG-branch
   ordering. Convergent branches can be deduplicated to one state group ID via a
   plain unique-index or point lookup, with no dictionary comparison.
3. **Fast-path state resolution.** State resolution v2/v2.1's first step —
   checking whether diverging tips actually differ — becomes a 32-byte
   comparison; equal digests mean no conflict set, skipping the algorithm
   entirely.

Delta chains are still needed to materialize state for client APIs and to
isolate the actual conflict set during resolution (a homomorphic hash can't be
inverted to name its summands); the accumulator only removes them from the
write-path and the fast-path equality check.

## Potential issues

### Direct-hop survival (ease of audit)

Because the hashes are attached to the transaction body rather than the
individual PDUs, they only survive the direct origin-to-first-hop transmission.
If an event is relayed, or fetched later via `/backfill`, the hashes are
missing.

This is an acceptable constraint: the direct `/send` hop is where real-time
early-warning detection matters. `unsigned` suffers the same survival gap for
the same reason — routinely stripped or rewritten by intermediate servers.

### False alarms (federation signal noise and DoS vectors)

If a malicious, misconfigured, or malfunctioning server transmits mismatched
digests in a transaction, it could trigger state resync loops for the receiver.

**Mitigations:**

1. **Rate-limiting:** Receiving servers implementing automated remediation
   methods SHOULD rate-limit out-of-band state sync requests triggered by
   mismatching hints. Repetitive warning logs are unnecessary and should be
   subject to a cool-down period.

2. **Reputation:** Servers implementing Bandit-based peer scoring on manually
   triggered or heavily federated endpoints SHOULD factor state accuracy into
   their weighting. If a peer consistently transmits mismatched digests that do
   not reflect the actual resolved state or differ too wildly from the perceived
   majority or authoritative ground truth, the receiver should temporarily
   decrement that peer's reputation score and the worthiness of their hints.

## Alternatives

### Hashes in the signed PDU

The primary alternative is placing the state hash directly into the signed
payload of the event, enforcing it as a protocol-level requirement.

**Disadvantages:**

- **PDU bloat:** PDUs already suffer from excessive meta-data.
- **Leads to confusion:** Matrix allows for servers being slightly out of sync.
  Implying consensus on every event leads to ambiguity (situations even arise
  where administrative power events can rewrite formerly correct state).
- **Compatibility:** Modifying the signed PDU alters the event's reference hash
  (unless the definition of "canonical event JSON" is further complicated). This
  requires a global room version upgrade and excludes older homeservers. It is
  possible this approach will be interleaved with MSC4242 (State DAGs), which
  _does_ make intentional PDU format changes intended for a new room version.

A transaction-level approach achieves similar diagnostic goal without friction.

### Hashes in the `unsigned` dictionary

**Advantages:**

- **Accessibility and persistence:** Generally, `unsigned` is more durable. This
  allows some degree of trustworthy relaying of the origin's viewpoint.

**Disadvantages:**

- **Tampering:** The `unsigned` dictionary is not covered by any signature,
  allowing silent modification in transit.
- **Survival:** Like transaction-level hashes, `unsigned` data is frequently
  stripped by relays or backfill endpoints, offering no structural advantage
  over transaction-level hashes.

By placing these digests in the `PUT /send` request body, they are automatically
protected by the sending server's `X-Matrix` authorization headers, providing
free tamper-resistance on the primary hop. Consequently, relaying servers assert
their own perceived state digest rather than blindly forwarding the origin
server's viewpoint — limiting the propagation of unverified hints and offering
broader auditability of major servers that frequently act as relays.

## Security considerations

Homeservers MUST NEVER use a _remote_ accumulator digest (received from a peer
via `/send` or `/state_accumulator`) as a source of truth to construct, modify,
or authorize state. Local state resolution MUST proceed normally as the sole
authoritative driver of state convergence. Locally-computed lattices, derived
from the server's timeline and resolved state, _are_ safe for any internal
optimizations and representations described in this proposal (state group
identity, fast-path deduplication, short-circuiting state resolution).

The hashes are diagnostic only. Servers still rely exclusively on their internal
state to judge soft-failures; any change to federation prioritization based on a
mismatch is an implementation's own discretion.

**State-isolation assurance:** Even a successful collision attack cannot corrupt
room state. Because remote digests are never used to construct, modify, or
authorize local state maps, the worst outcome of a forged digest is a missed
mismatch alarm — the adversary fools the receiver into believing sync is nominal
when it is not. No state is injected, no auth decisions are affected, and the
receiver's local database remains unaffected.

**"Honest hash" bypass:** It is important to contextualize the threat model. If
a malicious server wishes to hide a split-brain partition, it does not need to
find a lattice collision. Because Matrix room events are public, a malicious
server can simply compute the correct `LtHash` of the _honest_ room state and
transmit that correct hash in their federation payloads while secretly keeping a
diverged database. `LtHash` must therefore be understood as a highly efficient
fault _detection_ mechanism for honest-but-buggy servers and natural network
partitions, _not_ an authoritative proof of a peer's internal room state.

**Parameter security:** The lattice parameters ($L = 1024$ lanes, $q = 2^{16}$)
are the instantiation analyzed by Lewi et al. [^2], with an estimated security
level in excess of 200 bits against known lattice-reduction [^4] and generalized
birthday (k-list) attacks [^1]. This analysis requires that no element appear
with multiplicity $\ge 2^{16}$ in the accumulated multiset. MSC4500 satisfies
this structurally: the input is a resolved state _map_, which holds exactly one
`event_id` per `(type, state_key)` key — every element has multiplicity 1,
regardless of total room size. Total state cardinality ($N$) is _not_ bounded by
$2^{16}$; massive rooms are fully supported.

The `n_before` and `n_after` payload fields are diagnostic only — they help a
receiver gauge the magnitude of a divergence when choosing between bisection,
full resync, and inaction. They MUST NOT be used as a validation shortcut:
digest comparison is the sole equality check.

## Test vectors

To assist implementers, the following test vectors are provided. Scenarios 1-4
use the main accumulator: `SHAKE256` element expansion prefixed with the domain
separation tag `msc4500_lthash16\x00`, 16-bit little-endian wrapping lane
addition/subtraction, and standard `BLAKE2b-256` collapse digest. Scenario 5
uses the same machinery for the auxiliary shape lattice, with the distinct
domain separation tag `msc4500_lthash16_shape\x00` and the `event_id`-less
encoding described in [Shape checksum wire format](#shape-checksum-wire-format).

### Empty state

The starting lattice $S_0$ is 2048 bytes of all zeros.

- Lattice $S_0$ prefix (first 16 bytes): `00000000000000000000000000000000`
- Collapse digest:
  `200823e5158b3774c11b5c61850ada762f8264144a9bebec3ebac5a2adde67b8`

**This collapse digest is a reserved sentinel, not room-specific evidence.**
Every room shares this exact value before its `m.room.create` event is applied —
it is a global constant of the algorithm, not a per-room commitment. By
definition, the `before` digest of a room's create event is always this
constant. Receivers MUST NOT treat digest equality at the empty state as a
meaningful confirmation of anything about a specific room; it confirms only that
both sides implement the same empty-state convention. This value doubles as a
free extra test vector for the `before` digest of any room's create event.

### Scenario 1: one element (addition)

Add event `m.room.member` with state key `@alice:example.com` and event ID
`$event_1`.

- Raw encoded element:
  `0d006d2e726f6f6d2e6d656d626572120040616c6963653a6578616d706c652e636f6d246576656e745f31`
- Element 1 expansion prefix (first 16 bytes of
  $SHAKE256(\text{tag} \parallel \text{el}_1)$):
  `d72df88a72ff61da6b2287649ff6001c`
- Lattice $S_1$ prefix (first 16 bytes): `d72df88a72ff61da6b2287649ff6001c`
- Collapse digest:
  `3bcd9f595b4b5c7095b300ec5cf37ff1ff3f79400643f7ba66171e150ddb6606`

### Scenario 2: add-then-remove (element removal)

Subtracting the expanded element for `$event_1` from lattice $S_1$ returns the
accumulator to the empty state.

- Lattice $S_{\text{back}}$ prefix (first 16 bytes):
  `00000000000000000000000000000000`
- Collapse digest:
  `200823e5158b3774c11b5c61850ada762f8264144a9bebec3ebac5a2adde67b8`

### Scenario 3: two elements

Starting from $S_1$, add event `m.room.name` with empty state key `""` and event
ID `$event_2`.

- Raw encoded element: `0b006d2e726f6f6d2e6e616d650000246576656e745f32`
- Element 2 expansion prefix (first 16 bytes of
  $SHAKE256(\text{tag} \parallel \text{el}_2)$):
  `8c9d4997da61e28d7e6b83255fff064e`
- Lattice $S_2$ prefix (first 16 bytes): `63cb41224c614368e98d0a8afef5066a`
- Collapse digest:
  `99d3ed0ae604d2fb5849f7280062e27ecea4425b64b25190e067e3d6a755680c`

### Scenario 4: instant replacement

Starting from $S_2$, replace the membership event for `@alice:example.com` with
event ID `$event_3`. This is performed by subtracting the expansion for
`$event_1` and adding the expansion for `$event_3`.

- Raw encoded element for `$event_3`:
  `0d006d2e726f6f6d2e6d656d626572120040616c6963653a6578616d706c652e636f6d246576656e745f33`
- Element 3 expansion prefix (first 16 bytes of
  $SHAKE256(\text{tag} \parallel \text{el}_3)$):
  `9dd1af20e6ee125f8e98969793b8c650`
- Lattice $S_3$ prefix (first 16 bytes): `296ff8b7c050f4ec0c0419bdf2b7cc9e`
- Collapse digest:
  `8b611750bb056a38f9e3f9fcc74ae1f0771f12ade0daecc6963e302d15f8e67f`

### Scenario 5: shape checksum (mutation drift)

The shape lattice for the same state progression as Scenarios 3 and 4, using the
domain separation tag `msc4500_lthash16_shape\x00` and the
`len(type) || type || len(state_key) || state_key` encoding (no `event_id`) from
[Shape checksum wire format](#shape-checksum-wire-format).

At the point of Scenario 3 (two elements, `$event_1` and `$event_2`):

- Raw encoded shape element for the membership slot:
  `0d006d2e726f6f6d2e6d656d626572120040616c6963653a6578616d706c652e636f6d`
- Raw encoded shape element for the name slot: `0b006d2e726f6f6d2e6e616d650000`
- Shape lattice prefix (first 16 bytes): `02d418079bc5b05d4b9a61f633b40dfb`
- Shape collapse digest:
  `9aab4968674238606d7be6c20bf85c2ecd7e7ae19f5c63313ed3c456a91d432d`

At the point of Scenario 4 (membership slot's occupying event replaced by
`$event_3`), the shape lattice is **unchanged**: the shape element for the
membership slot depends only on `m.room.member` and `@alice:example.com`, so
subtracting and re-adding it nets to zero.

- Shape lattice prefix (first 16 bytes): `02d418079bc5b05d4b9a61f633b40dfb`
- Shape collapse digest:
  `9aab4968674238606d7be6c20bf85c2ecd7e7ae19f5c63313ed3c456a91d432d`

This is the canonical example of the mutation-drift classification in
[Reconciliation (bisecting forks)](#reconciliation-bisecting-forks): the main
accumulator digest changes between Scenario 3 and Scenario 4 (`99d3ed0a…` →
`8b611750…`), while the shape digest stays identical (`9aab4968…` in both),
correctly signaling that the occupied slots did not change — only which event
occupies one of them.

## Unstable prefix

For experimental implementations, the features should be referred to using the
following unstable identifiers. Everywhere else in this document,
`state_hashes`, `state_hash_mismatch`, and the `/state_accumulator` endpoint are
written under their eventual stable names for readability; unstable
implementations MUST substitute the identifiers below in the wire format
instead, with identical shapes and semantics. The capability flag is
`tk.nutra.msc4500.state_accumulator`, and the unstable federation endpoint is
`/_matrix/federation/unstable/tk.nutra.msc4500/state_accumulator/{room_id}`.

- The transaction payload key: `tk.nutra.msc4500.state_hashes` (replacing
  `state_hashes` at the root of the `/send` request body)
- The per-PDU mismatch result key: `tk.nutra.msc4500.state_hash_mismatch`
  (replacing `state_hash_mismatch` in the `/send` response body)
- The reconciliation endpoint:
  `GET /_matrix/federation/unstable/tk.nutra.msc4500/state_accumulator/{room_id}`
  (replacing `GET /_matrix/federation/v1/state_accumulator/{roomId}`)

## Backwards compatibility

This proposal is fully backwards-compatible:

- Unknown transaction keys (`state_hashes`) are silently ignored by existing
  servers, per current federation behavior.
- The unstable reconciliation endpoint returns a `404` response with
  `M_UNRECOGNIZED` or a non-Matrix body on non-implementing servers, which
  callers treat as an "unsupported" signal.
- No room version consensus rules are modified.

## Dependencies

This proposal currently has no known dependencies. The optional
[Synergy with MSC4521](#synergy-with-msc4521-state-set-sketch-reconciliation)
section depends on MSC4521's State-map binding profile, but implementing it is
not required to implement this proposal.

## Open questions

- Impact on or relevance to partial joins (MSC3902)?
- **Large or irrevocably broken rooms:** How should servers handle large or
  irrevocably broken rooms?
- **Client-Server impact:** What is the impact of a state bisect on the
  client-server relationship? Specifically, how should servers handle detecting
  missed events that fell through over the Client-Server `/sync` v5 endpoint?
  (See future work).
- **Self-verification:** Could servers perform self-verification (e.g. checking
  checksums of the result) before signing off on it? Is there value in auditing
  one's own state (either on-the-fly or on past events)?
- **Future reconciliation structures:** MSC4521's State-map binding (see
  [Synergy with MSC4521](#synergy-with-msc4521-state-set-sketch-reconciliation))
  now gives an optional PinSketch-based path for cheap state-level delta
  discovery after an accumulator mismatch. Is one sketch-based structure enough,
  or is there still a case for IBLT or Merkle-search-tree alternatives (e.g. for
  servers that want the accelerant without pulling in MSC4521's GF(64)
  machinery)?

## References

[^1]:
    **Bellare, M., & Micciancio, D. (1997).** _A New Paradigm for Collision-free
    Hashing: Incrementality at Reduced Cost._ Advances in Cryptology — EUROCRYPT
    '97. Lecture Notes in Computer Science, vol 1233. Springer, Berlin,
    Heidelberg. Available at: <https://doi.org/10.1007/3-540-69053-0_13>

[^2]:
    **Lewi, K., Kim, W., Maykov, I., & Weis, S. (2019).** _Securing Update
    Propagation with Homomorphic Hashing._ IACR Cryptology ePrint Archive,
    2019/227. Available at: <https://eprint.iacr.org/2019/227>

[^3]:
    **Digital Asset (Canton).** _LtHash16 Scala Documentation._ Available at:
    <https://docs.digitalasset.com/operate/3.5/scaladoc/com/digitalasset/canton/crypto/LtHash16.html>

[^4]:
    **Micciancio, D. (2002).** _Generalized Compact Knapsacks, Cyclic Lattices,
    and Efficient One-Way Functions._ Proceedings of the 43rd Annual IEEE
    Symposium on Foundations of Computer Science (FOCS '02). Available at:
    <https://cseweb.ucsd.edu/~daniele/papers/Cyclic.pdf>

[^5]:
    **Solana Labs (2025).** _SIMD-0215: Accounts Lattice Hash._ Solana
    Improvement Documents. Available at:
    <https://github.com/solana-foundation/solana-improvement-documents/pull/215>

[^6]:
    **Meta Platforms, Inc.** _folly::crypto::LtHash — Homomorphic hash using
    lattice-based cryptography._ Facebook Folly Library. Available at:
    <https://github.com/facebook/folly/blob/main/folly/crypto/LtHash.h>
