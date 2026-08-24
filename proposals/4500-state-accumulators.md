# MSC4500: State accumulator and transaction digests

State is a derived property of the DAG, meaning it changes over time as events
are received. Most basically, state is a `set()` of `$eventIDs`; it can also be
a dictionary of tuples to event IDs, e.g.,
`(state_key, event_type) -> event_id`. Given implied assumptions about globally
unique UUIDs, this dictionary can be converted to and from a de-structured set
without loss of injectivity or meaning, e.g.,
`(state_key, event_type, event_id)`.

Current implementations load the state map into memory, authenticate incoming
PDUs against their `prevs` (or the room's extremities and possibly
`prev_state_events` in a future room version[^0.a]), and finally persist the new
state as a series of diffs, periodically compacting them into full checkpoints.
Storing diffs and only persisting a new state group checkpoint every 100 hops
bounds the runtime complexity by a constant factor ($1/100$), but it does not
bound it asymptotically. The write-time complexity is still $O(S)$ per step and
$O(S^2)$ cumulatively over the room history or state DAG.

Additionally, these local implementation methods have no way of communicating
state equality over federation—Synapse's `state_groups` and the Conduit-based
`shortstatehash` are both implementation-based, not universal specifications.
Going forward, this will be useful to diagnose divergence early, during `/send`
transactions or `/state_ids` requests.

This MSC does not, on its own, profoundly reduce the state resolution algorithm
runtime. Combined with a HAMT[^0.b] that carries the delta itself, the `LtHash`
accumulator specified here is one ingredient of the future speedup — the piece
that collapses state into a fixed-size commitment[^0.c], mapping state groups
(with 128-bit security) into the globally unique 256-bit space, and allowing
near instant state group de-duplication, equivocation, or identifier generation,
regardless of the magnitude of the input stream.

Furthermore, this MSC, by placing a backwards compatible (safely ignored)
`state_hashes` key alongside `txn` request bodies, allows for instant, passive
state comparisons with federated peers. This is important because it allows
efficient (basically free) confirmation that two servers agree on room state.
This allows admins to be alerted and diagnose divergence early, if they choose;
it also makes possible future automated remediation or reconciliation methods.

The proposed wire scope covers `txn` payloads and a backwards-compatible
`/state_ids` HTTP cache validator. Both use the same 256-bit digest of the full
2048-byte lattice; the latter allows a responding server to omit the response
body when the requester's cached state agrees. The case of small divergence has
been loosely sketched out in MSC4521; the case of moderate divergence (>1000
events differ) may possibly be addressed by bloom filters and IBLTs (Kegan's
idea), or, like large divergences, they may remain an open problem.

The current `LtHash16` implementation, byte-for-byte compatible with Facebook
researcher's specification[^0.d], is available, together with test vectors, as a
Rust library (suitable for testing but pending final wire format adoption). A
complementary Golang implementation is also supplied, whose production-readiness
is also contingent upon wire format (algorithm) finalization. The underlying
idea is already in use by various platforms: Ethereum, Facebook's RocksDB
`folly`, and others[^0.e].

## Proposal

### Relationship to existing specification

This MSC introduces a cryptographic[^1.1.a] `state_hashes` object in the
`PUT /_matrix/federation/v1/send/{txnId}` payload. It also introduces an ETag to
the `/state_ids` endpoint, plus a causal redaction overlay validator.

The proposal is purely additive and does not break change PDU structure or
authorization rules. Such changes are left to the discretion of future
proposals.

Rather than attaching hashes to the `unsigned` event dict (often stripped or
rewritten), this proposal places them in the transaction body or payload.

When a homeserver sends or relays a federated transaction containing a state
event, or an effective redaction of a state event selected at that DAG point, it
computes the accumulator of the room state exactly at the DAG tip of each
referenced PDU. In State DAGs MSC4242, this is no longer relevant; digests need
only be computed during state transitions, not against all resolved `prevs`.

It then collapses each PDU's vectorial state into a standard 32-byte digest and
includes them in the transaction payload as a dictionary.

The 32-byte digest may then, `base64url` encoded, serve as a globally unique ID
for the given resolved state map. This has broad application across a variety of
endpoints, use cases, and future MSCs.

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
   tag `msc4500_lthash16_v1\x00`, is expanded to exactly 2048 bytes using the
   `SHAKE256` extendable-output function (XOF) from NIST FIPS 202:
   `expansion = SHAKE256("msc4500_lthash16_v1\x00" || element, 2048)`. A
   fixed-width hash cannot fill the lattice; this uniform XOF expansion is
   essential for identical lane distribution. `SHAKE256` is natively supported
   across virtually all cryptographic libraries without custom parameter block
   requirements.
