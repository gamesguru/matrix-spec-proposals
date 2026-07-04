# MSC0F06: Advisory State Hashing in Federation Transactions

**Authors:** [Your Name/Handle]
**Date:** 2026-07-04
**Version:** 1.0
**Status:** Draft

---

## Introduction

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
by embedding a global grand additive accumulator (LtHash16) into the
`PUT /_matrix/federation/v1/send/{txnId}` transaction body.

## Proposal

This proposal leverages global grand additive accumulators. Rather than attaching
hashes to individual events (which are routinely stripped, rewritten, or relayed
by intermediate servers), this proposal places the hashes in the body of the
federation transaction.

When a homeserver sends a transaction over federation, it calculates the $O(1)$
additive hash of the room's state exactly at the DAG tip of each included PDU.
It then collapses this mathematical state into a standard 32-byte digest and
includes it in the transaction payload.

### Algorithm Specification

To ensure an interop mechanism where all implementations byte-identically agree,
the algorithm cannot be deferred or negotiated. It is strictly defined as follows:

1. **Element Encoding:** For each active state event in the room, the element is
   serialized as a UTF-8 string concatenation:
   `type || "\x00" || state_key || "\x00" || event_id`.
2. **Domain Separation & Hashing:** The element string is hashed using
   BLAKE2b-256, prefixed with a domain separation tag:
   `BLAKE2b-256("msc0f06_lthash16" || element_encoding)`.
3. **Accumulator Lattice (LtHash16):** The system uses LtHash16. The local state
   is a lattice of 1024 16-bit integers (2048 bytes). The 32-byte element hash is
   mapped to this lattice and added using 16-bit wrapping addition.
4. **Collapse Function:** The final 2048-byte lattice is collapsed into a 32-byte
   digest using a final pass of BLAKE2b-256 over the raw lattice bytes. This
   32-byte digest (represented as a 64-character hex string) is the value
   transmitted over the network.

### The Transaction Payload

A new `state_hashes` dictionary is introduced at the root of the
`PUT /_matrix/federation/v1/send/{txnId}` request body. It maps the IDs of the
PDUs included in the transaction to their respective `before` and `after` digests.

* `before`: The 32-byte digest of the room state evaluated exactly at the PDU's
  `prev_events`, excluding the current event.
* `after`: The 32-byte digest of the room state after the current PDU is applied.
  (For non-state events, this will be identical to `before`).

```json
{
  "origin": "example.com",
  "pdus": [
    {
      "type": "m.room.message",
      "event_id": "$abc123def456",
      "sender": "@alice:example.com",
      "content": {
        "body": "Hello world",
        "msgtype": "m.text"
      }
    }
  ],
  "state_hashes": {
    "$abc123def456": {
      "before": "a85dfe1d480705482f37d582ffa27611117b577f8734532a5a6379bc666b2104",
      "after": "a85dfe1d480705482f37d582ffa27611117b577f8734532a5a6379bc666b2104"
    }
  }
}
```

### Network Payload Efficiency

Event bloat is a critical concern in Matrix federation. The full LtHash16 lattice
state (2048 bytes) is **never transmitted over the network.**

By transmitting only the collapsed 32-byte digests, the payload footprint is
negligible. Adding both `before` and `after` hashes consumes approximately 160
bytes of JSON overhead per PDU in the transaction.

### Receiver Behavior

The receiving server independently maintains its own LtHash16 lattice in local
storage.

1. It receives the `/send` transaction with the `state_hashes` payload.
2. It collapses its own local lattice at the corresponding point in the DAG into
   a 32-byte digest.
3. It compares its local digest to the incoming digest.
4. **Match:** The servers have mathematically proven they share the exact same
   view of the room state.
5. **Mismatch:** The receiver has instantly detected a state split. It can
   automatically trigger a background `/get_missing_events` or state resync
   operation to heal the split before it compounds.

