# MSC0500: An algebraic, group-valued digest for fast set reconciliation

Several federation mechanisms need to answer the same question: do two servers
hold the same set of identifiers, and if not, which ones differ? MSC0501
(federation missed-PDU reconciliation) needs it over a room's known event or
resolved state set. MSC0502 needs an analogous primitive for ephemeral state.
Future diagnostic and audit endpoints may need it again in other contexts.

This MSC defines that primitive once, as a named digest profile, so that
consumers reference a field, a hash derivation, a wire encoding, and a decode
contract rather than building them from scratch each time.

The profile is provisioned for aggregate, tree-localized differences of up to
approximately 4,000 elements between sets containing up to 1 million elements.
With the resident strata estimator in the reference implementation, the
common-case exchange is designed to complete within approximately 50 ms and
exchange less than 25 KiB in total, excluding event bodies. Larger differences
require dynamic-tree escalation or a frame-extension and bulk-retrieval
protocol; they are not guaranteed to remain within these latency or wire-size
bounds, and above these limits additional MSCs with better asymptotic complexity
may be preferred. The extraction decoder has approximately $O(k^2 \log k)$
field-operation complexity, where `k` is a node's configured capacity. With `n`
tree nodes requested and per-node capacities $k_i$, total decoding cost is
$\sum_{i=1}^{n} O\!\left(k_i^2 \log k_i\right)$. For a balanced difference of
size $\Delta$ localized across `n` leaf nodes, each leaf has approximately
$\Delta / n$ elements, giving total cost
$O\!\left(\frac{\Delta^2}{n}\log\frac{\Delta}{n}\right)$, up to distribution
imbalance and capacity overhead.

`algebraic_v1` is a coordinated algebraic ladder. The strata estimator and
extraction sketch, at any `(depth, prefix)`, are views of one syndrome map. A
128-bit XOR accumulator at room scope provides the agreement check and decode
verification. Increasing extraction capacity extends an exchange additively; it
does not restart it.

## Scope

This profile defines:

- derivation of short identifiers from canonical 32-byte element digests;
- the finite field and its `libminisketch` compatibility contract;
- the level-0 accumulator;
- the syndrome sketch, its serialization, and its capacity bounds;
- dynamic tree extraction and the strata estimator;
- the decode-and-verify contract;
- capacity provisioning budgets;
- the resident structure implementations are expected to maintain.

This profile does **not** define endpoints, frames, authorization, negotiation,
or scheduling. Those belong to the consuming MSC. A consumer MUST validate that
both sides of a comparison are digesting the same population before invoking
this kernel; the kernel MUST NOT be given the responsibility of deciding whether
two digests are comparable.

## Element derivation

The profile operates over a set `S` of opaque elements. Each consumer MUST map
every element to a canonical 32-byte digest before applying this profile.
Consumers define what the elements mean; the kernel treats them as an opaque set
and does not interpret their content.

Let `D(e)` be the consumer-defined 32-byte digest for element `e`.

```text
h_128(e) = first 128 bits of D(e)
h_64(e)  = first  64 bits of D(e)
```

"First" means the leading bytes of `D(e)` in network byte order. $h_{128}(e)$ is
the first 16 bytes. $h_{64}(e)$ is the first 8 bytes interpreted as an unsigned
big-endian integer.

### Matrix event-ID binding

For Matrix event-ID sets, `D(e)` is derived as follows. For room versions 3 and
later, event IDs are already derived from `SHA3-256` event hashes, so no
auxiliary hash is required. Implementations derive `D(e)` from the decoded event
ID using the room-version-specific event-ID alphabet:

- room versions 1 and 2: hash the UTF-8 event ID string with `SHA3-256`;
- room version 3: decode the event ID as unpadded standard Base64;
- room versions 4 and later: decode event ID as unpadded URL-safe Base64.

Because `minisketch` set elements are nonzero, if the first 8-byte chunk is zero
the implementation MUST use the next nonzero 8-byte chunk of `D(e)`; if all four
chunks are zero, it MUST use the integer value 1.

Room versions whose event IDs are not hash-derived MUST set `D(e)` to the
`SHA3-256` digest of the event-ID string, or exclude the event from the compared
population. This Matrix binding does not use `XXH3` or any other auxiliary hash.

## Field

<!-- Edit marker. -->

