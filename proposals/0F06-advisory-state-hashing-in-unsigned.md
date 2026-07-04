# MSC0F06: Advisory State Hashing in Unsigned

Matrix is designed around **Eventual Consistency**. Servers build a decentralized
Directed Acyclic Graph (DAG) of events and use State Resolution (e.g., State Res
v2) to converge on a shared state. However, federation lag, network partitions,
or implementation bugs can cause servers to diverge in their view of a room's
state.

When servers diverge, the result is often a "split-brain" room or a state reset.
Because Matrix lacks an out-of-band mechanism to rapidly verify state alignment,
servers often only realize they are desynchronized long after the divergence
occurred—typically when an authorization failure happens (e.g., a user is
incorrectly rejected from joining).

We need an "early-warning system" to mathematically prove that servers share
the exact same view of the room state at a specific point in the DAG.

While it is tempting to enforce strict consensus by adding state hashes directly
into the signed payload of the Protocol Data Unit (PDU), doing so would
fundamentally break Matrix's eventual consistency model. A server missing a
single state event would be permanently "fork-locked," unable to accept new
messages.

This proposal introduces an advisory, non-blocking state verification mechanism
by embedding a global grand additive accumulator (a form of homomorphic state
hash) into the `unsigned` dictionary of an event.

## Proposal

This proposal leverages global grand additive accumulators (such as LtHash).

When a homeserver sends an event over federation, it calculates the $O(1)$
additive hash of the room's state at the moment the event is created. It then
collapses this mathematical state into a standard 32-byte digest and includes
it in the `unsigned` object of the event.

### The `unsigned` Fields

Two new keys are introduced inside the `unsigned` object of a PDU:

- `state_hash_before`: The 32-byte digest (represented as a 64-character hex
  string) of the room state evaluated exactly at the `prev_events`, excluding
  the current event.
- `state_hash_after`: The 32-byte digest of the room state after the current
  event is applied. (For non-state events, this will be identical to
  `state_hash_before`).

```json
{
  "type": "m.room.message",
  "sender": "@alice:example.com",
  "content": {
    "body": "Hello world",
    "msgtype": "m.text"
  },
  "unsigned": {
    "age": 42,
    "org.matrix.msc0F06.state_hash_before": "a85dfe1d480705482f37d582ffa27611117b577f8734532a5a6379bc666b2104",
    "org.matrix.msc0F06.state_hash_after": "a85dfe1d480705482f37d582ffa27611117b577f8734532a5a6379bc666b2104"
  }
}
```

### Network Payload Efficiency

Event bloat is a critical concern in Matrix federation. Homomorphic hashing
algorithms (like LtHash16) require a large internal lattice state (typically
2048 bytes of 16-bit integers) to perform their commutative math.

**This 2048-byte state is never transmitted over the network.**

Instead, the sending server runs a standard, fast hash function (like BLAKE3
or BLAKE2b) over its internal 2048-byte lattice to produce a **32-byte digest**.

Adding both `state_hash_before` and `state_hash_after` consumes approximately
**160 bytes** of JSON footprint (including keys, quotes, and hex encoding). This
represents a negligible fraction (~0.24%) of the 65 KB PDU limit.

### Receiver Behavior

The receiving server independently maintains its own full homomorphic state
lattice in local memory/storage.

1. It receives the PDU with the 160-byte `unsigned` payload.
2. It collapses its own local lattice at that point in the DAG into a 32-byte
   digest.
3. It compares its local digest to the incoming digest.
4. **Match:** The servers have mathematically proven they share the exact same
   view of the room state.
5. **Mismatch:** The receiver has instantly detected a state split. It can
   automatically trigger a background `/get_missing_events` or state resync
   operation to heal the split before it compounds.

Because the checks are in `unsigned`, they are strictly **advisory**. If the
hashes do not match, the event is *still accepted* and processed according to
standard Matrix rules. This prevents the network from stalling.

## Deprecating State Group Transitions (Local Optimization)

While this proposal primarily addresses federation, the adoption of a global
grand additive accumulator profoundly optimizes local homeserver architecture.

