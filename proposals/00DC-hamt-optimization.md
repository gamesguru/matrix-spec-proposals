# MSC00DC: Faster state groups via an augmented HAMT

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
   periodically issue a burst $O(S)$ full-snapshot write to prevent read-latency
   degradation as the chain grows — it doesn't stall the room, but it is a
   large, spiky write that recurs every `MAX_STATE_DELTA_HOPS` events and whose
   cost is unavoidably tied to current room size. Historical point-queries
   require fetching the snapshot and decompressing the delta chain in memory.
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
state accumulator. [MSC4511](4511-a-topological-metadata-query-api.md) sketches
Merkleized metadata as future work. This proposal bridges the two by defining a
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
  child nodes, so each internal node also caches a subtree digest used to skip
  unchanged subtrees during delta isolation (below). By default this is a
  128-bit hash keyed with a per-server secret, computed over the two bitmaps,
  every inlined `(type, state_key, event_id)` tuple marked in `datamap`, and the
  digests of every child marked in `nodemap` (see Security considerations for
  why this is the default, and for the optional stronger variant). This digest
  is over logical content only — the bitmaps and the (key, digest) pairs they
  index — never over physical array layout. A reference layout may pack inlined
  entries from the front of a single backing array and child pointers from the
  back; that packing choice must not leak into the digest, or
  independently-written implementations produce non-reproducible digests for
  identical content.
- **Collision nodes:** Where the 5-bit hash-fragment path is exhausted and
  distinct keys still collide, entries are held in a node with no intrinsic
  ordering. Its digest contribution MUST use a fixed canonical ordering (e.g.
  sort by raw key bytes) — an unordered digest is nondeterministic and breaks
  reproducibility across reloads (and, for the optional unkeyed variant below,
  across implementations too).
- **State-group root:** MUST cache the true, unkeyed `LtHash16` lattice for the
  entire state group — this is the wire-facing accumulator MSC4500 defines, and
  it is what makes the root reproducible across servers and safe to use as the
  deterministic State Group ID (below). It is maintained independently of
  whatever digest scheme internal nodes use for subtree skipping: a keyed or
  otherwise server-local value at the root would not be comparable to another
  server's accumulator, which would defeat the point of building this structure
  on top of MSC4500. Implementations SHOULD also cache a 32-byte
  `BLAKE2b-256(lattice)` alongside it — the same collapse MSC4500 already
  defines for the wire-facing digest, so this introduces no new primitive — so
  the common-case root comparison is a cheap fixed-width equality check rather
  than a 2048-byte one, while the full lattice is retained for homomorphic
  incremental updates.

**Canonicality invariant.** Non-canonical shape does not affect the soundness of
the root's lattice-based equality: the lattice is a shape- and order-independent
sum over subtree contents, so equal lattices always imply equal content
regardless of trie shape. But canonical shape is load-bearing everywhere else
this proposal relies on it. Under the default keyed internal-node digest (Data
structure, above), the digest is computed over the bitmaps themselves, so two
equal-content subtrees with different shape — one inlining an entry the other
has boxed into a child — produce different digests outright; non-canonical shape
doesn't just slow subtree skipping down, it defeats it, and every comparison
falls through to deep diff. Canonical shape is also required for
pointer-identity sharing (step 1 of delta isolation below, and deduplication
generally), which only pays off if identical content actually converges to
identical node objects; and for the deep-diff step itself, which pairs
`datamap`/`nodemap` bits positionally between $A'$ and $B'$ and needs a defined
answer when one side has inlined an entry the other has boxed into a child — an
undefined shape under deletion makes that comparison ambiguous, not merely less
efficient. CHAMP gives canonical shape under insertion — shape is determined
purely by hash-prefix and bitmap occupancy, independent of insertion order — but
**not** under deletion unless implementations enforce the standard CHAMP repair
invariant: when a removal leaves a node holding a single entry and no children,
that entry MUST be inlined into the nearest ancestor with other content, rather
than left as a degenerate single-entry node. Skipping this repair reintroduces
insertion-order-dependent shape and breaks subtree skipping, pointer-identity
sharing, and positional deep-diff across otherwise-identical state maps.

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
`BLAKE2b-256(lattice)` (32 bytes — the same collapse MSC4500 already defines)
and store the lattice as the row value, updating it homomorphically in place;
foreign references then carry the 32-byte key, not the full lattice.

