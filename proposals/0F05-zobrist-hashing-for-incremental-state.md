# MSC0F05: Incremental Room State Hashing via Zobrist Hashing

**Authors:** [Your Name/Handle]
**Date:** 2026-07-03
**Version:** 1.0
**Status:** Draft

---

## Introduction

In the Matrix protocol, a room's state at any given event is represented as a
dictionary mapping a `(event_type, state_key)` tuple to a single `event_id`.
In large public rooms, this state dictionary can contain tens of thousands
of events.

Homeservers frequently need to compare state sets, group identical state sets
together, and compute unique cryptographic identifiers (often referred to as
"state group hashes") for a room's active state. For example, during state
resolution v2, homeservers must determine whether the state at different tips
of the DAG has diverged and find the common ancestors.

Currently, calculating a cryptographic hash of a room state set is highly
inefficient. A homeserver must:

1. Fetch all active state events from the database.
2. Sort the events by their type and state key.
3. Serialize the entire state dictionary into Canonical JSON format.
4. Hash the resulting string using a hash function like SHA-256.

This process is computationally expensive, especially since a single state
update (e.g., a user updating their display name) requires re-fetching,
re-sorting, re-serializing, and re-hashing the entire state map.

To solve this performance bottleneck, we propose introducing **Zobrist
Hashing** as a standardized mechanism for maintaining a rolling, incremental,
and cryptographically secure $O(1)$ hash of room state.

---

## Proposal

Zobrist hashing is an algebraic signature technique (often used in computer
board games like Chess to represent game board states) that leverages the
properties of the bitwise XOR ($\oplus$) operator.

Because XOR is commutative ($A \oplus B = B \oplus A$) and associative
($A \oplus (B \oplus C) = (A \oplus B) \oplus C$), we can construct a state
hash that is independent of the order of its elements and can be updated
incrementally.

### 1. Mathematical Definition

Let a room state set $S$ be a set of state entries, where each entry is a
tuple:
$$E = (T, K, I)$$

Where:

- $T$ is the `event_type` (string).
- $K$ is the `state_key` (string).
- $I$ is the `event_id` (string).

We define a cryptographically secure projection function $H(E)$ that maps a
state entry $E$ to a 256-bit bitstring:

$$
H(E) = \text{SHA-256}(T \mathbin{\Vert} \text{"\x00"}
\mathbin{\Vert} K \mathbin{\Vert} \text{"\x00"}
\mathbin{\Vert} I)
$$

The Zobrist hash of the state set $S = \{E_1, E_2, \dots, E_n\}$ is defined as
the bitwise XOR of the hashes of all its constituent entries:
$$\text{Zobrist}(S) = H(E_1) \oplus H(E_2) \oplus \dots \oplus H(E_n)$$

If $S$ is empty, its Zobrist hash is a 256-bit string of zeros:
$$\text{Zobrist}(\emptyset) = 0^{256}$$

---

### 2. Incremental $O(1)$ Updates

When the room state changes, the homeserver does not need to recompute the
entire Zobrist hash from scratch. Instead, it can perform local, constant-time
$O(1)$ XOR operations based on the changes:

#### Adding a State Entry

If a new state entry $E_{\text{new}}$ is added to the room state (with no
prior entry matching its type $T$ and state key $K$):
$$\text{Zobrist}(S') = \text{Zobrist}(S) \oplus H(E_{\text{new}})$$

#### Removing a State Entry

If a state entry $E_{\text{old}}$ is removed from the room state (with no
replacement):
$$\text{Zobrist}(S') = \text{Zobrist}(S) \oplus H(E_{\text{old}})$$

#### Replacing a State Entry

If an existing state entry $E_{\text{old}}$ is replaced by a new state
entry $E_{\text{new}}$ for the same type $T$ and state key $K$:

$$
\text{Zobrist}(S') = \text{Zobrist}(S) \oplus
H(E_{\text{old}}) \oplus H(E_{\text{new}})
$$

These operations are extremely fast, requiring only a single SHA-256 hash
evaluation of the modified entry and a couple of bitwise XOR operations.

---

### 3. Protocol Integration

This specification proposes Zobrist hashing for two primary use cases:

#### Local Homeserver Optimizations

Homeservers (such as Synapse, Dendrite, or Conduit) can implement Zobrist
hashing internally to index state groups and compare state sets. Instead of
querying entire state dictionaries from the database to detect state group
equality, they can compare 256-bit Zobrist hashes.

#### Federation API Extensions

To speed up federation state reconciliation (e.g., during `/state_ids` or
`/backfill` calls), federating servers can optionally exchange and compare
Zobrist hashes of room state. This allows immediate, out-of-band detection
of state divergence without needing to exchange large lists of event IDs.

---

## Potential issues

### 1. Birthday Bound and XOR Collisions

Unlike standard hash functions, XOR-based set hashing has a different
collision profile. For a $b$-bit hash size, the probability of finding a
collision in a set of size $N$ is governed by the Birthday Paradox.

By using a 256-bit hash size (derived from SHA-256), the safety margin is
extraordinarily large. The number of states required to have a $10^{-15}$
chance of a collision is approximately $2^{100}$ states—which is many orders
of magnitude larger than the total number of states that will ever exist
across all Matrix homeservers in history.

---

## Alternatives

### 1. Merkle Trees (e.g., Merkle Patricia Tries)

Instead of Zobrist hashing, state sets could be represented as Merkle trees.

- **Advantage:** Merkle trees provide cryptographic proofs of membership and
  exclusion.
- **Disadvantage:** Merkle tree updates require $O(\log N)$ hash operations and
  complex tree-rebalancing or trie-parsing code. Zobrist hashing is $O(1)$ and
  trivial to implement (requiring only a basic XOR loop), making it far more
  performant for bulk database synchronization and local checks.

### 2. Full State Canonical JSON Hashing

This is the current default, where the entire state is serialized and hashed.

- **Disadvantage:** It is $O(N)$ in time and memory, creating massive CPU
  spikes in large rooms when state is resolved or modified.

---

## Security considerations

### 1. Hash Projection Collision Resistance

The security of Zobrist hashing relies entirely on the projection function
$H(E)$ behaving like a random oracle. If an attacker can find two different
state entries $E_A$ and $E_B$ such that $H(E_A) = H(E_B)$, they could substitute
$E_A$ for $E_B$ in the room state without changing the Zobrist hash,
constituting a state-substitution attack.

By mandating **SHA-256** with strict null-byte delimiters (`\x00`) separating
fields, we ensure that:

1. Field injection attacks (e.g., creating an event type with a null byte to
   spoof a state key) are completely prevented.
2. Finding a collision or preimage remains as hard as breaking SHA-256, which
   is currently cryptographically infeasible.

---

## Unstable prefix

For experimental implementations, this feature should be referred to using
the unstable identifier `org.matrix.msc0F05`.

---

## Dependencies

This proposal does not depend on any outstanding, unaccepted MSCs.