Currently, homeservers like Synapse manage state by storing a graph of "state
groups." Because generating a cryptographic hash of an entire room state is an
$O(N)$ operation that would cause massive CPU spikes, Synapse cannot easily
detect if two discrete branches of a DAG result in the exact same state. To cope,
it uses "state group transitions"—storing pointers and deltas. Computing the
actual state, or determining if two state groups are identical, requires walking
this chain backward and expanding all deltas into memory.

With an $O(1)$ additive accumulator, the mathematical state digest *is* the state
group identifier.

1. **Instant Deduplication:** If two different branches of a DAG converge on the
   exact same state (a highly common occurrence), their 32-byte accumulator
   digests will perfectly match. The homeserver instantly deduplicates them into
   a single State Group ID without expanding or comparing dictionaries.
2. **$O(1)$ Equality Checks:** During State Resolution v2, determining if
   diverging branches have different states becomes an instant 32-byte integer
   comparison rather than a complex graph traversal.

This mathematical guarantee eliminates the computational overhead of state group
transitions, allowing them to be deprecated as a mechanism for state equality
and identity, relegating deltas strictly to storage compression.

## Potential issues

### 1. Untrusted Transit and Silent Stripping

Because the `unsigned` dictionary is not covered by the event's cryptographic
signature, any intermediate relay server can modify or strip the hashes in
transit.

- **Stripping:** If an intermediate server removes the hashes (to save bandwidth
  or due to legacy software), the receiving server simply falls back to the
  current Matrix baseline. The network degrades gracefully.
- **Modification:** A malicious or buggy server could alter the hash to a
  garbage value, causing a false mismatch.

### 2. False Alarms (Denial of Service)

If a malicious server intentionally forwards spoofed hashes, it could force the
receiving server to continually trigger state resync operations, acting as a
minor Denial of Service (DoS) vector.

**Mitigations:**

1. **Rate-Limiting:** Receiving servers MUST heavily rate-limit out-of-band
   state sync requests triggered by `unsigned` mismatches.
2. **Reputation:** Servers SHOULD track the reliability of peers. If a peer
   consistently forwards mismatching `unsigned` hashes that do not reflect the
   actual resolved state, the receiver should temporarily ignore `unsigned`
   hints from that peer.

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

The advisory `unsigned` approach achieves the same diagnostic goal with zero
breakage and seamless backward compatibility.

### 2. "Signed Unsigned" Metadata

To completely eliminate the untrusted transit vector without requiring a Room
Version Upgrade, a future MSC could introduce Origin-Signed Metadata within the
`unsigned` dictionary:

```json
  "unsigned": {
    "org.matrix.msc0F06.state_hash_before": "...",
    "org.matrix.msc0F06.signatures": {
      "example.com": {
        "ed25519:key1": "YmFzZTY0..."
      }
    }
  }
```

This ensures intermediate servers cannot tamper with the hashes, but increases
the payload size. This proposal defers this complexity, as the reputation and
rate-limiting mitigations are sufficient for an advisory system.

## Security considerations

The core security principle of this proposal is that **state hints in `unsigned`
are strictly advisory**.

Homeservers MUST NEVER use the `unsigned` hash as a source of truth to
construct, replace, or authorize state. All state resolution (State Res v2) and
DAG authorization rules MUST continue to rely strictly on signed, immutable
event data.

The hashes are diagnostic tools. If a hash is tampered with, the actual state
of the room remains mathematically secure. The worst-case outcome is a
performance degradation or diagnostic false alarm (triggering redundant state
syncs), never a security breach or state corruption.

Because the 32-byte digest is cryptographically secure, finding a malicious
state fork that produces the same hash as the honest state (forging agreement)
requires a preimage attack against the underlying hash function, which is
cryptographically infeasible.

## Unstable prefix

For experimental implementations, this feature should be referred to using the
unstable identifiers:

- `org.matrix.msc0F06.state_hash_before`
- `org.matrix.msc0F06.state_hash_after`

## Dependencies

This proposal logically builds upon the concepts of O(1) state hashing as
explored in MSC0F05, but does not strictly depend on any specific homomorphic
hashing algorithm being standardized, so long as federating servers agree on
the algorithm out-of-band or via negotiation.