### Write-path cost and snapshot-cliff elimination

Each state append (join, profile update, ban, etc.) writes $\log_{32}(S)$ trie
nodes along the path from the changed leaf to the root. For a room with
$S = 50{,}000$ state events, $\lceil \log_{32}(50{,}000) \rceil = 4$ node writes
— that bound holds up to $S \approx 1.05 \times 10^6$, which already covers
every real Matrix room; the largest known rooms sit in the $10^5$–$10^6$ range,
not the $10^8$–$10^9$ scale where the depth would climb to 6. This replaces the
legacy delta-chain append plus periodic $O(S)$ full-snapshot rewrite.
Synapse-lineage engines pay that $O(S)$ snapshot cost only periodically, once
the delta chain exceeds a hop ceiling (`MAX_STATE_DELTA_HOPS`, default 100), so
the honest comparison is against an _amortized_ $O(S / 100)$ per-**state-event**
legacy cost, not against the unamortized $O(S)$ figure — and since that
denominator counts state events specifically, not all room traffic, the true
amortized cost per state event is higher than a per-room-event framing would
suggest.

Counting rows alone favors the HAMT by roughly two orders of magnitude (~4
writes vs. ~500 amortized row-equivalents at the hop ceiling for our 50,000-
event room), but that comparison ignores that every HAMT node now carries a
subtree digest it didn't before (see Data structure). In bytes, using the
default 128-bit keyed-hash digest (16 bytes/node): 4 nodes × (~1KB CHAMP
payload + 16B digest) ≈ 4.1KB, against ~500 rows × ~100B ≈ 50KB — still roughly
a 12× win. Under the optional full-lattice-per-node variant (2048 bytes/node): 4
nodes × (~1KB + 2KB) ≈ 12KB against the same 50KB — closer to 4×. (The ~1KB
CHAMP payload figure is an assumption that swings with node occupancy, not a
fixed constant, and both figures are pre-compaction — see Write amplification in
Trade-offs for the multiplier an LSM-backed store adds on top, which the legacy
side's own snapshot-row storage is not exempt from either.) What survives
regardless of digest variant is that there is no full-$S$ structure ever
rewritten in one step; the mechanism behind that claim follows below.

The byte comparison above measures volume moved, not access pattern, and the
second axis favors the HAMT independently of the first — not because HAMT reads
are non-dependent (Trade-offs below is explicit that they are: point queries
cost $\log_{32} S$ serial, unprefetchable hops, same as any trie), but because
of how many dependent hops each side needs and how likely each hop is to already
be hot. Writing the new root costs $\log_{32}(S)$ dependent reads to descend the
existing trie — ~4 hops at $S = 50{,}000$ — followed by $\log_{32}(S)$
sequential appends of the resulting fresh nodes, since trie nodes are immutable
and nothing existing is ever mutated in place; the append itself is sequential,
the update as a whole is descend-then-append. Crucially, the top few levels of
that descent are shared across every state group in the room, so they stay
resident in cache under any realistic access pattern — effective _uncached_
dependent hops are closer to 1–2, not 4. Reconstructing current state from a
delta chain, by contrast, walks up to the full `MAX_STATE_DELTA_HOPS`
(default 100) predecessor pointers, and each chain is walked once and evicted —
delta rows have no equivalent of the trie's shared, permanently-hot upper
levels. The real comparison is dependent-hop count under realistic caching (~1–2
vs up to 100), not "dependent vs. not." Each of those hops is a serialized round
trip that can't be overlapped with the next, so the gap compounds as _latency_,
not just I/O volume — this holds on NVMe, and holds harder still on
network-attached storage, where per-hop latency runs tens of times higher than
local flash. This dependent-hop asymmetry is also why the win isn't a fixed
constant factor: the legacy side's cost grows with room size ($O(S)$
unamortized, $O(S/100)$ amortized against the hop ceiling), while the HAMT's
grows with $\log_{32}(S)$, so the gap between them widens, without bound, as
rooms grow — this design isn't merely faster today, it's what keeps per-append
cost from growing polynomially with room size at all. The HAMT root is already a
fully materialized, queryable snapshot after every append, so no replay of a
dependent chain is ever needed to produce one — that is what eliminates the
snapshot cliff in Background item 1.

