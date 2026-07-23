# MSC4511 Part II: Signed Overlay Metadata Attestations

This companion to [Part I](4511-part-a-topological-metadata-query-api.md)
defines a backwards-compatible signed attestation sidecar for sparse topology
query responses. It does not change event IDs or room-version event hashing; it
provides transferable evidence of what a responder asserted.

## Proposal

One room-agnostic option is to leave the PDU format and `event_id` derivation
unchanged, and attach a signed responder attestation to the metadata known by
the responding server. The attestation lives alongside the query response, not
inside the event itself, so current room versions keep their existing hashing
and signature rules.

This is not an event-authenticity proof. The responder chooses the values it
attests to, so an adversarial responder can still lie about an event it claims
to know. The useful property is narrower: a requester can obtain a transferable
statement that "server X asserts these topology values for event E", compare the
same fixed commitment root across multiple responders, and later present a
signed contradiction to other servers or operators. Native event authenticity
requires the Part III split-canonicalization design, where the metadata
commitment is part of event identity.

### Overlay commitment construction

The overlay commitment is computed per returned event over a fixed leaf set,
independent of the subset disclosed in the query response. The initial fixed
leaf set is:

- `event_id`
- `room_id`
- `prev_events`
- `auth_events`
- `sender_localpart`
- `sender_domain`
- `type`
- `state_key`
- `depth`
- `origin_server_ts`

For each field in the fixed set, the responder canonicalizes the value it holds
as Matrix canonical JSON. If the server does not know a field or does not store
it, the canonical value is `null`. Declining to disclose a known value does not
change the committed value: the server omits that field from `leaf_paths` and
the positional response, but still commits to the value it knows. A server MUST
NOT omit a field from the committed set merely because the requester did not ask
to disclose it.

The fixed set intentionally excludes responder-local processing results such as
`rejected` and `soft_failed`. Those fields remain queryable hints, but including
them in the overlay root would make two honest servers produce different roots
for the same event whenever their local processing results differ.

`sender_localpart` and `sender_domain` are derived from the `sender` field using
the same split rule as the native sketch. The fixed overlay set does not include
a redundant full `sender` leaf; a consumer that needs the full MXID can
reconstruct it as `"@" || sender_localpart || ":" || sender_domain` when both
leaves are disclosed.

The commitment is then computed as follows:

- order the fixed field names bytewise;
- compute a leaf hash for each fixed field with
  `SHA3-256("msc4511:overlay-leaf:v1" || field_name || "\x00" || canonical_value)`;
- combine the ordered leaf hashes into a binary Merkle tree using
  `SHA3-256("msc4511:overlay-node:v1" || left_hash || right_hash)` for inner
  nodes;
- compute the sidecar commitment root as
  `SHA3-256("msc4511:overlay-root:v1" || event_id || leaf_count || merkle_root)`,
  where `event_id` is the returned event ID, `leaf_count` is the number of fixed
  leaves encoded as an unsigned 32-bit integer in network byte order, and
  `merkle_root` is the root hash of the fixed-field tree.

All concatenations above are byte concatenations: domain-separation strings and
`field_name` are UTF-8 bytes; `\x00` is a single `0x00` byte; `canonical_value`
is the UTF-8 encoding of the canonical JSON value; and
`left_hash`/`right_hash`/component hashes are the raw 32-byte hash outputs.

The fixed-field tree uses the largest-power-of-two split rule at each level,
with no padding leaves. A one-leaf tree's root is that leaf hash, though this
overlay version defines more than one fixed leaf and therefore never uses the
degenerate shape.

For a response containing one or more overlay proofs, the responder builds a
response-level Merkle tree over the returned per-event overlay commitments. The
response tree leaves are ordered bytewise by event ID and computed as:

```text
response_leaf =
  SHA3-256("msc4511:overlay-response-leaf:v1" || event_id || "\x00" ||
           overlay_commitment)

response_inner =
  SHA3-256("msc4511:overlay-response-node:v1" || left_hash || right_hash)
```

The responder signs the canonical response attestation envelope with its
existing federation signing key:

```json
{
  "room_id": "!room:example.org",
  "response_root": "base64url_sha3_256_hash",
  "event_count": 42,
  "origin_server_ts": 1716000000000,
  "fields_version": "msc4511.overlay.v1"
}
```

