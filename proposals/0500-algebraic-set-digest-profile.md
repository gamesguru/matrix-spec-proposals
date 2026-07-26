# MSC0500: Stratified $\mathbb{F}_{2^{64}}$ PinSketch for fast set reconciliation

Several federation mechanisms need to answer one question: do two servers hold
the same set of identifiers, and if not, which ones differ? MSC0501 (federation
missed-PDU reconciliation) needs it over a room's known event or resolved state
set. MSC0502 may adapt the same machinery for ephemeral state, but its current
draft is not wire-compatible with this event-ID profile.

This MSC defines that primitive once, as a named digest profile, so that
consumers reference a field, a hash derivation, a wire encoding, and a decode
contract rather than building them from scratch each time.

The profile targets differences up to ~4,000 elements per exchange in
populations up to $10^6$, completing in ~50 ms and under 25 KiB per exchange,
excluding bodies. Decoding a capacity-`k` node costs $O(k^2 \log k)$; a
difference of size `Δ` spread over `n` nodes therefore costs
$O\!\left(\frac{\Delta^2}{n}\log\frac{\Delta}{n}\right)$. Larger differences are
a frame problem, not a reconciliation problem (§Scale boundary); the capped
round sequence extends the exchange ceiling to about 82,000 elements.

`algebraic_v1` couples the strata estimator, extraction sketch, and 128-bit
accumulator into one ladder. More capacity extends an exchange; it does not
restart it.

## Scope

This profile defines identifier derivation, the 64-bit field and `libminisketch`
compatibility contract, the level-0 accumulator, the syndrome sketch and its
capacity bounds, dynamic tree extraction and the strata estimator, the
decode-and-verify contract, capacity budgets, and the resident structure.

This profile does **not** define endpoints, frames, authorization, negotiation,
or scheduling; those belong to the consuming MSC. Consumers MUST still verify
that both sides digest the same population before comparing them.

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

Because `minisketch` set elements are nonzero, if the first 8-byte chunk of
`D(e)` is zero the implementation MUST use the next nonzero 8-byte chunk of
`D(e)`; if all four chunks are zero, it MUST use the integer value 1. This
applies to every consumer, not just Matrix bindings.

### Matrix event-ID binding

For Matrix event-ID sets, `D(e)` is derived as follows. For room versions 3 and
later, event IDs are already derived from `SHA-256` event hashes, so no
auxiliary hash is required. Implementations derive `D(e)` from the decoded event
ID using the room-version-specific event-ID alphabet:

- room versions 1 and 2: hash the UTF-8 event ID string with `SHA-256`;
- room version 3: decode the event ID as unpadded standard Base64;
- room versions 4 and later: decode event ID as unpadded URL-safe Base64.

Room versions whose event IDs are not hash-derived MUST set `D(e)` to the
`SHA-256` digest of the event-ID string, or exclude the event from the compared
population. This Matrix binding does not use `XXH3` or any other auxiliary hash.

## Field

The 64-bit Galois field is:

$$
\mathbb{F}_{2^{64}}
\cong
\mathbb{F}_{2}[x] \big/ \langle x^{64} + x^4 + x^3 + x + 1 \rangle,
$$

$h_{64}$ values are mapped to field elements by treating bit `i` of the integer
as the coefficient of $x^i$.

`algebraic_v1` sketches MUST be byte-for-byte compatible with `libminisketch` at
field size 64 for the same inserted $h_{64}$ values. This compatibility is the
normative interoperability test for the profile: an implementation that produces
a different byte string for the same input set is non-conforming, regardless of
whether its own decoder round-trips.

This requirement covers the odd-power coordinate order, little-endian coordinate
encoding, and the 64-bit field arithmetic together. If any one of those differs,
the sketch is not `algebraic_v1`.

This 64-bit field choice is the same algebraic reconciliation setting used by
PinSketch (Dodis et al., 2008) and the earlier finite-field set reconciliation
line introduced by Minsky, Trachtenberg, and Zippel (2003).

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
digest is a 16-byte value, encoded on the wire as unpadded base64url.

Insertion and removal are the same operation: XOR the same $h_{128}(e)$ value
into the accumulator and increment or decrement the count. There is no rebuild
path and no ordering requirement.

The count residual $c = \operatorname{abs}\left(|S_A| - |S_B|\right)$ is an
exact measurement of $|S_A\ \Delta\ S_B|$ when divergence is one-sided, which is
the common lagging-peer case. When both digest and count match over the same
population, the two sets agree except with negligible probability from an
accidental 128-bit collision.

The accumulator is an integrity anchor, not an authenticator. See "Decode and
verification" and the consuming MSC's security considerations.

## Syndrome sketch

The extraction layer is the odd-power syndrome map over the 64-bit field:

$$
\sigma_k(S) = \left(\sum h_{64}(e), \sum h_{64}(e)^3, ..., \sum h_{64}(e)^{2k-1}\right)
$$

Even powers are omitted because the Frobenius endomorphism makes them redundant
in characteristic 2: $s_{2i} = s_i^2$.