### Fast delta isolation algorithm

When comparing two state maps (e.g., $A$ and $B$) during a deep rebuild or
network split, the delta $\Delta$ can be extracted without delta chain
decompression in $O(|\Delta| \cdot \log_{32} S)$ time:

1. **Structural sharing:** Recursively walk the HAMT. If the pointer identities
   of node $A'$ and node $B'$ match (same process, same in-memory trie), skip
   the subtree in $O(1)$.
2. **Digest comparison:** Otherwise (e.g., across process boundaries or database
   reloads), compare the cached subtree digests of $A'$ and $B'$ — by default
   the 128-bit per-server-keyed hash, or the full unkeyed `LtHash16` sub-lattice
   under the optional stronger variant (see Security considerations). If they
   match, skip the subtree. The strength of this check comes from the digest's
   width and the underlying hash's collision resistance — and, for the keyed
   variant, from the attacker's inability to target a digest computed with a
   secret they don't have — not from homomorphism, which only buys cheap $O(1)$
   composition when a digest is updated incrementally. The root is a deliberate
   exception to the default keyed scheme, not an oversight: under the default
   variant every other level compares keyed 128-bit hashes, but the root always
   compares the mandatory unkeyed lattice, because the root's comparison must be
   reproducible across servers (it is the State Group ID) while internal-node
   comparisons never leave the local server. Applied at the root, this is the
   $O(1)$ whole-state-map convergence check described above, and implementations
   SHOULD compare the cached `BLAKE2b-256(lattice)` there rather than the full
   2048 bytes, for the same reason.
3. **Deep diff:** Only when digests differ, iterate the 32-bit CHAMP bitmaps and
   recurse into differing children to extract the exact mismatched leaves.

**Reference implementation.** This algorithm is not merely descriptive: `rezzy`
implements it directly (`isolate_delta`/`diff_hamt_nodes` in `hamt/delta.rs`),
including the lattice-then-structural-hash short-circuit in step 2 and the
recursive bitmap walk in step 3, and its own doc comment states the same
$O(|\Delta| \cdot \log_{32} S)$ bound. Implementations targeting this MSC in
Rust can use it as-is rather than re-deriving the walk.

**Producer-tracked deltas dominate isolate_delta, not the reverse.** Delta
isolation exists for the case where two states must be compared with no prior
relationship recorded between them (e.g., a state fork rediscovered after a
network partition, or a deep historical rebuild). It is not the preferred way to
obtain a delta when one is knowable for free at the point a new state is
produced: a single linear state change (one PDU altering one
`(event_type, state_key)`) never needs step 1–3 at all — the change is already
`O(1)` to name and `O(\log_{32} S)` to apply via a persistent insert/remove
against the prior root (see Write-path cost, above). Even a multi-way
state-resolution merge already computes its conflicted-key set as part of
resolving conflicts; that set _is_ the delta, at zero additional cost, whenever
the resolver is in a position to report it. `isolate_delta` is the fallback for
the residual case — comparing two already-opaque resolved states with no
recorded provenance between them — not the default path for every state
transition. An implementation that reaches for `isolate_delta` (or, worse, a
full flat-map comparison) on every state change where provenance was actually
available has reintroduced an unnecessary $O(S)$-or-worse step ahead of a change
this MSC's write path already makes $O(\log_{32} S)$ or, at worst,
$O(|\Delta| \cdot \log_{32} S)$.

### Optional: typed roots (per-event-type partitioning)

