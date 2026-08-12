# MSC00DC: Faster state group, via augmented HAMT

This proposal describes a non-normative local storage architecture leveraging a
32-way Hash Array Mapped Trie (HAMT) combined with MSC4500's `LtHash16` state
accumulators to accelerate Matrix state resolution.

## Background

Matrix homeservers spend a massive portion of their CPU and I/O budget managing
state resolution and querying historical room states. Existing implementations
suffer from systemic trade-offs:

1. **Delta chains / snapshot cliff:** To avoid rewriting massive $O(S)$ state
   maps on every event, engines like Synapse write $O(1)$ deltas but must
   periodically pause to write $O(S)$ full snapshots to prevent read-latency
   degradation. Historical point-queries require fetching the snapshot and
   decompressing the delta chain in memory.
2. **Branch obliviousness:** Engines using auto-incrementing integers for local
   State Group IDs cannot detect when two independent forks resolve to the exact
   same state, resulting in redundant storage and redundant state resolution
   math and disk I/O.
3. **CPU tax:** Engines that attempt to fix branch blindness by hashing the
   state (e.g., Conduit's `shortstatehash`) must sort the entire state map
   first, incurring a heavy $O(S \cdot \log S)$ CPU tax on every state event.

[MSC4500](4500-state-accumulators.md) introduces `LtHash16` as a wire-facing
state accumulator. [MSC4511](4511-part-a-topological-metadata-query-api.md)
introduces Merkleized metadata. This proposal bridges the two by defining a
local indexing structure: an **Augmented HAMT**.

## Proposal

Implementations MAY back their persistent state storage and live resolved state
maps with an immutable, structurally-shared 32-way CHAMP-style (Compressed Hash
Array Mapped Prefix) trie.

### Data structure

The state map `(type, state_key) -> event_id` is stored in the HAMT.

- **Internal nodes:** Contain a 32-bit `datamap` (marking leaf children) and a
  32-bit `nodemap` (marking internal children). They also cache a 64-bit
  non-cryptographic local **structural hash** computed from the bitmaps and
  child hashes.
- **Leaf nodes:** Contain the actual state tuples.
- **State-group root:** Caches the full 2048-byte `LtHash16` lattice for the
  entire state group.

### `O(1)` State group ID generation & deduplication

Instead of opaque database integers or $O(S \cdot \log S)$ sort-and-hash
routines, implementations use the `LtHash16` lattice cached at the HAMT root as
the deterministic State Group ID.

Because `LtHash` is homomorphic, the ID for a new state is computed in $O(1)$
time by taking the previous root's lattice, subtracting the removed state event,
and adding the new state event. If two independent network branches resolve to
the exact same state, their root lattices will naturally collide, allowing the
server to instantly deduplicate the state group without $O(S)$ comparisons.

### Fast delta isolation algorithm

When comparing two state maps (e.g., $A$ and $B$) during a deep rebuild or
network split, the delta $\Delta$ can be extracted without delta chain
decompression in $O(|\Delta| \cdot \log_{32} S)$ time:

1. **Short-circuit via LtHash:** If the 2048-byte `LtHash16` lattices at the
   roots of $A$ and $B$ are identical, the state maps have converged. Return an
   empty $\Delta$ in $O(1)$ time.
2. **Structural sharing:** Recursively walk the HAMT. If the pointer identities
   of node $A'$ and node $B'$ match, skip the subtree.
3. **Structural hashing:** If pointers differ (e.g., across process boundaries
   or database reloads), but the 64-bit `structural_hash` matches, skip the
   subtree.
4. **Deep diff:** Only when structural hashes differ, iterate the 32-bit CHAMP
   bitmaps and recurse into differing children to extract the exact mismatched
   leaves.

## Trade-offs

Replacing legacy delta chains with a persistent HAMT introduces specific costs:

- **Dependent reads:** Point queries require $\log_{32} S$ dependent I/O lookups
  (e.g., ~4 node fetches for a room with 50,000 state events) rather than a
  single hash-table probe.
- **Garbage collection:** Because nodes are structurally shared across multiple
  state groups, pruning old history requires implementing reference counting or
  mark-and-sweep garbage collection over the trie nodes.
- **Auth-chain sorting:** While this structure optimizes state resolution delta
  extraction, implementations must still fetch the auth-chain to topologically
  sort the isolated $\Delta$.

## Security considerations

The HAMT and its 64-bit structural hashes are strictly **local indexing aids**.
They are never transmitted over federation. The wire-facing equality commitment
remains the `LtHash16` digest (MSC4500), which provides the cryptographic
guarantees. The 64-bit local hash is sufficient because it operates only as a
performance accelerator against honest but unshared memory, not against active
adversaries.