Because the checks are advisory, if the hashes do not match, the PDU is *still
accepted* and processed according to standard Matrix rules. This prevents the
network from stalling.

## State Identity and Local Database Optimization

While this proposal primarily addresses federation, the adoption of a global
grand additive accumulator profoundly optimizes local homeserver architecture.

Currently, homeservers like Synapse manage state by storing a graph of "state
groups," utilizing delta chains (pointers and changes) because generating a hash
of an entire room state is an $O(N)$ operation.

With an $O(1)$ additive accumulator, the mathematical state digest *is* the state
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

### 1. Direct-Hop Survival (Topology Constraints)

Because the hashes are attached to the transaction body rather than the individual
PDUs, they only survive the direct origin-to-first-hop transmission. If an event
is relayed, or fetched later via `/backfill`, the hashes will not be present.

However, this is an acceptable constraint. The direct `/send` hop is precisely
where real-time early-warning detection is most valuable to prevent split-brain
rooms. The `unsigned` dictionary on individual PDUs suffers from similar survival
issues, as it is routinely stripped or rewritten by intermediate servers.

### 2. False Alarms (Denial of Service)

If a malicious server intentionally forwards spoofed hashes in the transaction,
it could force the receiving server to continually trigger state resync
operations, acting as a minor Denial of Service (DoS) vector.

**Mitigations:**

1. **Rate-Limiting:** Receiving servers MUST heavily rate-limit out-of-band
   state sync requests triggered by mismatching hints.
2. **Reputation:** Servers SHOULD track the reliability of peers. If a peer
   consistently sends mismatching hashes that do not reflect the actual resolved
   state, the receiver should temporarily ignore hints from that peer.

## Alternatives

### 1. Hashes in the Signed PDU

The primary alternative is placing the state hash directly into the signed
payload of the event, enforcing it as a protocol-level requirement.

**Disadvantages:**

* **Breaks Eventual Consistency:** Matrix relies on servers being slightly out
  of sync. Enforcing strict consensus on every event would cause massive
  fork-locking across the federation.
* **Bureaucracy:** Modifying the signed PDU alters the event's reference hash.
  This would require a massive global Room Version Upgrade and deprecate all
  older homeservers.

The transaction-level approach achieves the same diagnostic goal with zero
breakage and seamless backward compatibility.

### 2. Hashes in the `unsigned` Dictionary

Earlier iterations of this concept proposed placing the hashes in the `unsigned`
dictionary of the PDU.

**Disadvantages:**

* **Tampering:** The `unsigned` dictionary is not covered by any signature,
  allowing silent modification in transit.
* **Survival:** Like transaction-level hashes, `unsigned` data is frequently
  stripped by relays or backfill endpoints, offering no structural advantage over
  transaction-level hashes.

By moving the hashes to the `PUT /send` request body, the hashes are automatically
protected by the sending server's $X-Matrix$ authorization headers, providing
tamper-resistance on the primary hop for free.

## Security considerations

The core security principle of this proposal is that **state hints are strictly
advisory**.

Homeservers MUST NEVER use the accumulator hash as a source of truth to construct,
replace, or authorize state. All state resolution (State Res v2) and DAG
authorization rules MUST continue to rely strictly on signed, immutable event data.

The hashes are diagnostic tools. If a hash is tampered with (which is protected
against on the primary hop by the transaction signature), the actual state of the
room remains mathematically secure. The worst-case outcome is a performance
degradation or diagnostic false alarm (triggering redundant state syncs), never
a security breach or state corruption.

Because the 32-byte digest is cryptographically secure (via BLAKE2b-256), finding
a malicious state fork that produces the same hash as the honest state (forging
agreement) requires a preimage attack against the underlying hash function, which
is cryptographically infeasible.

## Unstable prefix

For experimental implementations, the key should be referred to using the
unstable identifier: `org.matrix.msc0F06.state_hashes`

## Dependencies

This proposal has no unaccepted dependencies.