Implementations MAY additionally partition the flat HAMT described above by
event type, trading a small amount of write-path complexity for cheaper
type-scoped bulk reads (e.g. "all `m.room.member` state," the most common
non-point Matrix state query):

```text
TypedRoot
 ├── structural_hash   local, keyed directory identity (see below)
 ├── state_group_id    the same unkeyed State Group ID as the flat root
 └── directory (sorted by event_type)
       event_type -> subtree_hash   -- one flat HAMT per event type
```

A type-scoped read resolves the directory, then traverses only the matching
subtree, at a cost of $O(\log_{32} S + S_T)$ rather than $O(S)$ for the matching
type's state size $S_T$. This is a read-side optimization only: it does not
create key ordering, and a request that isn't type-scoped (a state-key range, an
arbitrary predicate) gets no benefit from it.

**Normative-in-spirit caution for any implementation adopting this extension:**
the directory itself MAY use a cheap, local, keyed digest as its own structural
identity (parallel to an internal HAMT node's keyed digest in Data structure,
above) — but the typed root's `state_group_id` MUST remain the same unkeyed
`LtHash`-derived identity the flat root would produce for identical logical
content, computed directly from the full set of
`(event_type, state_key, event_id)` entries (`LtHash` addition is commutative
and associative, so summing it per event-type subtree and then combining is
exactly equivalent to summing it once over the flat list — but only when every
entry contributes through the identical encoding exactly once). It must **not**
be derived from, or replaced by, a combination of the subtrees' keyed structural
hashes: doing so silently produces a server-local, non-homomorphic value in the
one field this MSC requires to be cross-server comparable, defeating the `O(1)`
deduplication this proposal exists to provide. An implementation adding this
extension should verify the equivalence with a direct test — construct the same
logical state both ways and assert identical `state_group_id` — rather than
assume the composition is correct by inspection.

### Bounded forward repair

Where an implementation retroactively repairs descendant state groups after a
merge point or state reset — this is not universal; many Synapse-lineage engines
resolve only at the forward extremities and leave already-persisted descendants
alone, making resets sticky rather than self-healing — the repair walk can be
bounded by distance-to-convergence rather than by full subtree depth: each edge
into a descendant state group halts recomputation the first time the recomputed
root lattice matches the lattice already persisted for that state group. The
bound is per **edge**, not per branch, because branches merge: a descendant
reachable through both a converged parent and an unconverged parent still
requires recomputation via the unconverged edge, and the walk as a whole only
terminates once every live edge has converged. This requires per-state-group
root lattices to be **persisted at write time**, not recomputed on demand — an
implementation that recomputes rather than stores the lattice gets no benefit
from this bound, because reaching the comparison still requires materializing
the state it was meant to avoid materializing. This is a storage-layer
obligation on any engine adopting this proposal.

## Trade-offs

Replacing legacy delta chains with a persistent HAMT introduces specific costs:

- **Dependent reads:** Point queries require $\log_{32} S$ dependent node
  fetches (e.g., ~4 for a room with 50,000 state events) rather than a single
  hash-table probe. These are serial and unprefetchable — each fetch depends on
  the previous — so they cannot be parallelized the way a single indexed read
  can. How many become cold I/O depends on the store's layout: the upper levels
  are a small bounded set (one root, at most 32 nodes at level 1, at most 1024
  at level 2) that an implementation can keep resident, but only if its node
  keys cluster them; a purely content-derived key scatters them across the
  keyspace, so residency is paid per cache block rather than per node. This is a
  real cost relative to a single-probe index, not a wash relative to delta
  chains: see Write-path cost, above, for why the comparable legacy figure (up
  to `MAX_STATE_DELTA_HOPS`, uncached) is worse on both hop count and cache
  locality, not merely also-dependent.
- **Write amplification:** Each state append writes $\log_{32} S$ nodes instead
  of one delta row (see Write-path cost, above). On an LSM-backed store this is
  compounded further by compaction, which typically rewrites each node an
  additional 10–30× over its lifetime; the write multiplier should be evaluated
  against the specific storage engine, not assumed away by the $\log_{32} S$
  figure alone.