3. **Accumulation.** The 2048-byte expansion is interpreted as 1024
   little-endian unsigned 16-bit lanes and combined into the local lattice with
   lane-wise wrapping addition.
4. **Removal and replacement.** Removing an element is lane-wise wrapping
   subtraction of its expansion. Replacing the event for a `(type, state_key)`
   pair is one subtraction (old element) and one addition (new element) — the
   `O(1)` update at the heart of this proposal. A replace operation MUST only be
   accepted when the removed and added entries refer to the same
   `(type, state_key)` tuple. If the tuples differ, implementations MUST fail
   closed with a panic, exception, or equivalent hard error, and MUST NOT
   reinterpret the call as a replace, add, or remove.
5. **Initial state.** The accumulator of the empty state set is 2048 zero bytes.

6. **Collapse.** Compute the final 32-byte digest $D$ by hashing the final
   2048-byte sum lattice $S$ using `BLAKE2b-256`, encoded as an unpadded
   `base64url` string (43 characters), matching Matrix's event-ID convention:
   $$D = \text{base64url}(\text{BLAKE2b-256}(S))$$

**Reference implementations** are available in Rust[^1.2.rust] and
Golang[^1.2.go].

**NOTE:** elements bind the `event_id` only, never event content. Redacting an
event therefore has no effect on the accumulator (having no effect on event ID).

**Causal redaction overlay.** Redaction visibility is represented by a separate
overlay accumulator, not by changing the primary element tuple. The overlay uses
the same element encoding `(type, state_key, event_id)` and the same lattice
parameters, but expands elements under the domain separation tag
`msc4500_lthash16_redactions_v1\x00`. Its normative input at a DAG point `E` is
the following derived set:

$$
R(E) = \{\operatorname{tuple}(s) \mid s \in \operatorname{resolved\_state}(E)
\land \operatorname{effectively\_redacted\_in\_past}(E, s)\}.
$$

An incrementally maintained overlay lattice is only a cache of this function; it
MUST NOT be treated as an independent source of redaction or state membership.
Multiple accepted redactions of the same selected event still contribute one
overlay element. Redactions of timeline events, and redactions of state events
not selected in the resolved state at `E`, contribute nothing.

This overlay answers a narrower question than history-wide reconciliation: do
the peers agree about redactions that affect the presentation of the selected
state at this DAG point? It intentionally does not accumulate every redaction in
the room. Servers legitimately have different retained history horizons, so an
unframed room-global redaction digest would not be comparable. History-wide
redaction gaps belong to framed MSC0501 / MSC4521 reconciliation instead.

Hash-failure redaction of a locally corrupt event is also excluded. It is a
local, reversible repair condition, not consensus state. A server may use it for
local telemetry or refetch decisions, but MUST NOT include it in federated
overlay digests.

**NOTE:** It is the caller's responsibility to ensure the input is really a set
[^1.2.n2]. The digest allows deducting elements which were never added, and it
allows adding the same element twice (producing different digests). Due to the
wrapping math of the 16-bit lanes, adding the exact same element $2^{16}$
($65,536$) times will roll the accumulator's lanes back to zero, returning to
the starting digest. This degenerate state is materially unattainable when the
input domain is a resolved state map (a set whose elements all have a
multiplicity of 1). The inbound accumulator is strictly a one-way _comparative_
tool; homeserver databases MUST remain responsible for _managing_ actual set
element membership. Homeservers MUST therefore treat their local resolved state
map — keyed by `(type, state_key)` — as the authoritative source of state
membership, replacement, and deduplication. The accumulator is a non-invertible
commitment of that map's current `(type, state_key, event_id)` assignments, not
a set manager or delta-decoder.

<!-- Edit marker. -->

### Capability discovery

Servers advertise causal redaction overlay support through
`GET /_matrix/federation/v1/version`:

```json
{
  "unstable_features": {
    "tk.nutra.msc4500.redaction_overlay": true
  }
}
```

Once a server advertises this flag, it MUST emit the complete overlay wherever
this MSC requires `state_hashes`, and MUST emit the overlay validator on a
resolvable `/state_ids` response. Absence from an advertising server means no
assertion was made; it MUST NOT be interpreted as the empty-overlay sentinel or
as agreement. Servers that do not advertise the flag remain compatible with
legacy federation behavior.

### Transaction payload

Servers implementing this MSC MUST embed a `state_hashes` object at the root of
the `PUT /_matrix/federation/v1/send/{txnId}` request body when the transaction
contains a state event or an effective redaction targeting a state event
selected at that DAG point. Such a transaction MUST have an entry for every PDU
in the transaction, including every non-state PDU. Other transactions MAY
include `state_hashes`, but are not required to do so. The object has two
fields: a scalar `algorithm` identifying the digest algorithm used for every
entry (see below), and an `entries` dictionary mapping the IDs of the PDUs
included in the transaction to their respective state assertions. Namespacing
both fields under `state_hashes` keeps them from occupying generic names at the
transaction root that other MSCs might want. The `state_hashes` values always
represent the transaction sender's local resolved state, not necessarily the
origin server's (meaning relays forward their own view).