That odd-power syndrome form is standard BCH syndrome decoding machinery
(MacWilliams & Sloane, 1977) and is exactly the coding-theory substrate that
PinSketch specializes for reconciliation.

**Serialization.** Syndrome coordinates are serialized in increasing odd-power
order — `s1, s3, s5, ...` — and each coordinate is serialized as an unsigned
64-bit **little-endian** integer. The big-endian hash parsing in "Identifier
derivation" and the little-endian coordinate serialization here are both
normative and are deliberately different; the first follows Matrix hash
conventions and the second follows `libminisketch`. This endian split is
load-bearing for interoperability.

A sketch of capacity `k` is therefore exactly $8k$ bytes, encoded on the wire as
unpadded base64url.

**Subtraction.** Two sketches over the same population and capacity are
subtracted by XOR. The result is the syndrome of the symmetric difference. This
is the property that makes the profile group-valued and the reason a failed
exchange can be extended rather than restarted.

## Dynamic tree extraction

A single sketch at `depth = 0` covers the whole population and is exact only
while the true difference is within its capacity. When it is not, the population
is localized by recursive binary subdivision instead of a fixed partition.

`h_64(e)` determines an element's path down a binary tree: at depth `d`, an
element belongs to node `prefix` iff the leading `d` bits of `h_64(e)` equal
`prefix`. Depth 0 has a single node (`prefix = 0`) covering every element — the
same population a single flat sketch covers. Implementations MUST cap `depth` at
32, so `prefix` is at most 32 bits wide. If a node still overflows at its
requested capacity, the peer that detects the failure requests two child
sketches at `depth + 1`, for prefixes `2 * prefix` and `2 * prefix + 1`. A child
that still overflows is split again. If a node still overflows at `depth = 32`,
the peer that detects the failure MUST report failure for that prefix and fall
back to backfill or frame extension rather than splitting further. The recursion
terminates: each split reduces node population weakly, depth is bounded at 32,
and a node still overflowing at the cap is reported rather than split further.

Every node, at any depth, is decoded and verified exactly as in "Decode and
verification," below: it either decodes within its capacity and passes the
128-bit residual check, or it fails loudly and is split. There is no separate
"bucket" primitive and no persistent per-node resident state — see "Resident
structure." A `(depth, prefix)` pair is computed only when a peer actually
requests it.