- **Subtree-digest memory footprint:** Caching a digest at every internal node,
  not just the state-group root, is memory pressure that scales with total
  distinct nodes — structural sharing means this is _not_ state-group-count ×
  nodes-per-group (that double-counts nodes shared across groups); the live
  figure is closer to $S/31$ internal nodes for the first trie (~1,600 nodes at
  $S = 50{,}000$) — a full-occupancy lower bound; sparse occupancy at shallow
  depths pushes the real count higher — plus ~4 new nodes per subsequent state
  group. At the default 16-byte keyed-hash digest this stays modest even at
  Synapse scale. The optional full-lattice-per-node variant (2048 bytes/node) is
  the one that reaches into the gigabytes and is the cost an operator will
  notice first, independent of read/write latency — treat the two digest
  variants (see Security considerations) as distinct memory budgets, not one
  number.
- **Garbage collection:** Because nodes are structurally shared across arbitrary
  live state-group roots, a node MUST NOT be deleted until it is unreachable
  from every retained root. A pairwise old-root/new-root delta is sufficient to
  reclaim nodes only on a strict linear chain where the old root has no other
  live descendants; Matrix forks and unconverged forward extremities violate
  that assumption. Implementations therefore need either multi-root
  mark-and-sweep, or branch-safe reference counting based on live-root pins and
  persisted parent-to-child edges. In the latter model, publishing or retiring a
  root changes its root pin, while deleting a zero-reference node recursively
  releases its child edges. An occasional multi-root reachability audit remains
  useful for detecting bookkeeping or crash-recovery errors, but need not be the
  normal reclamation path. This is logical reclamation only: compacting sparse
  pages, tombstones, append-only segments, or LSM levels is a separate backing-
  store responsibility.
- **Auth-chain sorting:** While this structure optimizes state resolution delta
  extraction, implementations must still fetch the auth-chain to topologically
  sort the isolated $\Delta$; the auth-chain difference is a DAG problem, not a
  trie problem, and is not derivable from a trie diff.

## Alternatives

- **Raise or eliminate `MAX_STATE_DELTA_HOPS`.** Amortizes the $O(S)$ snapshot
  cost over more events but doesn't remove it, and pushes the other direction: a
  higher ceiling means longer chains, and every point query or state resolution
  pays for the full chain length it lands on, not just the amortized average. It
  also does nothing for branch obliviousness (Background item 2) or local-only
  state identity (item 3) — those require a content-derived,
  cross-server-comparable ID, which a longer delta chain cannot provide by
  construction.
- **Compress snapshots.** Reduces bytes moved per snapshot but not the $O(S)$
  work of assembling one, and does nothing for items 2 or 3 either.
- **Copy-on-write B-tree pages.** Gets structural sharing and immutability,
  which addresses some of the write-amplification and point-in-time-query costs
  above, but a B-tree's node identity is positional (key-range-based), not
  content-derived — two independently constructed trees over identical content
  don't reliably converge to identical page contents the way a HAMT's
  hash-prefix-addressed nodes do, so it doesn't give branch obliviousness or
  cross-server state identity without extra machinery layered on top.
- **Branching factor other than 32.** A 16-way trie roughly doubles depth
  ($\log_{16} S$ vs $\log_{32} S$) for smaller per-node bitmaps (16 bits vs 32
  bits) and a smaller reference layout per node; a 64-way trie roughly halves
  depth for larger nodes. 32 was chosen to align with MSC4500's `LtHash16`
  lattice slot count and existing 32-bit bitmap conventions in comparable HAMT
  implementations, keeping the per-level bitmap machinery (`datamap`/`nodemap`)
  a single machine word; the byte-comparison figures in Write-path cost scale
  with this choice and should be re-derived for a different branching factor,
  not assumed to hold.
