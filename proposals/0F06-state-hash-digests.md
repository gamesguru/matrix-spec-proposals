# MSC0F06: State accumulator endpoint and transaction digests

<!--
[Rendered](https://github.com/gamesguru/matrix-spec-proposals/blob/guru/4499-state-hash-digests.md)
 -->

When servers diverge, the result can be a serious nuisance. Matrix lacks an
out-of-band or real-time mechanism for state verification or re-alignment;
servers often only learn of de-synchronization once they disagree on a much
later authorization failure (e.g., another user's join is incorrectly rejected).

I present an "early-warning system" which rapidly confirms incremental state
consensus, or signals (with approximate delta sizes) as to its divergence, so servers
know they share the exact same view of a room at a given point in the DAG (or roughly
where they diverged).

This proposal does not impose any verification requirements on PDU handling. It
seeks to act as a secondary state convergence mechanism, while simultaneously
**replacing state group transitions** and naive iterative BFS implementations
with a cheap, bitwise, commutative, subtractable (supports element removal),
collision-resistant 2048-byte `LtHash16` accumulator function.

Avoiding diff chain reconstruction for point lookups will reduce Synapse's
electricity bill across a wide range of API state endpoints.

The accumulator under question may be called 'homomorphic' and solves the
following encryption problem: "Given the hash of an input, along with a small
update to the input, how can we compute the hash of the new input with its
update applied, without having to recompute the entire hash from scratch?"

Should this proposal be accepted, for the sake of federation clarity homeserves
must embed a canonical `BLAKE2b-256` digest (of their 2048-byte room state
accumulator) in the `PUT /_matrix/federation/v1/send/{txnId}` transaction body.

## Proposal

Rather than attaching hashes to individual events (which are routinely stripped,
rewritten, or relayed by intermediate servers), this proposal places the hashes
in the body of the federation transaction.

When a homeserver sends or relays a federated transaction, it calculates the sum
accumulation of the room's state exactly at the DAG tip of each included PDU.

It then collapses this vectorized state into a standard 32-byte digest and
includes it in the transaction payload.

### Algorithm specification

To guarantee interoperability, the algorithm is as follows:

1. **Input encoding.** Each entry in the room's resolved state map is
   serialized as the UTF-8 concatenation:
   `type || "\x00" || state_key || "\x00" || event_id`.
2. **Input expansion.** The encoded element, prefixed with the domain
   separation tag `msc4499_lthash16\x00`, is expanded to exactly 2048 bytes
   using the `BLAKE2Xb` extendable-output function (XOF):
   `expansion = BLAKE2Xb-2048("msc4499_lthash16\x00" || element)`. A fixed-width
   hash cannot fill the lattice; the XOF expansion is what makes the lane
   distribution uniform and implementation-identical.
3. **Accumulation.** The 2048-byte expansion is interpreted as 1024
   little-endian unsigned 16-bit lanes and combined into the local lattice with
   lane-wise wrapping addition.
4. **Removal and replacement.** Removing an element is lane-wise wrapping
   subtraction of its expansion. Replacing the event for a `(type, state_key)`
   pair is one subtraction (old element) followed by one addition (new element)
   — the `O(1)` update at the heart of this proposal.
5. **Initial state.** The accumulator of the empty state set is 2048 zero bytes.
6. **Collapse.** The wire digest is `BLAKE2b-256` over the raw 2048 lattice
   bytes, hex-encoded (64 characters).

**NOTE:** elements bind the `event_id` only, never event content. Redacting an
event therefore has no effect on the accumulator (having no effect on event ID).

**NOTE:** It is the caller's responsibility to ensure the input is really a set.
The digest allows deducting elements which were never added, and it allows adding
the same element twice (producing different digests). The digest will roll-over
if and only if the same element is applied `2^16` times. Thus pre-existence can
be checked in under 65,536 accumulator iterations (fitting purely within L1/L2 cache).

### Transaction payload

A new `state_hashes` dictionary is introduced at the root of the
`PUT /_matrix/federation/v1/send/{txnId}` request body. It maps the IDs of the
PDUs included in the transaction to their respective `before` and `after`
digests.

When a PDU lists multiple `prev_events`, the `before` state is the output of
state resolution (v2) applied across the states at each of those events — i.e.
the same resolved state the server would use to authorize the PDU. The `after`
state is `before` with the PDU applied, if it is an accepted state event;
otherwise `after` equals `before`.

- `before`: The 32-byte digest of the room state evaluated exactly at the PDU's
  `prev_events`, excluding the current event.
- `after`: The 32-byte digest of the room state after the current PDU is
  applied. (For non-state events, this will be identical to `before`).

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
    "$sample_pduid_abc123def456": {
      "before": "a85dfe1d480705482f37d582ffa27611117b577f8734532a5a6379bc666b2104",
      "after": "a85dfe1d480705482f37d582ffa27611117b577f8734532a5a6379bc666b2104"
    }
  }
}
```

### Network efficiency

To avoid event bloat, the full `LtHash16` lattice state (2048 bytes) is **never
transmitted over the network.**

By transmitting only the collapsed 32-byte digest, payload footprints stay
small. Adding both `before` and `after` hashes consumes approximately 160
unsigned bytes of JSON overhead per PDU in the transaction.

### Receiver contract

Each server independently maintains its own `LtHash16` lattice in local storage.

1. It receives the `/send` transaction with the `state_hashes` payload.
2. It collapses its own local lattice at the corresponding point in the DAG via
   fast bitmap operations and canonicalizes it over `BLAKE2b-256` into a 32-byte
   digest.
3. It compares its local digest to the incoming digest.
4. **Match:** The servers have the same proven view of the room state.
5. **Mismatch:** The receiver detects a state split. It can automatically
   trigger a background `/get_missing_events` or state resync operation to heal,
   while also alerting the sender with a response including their digest value.

If the receiver cannot quickly and reliably validate the `before` and `after`
hashes (i.e., from an in-memory LRU cache or with a single, minimal DB query),
they MUST defer the verification (and optional healing) pipelines to remain agile.

Because the checks are advisory, if the hashes do not match, the PDU is _still
accepted_ and processed according to standard Matrix authorization/resolution rules.

Whether or not homeservers implement an automated "healing" mechanism or merely
defer warning messages to admin logs (perhaps with _no_ healing mechanism) is
an implementation detail left to homeserver maintainers.

### Endpoint definition

`GET /_matrix/federation/v1/state_accumulator/{roomId}?event_id={eventId}`

Returns the raw lattice for the room state immediately **after** `eventId` is
applied (the `after` accumulator of that PDU).

**Response (200):**

```json
{
  "event_id": "$sample_pduid_abc123def456",
  "algorithm": "lthash16",
  "lattice": "<base64url, unpadded, 2048 raw bytes>",
  "digest": "a85dfe1d480705482f37d582ffa27611117b577f8734532a5a6379bc666b2104"
}
```

**Errors:** `404 M_NOT_FOUND` if the server does not hold resolved state at that
event (unknown event, outlier, or purged history). `403 M_FORBIDDEN` if the
requesting server is not a participant in the room or is denied by
`m.room.server_acl` — identical semantics to other federation endpoints.

**Rate limiting:** Servers SHOULD rate-limit per peer per room. Bisection
requires `O(log ΔD)` sequential calls, so a short burst allowance (e.g. 30
requests) with a sustained rate of ~1/second is a reasonable default. The
response is ~2.7 KB; amplification risk is negligible.

### Other affected endpoints

**TODO:**

- `/state`
- `/state_ids`
  - query parameter or header (e.g., `If-None-Match: <accumulator_digest>`)
  - Unchanged/cache quick return: `304 Not Modified`
- Room upgrades more reliable convergence/consensus.

## Reconciliation and bisecting forks

When the 32-byte digest triggers a mismatch alarm, the receiving server knows at
least one party is desynchronized. The receiver performs homomorphic subtraction
against the sender's full accumulator lattice.

The delta lattice tells you _that_ you've diverged and roughly _how much_, and
lets you **bisect** to _where_. Because both servers can produce digests at
historical DAG points, the receiver can query accumulators at $O(\log ΔD)$ depth
(binary search over HTTP) to find the earliest event where the digests diverged.

It is important to note that the delta lattice cannot name events you have never
seen—a lattice sum isn't invertible to its summands (the property that makes it
collision-resistant). Once the exact divergence point is isolated via bisection,
enumeration and healing are delegated to MSC4500's `room_diff` and
`room_events`.

Furthermore, this MSC cannot detect omissions in messages, redactions, or other
non-state-altering events. For this capability, it fully defers to MSC4500.

## Synergy with MSC4500 (event set reconciliation)

This proposal and MSC4500 (`room_digest` / `room_diff`) solve fundamentally
different sets. MSC4499's accumulator covers the room's _current state set_ at
arbitrary DAG positions. MSC4500's bloom digest and RMQ fall-back cover the
_event set_ (full PDU timeline).

Because state divergence implies event-set divergence (with the converse _often_
also holding true), the two proposals nicely complement each other:

1. **Detect (MSC4499, passive, free):** Every `/send` carries before/after
   digests. Active rooms get continuous state-consistency checks with zero extra
   round trips.
2. **Bisect (MSC4499, active):** On mismatch, optional bisection via the
   `/state_accumulator` endpoint alerts to the divergence point.
3. **Reconcile (MSC4500):** `room_diff` (with a `scope: "state"`
   parameter) fetches what is missing, auth chains included.

Because MSC4499 gives active rooms free passive detection, MSC4500's periodic
polling can back off significantly for rooms with recent inbound transactions.

## Implementation notes

The natural storage model is one 2048-byte lattice per state group. Creating a
new state group from a delta is one subtraction plus one addition against the
parent's lattice — O(1), no chain walk. Historical `/state_accumulator` queries
then reduce to the existing event (state group lookup plus a single row read).

Servers without persisted lattices can compute one on demand during legacy
delta chain or BFS walk iteration (accumulating the already materialized state
and caching the accumulator, thereby obviating the need for traversals of that
delta chain during any future point lookup).

### State identity and local DB optimizations

While this proposal primarily addresses federation, the adoption of a grand sum
accumulator profoundly optimizes local homeserver architecture.

Currently, homeservers like Synapse manage state by storing a graph of "state
groups," using delta chains (pointers and changes) because generating a hash of
an entire room state, specifically materializing the state, is an $O(S)$ operation.

With an $O(1)$ sum accumulator, the state digest _is_ the state group identifier.

1. **Instant Deduplication:** If two different branches of a DAG converge on the
   exact same state (very common occurrence), their 32-byte accumulator
   digests will perfectly match. The homeserver instantly deduplicates them into
   a single State Group ID without expanding or comparing dictionaries.
2. **$O(1)$ Equality Checks:** During State Resolution v2/v2.1, determining if
   diverging branches have different states becomes an instant 32-byte integer
   comparison rather than a complex graph traversal and dictionary comparison.

~~This mathematical guarantee provides a perfect $O(1)$ identity mechanism.~~

While delta chains remain necessary to materialize state into memory and to
compute conflict sets during state resolution, the accumulator relegates deltas
purely to storage compression and retrieval, eliminating the need to walk chains
during fast-path "state equality" checks.

## Potential issues

### Direct-hop survival (ease of audit)


Because the hashes are attached to the transaction body rather than the
individual PDUs, they only survive the direct origin-to-first-hop transmission.
If an event is relayed, or fetched later via `/backfill`, the hashes are missing.

However, this is an acceptable constraint. The direct `/send` hop is precisely
where real-time early-warning detection is most valuable to prevent split-brain.
The `unsigned` dictionary on individual PDUs suffers from similar survival issues,
as it is routinely stripped or rewritten by intermediate servers.

### False alarms (DoS)
<!-- Edit marker. -->

If a malicious server intentionally forwards spoofed hashes in the transaction,
it could force the receiving server to continually trigger state resyncs.

**Mitigations:**

1. **Rate-limiting:** Receiving servers SHOULD rate-limit out-of-band state sync
   requests triggered by mismatching hints. Repetitive warning logs are unnecessary.
2. **Reputation:** Servers SHOULD track the reliability of peers. If a peer
   consistently sends mismatching hashes that do not reflect the actual resolved
   state or differ too wildly from the majority, the receiver should temporarily
   decrement that peer's reputability and the worthiness of their hints.

## Alternatives

### Hashes in the signed PDU

The primary alternative is placing the state hash directly into the signed
payload of the event, enforcing it as a protocol-level requirement.

**Disadvantages:**

- **Breaks Eventual Consistency:** Matrix relies on servers being slightly out
  of sync. Enforcing strict consensus on every event would cause massive
  fork-locking across the federation.
- **Bureaucracy:** Modifying the signed PDU alters the event's reference hash.
  This would require a massive global Room Version Upgrade and deprecate all
  older homeservers.

The transaction-level approach achieves the same diagnostic goal with zero
breakage and seamless backward compatibility.

### Hashes in the `unsigned` dictionary

Earlier iterations of this concept proposed placing the hashes in the `unsigned`
dictionary of the PDU.

**Disadvantages:**

- **Tampering:** The `unsigned` dictionary is not covered by any signature,
  allowing silent modification in transit.
- **Survival:** Like transaction-level hashes, `unsigned` data is frequently
  stripped by relays or backfill endpoints, offering no structural advantage
  over transaction-level hashes.

By moving the hashes to the `PUT /send` request body, the hashes are
automatically protected by the sending server's $X-Matrix$ authorization
headers, providing tamper-resistance on the primary hop for free.

## Security considerations

The core security principle of this proposal is that **state hints are strictly
advisory**.

Homeservers MUST NEVER use the accumulator hash as a source of truth to
construct, replace, or authorize state. All state resolution (State Res v2) and
DAG authorization rules MUST continue to rely strictly on signed, immutable
event data.

The hashes are diagnostic tools. If a hash is tampered with (which is protected
against on the primary hop by the transaction signature), the actual state of
the room remains mathematically secure. The worst-case outcome is a performance
degradation or diagnostic false alarm (triggering redundant state syncs), never
a security breach or state corruption.

Because the 32-byte digest is cryptographically secure (via `BLAKE2b-256`),
forging a different _state set_ with the same digest requires either a colliding
set under `LtHash16` (a lattice problem believed hard at these parameters, per
Bellare-Micciancio and the LtHash security analysis) or a second preimage /
collision in the `BLAKE2b-256` collapse. Both are currently believed
cryptographically infeasible.

## Test vectors

To assist implementers, the following test vectors are provided. They are
generated using the `BLAKE2Xb-2048` element expansion (with the domain prefix
`msc4502_lthash16\x00`), 16-bit little-endian wrapping lane
addition/subtraction, and `BLAKE2b-256` collapse digest.

### Empty state

The starting lattice $S_0$ is 2048 bytes of all zeros.

- Collapse digest:
  `200823e5158b3774c11b5c61850ada762f8264144a9bebec3ebac5a2adde67b8`

### Scenario 1: one element (addition)

Add event `m.room.member` with state key `@alice:example.com` and event ID
`$event_1`.

- Raw encoded element:
  `6d2e726f6f6d2e6d656d6265720040616c6963653a6578616d706c652e636f6d00246576656e745f31`
- Lattice $S_1$ (first 16 bytes): `bb622953b181356f0884390c7e309cf1`
- Collapse digest:
  `d8d3ac07b6152e0c6beddac611371082ff345c3ac1018aa8096fde848d0d0ebd`

### Scenario 2: add-then-remove (element removal)

Subtracting the expanded element for `$event_1` from lattice $S_1$ returns the
accumulator to the empty state.

- Lattice $S_{\text{back}}$ (first 16 bytes): `00000000000000000000000000000000`
- Collapse digest:
  `200823e5158b3774c11b5c61850ada762f8264144a9bebec3ebac5a2adde67b8`

### Scenario 3: two elements

Starting from $S_1$, add event `m.room.name` with empty state key `""` and event
ID `$event_2`.

- Raw encoded element: `6d2e726f6f6d2e6e616d650000246576656e745f32`
- Lattice $S_2$ (first 16 bytes): `384dd78be7edeff6c1e4027a656e437b`
- Collapse digest:
  `06457ed60e766a6caaa65804b92056b244ee7339850630b8dee69efc63e73b20`

### Scenario 4: instant replacement

Starting from $S_2$, replace the membership event for `@alice:example.com` with
event ID `$event_3`. This is performed by subtracting the expansion for
`$event_1` and adding the expansion for `$event_3`.

- Raw encoded element for `$event_3`:
  `6d2e726f6f6d2e6d656d6265720040616c6963653a6578616d706c652e636f6d00246576656e745f33`
- Lattice $S_3$ (first 16 bytes): `87c317f1e6d4fe59f2bebc9326356734`
- Collapse digest:
  `4eee9f4aa350d1dde5529a445edbd6f0b95c47c9e73c5335a117115ee2235f10`

## Unstable prefix

For experimental implementations, the features should be referred to using the
following unstable identifiers:

- The transaction payload key: `org.matrix.msc0F06.state_hashes`
- The reconciliation endpoint:
  `GET /_matrix/federation/unstable/org.matrix.msc0F06/state_accumulator/{room_id}`

## Dependencies

This proposal currently has no known dependencies, blockers, or open questions.
