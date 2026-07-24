# MSC0500: An algebraic, group-valued digest for fast set reconciliation

Several federation mechanisms need to answer the same question: do two servers
hold the same set of identifiers, and if not, which ones differ? MSC0501
(federation missed-PDU reconciliation) needs it over a room's known event or
resolved state set. MSC0502 needs an analogous primitive for ephemeral state.
Future diagnostic and audit endpoints may need it again in other contexts.

This MSC defines that primitive once, as a named digest profile, so that
consumers reference a field, a hash derivation, a wire encoding, and a decode
contract rather than building them from scratch each time.

The algorithm supports differences of up to 4,000 elements between sets of up to
1 million total. It achieves this in under 50 ms with under 25 KB exchanged.

`algebraic_v1` is a coordinated algebraic ladder. The resident bucket syndromes,
strata estimator, and extraction sketch are views of one syndrome map. Separate
128-bit XOR accumulators—at room and bucket scope—provide agreement checks,
localization, and decode verification. Increasing extraction capacity extends an
exchange additively; it does not restart it.

## Scope

This profile defines:

- derivation of short identifiers from canonical 32-byte element digests;
- the finite field and its `libminisketch` compatibility contract;
- the level-0 accumulator;
- the syndrome sketch, its serialization, and its capacity bounds;
- the bucket summary and strata estimator;
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

"First" means the leading bytes of `D(e)` in network byte order. `h_128(e)` is
the first 16 bytes. `h_64(e)` is the first 8 bytes interpreted as an unsigned
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

The 64-bit field is:

$$
GF(2^{64}) \cong GF(2)[x] \big/ \langle x^{64} + x^4 + x^3 + x + 1 \rangle
$$

<!--
$$
\mathbb{F}_{2^{64}}
\cong
\mathbb{F}_{2}[x]\big/\bigl(x^{64}+x^{4}+x^{3}+x+1\bigr)
$$
-->

`h_64` values are mapped to field elements by treating bit `i` of the integer as
the coefficient of `x^i`.

`algebraic_v1` sketches MUST be byte-for-byte compatible with `libminisketch` at
field size 64 for the same inserted `h_64` values. This compatibility is the
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
\mathrm{count} &= |S|
\end{aligned}
$$

$$\bigoplus$$ denotes bitwise XOR over all selected 128-bit values. The digest
is encoded as 16 raw bytes using unpadded base64url.

Insertion and removal are the same operation: XOR the same `h_128(e)` value into
the accumulator and increment or decrement the count. There is no rebuild path
and no ordering requirement.

The count residual `c = abs(count_A - count_B)` is an exact measurement of
`|S_A △ S_B|` when divergence is one-sided, which is the common lagging-peer
case. When both digest and count match over the same population, the two sets
agree except with negligible probability from an accidental 128-bit collision.

The accumulator is an integrity anchor, not an authenticator. See "Decode
verification" and the consuming MSC's security considerations.

## Syndrome sketch

The extraction layer is the odd-power syndrome map over the 64-bit field:

```text
sigma_k(S) = (sum h_64(e), sum h_64(e)^3, ..., sum h_64(e)^(2k-1))
```

Even powers are omitted because the Frobenius endomorphism makes them redundant
in characteristic 2: `s_{2i} = s_i^2`.

**Serialization.** Syndrome coordinates are serialized in increasing odd-power
order — `s1, s3, s5, ...` — and each coordinate is serialized as an unsigned
64-bit **little-endian** integer. The big-endian hash parsing in "Identifier
derivation" and the little-endian coordinate serialization here are both
normative and are deliberately different; the first follows Matrix hash
conventions and the second follows `libminisketch`.

A sketch of capacity `k` is therefore exactly `8 * k` bytes, encoded on the wire
as unpadded base64url.

**Subtraction.** Two sketches over the same population and capacity are
subtracted by XOR. The result is the syndrome of the symmetric difference. This
is the property that makes the profile group-valued and the reason a failed
exchange can be extended rather than restarted.

**Capacity bounds.** An unbucketed sketch MUST NOT exceed capacity 64 on the
wire. In bucketed mode, the sum of per-bucket capacities MUST NOT exceed 4096. A
future profile MAY raise these caps; `algebraic_v1` MUST NOT.

