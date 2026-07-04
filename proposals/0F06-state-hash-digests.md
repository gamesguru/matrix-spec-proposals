# MSC0F06: State accumulator endpoint and transaction digests

<!--
[Rendered](https://github.com/gamesguru/matrix-spec-proposals/blob/guru/4499-state-hash-digests.md)
 -->

Matrix is designed around **eventual consistency**. Servers build a
decentralized DAG and use state resolution to converge on a shared state.
However, federation lag, network partitions, or implementation bugs can cause
servers to diverge in their view of a room's state.

When servers diverge, the result can be a serious nuisance. Matrix lacks an
out-of-band or real-time mechanism to for state verification or re-alignment;
servers often only learn of de-synchronization once they disagree on a much
later authorization failure (e.g., another user's join is incorrectly rejected).

I present an "early-warning system" which rapidly confirms incremental state
consensus, or signals (with traceable proof) as to its divergence, so servers
know they share the exact same view of a room at a given point in the DAG (or
where they diverged).

It is tempting to enforce strict consensus by adding state hashes directly into
the signed payload of the PDU, but doing so is too rigid for the fundamentally
dynamic "Matrix" model of eventual consistency. Administrative actions may win
the topological power sort, shadowing or clobbering previously consolidated
state groups. A server missing a single state event would be permanently forked
out, unable to accept new messages.

This proposal does not impose any verification requirements on PDU handling. It
seeks to act as a secondary state convergence mechanism, while simultaneously
**replacing state group transitions** and naive BFS sweeps with a cheap,
bitwise, commutative, invertible, collision-resistant 2048-bit `LtHash16`
accumulator function.

The accumulator under question may be called 'homomorphic' and solves the
following problem: "Given the hash of an input, along with a small update to the
input, how can we compute the hash of the new input with its update applied,
without having to recompute the entire hash from scratch?"

Should this proposal be accepted, homeserves must embed a canonical
`BLAKE2b-256` digest (of their 2048-bit state accumulator integer) in the
`PUT /_matrix/federation/v1/send/{txnId}` transaction body.

## Proposal

Rather than attaching hashes to individual events (which are routinely stripped,
rewritten, or relayed by intermediate servers), this proposal places the hashes
in the body of the federation transaction.

When a homeserver sends a transaction over federation, it calculates the sum
hash of the room's state exactly at the DAG tip of each included PDU.

It then collapses this vectorized state into a standard 32-byte digest and
includes it in the transaction payload.

### Algorithm specification

To ensure an interoperability the algorithm is strictly defined as follows:

1. **Element Encoding:** For each active state event in the room, the element is
   serialized as a UTF-8 string concatenation:
   `type || "\x00" || state_key || "\x00" || event_id`.
2. **Domain Separation & Hashing:** The element string is hashed using
   `BLAKE2b-256`, prefixed with a domain separation tag:
   `BLAKE2b-256("msc0f06_lthash16" || element_encoding)`.
3. **Accumulator Lattice (LtHash16):** The system uses LtHash16. The local state
   is a lattice of 1024 16-bit integers (2048 bytes). The 32-byte element hash
   is mapped to this lattice and added using 16-bit wrapping addition.
4. **Collapse Function:** The final 2048-byte lattice is collapsed into a
   32-byte digest using a final pass of `BLAKE2b-256` over the raw lattice
   bytes. This 32-byte digest (represented as a 64-character hex string) is the
   value transmitted over the network.

### Transaction payload

A new `state_hashes` dictionary is introduced at the root of the
`PUT /_matrix/federation/v1/send/{txnId}` request body. It maps the IDs of the
PDUs included in the transaction to their respective `before` and `after`
digests.

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
4. **Match:** The servers have proven they have the same view of the room state.
5. **Mismatch:** The receiver detects a state split. It can automatically
   trigger a background `/get_missing_events` or state resync operation to heal,
   while also alerting the sender with a response including their digest value.

Because the checks are advisory, if the hashes do not match, the PDU is _still
accepted_ and processed according to standard Matrix rules. Returning a
`M_INVALID_PARAM` seems excessive and a bit out of place here.

## Reconciliation (accumulator endpoint)

When the 32-byte digest triggers a mismatch alarm, the receiving server knows it
is desynchronized, but the digest itself cannot reveal _which_ events are
missing. Historically, servers would fall back to `GET /state_ids`, exchanging
lists of tens of thousands of event IDs to find a single missing state event.

Because this proposal uses an additive accumulator, we can bypass this heavy
lookup entirely using homomorphic subtraction.

This proposal introduces a new federation endpoint:
`GET /_matrix/federation/v1/state_accumulator/{room_id}?event_id={event_id}`

If Server B detects a mismatch from Server A:

1. Server B calls the `state_accumulator` endpoint on Server A.
2. Server A responds with its raw, uncollapsed 2048-byte LtHash16 lattice
   (Base64 encoded) for the state exactly at `event_id`.
3. Server B decodes the lattice and performs 16-bit wrapping subtraction against
   its own local lattice:
   $Lattice_{Delta} = Lattice_B - Lattice_A \pmod{2^{16}}$.
4. The delta lattice tells you _that_ you've diverged and roughly _how much_,
   and lets you **bisect** to _where_. Because both servers can produce digests
   at historical DAG points, the receiver can query accumulators at $O(\log N)$
   depth to find the earliest event where the digests diverged.

It is important to note that the delta lattice cannot name events you have never
seen—a lattice sum isn't invertible to its summands (the property that makes it
collision-resistant). Once the exact divergence point is isolated via bisection,
enumeration and healing are delegated to MSC4500's `room_diff` and
`room_events`.

## Synergy with MSC4500 (Event Set Reconciliation)

This proposal (MSC4502) and MSC4500 (`room_digest` / `room_diff`) solve
fundamentally different sets. MSC4502's accumulator covers the room's _current
state set_ at a specific DAG point. MSC4500's bloom digest covers the _event
set_ (the timeline of PDUs).

Because state divergence almost always implies event-set divergence, the two
proposals form a clean pipeline:

1. **Detect (MSC4502, passive, free):** Every `/send` carries before/after
   digests. Active rooms get continuous state-consistency checks with zero extra
   round trips.
2. **Localize (MSC4502, active):** On mismatch, bisection via the
   `state_accumulator` endpoint isolates the divergence point.
3. **Enumerate + Heal (MSC4500):** `room_diff` (with a `scope: "state"`
   parameter) fetches what is missing, auth chains included.

Because MSC4502 gives active rooms free passive detection, MSC4500's periodic
polling can back off significantly for rooms with recent inbound transactions.

## State identity and local DB optimizations

While this proposal primarily addresses federation, the adoption of a grand sum
accumulator profoundly optimizes local homeserver architecture.

Currently, homeservers like Synapse manage state by storing a graph of "state
groups," utilizing delta chains (pointers and changes) because generating a hash
of an entire room state is an $O(N)$ operation.

With an $O(1)$ sum accumulator, the mathematical state digest _is_ the state
group identifier.

1. **Instant Deduplication:** If two different branches of a DAG converge on the
   exact same state (a highly common occurrence), their 32-byte accumulator
   digests will perfectly match. The homeserver instantly deduplicates them into
   a single State Group ID without expanding or comparing dictionaries.
2. **$O(1)$ Equality Checks:** During State Resolution v2, determining if
   diverging branches have different states becomes an instant 32-byte integer
   comparison rather than a complex graph traversal.

This mathematical guarantee provides a perfect $O(1)$ identity mechanism. While
delta chains remain absolutely necessary to materialize state into memory and to
compute conflict sets during state resolution, the accumulator relegates deltas
purely to storage compression and retrieval, eliminating the need to walk chains
simply to determine state equality.

## Potential issues

### Direct-hop survival (ease of audit)

Because the hashes are attached to the transaction body rather than the
individual PDUs, they only survive the direct origin-to-first-hop transmission.
If an event is relayed, or fetched later via `/backfill`, the hashes will not be
present.

However, this is an acceptable constraint. The direct `/send` hop is precisely
where real-time early-warning detection is most valuable to prevent split-brain
rooms. The `unsigned` dictionary on individual PDUs suffers from similar
survival issues, as it is routinely stripped or rewritten by intermediate
servers.

### 2. False alarms (DoS)

If a malicious server intentionally forwards spoofed hashes in the transaction,
it could force the receiving server to continually trigger state resync
operations, acting as a minor Denial of Service (DoS) vector.

**Mitigations:**

1. **Rate-limiting:** Receiving servers SHOULD rate-limit out-of-band state sync
   requests triggered by mismatching hints.
2. **Reputation:** Servers SHOULD track the reliability of peers. If a peer
   consistently sends mismatching hashes that do not reflect the actual resolved
   state, the receiver should temporarily decrement that peer's reputability and
   the worthiness of their hints.

## Alternatives

### 1. Hashes in the Signed PDU

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

### 2. Hashes in the `unsigned` Dictionary

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
finding a malicious state fork that produces the same hash as the honest state
(forging agreement) requires a preimage attack against the underlying hash
function, which is currently believed cryptographically infeasible.

## Unstable prefix

For experimental implementations, the features should be referred to using the
following unstable identifiers:

- The transaction payload key: `org.matrix.msc0F06.state_hashes`
- The reconciliation endpoint:
  `GET /_matrix/federation/unstable/org.matrix.msc0F06/state_accumulator/{room_id}`

## Dependencies

This proposal currently has no known dependencies, blockers, or open questions.