The 64-bit Galois field is:

$$
\mathbb{F}_{2^{64}}
\cong
\mathbb{F}_{2}[x] \big/ \langle x^{64} + x^4 + x^3 + x + 1 \rangle,
$$

<!--
$$
\mathbb{F}_{2^{64}}
\cong
\mathbb{F}_{2}[x]\big/\bigl(x^{64}+x^{4}+x^{3}+x+1\bigr)
$$
-->

$h_{64}$ values are mapped to field elements by treating bit `i` of the integer
as the coefficient of $x^i$.

`algebraic_v1` sketches MUST be byte-for-byte compatible with `libminisketch` at
field size 64 for the same inserted $h_{64}$ values. This compatibility is the
normative interoperability test for the profile: an implementation that produces
a different byte string for the same input set is non-conforming, regardless of
whether its own decoder round-trips.

The 128-bit accumulator layer is a plain XOR group over 16-byte strings and is
not a field operation.

## Level-0 accumulator

$$
\begin{aligned}
\mathrm{digest} &= \bigoplus_{e \in S} h_{128}(e) \\
\\
\mathrm{count} &= |S|,
\end{aligned}
$$

where $\bigoplus$ denotes bitwise XOR over all selected 128-bit values. The
digest is encoded as 16 raw bytes using unpadded base64url.

Insertion and removal are the same operation: XOR the same $h_{128}(e)$ value
into the accumulator and increment or decrement the count. There is no rebuild
path and no ordering requirement.

The count residual $c = \operatorname{abs}\left(|S_A| - |S_B|\right)$ is an
exact measurement of $|S_A\ \Delta\ S_B|$ when divergence is one-sided, which is
the common lagging-peer case. When both digest and count match over the same
population, the two sets agree except with negligible probability from an
accidental 128-bit collision.

The accumulator is an integrity anchor, not an authenticator. See "Decode
verification" and the consuming MSC's security considerations.

## Syndrome sketch

The extraction layer is the odd-power syndrome map over the 64-bit field:

$$
\sigma_k(S) = \left(\sum h_{64}(e), \sum h_{64}(e)^3, ..., \sum h_{64}(e)^{2k-1}\right)
$$

Even powers are omitted because the Frobenius endomorphism makes them redundant
in characteristic 2: $s_{2i} = s_i^2$.

**Serialization.** Syndrome coordinates are serialized in increasing odd-power
order — `s1, s3, s5, ...` — and each coordinate is serialized as an unsigned
64-bit **little-endian** integer. The big-endian hash parsing in "Identifier
derivation" and the little-endian coordinate serialization here are both
normative and are deliberately different; the first follows Matrix hash
conventions and the second follows `libminisketch`.

A sketch of capacity `k` is therefore exactly $8k$ bytes, encoded on the wire as
unpadded base64url.

**Subtraction.** Two sketches over the same population and capacity are
subtracted by XOR. The result is the syndrome of the symmetric difference. This
is the property that makes the profile group-valued and the reason a failed
exchange can be extended rather than restarted.