## Bucket summary

Buckets localize two-sided differences that a single sketch cannot decode.

`bucket_count` is 256 in this profile. A value `h_64(e)` is assigned to the
bucket named by its leading 8 bits. Each bucket carries:

- a 128-bit bucket accumulator over `h_128(e)` for members of that bucket;
- a 24-bit member count.

Bucket summaries are diagnostic inputs for capacity provisioning, not a transfer
mechanism. A consumer requests one only after the count residual is zero or a
direct decode has failed — both of which indicate a two-sided difference. They
are not sent on the common one-sided lag path.

After receiving a bucket summary, a peer MAY issue a further sketch request
restricted to the differing buckets, with per-bucket capacities derived from the
per-bucket count residuals. Bucketed sketches are the concatenation of one
sketch per requested bucket, in ascending `bucket_id` order.

## Strata estimator

Implementations MAY maintain a 32-entry strata estimator for pre-decode
difference sizing. Consumers that expose the estimator in their wire contract
define whether it is optional; MSC0501 requires all 32 entries in `room_digest`
responses.

Stratum `i` contains the same odd syndrome coordinates `s1` through `s15` as an
extraction sketch, but only for elements whose `h_64(e)` has exactly `i`
trailing zero bits. Stratum 31 also includes every value with 31 or more
trailing zero bits. Each stratum is therefore a 64-byte sketch.

Two peers XOR corresponding strata and inspect the highest nonempty residual
stratum to estimate `|S_A △ S_B|` before choosing between direct extraction,
bucket localization, or abandoning the comparison.

The estimator is advisory. It MUST NOT override a consumer's population check,
and it MUST NOT substitute for 128-bit residual verification of a decoded
difference.

## Decode and verification

A decoder recovers up to `k` elements from a capacity-`k` syndrome residual.
Decode either succeeds with a set of `h_64` values, or fails.

Decode failure is loud, and this is the central operational property of the
profile: a failed decode is reported as failure, not as an empty difference.
Consumers MUST distinguish `decoded` from `capacity_exceeded`.

**Verification.** A decoded difference MUST be checked against the 128-bit
accumulator before it is trusted. Concretely, for a peer that resolves decoded
short IDs to full identifiers:

```text
expected_other_side = residual_digest XOR accumulator(own_side_full_ids)
```

The peer resolves the short IDs it holds, computes their 128-bit accumulator,
and compares. A mismatch means the decode was wrong or the populations differed;
the result MUST be discarded.

A peer cannot compute the 128-bit accumulator for identifiers it does not hold.
The asymmetry is intentional: each side verifies the half it can resolve, and
the residual carries the other half.

**Adversarial limits.** XOR accumulators are fault-detecting, not binding. Any
set of 129 128-bit values is linearly dependent over `GF(2)`, so a peer with
freedom over which identifiers to include can construct a nonempty subset whose
accumulator is zero. Nothing in this profile relies on the accumulator being
binding. Consumers MUST verify transferred objects by their own rules —
signatures, hashes, authorization — and MUST NOT treat accumulator agreement as
evidence of authenticity.

Deployments needing adversarial robustness MAY define a future profile with
negotiated per-link salting for transmitted extraction sketches. Such a profile
MUST specify salt negotiation, salt derivation, the salted identifier mapping,
and how both sides identify the profile before subtraction. `algebraic_v1`
defines no salting and its fixed `h_64` mapping MUST remain byte-compatible
across implementations. Deployments needing transferable accumulator evidence
should look to an LtHash-style profile under a future `digest_type` rather than
to `algebraic_v1`.

## Capacity provisioning

Provision extraction capacity from the count residual. In the common one-sided
lag case, `c = abs(count_A - count_B)` equals the exact difference size.

```text
k = ceil(1.5 * c) + 4 + ceil(observed_event_rate_per_second * estimated_RTT)
```

The three terms cover, respectively: measurement slack when divergence is not
purely one-sided, a small floor for tiny differences, and events arriving
concurrently during the round trip.