When a PDU lists multiple `prev_events`, the `before` state is the output of the
room version's state resolution algorithm applied across the states after each
predecessor. This includes the auth-chain difference, reverse-topological
ordering and iterative authorization of conflicted power events (including power
levels, kicks, bans, and join rules), followed by mainline ordering and
iterative authorization of the remaining conflicted state. Thus `before` is the
state at the PDU's own DAG position used for the state-before-event
authorization check; it is not necessarily the receiver's current state resolved
across all of its forward extremities.

If the PDU is a non-rejected state event, `after` is that DAG-position state
with the PDU's `(type, state_key)` binding replaced by the PDU's event ID. For a
non-state or rejected event, `after` equals `before`. If the PDU is an effective
redaction whose target is selected in that DAG-position state,
`redactions_after` adds the target's `(type, state_key, event_id)` overlay
element even though `after` equals `before`. This replacement is not a shortcut
around state resolution: when this branch is later resolved with other branches,
the room version's complete state resolution algorithm decides whether the PDU
survives into the resulting state.

In particular, a PDU can name a sole, old predecessor from before a ban or join
rule change and pass authorization at its own DAG position. A receiver that has
newer extremities separately checks the PDU against its current resolved state
and can soft-fail it. Soft-failed state events still participate in state
resolution if later events reference them, so the resulting current-state
accumulator MUST be computed from the resolution result, not by unconditionally
applying that stale branch's `after` delta to the receiver's current lattice.