- **Plain HAMT instead of CHAMP.** A plain HAMT boxes every entry as a child
  node regardless of subtree occupancy; CHAMP's compression — inlining leaf
  entries directly into the parent when a subtree holds only leaves — is what
  keeps node density and cache behavior favorable for the shallow, high-fan-out
  tries typical of Matrix room state. That density is the point for this
  workload, not the ordering canonicality CHAMP is more commonly cited for (see
  Canonicality invariant, above, which this proposal needs for a different
  reason: pointer-identity sharing and positional deep-diff, not ordering per
  se).

None of these alternatives address branch obliviousness or local-only state
identity (Background items 2–3) without independently reinventing a
content-derived, homomorphic state identifier — which is what MSC4500's
`LtHash16` already provides, and what this proposal exists to index locally.

## Dependencies

This proposal has a hard dependency on [MSC4500](4500-state-accumulators.md):
the state-group root's mandatory lattice, the `BLAKE2b-256` collapse used for
the cached root digest and state-group table key, and the deterministic State
Group ID scheme in `O(1)` State group ID generation & deduplication (above) all
assume `LtHash16` is available. Without MSC4500, this proposal has no
cross-server-comparable identifier to build the HAMT root around, and reduces to
a purely local delta-chain replacement with no branch-obliviousness or
state-identity benefit.

## Security considerations

The HAMT is a strictly **local indexing aid**. It is never transmitted over
federation, and adopting it requires no change to MSC4511 canonicalization or
any other wire-facing behavior. The wire-facing equality commitment remains the
`LtHash16` digest defined in MSC4500 — which is why the state-group root MUST
cache the true, unkeyed lattice: a keyed or otherwise server-local root digest
could not be compared against another server's accumulator, defeating the point
of building this structure on top of MSC4500.

That locality does not make internal-node digests purely cosmetic, though: the
trie's _contents_ — state keys and event IDs — are adversary-supplied by
federated participants in the room. A cheap, gameable skip test at internal
nodes is a correctness hazard, not just a performance one: a false subtree match
at step 2 of the delta-isolation algorithm causes the server to silently skip a
subtree that actually differs, producing a wrong $\Delta$ and a divergent local
state view. A short non-cryptographic hash (e.g. 64 bits) is grindable in
~$2^{32}$ work by anyone able to mint state events in a shared room, which is
cheap, so that's ruled out.

Two variants close the grinding path at internal nodes, and they are not
interchangeable — the default is a smaller cache _alongside_ the mandatory root
lattice, not a substitute for it:

- **Default: 128-bit hash keyed with a per-server secret.** Cheap (16
  bytes/node), closes grinding because the attacker cannot target a digest
  computed with a secret they don't have, and is the more attractive default
  given the memory analysis in Trade-offs. It is not homomorphic, so it cannot
  replace the root lattice for the `O(1)` incremental State Group ID updates
  described above — that still requires the true unkeyed lattice at the root,
  maintained independently. Because these digests are never compared across
  servers, only across reloads of the same server, the key MUST be persisted and
  stable across restarts: an implementation that regenerates or rotates it on
  boot invalidates every cached internal-node digest on disk, forcing a full
  subtree-digest rebuild before delta isolation can skip anything again.
- **Optional: full unkeyed `LtHash16` sub-lattice at every internal node.** At
  2048 bytes/node this is not grindable regardless of keying, gives exact,
  key-independent lattice-strength equality at every level (not just the root),
  and composes homomorphically the same way the root does. Implementations that
  want that uniform guarantee, or `O(1)`-updatable digests at every level rather
  than just the root, should pick this variant and budget the memory cost
  accordingly (see Trade-offs).

Either way, implementations SHOULD cache a 32-byte `BLAKE2b-256` of the root
lattice alongside the lattice itself — the same collapse MSC4500 already defines
— so the common-case convergence check (step 2 of delta isolation, applied at
the root) is a cheap fixed-width comparison rather than a 2048-byte one, while
the full lattice is retained for homomorphic updates.

## Unstable prefix

None required. Nothing described here is wire-facing or introduces a new
endpoint, event field, or federation behavior; it is a local storage
implementation detail servers MAY adopt independently of one another, with no
client- or server-visible API surface to gate behind a feature flag.
