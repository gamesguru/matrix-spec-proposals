# Local storage architecture notes: HAMT/DAFSA layering and auth-chain set algebra

Non-normative implementation notes. This is **not** a Matrix Spec Change — it
documents local homeserver storage-engine decisions that sit entirely behind the
wire boundary defined by [MSC4500](4500-state-accumulators.md) (state
accumulator digests) and [MSC4521](4521-algebraic-set-reconciliation.md)
(algebraic set reconciliation). Nothing described here is part of, or implied
by, either MSC's normative text; it exists so the local engineering rationale
behind those proposals' "Implementation notes" sections is written down
somewhere, rather than living only in exploratory chat transcripts
(`docs/Gemini-_02.md`, gitignored/untracked).

Two lower-priority leads from that research, kept out of MSC4500 itself because
both are internal storage-engine decisions, not wire contract:

1. Where a HAMT belongs versus where a DAFSA belongs, for a homeserver's local
   state and policy data.
2. Why an auth-chain transitive closure (Conduit/conduwuit-style) and a
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
  held versions (see the HAMT subtree-caching subsection already added to
  MSC4500's Implementation notes), plus cheap structural equality between two
  versions.

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

## 2. Auth-chain storage: transitive closure vs. chain cover

Two engines exist in the wild, and they made different but locally appropriate
choices given different underlying storage engines — this is not a case where
one implementation is doing it "wrong":

- **Chain-cover reachability index (Synapse / relational storage).** Instead of
  storing a state event's full ancestor set, the engine partitions the
  authorization DAG into linear, non-overlapping chains and records only the
  furthest point reached on each chain. Ancestry becomes one integer comparison
  per chain instead of a full set membership test. This exists specifically
  because storing a large per-row array of ancestor IDs in a relational table
  causes real table bloat and join cost at scale — the chain cover is Synapse's
  way of avoiding duplicating a large, heavily-overlapping set across many rows
  without a native "this set equals that set plus one element" primitive in SQL.
- **Transitive closure (Conduit/conduwuit lineage / RocksDB or other LSM-backed
  storage).** The engine instead stores, per state event, the full compressed
  set of ancestor `ShortEventId`s directly as a sorted integer array
  (delta-varint or Roaring-bitmap compressed). This is viable specifically
  because an LSM-tree key-value store does not incur the relational row-bloat
  problem the chain cover exists to solve — persisting a compact sorted-integer
  blob per state event costs a few hundred bytes there, not a multi-row
  relational fan-out.

The right takeaway is architectural fit, not a general recommendation: **chain
covers exist to solve a problem specific to relational storage; an LSM-backed
engine that never has that problem doesn't need the chain-cover machinery to get
the same ancestry queries.**

**A caution on the numbers actually seen in the research thread:** the same
conversation used "typically fewer than 50–100 ancestor events" in one place and
a "20,000+ ancestors" illustration in another, for the same kind of room. The
larger figure was used to illustrate _why_ naive per-row relational storage
degrades badly, not as a claim about typical auth-chain size in a real room —
real auth-chain size depends heavily on room age and history and needs to be
measured against actual room data before it's used to size any cache or storage
decision. This is the same category of open empirical question as the
state-group convergence telemetry noted elsewhere; don't carry either number
into a design decision as though it were measured.

### `ShortEventId` ordering: what it buys you, and the one thing it must never be used for

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
workers can interleave ID assignment further. The only authoritative topological
ordering during state resolution v2 is the event's own DAG `depth` plus sender
power level — `ShortEventId` comparison must not be substituted for that,
however tempting the free ordering looks.

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

## Where this fits relative to the MSCs

None of the structures described here (HAMT, DAFSA, transitive-closure auth
chains, chain covers, `ShortEventId` interning) appear on the wire. They are
local indexing and storage choices that sit entirely behind:

- MSC4500's `LtHash16` accumulator digest, which is the sole cross-server
  equality commitment for room state.
- MSC4521's `algebraic_v1` PinSketch exchange, which is the sole cross-server
  event-ID set reconciliation mechanism.

An implementation is free to use any, none, or a different combination of the
local structures described here without any wire-visible consequence, exactly as
MSC4500's own security considerations already require of any local optimization
built on top of its accumulator.