The signature proves only that the responder made the assertion. It does not
prove that the asserted values are true, that the event exists, or that the
event ID is bound to those values by the room version.

`fields_version` identifies the fixed overlay leaf set, tree construction, and
domain-separation strings together. A verifier MUST reject an overlay proof with
an unknown `fields_version`, because it cannot otherwise know the field set,
leaf ordering, or root preimage. The only `fields_version` defined by this MSC
is `msc4511.overlay.v1`, which uses the `msc4511:overlay-*:v1` domain-separation
strings above.

### Overlay proof responses

The response can carry an `overlay_proofs` sidecar object containing one signed
response attestation and a per-event `events` map. Each event entry contains the
sibling hashes needed to recompute the fixed-field Merkle root for disclosed
fields, the final `overlay_commitment`, and the response-tree inclusion path for
that commitment:

```json
"overlay_proofs": {
  "attestation": {
    "room_id": "!room:example.org",
    "response_root": "base64url_sha3_256_hash",
    "event_count": 42,
    "origin_server_ts": 1716000000000,
    "fields_version": "msc4511.overlay.v1",
    "signatures": {
      "example.org": {
        "ed25519:key": "signature_base64"
      }
    }
  },
  "events": {
    "$missing_event_A": {
      "leaf_paths": {
        "prev_events": [],
        "sender_domain": [
          { "side": "right", "hash": "base64url_sha3_256_hash" },
          { "side": "left", "hash": "base64url_sha3_256_hash" }
        ],
        "type": [
          { "side": "left", "hash": "base64url_sha3_256_hash" }
        ]
      },
      "overlay_commitment": "base64url_sha3_256_hash",
      "leaf_index": 0,
      "response_path": [
        { "side": "right", "hash": "base64url_sha3_256_hash" },
        { "side": "left", "hash": "base64url_sha3_256_hash" }
      ]
    }
  }
}
```

The `leaf_paths` object maps each disclosed field name to the sibling hashes
needed to rebuild the fixed-field Merkle root. For a field whose leaf is the
tree root, the path is empty and the leaf hash is used as-is. Otherwise, the
sibling list is ordered from the leaf level upward to the root. `leaf_index` is
the zero-based bytewise rank of the event ID in the signed response tree's
event-ID ordering.

Selective disclosure is meaningful only because the committed leaf set is fixed
independently of the disclosed subset. For example, a responder can disclose and
prove `sender_domain` while withholding `sender_localpart`; the sibling hashes
commit to whatever value, or `null`, the responder placed in the
`sender_localpart` leaf. This is still an attestation by that responder, not an
event-intrinsic proof.

### Overlay verification

To verify an overlay attestation, the requester performs the following steps:

1. Canonicalize each returned field and compute its domain-separated leaf hash.
2. Apply each step in `leaf_paths`, computing the parent inner hash using the
   provided left or right sibling, to reconstruct the fixed-field `merkle_root`.
3. Combine the reconstructed root with `event_id` and the fixed leaf count to
   compute the master `overlay_commitment`.
4. Verify that the computed commitment matches the `overlay_commitment` in the
   per-event `events` entry.
5. Verify that `event_count` is positive and that `leaf_index` is in
   `0..event_count-1`. A complete response MAY additionally verify that
   `event_count` equals the number of entries in `overlay_proofs.events`, but a
   forwarded single-event proof MUST NOT fail merely because the other response
   entries are absent.
6. Use the event ID and `overlay_commitment` to compute the response-tree leaf,
   then apply `response_path` to reconstruct the response root. The expected
   path length and each left/right sibling position are derived from
   `event_count`, `leaf_index`, and the largest-power-of-two split rule; a path
   that is inconsistent with that position fails verification.
7. Verify that the reconstructed response root matches the `response_root` in
   the signed response attestation.
8. Verify that `fields_version` is recognized.
9. Verify the responder's signature over the response attestation using normal
   Matrix federation signing-key rules. The signing server MUST be the
   responding server, not the event's `sender_domain`.

If a required hash is missing or any hash check fails, verification fails. By
chaining hashes upward, the server only needs to send missing sibling hashes in
the proof, and the requester recomputes the commitment locally.

