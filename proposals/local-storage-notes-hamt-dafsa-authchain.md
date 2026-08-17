# Local storage architecture notes: HAMT/DAFSA layering and auth-chain set algebra

Non-normative implementation notes. This is **not** a Matrix Spec Change — it
documents local homeserver storage-engine decisions that sit entirely behind the
wire boundary defined by [MSC4500](4500-state-accumulators.md) (state
accumulator digests) and [MSC4521](4521-algebraic-set-reconciliation.md)
(algebraic set reconciliation). Nothing described here is part of, or implied
by, either MSC's normative text; it exists so the local engineering rationale
behind those proposals' "Implementation notes" sections is written down
somewhere, rather than living only in exploratory research notes.

Three lower-priority leads, kept out of MSC4500 and MSC4521 themselves because
all three are internal storage-engine decisions, not wire contract:

1. Where a HAMT belongs versus where a DAFSA belongs, for a homeserver's local
   state and policy data.
2. How a Merkle-ized prefix trie can isolate _which_ `(type, state_key)` tuples
   diverged between two locally-held state maps, once MSC4500's accumulator has
   already told a server _that_ they diverge.
3. Why an auth-chain transitive closure (Conduit/conduwuit-style) and a
   chain-cover reachability index (Synapse-style) are two engine-appropriate
   answers to the same question, not a superiority ranking, and what is and
   isn't safe to assume about `ShortEventId` ordering along the way.

## 1. HAMT vs. DAFSA: two different jobs, not competing options

These are not substitutes for each other. Picking between them is a question of
which invariant the underlying data actually has:

- **A Deterministic Acyclic Finite State Automaton (DAFSA / DAWG)** is built by
  minimizing a _static, closed_ set of strings so that shared prefixes and
  suffixes collapse into one path through the graph. It is extremely compact and
  gives `O(k)` lookup in the length of the query string, but the minimization
  that gives it that compactness assumes batch construction — it is not designed
  for a live structure that is mutated incrementally, transaction by
  transaction, under concurrent access.