**Requests MUST form an antichain.** No entry in a single exchange's `requests`
may be a tree-ancestor of another entry (i.e. one entry's `(depth, prefix)`
range MUST NOT contain another's). Overlapping entries would double-count
elements in the aggregate capacity check and make their sketches non-independent
for subtraction. A consumer MUST reject a request violating this before
subtraction.

**Capacity bounds.** A `sketch` exchange consists of one or more extraction
requests, each a `(depth, prefix, capacity)` triple. A single entry's `capacity`
MUST NOT exceed 64; the sum of `capacity` across all requests in a single
exchange MUST NOT exceed 4096. These are separate bounds for separate reasons:
the per-entry cap bounds decode cost ($O(k^2 \log k)$ per node), while the
aggregate cap bounds total wire size and responder work across a whole exchange.
A future profile MAY raise either cap; `algebraic_v1` MUST NOT.

**Materializing a node.** Producing the syndrome sketch for `(depth, prefix)`
requires the subset of the population whose `h_64(e)` shares that `depth`-bit
prefix. A responder MUST NOT satisfy this by scanning its full known-event set
per request: since `h_64(e)` is a fixed 64-bit key per element, any
`(depth, prefix)` subset is a contiguous range under `h_64`-sorted order.
Implementations MUST maintain (or build and cache) an index of known event IDs
ordered by `h_64`, so that a node's element subset is a range slice — O(log n)
to locate plus the slice size — not a full-population scan. This index holds
only IDs and `h_64` keys, not precomputed syndromes; it is far cheaper than the
resident per-node syndrome structure a fixed partition would require (see
"Resident structure"), and unlike that structure it serves every depth, not one
fixed depth.

The depth-limited refine-and-resolve shape mirrors the practical reconciliation
architecture validated by Erlay (Naumenko et al., 2019): keep the field math
fixed, size the exchange before decoding, and split only when the current
capacity is not enough.

## Strata estimator

Implementations SHOULD maintain a 32-entry strata estimator for pre-decode
difference sizing. Consumers that expose the estimator in their wire contract
define whether it is optional; MSC0501 requires all 32 entries in `room_digest`
responses.

This is the Difference Digest / strata-estimator idea from Eppstein, Goodrich,
Uyeda, and Varghese (2011): use a compact pre-decode summary to estimate
`|S_A \Delta S_B|` before committing to a decoder.

Stratum `i` contains the same odd syndrome coordinates `s1` through `s15` as an
extraction sketch, but only for elements whose $h_{64}(e)$ has exactly `i`
trailing zero bits. Stratum 31 also includes every value with 31 or more
trailing zero bits. Each stratum is therefore a 64-byte sketch.

Two peers XOR corresponding strata and inspect the highest nonempty residual
stratum to estimate $|S_A \Delta S_B|$ before choosing between a single depth-0
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
the residual carries the other half. See "Security considerations" below for
adversarial limits.

## Security considerations

XOR accumulators are fault-detecting, not binding. Any set of 129 `128-bit`
values is linearly dependent over $\mathbb{F}_2$, so a peer with freedom over
which identifiers to include can construct a nonempty subset whose accumulator
is zero. Nothing in this profile relies on the accumulator being binding.
Consumers MUST verify transferred objects by their own rules and MUST NOT treat
accumulator agreement as evidence of authenticity.

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
   the scale boundary section below.

**Scale boundary.** Dynamic tree extraction is for bounded interior gaps within
an agreed frame, not arbitrary divergence. It is round-limited rather than
log-limited: once the search frontier outruns a round's capacity, the cost is
about `Δ / aggregate_cap` rounds, and a 20-round cap with this profile's 4096
aggregate capacity yields about 82,000 elements. Larger differences should fall
back to backfill or frame extension.

## Resident structure

To make extraction deployable, implementations SHOULD maintain a resident
per-population structure:

<!-- markdownlint-disable MD013 -->

| Layer                 | Width            | Size  | Purpose                                      |
| --------------------- | ---------------- | ----- | -------------------------------------------- |
| Integrity accumulator | 128 bits         | 16 B  | ETag, level-0 agreement, decode verification |
| Strata estimator      | 64 bits × 8 × 32 | 2 KiB | pre-decode difference estimation             |

<!-- markdownlint-enable MD013 -->

Fixed resident state is ~2 KiB per population, independent of population size.
Node sketches are computed on demand from the `h_64`-sorted index (§Dynamic tree
extraction), which is `O(n)` in identifiers and not part of the fixed state.

**Update procedure.** On inserting or removing element `e`:

1. Compute $y = h_{128}(e)$ and $x = h_{64}(e)$.
2. XOR $y$ into the integrity accumulator and adjust the count by `+1` or `-1`.
3. Choose the estimator stratum from `x.trailing_zeros()`.
4. Compute $x^2$ once.
5. Update $x, x^3, ..., x^15$ by repeated multiplication by $x^2$.

In characteristic 2, insertion and removal are the same XOR operation, so no
separate deletion path is needed.

This update path is the operational side of the same BCH/PinSketch machinery and
is the reason the profile can stay fully additive while still supporting
pre-decode sizing.

**Measured cost.** The reference implementation measures about 618 ns per
resident update with the portable multiply and about 52 ns with `PCLMULQDQ` on
the benchmarked `x86-64` machine; the underlying $\mathbb{F}_{2^{64}}$ multiply
measures about 77.25 ns portable and 6.50 ns with `PCLMULQDQ`.

The strata estimator is an optimization, not a correctness requirement.

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
policy and rebuild on demand.

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
capacity up front, but they need their own wire format and a second decoder.
Dynamic tree extraction reuses PinSketch's decoder and `D(e)` digest with no
capacity guess.

**Bloom filters.** Rejected. A Bloom filter is a homomorphism into an idempotent
monoid: it supports membership tests but not subtraction.

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

- Dodis, Y., Ostrovsky, R., Reyzin, L., & Smith, A. (2008). Fuzzy extractors:
  How to generate strong keys from biometrics and other noisy data. _SIAM
  Journal on Computing, 38_(1), 97-139. <https://doi.org/10.1137/060651380>
- Minsky, Y., Trachtenberg, A., & Zippel, R. (2003). Set reconciliation with
  nearly optimal communication complexity. _IEEE Transactions on Information
  Theory, 49_(9), 2213-2218. <https://doi.org/10.1109/TIT.2003.815784>
- MacWilliams, F. J., & Sloane, N. J. A. (1977). _The theory of error-correcting
  codes_. North-Holland Mathematical Library.
  <https://neilsloane.com/doc/ms77.html>
- Naumenko, G., Maxwell, G., Wuille, P., Sasha, A., & Boneh, D. (2019). Erlay:
  Efficient transaction relay for Bitcoin. In _Proceedings of the 2019 ACM
  SIGSAC Conference on Computer and Communications Security (CCS)_ (pp.
  817-831). <https://doi.org/10.1145/3319535.3354237>
- Eppstein, D., Goodrich, M. T., Uyeda, F., & Varghese, G. (2011). What's the
  difference?: Efficient set reconciliation without prior context. _ACM SIGCOMM
  Computer Communication Review, 41_(4), 218-229.
  <https://doi.org/10.1145/2018436.2018462>
- Wuille, P. (n.d.). libminisketch byte-compatibility reference for 64-bit
  field. GitHub. <https://github.com/bitcoin-core/minisketch>
- Yang, L., Gilad, Y., & Alizadeh, M. (2024). Practical Rateless Set
  Reconciliation. In _Proceedings of the 2024 ACM SIGCOMM Conference_ (pp.
  595-612). <https://doi.org/10.1145/3651890.3672219>
- [`rezzy`](https://github.com/gamesguru/rezzy/tree/788ae96c0e1601790d8f4618754726ac70e7c24b)
  at commit `788ae96c0e1601790d8f4618754726ac70e7c24b` - reference MSC0500
  implementation, interoperability tests, and benchmark harness