This makes the overlay useful for accountability and corroboration:

- an attestation can be forwarded to a third party, unlike local hint suspicion;
- two honest servers answering about the same event can produce the same
  `overlay_commitment` even if they disclose different subsets;
- contradictory commitments included under signed response roots for the same
  `(room_id, event_id, fields_version)` tuple are useful evidence for debugging
  or reputation.

The overlay also preserves the current repair workflow. If a server only needs
to find likely bridge points, candidate peers, or a merge base, it can still use
the sparse query as a hint. If it needs authenticity before accepting an event,
it must still fetch and verify the full PDU through existing mechanisms.

The important part here is the boundary: signed commitment over a responder's
asserted metadata, not commitment that redefines the event itself.

The main trade-off is that this is responder-scoped rather than event-intrinsic.
That makes it easy to deploy incrementally, but it also means the attestation is
only as trustworthy as the server that signed it. It is a good fit for sparse
repair and operator workflows, but it does not replace a native event-level
commitment model if the protocol later wants the proof to be part of event
identity.

---

## Future extensions

Future overlay versions may define additional fixed leaf sets, different
response-tree batching profiles, or fresher liveness envelopes. A new
`fields_version` MUST identify the fixed leaf set, tree construction, and
domain-separation strings together, and verifiers MUST reject unknown versions.

## Performance characteristics and benchmarking

Exact speedups depend on implementation, database layout, cache state, and
workload, but the theoretical bandwidth bounds and storage overhead can be
quantified.

### Asymptotic bandwidth analysis

For a traversal visiting `N` events:

- full-event retrieval transfers `O(N * S_event)` bytes;
- sparse topology query transfers `O(N * S_meta + P)` bytes, where `S_meta` is
  the size of the requested metadata fieldset and `P` is the size of optional
  proof material.

When proofs are not requested, the bandwidth reduction approaches
`1 - (S_meta / S_event)`. As an illustrative range, if a full event is 1 to 5
KiB and the requested topology metadata is 80 to 300 bytes per event, the
bandwidth reduction is roughly 70% to 98%. Implementations MUST NOT rely on
these illustrative percentages as protocol guarantees.

### Storage overhead

With the initial fixed leaf set, a responder computes one fixed-field Merkle
root per attested event, then builds a response-level Merkle tree over those
event commitments and signs the response root once. Each disclosed field proof
carries roughly `ceil(log2(leaf_count))` sibling hashes, and each attested event
also carries a response-tree inclusion path. This avoids one Ed25519 signature
per event while preserving transferable evidence: a third party can verify the
single response signature and the event commitment's inclusion path. The
trade-off is that a standalone event attestation must carry the response root,
signature, and inclusion path, not just the event's fixed-field proof.

The overlay profile does not require persistent Merkle storage. Because the
fixed leaf set is event-intrinsic and excludes responder-local processing
results, a server MAY cache per-event overlay commitments, but it can also
recompute them from stored event metadata on demand.

### Cacheability

Event topology is immutable. Once an event's `prev_events`, `auth_events`,
`depth`, and event ID are known, those values do not change. Responses for
stable, authorization-equivalent queries are therefore cacheable in a way that
linear `/backfill` responses are not: a cached sparse topology answer can remain
useful even when later room history advances.

Signed overlay attestations include `origin_server_ts` in the signed envelope so
contradictory response roots can be ordered approximately in time. The attested
field set is restricted to event-intrinsic metadata, so an old attestation
should remain valid evidence that the responder made that assertion at the
signed timestamp. Responder-local hint fields such as `rejected` and
`soft_failed` are excluded from the overlay root precisely because their values
can change over time.

For liveness-sensitive decisions, requesters SHOULD reject or de-prioritize
overlay attestations whose signed `origin_server_ts` is more than 24 hours old,
unless local policy or operator tooling is explicitly evaluating historical
evidence.

### Empirical benchmarking

Implementations SHOULD benchmark this overlay profile against their specific
event store and federation workload. Recommended metrics include total bytes
transferred, number of round trips, database rows read, full event JSON decode
count, CPU time, active RAM usage, wall-clock latency, and success rate for gap
repair path selection.