**Capacity bounds.** A `sketch` exchange consists of one or more extraction
requests, each a `(depth, prefix, capacity)` triple (see "Dynamic tree
extraction," below). The sum of `capacity` across all requests in a single
exchange MUST NOT exceed 4096. A future profile MAY raise this cap;
`algebraic_v1` MUST NOT.

## Dynamic tree extraction

A single sketch at `depth = 0` covers the whole population and is exact only
while the true difference is within its capacity. When it is not, the population
is localized by recursive binary subdivision instead of a fixed partition.

`h_64(e)` determines an element's path down a binary tree: at depth `d`, an
element belongs to node `prefix` iff the leading `d` bits of `h_64(e)` equal
`prefix`. Depth 0 has a single node (`prefix = 0`) covering every element — the
same population a single flat sketch covers. If a node's sketch fails to decode
at its requested capacity, the peer that detects the failure requests two child
sketches at `depth + 1`, for prefixes `2 * prefix` and `2 * prefix + 1`. A child
that still overflows is split again. This is recursive: since each split
strictly partitions its parent's population, the recursion terminates — worst
case at `depth = 64`, where `h_64` no longer distinguishes elements.

Every node, at any depth, is decoded and verified exactly as in "Decode and
verification," below: it either decodes within its capacity and passes the
128-bit residual check, or it fails loudly and is split. There is no separate
"bucket" primitive and no persistent per-node resident state — see "Resident
structure." A `(depth, prefix)` pair is computed only when a peer actually
requests it.

## Strata estimator

Implementations MAY maintain a 32-entry strata estimator for pre-decode
difference sizing. Consumers that expose the estimator in their wire contract
define whether it is optional; MSC0501 requires all 32 entries in `room_digest`
responses.

Stratum `i` contains the same odd syndrome coordinates `s1` through `s15` as an
extraction sketch, but only for elements whose $h_{64}(e)$ has exactly `i`
trailing zero bits. Stratum 31 also includes every value with 31 or more
trailing zero bits. Each stratum is therefore a 64-byte sketch.

Two peers XOR corresponding strata and inspect the highest nonempty residual
stratum to estimate $|S_A △ S_B|$ before choosing between a single depth-0
extraction, provisioning an initial dynamic-tree request, or abandoning the
comparison.

The estimator is advisory. It MUST NOT override a consumer's population check,
and it MUST NOT substitute for 128-bit residual verification of a decoded
difference. Strata summaries accurately estimate the residual only when both
sides use the same validated frame, stratum assignment, hash mapping, and
coordinate order. If any of those boundaries shift, the estimate is meaningless.
A server MUST NOT estimate or subtract across differing frames; the estimator
MUST NOT substitute for or override frame validation.

## Decode and verification

A decoder recovers up to `k` elements from a capacity-`k` syndrome residual.
Decode either succeeds with a set of $h_{64}$ values, or fails.

Decode failure is loud, and this is the central operational property of the
profile: a failed decode is reported as failure, not as an empty difference.
Consumers MUST distinguish `decoded` from `capacity_exceeded`.

**Verification.** A decoded difference MUST be checked against the 128-bit
accumulator before it is trusted. Concretely, for a peer that resolves decoded
short IDs to full identifiers:

$$
\mathrm{expected\_other\_side} =
\mathrm{residual\_digest} \oplus
\mathrm{accumulator}(\mathrm{own\_side\_full\_ids})
$$

The peer resolves the short IDs it holds, computes their 128-bit accumulator,
and compares. A mismatch means the decode was wrong or the populations differed;
the result MUST be discarded.

A peer cannot compute the 128-bit accumulator for identifiers it does not hold.
The asymmetry is intentional: each side verifies the half it can resolve, and
the residual carries the other half.

**Adversarial limits.** XOR accumulators are fault-detecting, not binding. Any
set of 129 `128-bit` values is linearly dependent over $\mathbb{F}_2$, so a peer
with freedom over which identifiers to include can construct a nonempty subset
whose accumulator is zero. Nothing in this profile relies on the accumulator
being binding. Consumers MUST verify transferred objects by their own rules —
signatures, hashes, authorization — and MUST NOT treat accumulator agreement as
evidence of authenticity.

Deployments needing adversarial robustness MAY define a future profile with
negotiated per-link salting for transmitted extraction sketches. Such a profile
MUST specify salt negotiation, salt derivation, the salted identifier mapping,
and how both sides identify the profile before subtraction. `algebraic_v1`
defines no salting and its fixed $h_{64}$ mapping MUST remain byte-compatible
across implementations. Deployments needing transferable accumulator evidence
should look to an LtHash-style profile under a future `digest_type` rather than
to `algebraic_v1`.

## Capacity provisioning

Provision extraction capacity from the count residual. In the common one-sided
lag case, $c = \operatorname{abs}\left(|S_A| - |S_B|\right)$ equals the exact
difference size.

$$
k = \left\lceil 1.5c \right\rceil + 4 +
\left\lceil r_{\mathrm{obs}} \cdot \widehat{\mathrm{RTT}} \right\rceil
$$

The three terms cover, respectively: measurement slack when divergence is not
purely one-sided, a small floor for tiny differences, and events arriving
concurrently during the round trip.

If decode fails at `k`, retry at larger `k` up to the cap, or split into
dynamic-tree children to localize a two-sided difference. Because sketches
subtract, a retry at higher capacity is a continuation of the same comparison,
not a restart. Additive extension is valid only when the syndrome coordinates
are strictly prefix-compatible: the frame anchor, hash mapping, field size, and
coordinate order MUST remain unchanged. If no compatible frame exists, peers
MUST perform frame discovery or backfill before retrying.

The escalation sequence is:

1. Validate the frame and abort, or use topology backfill, if the frame anchor
   does not match.
2. Execute the compact `algebraic_v1` exchange at depth 0.
3. Resolve small over-capacity differences by additive syndrome extension.
4. Isolate failures independently through dynamic tree extraction: split the
   overflowing node and retry each child.
5. For a difference so large or so heavy-tailed that it exhausts the 4096
   aggregate capacity across the tree, or would require recursing to impractical
   depth, treat it as a frame problem rather than a reconciliation problem — see
   "Scope," below.

**Why this and not a probabilistic filter.** Every node in the recursion uses
the same PinSketch decoder as the depth-0 case; no second decoder is introduced.
Every failure is loud, per "Decode and verification," at every depth. There is
no equivalent of a false positive: a node either decodes exactly at its offered
capacity, or it is split and tried again. This is why dynamic tree extraction is
preferred over both RIBLT and Bloom filters as the heavy-tail mechanism; see
Alternatives. It also carries no persistent resident cost proportional to tree
size — see "Resident structure," below — because nodes are computed only when
requested, unlike a fixed partition maintained for every population regardless
of whether it ever diverges.

**Scope.** Dynamic tree extraction is for a large but bounded difference within
an otherwise negotiated, shared frame — the "Swiss cheese" interior-gap case. A
difference approaching the size of the population itself (e.g. a server
restoring from near-zero state) is not a reconciliation problem. Peers SHOULD
recognize this from the count residual or an early, broadly-overflowing root
sketch and fall back to backfill or a frame-extension protocol rather than
recursing through most of the hash space.

## Resident structure

To make extraction deployable, implementations SHOULD maintain a resident
per-population structure:

<!-- markdownlint-disable MD013 -->

| Layer                 | Width            | Size  | Purpose                                      |
| --------------------- | ---------------- | ----- | -------------------------------------------- |
| Integrity accumulator | 128 bits         | 16 B  | ETag, level-0 agreement, decode verification |
| Strata estimator      | 64 bits × 8 × 32 | 2 KiB | pre-decode difference estimation             |

<!-- markdownlint-enable MD013 -->

Total resident state is approximately 2 KiB per active population. Dynamic tree
extraction (above) maintains no persistent per-node structure: sketches at any
`(depth, prefix)` are computed on demand, from the store, only when a peer
actually requests that node. This is a deliberate tradeoff against a prior
design that additionally maintained a fixed 256-way partition at all times (a
further ~21 KiB per population): that structure paid its cost on every
population whether or not it ever diverged, where dynamic extraction pays cost
only on the specific nodes a real difference touches.

**Update procedure.** On inserting or removing element `e`:

1. Compute $x = h_{64}(e)$.
2. Choose the estimator stratum from `x.trailing_zeros()`.
3. Compute $x^2$ once.
4. Update $x, x^3, ..., x^15$ by repeated multiplication by $x^2$.

In characteristic 2, insertion and removal are the same XOR operation, so no
separate deletion path is needed.

**Measured cost.** The reference implementation measures about 618 ns per
resident update with the portable multiply and about 52 ns with PCLMULQDQ on the
benchmarked `x86-64` machine. The underlying $\mathbb{F}_{2^{64}}$ multiply
measured about 77.25 ns portable and 6.50 ns with `PCLMULQDQ`, with
bit-identical results. Either path is small relative to the database write that
accompanies the update.

The resident 64-bit syndrome layer is an optimization, not a correctness
requirement. An implementation that computes sketches by scanning its store is
conforming, but SHOULD apply stricter request budgets, since its cost per
request scales with population size rather than difference size.

## Advertisement

Consumers advertise support through their own capability mechanism. The
canonical feature flag for this profile is:

```json
{
  "unstable_features": {
    "tk.nutra.msc0500.digest.algebraic_v1": true
  }
}
```

Dynamic tree extraction is part of `algebraic_v1` itself, not a separate
`digest_type`: a server that supports `algebraic_v1` supports depth-0 sketches
and their recursive refinement under the same flag, since both use the same
field, hash derivation, and decoder.

A future profile that changes the field, the hash derivation, the coordinate
ordering, or the capacity caps MUST use a new `digest_type` name. Profiles are
not versioned in place, because a comparison between two different profiles has
no defined meaning and must fail at negotiation rather than at decode.

## Potential issues

**Fixed capacity caps.** The 4096 aggregate cap is conservative and chosen for
the small one-sided differences expected to dominate normal federation repair.
Populations with routinely large or heavy-tailed differences will hit the cap
and fall back to dynamic tree extraction more often than necessary. Unlike a
rateless encoding, tree extraction requires no second decoder.

**64-bit collisions.** Two distinct identifiers can share $h_{64}$. At the
population sizes in scope this is rare, and the 128-bit verification step
catches the resulting bad decode, but it does mean a decode can fail for reasons
unrelated to capacity. Implementations MUST NOT interpret repeated verification
failure at adequate capacity as evidence of peer misbehavior without further
diagnosis.

**Resident state on many small populations.** 2 KiB per population is cheap even
in aggregate for a server participating in very many mostly-idle rooms.
Implementations SHOULD still evict resident structures under an LRU or TTL
policy and rebuild on demand; the accumulator alone (16 bytes) is enough for the
common no-difference path.

## Alternatives

**Fixed-Capacity Invertible Bloom Lookup Tables (IBLT).** Standard IBLTs are in
the same group-valued family and offer linear-time decoding. They are rejected
for the baseline because BCH/PinSketch-style syndromes are significantly more
compact. An IBLT requires three fields per cell (`count`, `id_sum`, `hash_sum`)
and typically requires 1.35x to 1.5x more cells than the expected difference
size to decode successfully. `algebraic_v1` requires exactly one field element
per unit of capacity, making it both cheaper per exchange and cheaper to
provision when dynamic tree extraction requests a node's sketch.

**Rateless IBLT (RIBLT).** Rejected. Rateless variants remove the need to choose
capacity up front while staying group-valued, but securing them against an
adversarial peer requires their own wire format: signed fixed-width count
semantics, overflow bounds, checksum and domain separation, chunk
authentication, explicit negotiated materialization limits such as finite
prefixes, and a termination rule — on top of a second decoder implementation
distinct from PinSketch. Dynamic tree extraction (above) handles heavy-tailed
differences instead, reusing PinSketch's existing decoder and `D(e)` digest with
no capacity guess and no second decoder, while remaining exact.

**Bloom filters.** Rejected. A Bloom filter is a homomorphism into an idempotent
monoid: it supports membership tests but not subtraction. A salted,
extremity-gated `bloom_v1` fallback was drafted and discarded in favor of
dynamic tree extraction: both handle the same heavy-tail case, but tree
extraction stays exact (loud decode failure at every step, no false positives)
and needs no new decoder, where a Bloom-based fallback would still need a
termination rule to bound its residual risk of a silently, permanently missed
event. See the MSC0501 architecture note for the full argument.

**LtHash / homomorphic hashing.** Provides binding accumulators at substantially
higher per-update cost. Appropriate where accumulator evidence must be
transferable to a third party; unnecessary where, as here, transferred objects
are independently verifiable by signature and hash. Left to a future
`digest_type`.

## Unstable prefix

<!-- markdownlint-disable MD013 -->

| Proposed final identifier | Purpose     | Development identifier                 |
| ------------------------- | ----------- | -------------------------------------- |
| `algebraic_v1`            | digest type | `algebraic_v1`                         |
| feature flag              | capability  | `tk.nutra.msc0500.digest.algebraic_v1` |

<!-- markdownlint-enable MD013 -->

## Dependencies

None. This MSC defines a self-contained primitive.

Known consumers and possible consumers:

- MSC0501 (federation missed-PDU reconciliation) — over a room's known-event set
- MSC0502 (federation EDU state reconciliation) may adapt the same algebraic
  machinery for EDU entries, but its current draft has separate version and
  content-hash semantics and is not wire-compatible with this event-ID profile.

## References

- Gennaro Boneh et al., PinSketch / set reconciliation via BCH syndromes
- Pieter Wuille, `libminisketch` — byte-compatibility reference for 64-bit field
- Eppstein, Goodrich, Uyeda, Varghese, "What's the Difference? Efficient Set
  Reconciliation without Prior Context" (strata estimator, IBLT)
- Bessani et al., rateless IBLT constructions
