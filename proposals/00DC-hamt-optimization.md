# MSC00DC: Faster state group, via augmented HAMT

This proposal describes a non-normative local storage architecture leveraging a
32-way Hash Array Mapped Trie (HAMT) combined with MSC4500's `LtHash16` state
accumulators to accelerate Matrix state resolution. Nothing here is wire-facing:
the structure is an implementation detail servers MAY adopt locally, never
transmitted over federation, and it requires no change to MSC4511
canonicalization or any other normative text.

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
3. **Local-only state identity:** Engines that attempt to fix branch
   obliviousness by hashing the state (e.g., Conduit's `shortstatehash`) still
   compute that hash over locally-assigned short IDs. The hash is no more
   portable than the integer it replaces — it can only ever be compared against
   another hash produced by the same server's local ID allocation, so two
   servers that resolve to identical state still can't recognize it without an
   $O(S)$ materialized comparison. (The naive sort-then-hash construction also
   costs $O(S \cdot \log S)$ CPU, but that tax is on the order of a millisecond
   for realistic $S$ and isn't the load-bearing defect — local-only identity
   is.)

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

- **Internal nodes:** Contain a 32-bit `datamap` (marking inlined-entry
  children) and a 32-bit `nodemap` (marking internal-node children). CHAMP
  inlines leaf entries directly into the parent node rather than boxing them as
  child nodes, so each internal node also caches a per-node `LtHash16`
  sub-lattice, computed homomorphically as the sum of: a keyed digest of every
  inlined `(type, state_key, event_id)` tuple marked in `datamap`, plus the
  cached sub-lattices of every child marked in `nodemap`. This digest is over
  logical content only — the two bitmaps and the (key, digest) pairs they index
  — never over physical array layout. A reference layout may pack inlined
  entries from the front of a single backing array and child pointers from the
  back; that packing choice must not leak into the digest, or
  independently-written implementations produce non-reproducible sub-lattices
  for identical content.
- **Collision nodes:** Where the 5-bit hash-fragment path is exhausted and
  distinct keys still collide, entries are held in a node with no intrinsic
  ordering. Its digest contribution MUST use a fixed canonical ordering (e.g.
  sort by raw key bytes) — an unordered digest is nondeterministic and breaks
  cross-implementation and cross-reload comparison.
- **State-group root:** Its cached sub-lattice _is_ the full `LtHash16`
  accumulator for the entire state group (see below) — there is no separate
  root-only hash.

**Canonicality invariant.** Subtree-lattice skipping (below) is sound only if
identical content implies identical trie shape. CHAMP gives this under insertion
— shape is determined purely by hash-prefix and bitmap occupancy, independent of
insertion order. It does **not** hold under deletion unless implementations
enforce the standard CHAMP repair invariant: when a removal leaves a node
holding a single entry and no children, that entry MUST be inlined into the
nearest ancestor with other content, rather than left as a degenerate
single-entry node. Skipping this repair reintroduces insertion-order-dependent
shape and silently breaks structural sharing across otherwise-identical state
maps.

### `O(1)` State group ID generation & deduplication

Instead of opaque database integers or $O(S \cdot \log S)$ sort-and-hash
routines, implementations use the `LtHash16` lattice cached at the HAMT root as
the deterministic State Group ID.

Because `LtHash` is homomorphic, the ID for a new state is computed in $O(1)$
time by taking the previous root's lattice, subtracting the removed state event,
and adding the new state event. If two independent network branches resolve to
the exact same state, their root lattices will naturally collide, allowing the
server to instantly deduplicate the state group without $O(S)$ comparisons.

The 2048-byte lattice itself is a poor primary key — 256× the width of an int64
in every index entry, B-tree page, and foreign-key reference that points at a
state group. Implementations SHOULD key the state-group table on
`SHA256(lattice)` (32 bytes) and store the lattice as the row value, updating it
homomorphically in place; foreign references then carry the 32-byte key, not the
full lattice.

### Write-path cost and snapshot-cliff elimination

Each state append (join, profile update, ban, etc.) writes $\log_{32}(S)$ trie
nodes along the path from the changed leaf to the root — at most 4 node writes
for $S \approx 10^6$, 6 for $S \approx 10^9$ — replacing the legacy delta-chain
append plus periodic $O(S)$ full-snapshot rewrite. Synapse-lineage engines pay
that $O(S)$ snapshot cost only periodically, once the delta chain exceeds a hop
ceiling (`MAX_STATE_DELTA_HOPS`, default 100), so the honest comparison is
against an _amortized_ $O(S / 100)$ per-event legacy cost, not against the
unamortized $O(S)$ figure. Concretely, for a room with 50,000 state events: the
legacy engine pays ~500 amortized row-equivalents of snapshot-rewrite work per
event at the hop ceiling, against ~4 dependent node writes for the HAMT. That
gap — not the read-side win alone — is the basis for eliminating the snapshot
cliff described in Background item 1: there is no periodic pause, because there
is no full-$S$ structure ever rewritten in one step.

### Fast delta isolation algorithm

When comparing two state maps (e.g., $A$ and $B$) during a deep rebuild or
network split, the delta $\Delta$ can be extracted without delta chain
decompression in $O(|\Delta| \cdot \log_{32} S)$ time:

1. **Structural sharing:** Recursively walk the HAMT. If the pointer identities
   of node $A'$ and node $B'$ match (same process, same in-memory trie), skip
   the subtree in $O(1)$.
2. **Lattice comparison:** Otherwise (e.g., across process boundaries or
   database reloads), compare the cached `LtHash16` sub-lattices of $A'$ and
   $B'$. If they match, skip the subtree — at cryptographic strength, not
   probabilistic accelerator strength, because the sub-lattice is homomorphic
   over the same keyed digest used at the root. Applied at the root, this is the
   $O(1)$ whole-state-map convergence check; it is the same rule as any other
   level, not a separate special case.
3. **Deep diff:** Only when sub-lattices differ, iterate the 32-bit CHAMP
   bitmaps and recurse into differing children to extract the exact mismatched
   leaves.

### Bounded forward repair

Where an implementation retroactively repairs descendant state groups after a
merge point or state reset — this is not universal; many Synapse-lineage engines
resolve only at the forward extremities and leave already-persisted descendants
alone, making resets sticky rather than self-healing — the repair walk can be
bounded by distance-to-convergence rather than by full subtree depth: each
descendant branch halts at the first generation whose recomputed root lattice
matches the lattice already persisted for that state group, since a match proves
no further descendant in that branch can differ. This requires per-state-group
root lattices to be **persisted at write time**, not recomputed on demand — an
implementation that recomputes rather than stores the lattice gets no benefit
from this bound, because reaching the comparison still requires materializing
the state it was meant to avoid materializing. This is a storage-layer
obligation on any engine adopting this proposal.

## Trade-offs

Replacing legacy delta chains with a persistent HAMT introduces specific costs:

- **Dependent reads:** Point queries require $\log_{32} S$ dependent I/O lookups
  (e.g., ~4 node fetches for a room with 50,000 state events) rather than a
  single hash-table probe. These are serial and unprefetchable — each lookup
  depends on the previous — so they cannot be parallelized the way a single
  indexed read can.
- **Write amplification:** Each state append writes $\log_{32} S$ nodes instead
  of one delta row (see Write-path cost, above). On an LSM-backed store this is
  compounded further by compaction, which typically rewrites each node an
  additional 10–30× over its lifetime; the write multiplier should be evaluated
  against the specific storage engine, not assumed away by the $\log_{32} S$
  figure alone.
- **Lattice memory footprint:** Caching a 2048-byte `LtHash16` sub-lattice at
  every internal node, not just the state-group root, is real memory pressure —
  at Synapse scale, 2048 bytes × (state-group count × average internal-node
  count per group) reaches into the gigabytes, and is the cost an operator will
  notice first, independent of read/write latency. Implementations
  memory-constrained enough that this matters have a fallback (see Security
  considerations).
- **Garbage collection:** Because nodes are structurally shared across multiple
  state groups, pruning old history requires implementing reference counting or
  mark-and-sweep garbage collection over the trie nodes.
- **Auth-chain sorting:** While this structure optimizes state resolution delta
  extraction, implementations must still fetch the auth-chain to topologically
  sort the isolated $\Delta$; the auth-chain difference is a DAG problem, not a
  trie problem, and is not derivable from a trie diff.

## Security considerations

The HAMT is a strictly **local indexing aid**. It is never transmitted over
federation, and adopting it requires no change to MSC4511 canonicalization or
any other wire-facing behavior; the wire-facing equality commitment remains the
`LtHash16` digest defined in MSC4500.

That locality does not make the per-node sub-lattice a purely cosmetic
accelerator, though: the trie's _contents_ — state keys and event IDs — are
adversary-supplied by federated participants in the room. A cheap, gameable skip
test at internal nodes is a correctness hazard, not just a performance one: a
false subtree match at step 2 of the delta-isolation algorithm causes the server
to silently skip a subtree that actually differs, producing a wrong $\Delta$ and
a divergent local state view. This proposal therefore specifies the per-node
cache as a full `LtHash16` sub-lattice rather than a short non-cryptographic
hash — a 64-bit structural hash would be grindable in ~$2^{32}$ work by anyone
able to mint state events in a shared room, which is cheap. The homomorphic
lattice closes this at the cost described in Trade-offs. Implementations for
which the 2048-byte-per-node memory cost is prohibitive MAY substitute a 128-bit
hash keyed with a per-server secret at internal (non-root) nodes; this closes
the grinding path (the attacker cannot target a hash they cannot compute)
without providing the lattice's exact, key-independent equality guarantee, and
implementations choosing it should treat it as a narrower mitigation, not an
equivalent one.