## Relationship to other proposals

The overlay profile is a backwards-compatible accountability layer for Part I's
sparse query response. It does not replace Part III's native room-version
commitment model: a signed overlay proves only that the responding server made a
claim, while split canonicalization can make selected metadata part of event
identity.

## Security considerations

The major risks are excessive proof generation, metadata leakage, stale
attestations being treated as fresh liveness evidence, and verifiers confusing a
signed responder assertion with event authenticity. These are mitigated by hard
local limits, normal federation authorization, timestamp freshness policy, and
the explicit rule that overlay attestations are responder-scoped evidence only.
A server must still fetch and verify full events before accepting them,
repairing state, or considering a gap resolved.

### Bandwidth consumption

This MSC does expose a bandwidth-consumption surface for servers which implement
the endpoint. Authenticated federation peers could issue repeated large bounded
topology queries, so implementations should apply the same conservative
response-size and rate-limit controls described above.

This is not unique to this overlay profile: `/event`, `/backfill`,
`/get_missing_events`, and `/state_ids` already expose heavier bandwidth
surfaces.

The intended use case here is accountable sparse metadata: repair tooling can
record which server asserted which topology facts without requiring one
signature per returned event.

Servers should still treat this as an optional endpoint with hard response-size
limits, per-origin rate limits, and conservative defaults. If a deployment does
not see federation repair value from this query shape, it can decline to expose
the endpoint.

### Hint validation and reputation

Because overlay attestations do not make metadata true, requesting servers are
still exposed to potential misdirection from responding nodes. To mitigate this
without strictly standardizing a global reputation system, implementations
should rely on local heuristics.

Requesting servers SHOULD track topology hints they later verify against full
events. If a responding server repeatedly returns metadata contradicted by
verified event payloads, the requester MAY deprioritize that server for future
topology queries, apply local rate limits, or ignore its topology hints for a
limited period.

Signed overlay attestations make this evidence transferable. A requester that
obtains an `overlay_proofs` entry can show a third party that the responding
server signed a response root containing a particular commitment for
`(room_id, event_id, fields_version)` at the envelope's `origin_server_ts`. This
still does not prove the attested metadata is true, but it does make
contradictions between responders, or contradictions with a later fetched PDU,
auditable outside the original requester's local logs.

Fields such as `sender`, `type`, `depth`, `prev_events`, and `auth_events` are
falsifiable when the full PDU is eventually fetched, and are therefore useful
inputs to these heuristics. `candidate_servers` is not directly falsifiable in
the same way; a poor candidate may simply be stale or unavailable rather than
provably false.

Implementations should decay these penalties over time to prevent transient
corruption or partial-state desyncs from permanently poisoning a peer. Such
reputation data MUST NOT cause the requester to reject a valid event which
passes normal Matrix authorization and event verification.

## References

- [Matrix Server-Server API](https://spec.matrix.org/latest/server-server-api/)
  for `/event`, `/backfill`, `/get_missing_events`, `/state_ids`, federation
  authorization, and the existing PDU flow this proposal tries to avoid
  overusing.
- [Matrix room version 12](https://spec.matrix.org/latest/rooms/v12/) for the
  current default room-version baseline, including event format behavior
  inherited from room version 11, event IDs inherited from room versions 3 and
  later, and v12-specific room ID and state-resolution changes.
- [Polkadot Fellowship RFC-0078: Merkleized Metadata][polkadot-rfc-0078] as
  prior art for committing to metadata with a root hash while revealing only the
  pieces needed by the verifier.
- [Crosby and Wallach, Efficient Data Structures for Tamper-Evident
  Logging][crosby-wallach] as foundational work on the security model and proof
  semantics for tamper-evident logs, including logarithmic membership and
  consistency proofs and authenticated query results over logged event
  attributes.
- [RFC 6962, Section 2.1](https://datatracker.ietf.org/doc/html/rfc6962#section-2.1)
  for the Merkle tree construction used by overlay fixed-field and response
  trees.

[polkadot-rfc-0078]:
  https://polkadot-fellows.github.io/RFCs/approved/0078-merkleized-metadata.html
[crosby-wallach]:
  https://static.usenix.org/event/sec09/tech/full_papers/crosby.pdf