- **A (Merkle-ized) Hash Array Mapped Trie / CHAMP-style trie** is built for
  exactly the opposite case: a _live, mutating_ key-value map that needs cheap
  `O(log₃₂ N)` incremental updates with structural sharing across concurrently
  held versions (see
  [§2](#2-fast-state-map-delta-isolation-via-merkle-ized-prefix-tries) below),
  plus cheap structural equality between two versions.

Concretely, for a homeserver:

- **DAFSA-shaped data:** federation server ACL / blocklist patterns, and the
  spec-fixed table of default power-level requirements per event type. Both are
  read-mostly, rebuilt in bulk on the rare occasions they change, and benefit
  from the DAFSA's prefix/suffix sharing (domain patterns in particular share a
  lot of structure).
- **HAMT-shaped data:** the live per-room resolved state map,
  `(type, state_key) -> event_id`, which mutates on every state event and needs
  cheap structural comparison across concurrently-held DAG branches.

**Caution carried over from the research thread:** do not put `event_id`
mappings into a DAFSA. Event IDs are opaque random-looking strings with no
shared prefixes or suffixes to exploit, so a DAFSA gains nothing from them while
still paying its batch-rebuild cost on every mutation — it is a bad fit in both
directions at once for that particular key shape.

## 2. Fast state-map delta isolation via Merkle-ized prefix tries

MSC4500's `LtHash16` accumulator proves _that_ two candidate state maps
disagree; its one-way construction cannot name _which_ `(type, state_key)`
tuples diverged. Implementations MAY back their live in-memory resolved state
map with an immutable, structurally-shared 32-way prefix trie (a Merkle-ized
Hash Array Mapped Trie, i.e. HAMT/CHAMP-style) to isolate a divergent tuple set
$\Delta$ between two locally-held state maps in time proportional to
$|\Delta| \cdot \log_{32} N$ — paying trie depth per differing key, not the
total room size $N$ — without transmitting or comparing the trie itself over
federation.

This solves a different problem from MSC4500's
[Fast local divergence lookup](4500-state-accumulators.md#fast-local-divergence-lookup-optional):

- **LCA forest search (binary lifting)**, described there, operates over the
  _delta-parent storage forest_ (`state_group` parent pointers) to find, in
  $O(\log n)$ hops, the historical group at which two DAG tips' delta chains
  diverged. It answers "where in history did these two lineages split."
- **State-map key diffing (Merkle-ized prefix trie)**, described here, operates
  over the _live key-value state map_ itself (`(type, state_key) -> event_id`)
  to isolate exactly which tuples differ between two resolved state maps
  regardless of their delta lineage. It answers "which keys actually differ,"
  and remains correct across the disconnected-storage and fork-healing cases in
  which the LCA table is required to defer to bisection.

**Node layout.** To avoid caching a 2048-byte `LtHash16` lattice at every
internal node — which would multiply the accumulator's memory footprint by the
trie's node count — only the room's current resolved state carries the full
lattice, at the state-group root. Internal trie nodes carry a lightweight
32-byte structural hash instead (e.g. a CHAMP-style dual-bitmap node hashed as
`SHA256(datamap || nodemap || child_hashes)`), and leaves carry the
`(type, state_key) -> event_id` tuple itself. The `LtHash16` lattice remains the
wire-facing equality commitment defined by MSC4500; the trie's structural hashes
are a purely local indexing aid and are never transmitted.

**Diffing algorithm, and what the structural hash actually buys.** Given two
candidate state maps represented as tries sharing a common ancestor by
construction (e.g. two DAG branches both derived incrementally from a shared
root via persistent structural sharing), the two subtree roots at any position
that haven't changed are the _same node in memory_ — comparing them is a free,
exact `O(1)` pointer-equality check, and the 32-byte structural hash adds
nothing in that in-process case. The hash earns its cost in two other
situations: comparing two tries that are not sharing the same process heap (e.g.
persisted to disk and reloaded across a restart, or held by two separate worker
processes), where pointer identity does not survive; and as a defensive check
against a subtly corrupted subtree that would otherwise be indistinguishable
from a shared one by pointer alone. Implementations that only ever compare
in-process, structurally-shared tries can walk by pointer identity and skip the
structural hash entirely; implementations that need cross-process or
cross-restart comparison need the hash, and a non-cryptographic 64-bit hash is
enough for that purely local indexing role — there is no adversary to resist
here, `LtHash16` already carries the wire-facing security property, and the
honest-failure question is accidental collision probability over the number of
nodes actually compared, not an active collision-forcing game. At $10^6$
compared nodes, the birthday-bound collision probability for a 64-bit local
index is still only on the order of $10^{-8}$, which is an acceptable deployment
risk for this purely local role rather than a wire security parameter.
Regardless of which comparison mode is in use: wherever two nodes at the same
trie position are known identical (by pointer or by matching structural hash),
the entire subtree beneath them is skipped without being read; only positions
that differ are recursed into, down to the differing leaves. Because state
changes between two closely-related branches are typically a handful of tuples
out of a much larger room, most of the trie prunes away at or near the top level
in the common case — the exact fraction depends on how the changed keys' hashes
happen to distribute across the trie and is not a fixed bound. This technique
assumes the two tries were built via incremental, structurally shared updates
from a common lineage (as is naturally the case for a server's own state-group
history); comparing two independently-constructed tries with no shared structure
and no persisted structural hashes degrades to a full walk.

**Handoff.** The isolated tuple set $\Delta$ feeds directly into local state
resolution (skipping the full state-map materialization that would otherwise be
needed to build a conflict set) or, for federation repair, seeds the divergent
event IDs into an `algebraic_v1` exchange under
[MSC4521](4521-algebraic-set-reconciliation.md) to reconcile the remaining
event-ID sets over the network in `O(d)` bandwidth. The trie itself is strictly
a local indexing structure: `LtHash16` remains the sole cross-server equality
commitment, and MSC4521's PinSketch remains the sole cross-server reconciliation
mechanism — nothing about this trie is part of the wire contract.

## 3. Auth-chain storage: transitive closure vs. chain cover

Two engines exist in the wild, and they made different but locally appropriate
choices given different underlying storage engines — this is not a case where
one implementation is doing it "wrong," and the two approaches are not only a
relational-versus-LSM story:

- **Chain-cover reachability index (Synapse / relational storage).** Instead of
  storing a state event's full ancestor set, the engine partitions the
  authorization DAG into linear, non-overlapping chains and records only the
  furthest point reached on each chain. Ancestry becomes one integer comparison
  per chain instead of a full set membership test. The storage-bloat pressure
  that motivated this is specific to relational storage — a large per-row array
  of ancestor IDs causes real table bloat and join cost at scale — but the chain
  cover also changes the _algorithmic_ cost of computing an auth-difference
  between two conflicting state sets: intersecting two large sorted ancestor
  arrays is `O(|closure|)` comparisons, while the chain cover reduces that same
  query to `O(#chains)` integer comparisons, a benefit that isn't specific to
  relational storage and matters more as the closure grows.
- **Transitive closure (Conduit/conduwuit lineage / RocksDB or other LSM-backed
  storage).** The engine instead stores, per state event, the full compressed
  set of ancestor `ShortEventId`s directly as a sorted integer array
  (delta-varint or Roaring-bitmap compressed). This is viable specifically
  because an LSM-tree key-value store does not incur the relational row-bloat
  problem that motivates the chain cover — persisting a compact sorted-integer
  blob per state event costs a few hundred bytes there, not a multi-row
  relational fan-out — and a linear merge scan over two sorted arrays is cheap
  while the closures stay small.

The right takeaway is architectural fit, not a general recommendation: the
storage-bloat problem the chain cover exists to solve is specific to relational
storage, so an LSM-backed engine that never has that problem doesn't need the
chain-cover machinery to get _storage_ relief — but if auth-chain closures grow
large (tens of thousands of ancestors), the chain cover's algorithmic advantage
for computing auth differences (a handful of integer comparisons instead of a
full sorted-array intersection) applies regardless of storage engine, and an
LSM-backed implementation may still want equivalent machinery once closures are
large enough for that cost to dominate.

**A caution on the numbers actually seen in the research thread:** the same
conversation used "typically fewer than 50–100 ancestor events" in one place and
a "20,000+ ancestors" illustration in another, for the same kind of room. The
larger figure was used to illustrate _why_ naive per-row relational storage (and
a naive sorted-array intersection) degrades badly, not as a claim about typical
auth-chain size in a real room — real auth-chain size depends heavily on room
age and history and needs to be measured against actual room data before it's
used to size any cache or storage decision. This is the same category of open
empirical question as the state-group convergence telemetry noted elsewhere;
don't carry either number into a design decision as though it were measured.

### `ShortEventId` ordering: benefits and advised restrictions

Where a monotonically-increasing integer ID is minted at ingestion time and used
as the transitive-closure array's element type, sorting that array by value
gives:

- Fast, sorted-array set algebra (union / intersection / difference) for
  computing the authorization difference between two conflicting state sets
  during state resolution — a linear merge scan over two small sorted arrays,
  not a hash-based structure.
- Good disk/cache compression, since sequential-ish integers compress far better
  than random hash values.

**What it must never be used for:** inferring chronological or topological event
order. Backfilled history is ingested "now," so old events can receive _larger_
`ShortEventId` values than events ingested earlier; concurrent federation
workers can interleave ID assignment further. `ShortEventId` is not an input to
state resolution v2's ordering at all, and the event's own `depth` field is
sender-asserted and unvalidated, so it is not a safe substitute either. The
authoritative ordering state resolution v2 actually uses is the **reverse
topological power ordering**: a Kahn-style topological sort over the
auth-difference subgraph, with ties broken by
`(power level of the sender in that event's auth state, origin_server_ts, event_id)`;
the iterative auth-checking pass over conflicted control events uses that
ordering. The power-levels mainline ordering is a separate later phase applied
to the remaining conflicted events, not part of the same ordering rule. Neither
`ShortEventId` nor `depth` is a safe stand-in for either phase, however tempting
the free integer comparison looks.

### Why an `O(A)` linear scan over ~100 integers can beat a "better" `O(1)` structure

For small, fixed-size sets (an auth chain's direct `auth_events`, typically a
handful of entries, not the transitive closure above), a plain linear scan over
a contiguous array is a legitimate, often _better_ choice than reaching for a
hash-set purely for its asymptotic complexity:

- A hundred `u64`s is under a kilobyte — it fits in L1 cache, and a sequential
  scan over contiguous memory is cheap regardless of nominal complexity class.
- A hash-set membership test pays hashing cost and, on a sparse layout, cache
  misses chasing pointers — overhead that a small contiguous array simply
  doesn't have.

This stops being true once the set actually grows large (thousands+) or the
check runs in a genuinely hot, latency-sensitive inner loop — at that point a
sorted array with binary search, a bitmask, or another dedicated structure is
worth it. For a set sized in the tens to low hundreds, optimizing this before
measuring it is very likely wasted engineering effort.

## 4. Physical storage & key encoding (Ordered Key-Value Stores)

While the HAMT is logically a pointer-chasing trie, physical layout in an
ordered key-value store (e.g. RocksDB SSTables, or B-tree pages) is purely a
function of the key encoding. A naive content-addressed key (`[digest]`)
uniformly scatters a room's nodes across the entire database.

A **room-and-depth bucketed key encoding** addresses this:
`[shortroomid][depth][digest]`

This encoding provides two structural properties without changing the HAMT
logic:

1. **Resident upper prefix:** All of a room's level 0, 1, and 2 nodes are
   clustered into a contiguous keyspace. As the room grows, this upper prefix
   becomes a vanishingly small fraction of the total structure (e.g., at _S_ =
   50,000, levels 0–2 are ~1,000 nodes, representing ~60% of the trie — a 32-way
   HAMT has roughly _N_/31 total nodes, so ~1,613 total nodes at this size; at
   _S_ = 1,000,000, those same ~1,000 nodes are ~3%). An implementation can
   reasonably expect to keep this prefix resident. (This is a layout argument,
   not a measurement, and should be validated against actual block cache
   telemetry before relying on it to size a cache.)
2. **Room-scoped deduplication:** Deduplication is preserved within the room.
   State-event references are room-unique, so room-prefixing does not materially
   reduce deduplication for the state-map trie.

**Resolver API implication.** A key of `[shortroomid][depth][digest]` means a
node cannot be fetched from its digest alone. The resolver interface must carry
the room and the depth (e.g.,
`FnMut(ShortRoomId, Depth, &StructuralHash) -> Result<HamtNode>`). Since the
trie traversal inherently knows its current depth, this is mechanically
straightforward, but it represents an API change from a purely content-addressed
store.

**Depth-in-key relies on content-determined depth.** Depth-in-key is safe only
because depth is content-determined here: in a hash-prefix-indexed trie, a
subtrie at depth _d_ is exactly the entry set sharing a _d_-length hash prefix,
and the entries determine their own hashes. In the non-compressed CHAMP case, a
removal leaving a single entry inlines that entry directly into its parent node
— the storage depth is the parent's depth, which is itself determined by the
_d_-prefix entry set alone, not by anything else in the trie. In that case the
same digest cannot legitimately appear at two depths, so prefixing by depth
doesn't fragment deduplication.

**True path compression breaks this**, and is a distinct operation from the
singleton-inlining case above: collapsing a _chain_ of single-child internal
nodes into one compressed node places that node at a depth that depends on how
many single-child ancestors happened to precede it — a property of the rest of
the trie's population, not of the collapsed entry set's own hash prefix. So the
same digest can legitimately occupy different depths in different generations.
No naming convention recovers content-determination here — the choice is between
excluding compressed nodes from depth bucketing (keying them by digest alone)
and accepting a bounded intra-room deduplication loss where a shared compressed
node is stored once per distinct depth. Which is cheaper is unmeasured.

## Where this fits relative to the MSCs

None of the structures described here (HAMT, DAFSA, Merkle-ized prefix tries,
transitive-closure auth chains, chain covers, `ShortEventId` interning,
depth-bucketed key encodings) appear on the wire. They are local indexing and
storage choices that sit entirely behind:

- MSC4500's `LtHash16` accumulator digest, which is the sole cross-server
  equality commitment for room state.
- MSC4521's `algebraic_v1` PinSketch exchange, which is the sole cross-server
  event-ID set reconciliation mechanism.

An implementation is free to use any, none, or a different combination of the
local structures described here without any wire-visible consequence, exactly as
MSC4500's own security considerations already require of any local optimization
built on top of its accumulator.