- `algorithm`: A single string identifying the complete digest profile used for
  every entry in this transaction's `state_hashes.entries` dictionary. This MSC
  defines `lthash16-v1+redactions-v1`, comprising the primary `lthash16-v1`
  accumulator and the causal redaction overlay with its separate DST (see
  [Algorithm specification](#algorithm-specification)). One value governs the
  whole transaction; mixing algorithms within a single transaction serves no
  purpose and is not supported. A receiver that does not recognize the algorithm
  MUST silently skip hash validation for the entire transaction, the same as any
  other deferral case in the [Receiver contract](#receiver-contract) — this
  preserves forward compatibility if a future revision introduces a new digest
  family (e.g. a wider lattice or a different XOF) without causing receivers on
  the old algorithm to raise false mismatch alarms against upgraded senders.
- `entries`: A dictionary keyed by the IDs of the PDUs included in the
  transaction. It MUST contain exactly one entry for every PDU in `pdus` when
  `state_hashes` is present. Under `lthash16-v1+redactions-v1`, each value
  either asserts that PDU's primary and overlay `before` and `after` digests, or
  explicitly marks the assertion as limited. A supporting sender MUST emit all
  four digest fields for a non-limited entry; omission is malformed, not an
  assertion that the overlay is empty. The empty overlay is represented by its
  defined sentinel digest. A receiver that observed the sender advertise
  `tk.nutra.msc4500.redaction_overlay` SHOULD report an omitted overlay as a
  protocol violation, while continuing ordinary PDU processing.
  - `before`: The 32-byte digest of the room state evaluated exactly at the
    given PDU's `prev_events`, excluding and preceding the given event. This is
    JSON `null` when `limited` is `true` and the sender cannot resolve that DAG
    point.
  - `after`: The 32-byte digest of the room state after the current PDU is
    applied. For non-state events, this is identical to `before`. This field
    MUST be omitted when `limited` is `true`.
  - `redactions_before`: The 32-byte causal redaction overlay digest evaluated
    over the selected state at the same DAG point as `before`. This is JSON
    `null` when `limited` is `true`.
  - `redactions_after`: The 32-byte causal redaction overlay digest after the
    current PDU is applied. This field MUST be omitted when `limited` is `true`.
  - `limited`: The boolean `true` when the sender cannot resolve the state at
    all of the PDU's `prev_events` and therefore makes no digest assertion. It
    MUST be omitted or `false` when all four digests are present.

**Sender-side partial state.** A server MUST NOT emit a guessed or approximated
digest. If a sending or relaying server cannot compute the resolved state at a
given PDU's position — because it is itself operating under Partial State
(MSC3706), is missing ancestry, or holds an unpersisted accumulator it declines
to backfill on demand — its entry MUST contain `"limited": true` and
`"before": null` and `"redactions_before": null`, and MUST omit `after` and
`redactions_after`. Receivers MUST treat such an entry as an explicit deferral,
not as a mismatch. An implementation MUST NOT use an empty string in place of
JSON `null`: retaining one representation keeps the wire format type-safe and
canonical.

This payload deliberately carries no state-cardinality fields. A count cannot
establish set equality or reliably estimate symmetric-difference magnitude, and
no recovery choice in this MSC consumes such an estimate. This differs from
MSC4521, whose counts and strata estimates provision and verify a bounded decode
operation; those values have a specified consumer and are not substitutes for
MSC4500's equality commitment.

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
    "algorithm": "lthash16-v1+redactions-v1",
    "entries": {
      "$sample_pduid_abc123def456": {
        "before": "qF3-HUgHBUgvN9WC_6J2ERF7V3-HNFMqWmN5vGZrIQQ",
        "after": "qF3-HUgHBUgvN9WC_6J2ERF7V3-HNFMqWmN5vGZrIQQ",
        "redactions_before": "IAgj5RWLN3TBG1xhhQradi-CZBRKm-vsPrrFoq3eZ7g",
        "redactions_after": "IAgj5RWLN3TBG1xhhQradi-CZBRKm-vsPrrFoq3eZ7g"
      }
    }
  }
}
```

### Network efficiency

To avoid event bloat, the full `LtHash16` lattice state (2048 bytes) is **never
explicitly transmitted over transactions.**

Transmitting only the collapsed 32-byte digest keeps payload footprints small.
Only the collapsed primary and overlay digests and their field names are added
per resolvable PDU; the 2048-byte lattices are never duplicated on the wire.

### Receiver contract

Each server independently maintains its own `LtHash16` lattice in local storage.

When a server catches a `/send` transaction containing the `state_hashes`
payload, it collapses its own local primary and causal-redaction-overlay
lattices at that exact DAG point using fast bitmap operations, hashing each down
to a canonical 32-byte `BLAKE2b-256` digest. If the local digests match the
incoming ones, all systems are nominal.

For each entry with `limited: true`, the receiver MUST defer validation for that
PDU. A receiver MUST likewise defer if a malformed or incomplete entry does not
provide all four primary and overlay digests; transaction and PDU processing
continue under the standard federation rules.

If digests mismatch, servers SHOULD log an error or warning message of the state
split. The receiver can automatically trigger a rate-limited background
`/get_missing_events` fetch with servers currently participating in the room's
resolved state, while replying to the sender with the mismatched digest embedded
in a `state_hash_mismatch` dictionary as part of the PDU's processing result and
the `200 OK` response. Unknown keys in per-PDU result objects are silently
ignored by existing implementations, so adding `state_hash_mismatch` is
backwards-compatible. `state_hash_mismatch.algorithm` echoes back the algorithm
identifier from the triggering transaction's `state_hashes.algorithm`, so a
sender receiving the mismatch can tell which digest family the receiver
evaluated against.

```json
{
  "pdus": {
    "$sample_pduid_abc123def456": {
      "state_hash_mismatch": {
        "algorithm": "lthash16-v1+redactions-v1",
        "expected_after": "uF3-HUgHBUgvN9WC_6J2ERF7V3-HNFMqWmN5vGZrIQQ",
        "received_after": "qF3-HUgHBUgvN9WC_6J2ERF7V3-HNFMqWmN5vGZrIQQ",
        "expected_redactions_after": "IAgj5RWLN3TBG1xhhQradi-CZBRKm-vsPrrFoq3eZ7g",
        "received_redactions_after": "gQgj5RWLN3TBG1xhhQradi-CZBRKm-vsPrrFoq3eZ7g"
      }
    }
  }
}
```

Mismatch handling SHOULD be deduplicated per room (i.e. the first detection
triggers logging, but subsequent mismatching transactions MUST be subject to
exponential backoff or local rate-limiting to limit logger output and network
activity). Note that if a receiving server **rejects** an incoming state event
due to auth/power-level rules, their `after` hash will instantly (and correctly)
mismatch the sender's `after` hash. This mechanism instantly detects split-brain
authorization failures.

Primary and overlay mismatches SHOULD be reported separately. A primary mismatch
means the servers disagree about the selected state event IDs. An overlay
mismatch with a matching primary digest means the servers agree on selected
state IDs but disagree about whether one of those selected events has been
effectively redacted in the causal past; operationally, this most often points
at missing redaction or target ancestry and is a fetch/reconciliation signal. If
both digests mismatch, the primary state disagreement is the first condition to
investigate, because redaction effectiveness itself depends on authorized state
such as power levels.

Homeservers operating under Partial State (MSC3706) MUST silently defer hash
validation for that room. They cannot compare state to emit warnings (until the
room state is fully synchronized).

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
under standard rules. Whether a homeserver implements an automated healing
pipeline or merely logs the divergence for admin intervention is left as an
implementation detail.

### Other affected endpoints

The introduction of a cryptographically verifiable state accumulator enables
several zero-cost optimizations across the existing Matrix Client-Server and
Server-Server APIs.

- **`GET /_matrix/federation/v1/state/{roomId}`**,
  **`GET /_matrix/federation/v1/state_ids/{roomId}`**, and
  **`/_matrix/client/v3/rooms/{roomId}/state`**. Currently, homeservers must
  fully materialize the room state to serve these endpoints, which is an
  expensive $O(S)$ operation for large rooms.

The primary digest is an ID-set validator only. Endpoints that return full event
objects, including the client `/state` endpoint, can be affected by redaction of
selected state events even when the selected event IDs are unchanged. A
client-facing validator for those representations would therefore need to bind
both the primary digest and a redaction overlay digest in its own extension;
this MSC only specifies the backwards-compatible federation `/state_ids`
validator.

#### Backwards-compatible `/state_ids` optimization

A server which has completely resolved the state and auth chain for the exact
`event_id` requested by `GET /_matrix/federation/v1/state_ids/{roomId}` SHOULD
include an entity-tag of the form `ETag: "lthash16-v1:<digest>"` on its `200 OK`
response, where `<digest>` is the unpadded base64url-encoded collapse digest of
that resolved state. The algorithm identifier is part of the opaque entity-tag;
validators from different accumulator versions MUST NOT compare equal.

Unlike an ordinary server-issued opaque ETag, this validator is globally
derived: a requester MAY compute and send it without having received it from
that responder, and honest servers derive identical values for identical
resolved state at the same request target. Responses carrying it SHOULD include
`Cache-Control: private` so a shared HTTP cache does not reuse one federation
peer's authenticated response for another peer. The response body's
`auth_chain_ids` are the transitive authorization closure of its `pdu_ids`;
therefore equal selected state event IDs also imply equal auth-chain IDs. A
future room version or endpoint semantics that break this derivation MUST use a
validator that commits to both response sets instead.

A responder advertising `tk.nutra.msc4500.redaction_overlay` MUST also include a
causal redaction overlay validator of the form
`X-Matrix-MSC4500-Redactions: "lthash16-redactions-v1:<digest>"`, where
`<digest>` is evaluated at the same requested `event_id`. This header is not a
substitute for the entity-tag: the ETag validates the endpoint's ID-only JSON
body, while the overlay header lets peers cheaply detect disagreement over
effective redactions of the selected state at that DAG point. A redaction MUST
NOT invalidate the primary `/state_ids` ETag unless it changes the selected
state event IDs.

A requester which has cached that response MAY send its entity-tag verbatim in
`If-None-Match`. If the responder can reproduce the same validator for the same
request target, it MAY return `304 Not Modified` with no response body. It MUST
NOT return `304` merely because a digest supplied by the requester matches a
remote or otherwise unverified accumulator: the validator MUST be derived from
the responder's own resolved state at the requested DAG point. Unsupported,
unknown, or malformed validators MUST be ignored, yielding the existing `200`
response and JSON body. Thus this extension changes neither the endpoint's URL
nor its JSON schema, and implementations unaware of it remain interoperable. The
The overlay header remains backwards-compatible: unaware implementations ignore
it. Aware implementations compare it only when they have independently resolved
the same requested DAG point. If an advertising responder omits it, the
requester MUST treat the overlay check as unavailable, not successful, and
SHOULD report the protocol violation.

Conditional requests are a steady-state polling optimization only. Once state
divergence is known or suspected, a requester MUST issue `/state_ids`
unconditionally and MUST NOT allow a peer-supplied `304` response to suppress a
state transfer on the recovery path.

## Synergy with MSC0501 (event set reconciliation)

This proposal and MSC0501 (`room_digest` / `room_diff`) solve fundamentally
different sets. MSC4500's accumulator covers the room's _resolved state set_ at
arbitrary DAG positions. MSC0501's algebraic digest and bounded extremity
fallback cover the _known event set_ (accepted events and retained rejection
tombstones across the frame).

Because state divergence implies event-set divergence (with the converse _often_
also holding true), the two proposals complement each other: MSC4500 provides
continuous, passive, free state-consistency detection on every `/send`; when a
mismatch is reported, MSC0501's `room_diff` / `room_events` reconcile the
missing event set. The receiver admits verified events to its DAG and recomputes
its resolved state locally; remote state digests and state maps are never write
targets. Because MSC4500 gives active rooms free passive detection, MSC0501's
polling interval can be lengthened (rate-limited to a longer period) for rooms
with recent inbound transactions.

MSC4500 does not detect omissions in ordinary messages or history-wide
redactions. Its causal overlay detects only redactions that affect state events
selected at the asserted DAG point. Broader timeline reconciliation remains the
domain of MSC0501.

## Synergy with MSC4521 (state-set sketch reconciliation)

MSC4521's State-map binding profile (see
[Element derivation](../4521-algebraic-set-reconciliation.md#element-derivation)
and
[State-map binding](../4521-algebraic-set-reconciliation.md#state-map-binding))
lets two servers that already know their resolved state maps diverge exchange a
PinSketch syndrome sketch directly and decode the symmetric difference. This is
a natural companion to the mismatch signal MSC4500 produces, but it is optional
and independent: a server MAY implement MSC4500's transaction digests and never
implement this section at all. This MSC defines nothing about _when_ to
reconcile, only the passive detection signal; MSC4521 defines the reconciliation
primitive.

## Implementation notes (non-normative)

The following is advisory storage and indexing guidance for implementers, not
part of the wire contract.

The natural storage model is one 2048-byte primary lattice per state group, with
an option to also persist the 256-bit digest. Care and creativity may need to be
applied to the redesign of Synapse's `event_to_state_groups`, for example by
handling total rewrites with a single pointer flip or by computing the set
partitions (for partial or heterogeneous rewrites) in SIMD and L1 cache before
issuing any database commands.

The causal redaction overlay is normatively the function $R(E)$ defined above,
not independently maintained state. Implementations MAY cache its lattice
incrementally and SHOULD represent cached values as pointer-shared immutable
roots, not mandatory 2048-byte copies on every state group. Most rooms have no
currently selected redacted state events, so the all-zero overlay lattice is a
global sentinel. Even in rooms with such redactions, the overlay changes only
when an effective redaction targets a state event selected at that DAG point, or
when state resolution selects a different redacted/non-redacted state event for
a binding. Implementations can therefore store many state groups pointing at the
same overlay value.

An implementation that caches the overlay MUST retain a path to recompute it
from authoritative resolved-state membership and causal redaction data. Before
classifying an overlay mismatch as peer divergence, it MUST verify or recompute
the local derived value. A full recomputation is $O(S)$ in selected-state size;
colocated redaction status, a compact status bitmap, or an index of selected
redacted events can reduce its practical cost without changing the normative
set.

At multi-predecessor events, neither the primary nor the overlay lattice can be
computed by directly combining parent lattices; both follow from the room
version's state resolution result. The overlay's marginal work is checking
redaction status for selected state events already enumerated to construct the
primary lattice. Implementations SHOULD colocate that status with the selected
state row, short event ID, or equivalent state-map metadata. Storing it in a
separate table can turn merge construction into an avoidable extra scan.

Creating a new state group ID (digest) from a singular delta is one subtraction
plus one addition. A bundle of 100 deltas is 100 additions and 100 subtractions.

Servers without persisted lattices can compute them on demand per-event during
delta chain traversals or state resolution.

Homeservers who do not yet support large customers (millions of rooms or users)
may elect for a monolithic database migration once the wire format is
stabilized.

### State identifiers and local storage optimizations

The following are local-only indexing optimizations with no wire-visible effect.
They are advisory; a server MAY implement none, some, or all of them.

Locally, an accumulator makes state identity path-independent instead of
path-dependent (cf. Solana's "Accounts Lattice Hash" [^3.1.a], which computes
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
   methods MUST rate-limit out-of-band state sync requests triggered by
   mismatching hints (e.g. exponential backoff per room or per origin).
   Repetitive warning logs should likewise be subject to a cooldown period.

2. **Peer deprioritization:** A malicious or malfunctioning peer could transmit
   mismatched digests to trigger spurious state resyncs. Receiving servers MAY
   locally rate-limit, deprioritize, or ignore transaction hashes from peers
   that consistently provide unresolvable or malicious digests. Such local
   treatment MUST NOT cause the receiver to reject a valid event that passes
   normal Matrix authorization and event verification.

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
  The broader state-resolution lineage here is MSC1442, MSC4297, and MSC1759,
  which show how room versions evolve the conflict-resolution rules that this
  proposal tries not to disturb.

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

### Historical repair endpoints

MSC2451 (`query_auth`) is the historical example of a federation repair API that
tried to recover missing or stale state-related information over the wire. It is
relevant here as a cautionary predecessor: this MSC keeps the repair primitive
additive and diagnostic, rather than trying to turn remote state into an
authoritative write target.

### Reconciliation endpoint and bisection (considered and rejected)

An earlier revision of this MSC defined a
`GET /state_accumulator/{roomId}?event_id={eventId}` federation endpoint to
query the raw accumulator lattice (and optionally a shape checksum) at arbitrary
historical DAG points, together with an `O(log ΔD)` bisection walk over
`prev_events` to locate the earliest divergence point. This was rejected as out
of scope for this proposal:

- **Value is thin.** The 32-byte digest is already carried in the transaction
  payload; exposing the full 2048-byte lattice added ~3 KB responses for the
  sole purpose of enabling homomorphic subtraction, which has no consumer once
  the bisection walk is removed.
- **Redundant with later MSCs.** Divergence-point lookup and enumeration/healing
  are handled more elegantly by other proposals — MSC4511 provides graph
  metadata and ancestor hints, and MSC0501 / MSC4521 reconcile the missing event
  set directly. The absence of historical resolved-state accumulators in those
  MSCs does not justify a bespoke endpoint and bisection protocol here.
- **Awkward semantics.** The DAG is a partial order, so bisection over forked
  histories does not reduce to a single earliest divergence event but to a
  frontier of candidates, undercutting the clean `git bisect` analogy.

This MSC therefore confines itself to establishing a quantum-resistant wire
agreement state hash in the transaction payload. If a future consumer needs
historical resolved-state accumulator points, it can define a focused endpoint
(e.g. on `/state_ids`) then.

## Security considerations

Homeservers MUST NEVER use a _remote_ accumulator digest (received from a peer
via `/send`) as a source of truth to construct, modify, or authorize state.
Local state resolution MUST proceed normally as the sole authoritative driver of
state convergence. Locally-computed lattices, derived from the server's timeline
and resolved state, _are_ safe for any internal optimizations and
representations described in this proposal (state group identity, fast-path
deduplication, short-circuiting state resolution).

The hashes are diagnostic only. Servers still rely exclusively on their internal
state to judge soft-failures; any change to federation prioritization based on a
mismatch is an implementation's own discretion.

**State-isolation assurance:** Even a successful collision attack cannot corrupt
room state. Because remote digests are never used to construct, modify, or
authorize local state maps, a forged transaction digest can at worst suppress a
mismatch alarm. A dishonest `/state_ids` responder can additionally return a
false `304` and delay refresh of a requester's steady-state cache; this is why
conditional requests are forbidden once divergence is known or suspected. No
state is injected, no auth decisions are affected, and an unconditional recovery
request still returns the ordinary authenticated response body.

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

## Test vectors

To assist implementers, the following test vectors are provided. They use the
main accumulator: `SHAKE256` element expansion prefixed with the domain
separation tag `msc4500_lthash16_v1\x00`, 16-bit little-endian wrapping lane
addition/subtraction, and a `BLAKE2b-256` collapse digest encoded as unpadded
`base64url` (the wire form).

### Empty state

The starting lattice $S_0$ is 2048 bytes of all zeros.

- Lattice $S_0$ prefix (first 16 bytes): `00000000000000000000000000000000`
- Collapse digest: `IAgj5RWLN3TBG1xhhQradi-CZBRKm-vsPrrFoq3eZ7g`

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
  `c6a4f2e8f4016c9aaf9c52e67020f221`
- Lattice $S_1$ prefix (first 16 bytes): `c6a4f2e8f4016c9aaf9c52e67020f221`
- Collapse digest: `0mRyt9cOWBGyKqV14a2omLPIOJFUfX0LkJcqpE20LbI`

### Scenario 2: add-then-remove (element removal)

Subtracting the expanded element for `$event_1` from lattice $S_1$ returns the
accumulator to the empty state.

- Lattice $S_{\text{back}}$ prefix (first 16 bytes):
  `00000000000000000000000000000000`
- Collapse digest: `IAgj5RWLN3TBG1xhhQradi-CZBRKm-vsPrrFoq3eZ7g`

### Scenario 3: two elements

Starting from $S_1$, add event `m.room.name` with empty state key `""` and event
ID `$event_2`.

- Raw encoded element: `0b006d2e726f6f6d2e6e616d650000246576656e745f32`
- Element 2 expansion prefix (first 16 bytes of
  $SHAKE256(\text{tag} \parallel \text{el}_2)$):
  `8107236052d1e6d7193cada70d85fa2c`
- Lattice $S_2$ prefix (first 16 bytes): `47ac154946d35272c8d8ff8d7da5ec4e`
- Collapse digest: `aH8bXDxcQTK2_cA8Bw4BKHsBrsBE6YVgzN_uUBAJzA8`

### Scenario 4: instant replacement

Starting from $S_2$, replace the membership event for `@alice:example.com` with
event ID `$event_3`. This is performed by subtracting the expansion for
`$event_1` and adding the expansion for `$event_3`.

- Raw encoded element for `$event_3`:
  `0d006d2e726f6f6d2e6d656d626572120040616c6963653a6578616d706c652e636f6d246576656e745f33`
- Element 3 expansion prefix (first 16 bytes of
  $SHAKE256(\text{tag} \parallel \text{el}_3)$):
  `14e9b8900236b9d0d2e07dc6b392fa14`
- Lattice $S_3$ prefix (first 16 bytes): `95f0dbf054079fa8eb1c2a6ec017f441`
- Collapse digest: `DB65faOdzCq5z6YcTaMp282OIwuJKnBYOFfJNEJJJ6k`

## Backwards compatibility

This proposal is fully backwards-compatible:

- Unknown transaction keys (`state_hashes`) are silently ignored by existing
  servers, per current federation behavior.
- No room version consensus rules are modified.

## Dependencies

This proposal currently has no known dependencies. The optional
[Synergy with MSC4521](#synergy-with-msc4521-state-set-sketch-reconciliation)
section relies on MSC4521's State-map binding profile, but implementing it is
not required to implement this proposal.

## Open questions

- Impact on or relevance to partial joins (MSC3902)?
- **Large or irrevocably broken rooms:** How should servers handle large or
  irrevocably broken rooms?
- **Client-Server impact:** How should a server surface a detected state
  divergence to clients, if at all? For example, how should it handle detecting
  missed events that fell through over the Client-Server `/sync` v5 endpoint?
  (See future work).
- **Self-verification:** Could servers perform self-verification (e.g. checking
  checksums of the result) before signing off on it? Is there value in auditing
  one's own state (either on-the-fly or on past events)?
- **Future reconciliation structures:** MSC4521's State-map binding (see
  [Synergy with MSC4521](#synergy-with-msc4521-state-set-sketch-reconciliation))
  gives an optional PinSketch-based path for cheap state-level delta discovery
  after an accumulator mismatch. Is one sketch-based structure enough, or is
  there still a case for IBLT or Merkle-search-tree alternatives (e.g. for
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

[^0.a]: See MSC4242 (State DAGs).

[^0.b]:
    Hash array mapped trie: a persistent data structure with properties of an
    in-memory set or dictionary, achieving efficient read/write requirements via
    "structural sharing."

[^0.c]:
    In cryptography, a _commitment_ is an opaque value (typically encrypted or
    hashed, and unalterable) which one party generates, shares, or signs
    (without fully revealing) that another party can later verify or
    independently reconstruct.

[^0.d]:
    **Meta Platforms, Inc.** _folly::crypto::LtHash — Homomorphic hash using
    lattice-based cryptography._ Facebook Folly Library. Available at:
    <https://github.com/facebook/folly/blob/main/folly/crypto/LtHash.h>

[^0.e]:
    Forum discussion and example commercial use case for an `LtHash16` function.

    _What shared state do ACS commitments cover? - App Development - Canton
    Network Forum_
    <https://forum.canton.network/t/what-shared-state-do-acs-commitments-cover/5012>

[^1.1.a]:
    _Cryptographic_ here means "secure" (collision resistant and/or
    non-invertible). SHA, BLAKE; AES — these are cryptographic hashes (AES is an
    encryption scheme, not hash). MD5; XXH3; Poseidon; Zobrist —
    **non-**cryptographic hashes (Poseidon is _pseudo_-cryptographic).

[^1.2.rust]:
    `rezzy/src/state/lthash.rs` at master · gamesguru/rezzy
    <https://github.com/gamesguru/rezzy/blob/e74a5e8302192d922cd9535b69596a1f219fdfa9/src/state/lthash.rs#L146>

[^1.2.go]:
    `lthash/lthash.go` · main · Wombat-Foundation / gomatrixcrypto · GitLab
    <https://gitlab.com/wombat-foundation/gomatrixcrypto/-/blob/e64f500dd026ffbdd12e1f004093a54c26a4b8dd/lthash/lthash.go#L80>

[^1.2.n2]:
    **Digital Asset (Canton).** _LtHash16 Scala Documentation._ Available at:
    <https://docs.digitalasset.com/operate/3.5/scaladoc/com/digitalasset/canton/crypto/LtHash16.html>

[^3.1.a]:
    **Solana Labs (2025).** _SIMD-0215: Accounts Lattice Hash._ Solana
    Improvement Documents. Available at:
    <https://github.com/solana-foundation/solana-improvement-documents/pull/215>

[^4]:
    **Micciancio, D. (2002).** _Generalized Compact Knapsacks, Cyclic Lattices,
    and Efficient One-Way Functions._ Proceedings of the 43rd Annual IEEE
    Symposium on Foundations of Computer Science (FOCS '02).
    <https://cseweb.ucsd.edu/~daniele/papers/Cyclic.pdf>