If decode fails at `k`, retry at larger `k` up to the cap, or request a bucket
summary to localize a two-sided difference. Because sketches subtract, a retry
at higher capacity is a continuation of the same comparison, not a restart.

## Resident structure

To make extraction deployable, implementations SHOULD maintain a resident
per-population structure:

<!-- markdownlint-disable MD013 -->

| Layer                               | Width             | Size   | Purpose                                      |
| ----------------------------------- | ----------------- | ------ | -------------------------------------------- |
| Integrity accumulator               | 128 bits          | 16 B   | ETag, level-0 agreement, decode verification |
| Bucket accumulators                 | 128 bits × 256    | 4 KiB  | two-sided localization, fault detection      |
| Bucket counts                       | 24 bits × 256     | 768 B  | count residuals and provisioning             |
| Bucket syndromes `s1` through `s15` | 64 bits × 8 × 256 | 16 KiB | fast-path extraction                         |
| Strata estimator                    | 64 bits × 8 × 32  | 2 KiB  | pre-decode difference estimation             |

<!-- markdownlint-enable MD013 -->

Total resident state is approximately 23 KiB per active population.

**Update procedure.** On inserting or removing element `e`:

1. Compute `x = h_64(e)`.
2. Choose the bucket from the leading 8 bits of `x`.
3. Choose the estimator stratum from `x.trailing_zeros()`.
4. Compute `x^2` once.
5. Update `x, x^3, ..., x^15` by repeated multiplication by `x^2`.

In characteristic 2, insertion and removal are the same XOR operation, so no
separate deletion path is needed.

**Measured cost.** The reference implementation measures about 618 ns per
resident update with the portable multiply and about 52 ns with PCLMULQDQ on the
benchmarked x86-64 machine. The underlying `GF(2^64)` multiply measured about
77.25 ns portable and 6.50 ns with PCLMULQDQ, with bit-identical results. Either
path is small relative to the database write that accompanies the update.

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

A future profile that changes the field, the hash derivation, the coordinate
ordering, or the capacity caps MUST use a new `digest_type` name. Profiles are
not versioned in place, because a comparison between two different profiles has
no defined meaning and must fail at negotiation rather than at decode.

## Potential issues

**Fixed capacity caps.** The 64 and 4096 caps are conservative and chosen for
the small one-sided differences expected to dominate normal federation repair.
Populations with routinely large or heavy-tailed differences will hit the cap
and fall back to bucket localization more often than necessary. A rateless
encoding (see Alternatives, below) is the natural answer, at the cost of a
second decoder.

**64-bit collisions.** Two distinct identifiers can share `h_64`. At the
population sizes in scope this is rare, and the 128-bit verification step
catches the resulting bad decode, but it does mean a decode can fail for reasons
unrelated to capacity. Implementations MUST NOT interpret repeated verification
failure at adequate capacity as evidence of peer misbehaviour without further
diagnosis.

**Resident state on many small populations.** 23 KiB per population is cheap for
active rooms and expensive in aggregate for a server participating in very many
mostly-idle ones. Implementations SHOULD evict resident structures under an LRU
or TTL policy and rebuild on demand; the accumulator alone (16 bytes) is enough
for the common no-difference path.

## Alternatives

**Rateless IBLT (RIBLT).** Invertible Bloom Lookup Tables are in the same
group-valued family and remove the need to choose capacity up front. This
profile keeps BCH/PinSketch-style syndromes as the baseline because they are
more compact per unit of extraction capacity, extend additively, and can be
maintained cheaply in the resident bucket array. A future profile MAY define a
rateless encoding for large or heavy-tailed differences; the two are
complementary rather than competing.

**Bloom filters.** Rejected. A Bloom filter is a homomorphism into an idempotent
monoid: it supports membership tests but not subtraction. See the MSC0501
architecture note for the full argument.

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
- Pieter Wuille, `libminisketch` — the byte-compatibility reference for field
  size 64
- Eppstein, Goodrich, Uyeda, Varghese, "What's the Difference? Efficient Set
  Reconciliation without Prior Context" (strata estimator, IBLT)
- Bessani et al., rateless IBLT constructions
