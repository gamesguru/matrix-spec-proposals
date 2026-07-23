# MSC4511 Part III: Merkleized Metadata Room-Version Sketch

This companion to [Part I](4511-part-a-topological-metadata-query-api.md)
sketches how a future room version could make selected topology metadata
independently provable by committing it into event identity. Current room
versions should use Part I as hint-only; Part II provides responder-scoped
attestations without a room-version change.

## Proposal

To make selected event metadata independently verifiable, this MSC sketches a
split canonicalization design for future room versions to opt into.

A compatible future room version modifies event hashing to generate an
`event_root` from isolated metadata leaves:

- `prev_events_hash`: canonical hash of the event's `prev_events`;
- `auth_events_hash`: canonical hash of the event's `auth_events`;
- `event_header_root`: Merkle root over routing and authorship fields:
  `room_id`, `sender_localpart`, `sender_domain`, `type`, `state_key`,
  `redacts`, `depth`, and `origin_server_ts`;
- `content_hash`: canonical hash of the remaining event body after the topology
  and header components above are separated out. This is distinct from the
  legacy event `hashes` field unless a future room-version MSC explicitly maps
  them together;
- `other_signed_fields_hash`: canonical hash of every remaining signed event
  field that is not included in `prev_events_hash`, `auth_events_hash`,
  `event_header_root`, or `content_hash`, strictly excluding the `signatures`
  and `unsigned` dictionaries;
- `event_root`: the root hash committing to the above components.

The future room version MUST define this partition so every signed,
identity-relevant event field is committed to exactly once. Two events which
differ in any signed field that contributes to event identity, including
`redacts`, MUST NOT derive the same `event_root` or event ID.

`sender_localpart` and `sender_domain` MUST be committed as two independent
header leaves rather than one combined `sender` leaf, using the same
first-`:`-boundary split defined above for the hint-mode `sender_domain` field:
the local part is everything between the leading `@` and the first `:`, and the
domain is everything after it. A room version adopting this format MUST reject
events whose `sender` does not parse under that grammar before deriving
`event_root`, since an unparsable `sender` would otherwise have no defined
split. Splitting the leaf this way is required, not merely convenient: with a
single `sender` leaf, any proof that discloses authorship information
necessarily discloses the full MXID, including the localpart. With
`sender_domain` committed separately, a prover can disclose and prove only the
sending server's identity, and a verifier can check signature entitlement,
without either party handling the sender's localpart at all. The sender's full
MXID remains recoverable and provable by disclosing both leaves together as
`"@" || sender_localpart || ":" || sender_domain`, so no authorship information
is lost, only made separable.

The hash algorithm is `SHA3-256`. Each hash input is domain-separated:

```text
leaf_hash =
  SHA3-256("msc4511:leaf:v1" || field_name || "\x00" || canonical_value)

inner_hash =
  SHA3-256("msc4511:node:v1" || left_hash || right_hash)

event_root =
  SHA3-256("msc4511:root:v1" || prev_events_hash || auth_events_hash ||
           event_header_root || content_hash || other_signed_fields_hash)
```

All concatenations above are byte concatenations: domain-separation strings and
`field_name` are UTF-8 bytes; `\x00` is a single `0x00` byte; `canonical_value`
is the UTF-8 encoding of the canonical JSON value; and
`left_hash`/`right_hash`/component hashes are the raw 32-byte hash outputs.

The top-level component hashes (`prev_events_hash`, `auth_events_hash`,
`content_hash`, and `other_signed_fields_hash`) are computed with the leaf-hash
construction above, using the field names `prev_events`, `auth_events`,
`content`, and `other_signed_fields` respectively.

The domain-separation strings use the stable MSC identifier `msc4511` and are
part of the event ID derivation. Implementations MUST NOT use the unstable
endpoint namespace or an implementation-local identifier for these domain
separators, because changing the identifier changes the derived `event_root` and
event ID.

### Header tree construction

The `event_header_root` is constructed as a binary Merkle tree. Header leaves
are ordered bytewise by field name. Missing optional fields, such as `redacts`,
use the canonical JSON value `null`; present fields use their standard Matrix
canonical JSON encoding.

Because the number of header leaves is not guaranteed to be a power of two,
implementations MUST construct `event_header_root` using the Merkle tree
algorithm defined in
[RFC 6962, Section 2.1](https://datatracker.ietf.org/doc/html/rfc6962#section-2.1),
substituting the domain-separated leaf and inner hash constructions defined
above for the RFC's `0x00`- and `0x01`-prefixed hashes. Only the tree shape (the
largest-power-of-two split rule and its recursion) is taken from RFC 6962; no
padding leaves are used.

<!-- RFC 6962 tree shape: dyadic interval decomp over ordered leaves. -->

### Event IDs and signatures

The event ID is derived directly from the root:
`"$" || unpadded_base64url(event_root)`.

The event signature covers the canonical signed envelope containing this root:

```json
{
  "room_id": "!room:example.org",
  "room_version": "<room_version>",
  "event_root": "unpadded_base64url_sha3_256_hash"
}
```

For room versions adopting this format, a future room-version MSC MUST specify
how the root signature interacts with, or replaces, existing event authorization
and verification rules. This keeps the proposal focused on the topology query
API and defers signature migration mechanics to the room-version proposal.

### Draft test vectors

The following vectors are non-normative implementation regression vectors for
the split-canonicalization sketch above. They are generated by
`cmd/merkle-vectors` in the `gomatrixcrypto` reference repository and are
included to make the `msc4511:*:v1` domain-separation strings, Matrix Canonical
JSON inputs, RFC 6962 tree shape, component ordering, missing optional header
fields, and unpadded base64url event ID encoding easy to cross-check while this
MSC is still unstable.

The sample inputs are:

```json
{
  "event_header_root_fields": {
    "room_id": "!room:example.org",
    "sender_localpart": "alice",
    "sender_domain": "example.org",
    "type": "m.room.message",
    "state_key": null,
    "redacts": null,
    "depth": 42,
    "origin_server_ts": 123456789
  },
  "prev_events": ["$a:example.org"],
  "auth_events": ["$auth:example.org"],
  "content": {
    "body": "hello",
    "msgtype": "m.text"
  },
  "other_signed_fields": {
    "origin": "example.org"
  },
  "signature_envelope": {
    "event_root": "4ccc880527fe5f97d27a04105bb55e6c6e75d87928e54a6cd2973c224802ce91",
    "room_id": "!room:example.org",
    "room_version": "tk.nutra.msc4511.12"
  }
}
```

The `origin` value above is included only as sample signed input for the test
vector's `other_signed_fields_hash`. It does not define `origin` as a queryable
field for this MSC.

The `signature_envelope` value above is the sample canonical signed envelope for
the stated `event_root`. It is signed with the sample Ed25519 key below to make
the draft vector self-contained.

The generated outputs are:

```text
[msc4511-merkle]
event_header_root_hex = db91cc8e8d3eb0d13885c32f28dbd4215a111081383e25263749c65d9bf8bc37
prev_events_hash_hex = fe8934c852d5a646390f3734f99911606c40f4f8ca7fe4065814081e2fb1faef
auth_events_hash_hex = 2309b8433c96de36d4a55cfb263f3f3131a0874324a9bda59bfd9e73e3846ea1
content_hash_hex = 8bfc6857f7a86d45b263c551057d052dfa73ef29dee6e842c90d12143abec729
other_signed_fields_hash_hex = 272428680275d80a8b02254dbbbe13e93af0153a6e8d80746d7d95dd1df48d59
event_root_hex = 4ccc880527fe5f97d27a04105bb55e6c6e75d87928e54a6cd2973c224802ce91
event_id = $TMyIBSf-X5fSegQQW7VebG512Hko5Ups0pc8IkgCzpE
event_signature_private_key_base64 = tyNS/1BppUG0XaG+6kzHwz+vj22Ikq0bRebV/Qzu+FI
event_signature_public_key_base64 = LYZrYjxYptzTRzEYBZzYMMEfX/2yYYqQ+RCw62Hmsz4
event_signature_base64 = 592xXLqbyExpxL1Te7zobls1Gh+IYYbliYCN3jTTn2Ny0kRnFGCEc22Sh/ifTCh/IDsJWVnmRFgrWA7JAqchBA
```

`prev_events_hash_hex`, `auth_events_hash_hex`, `content_hash_hex`, and
`other_signed_fields_hash_hex` are unchanged from the pre-split vectors, since
none of those inputs reference `sender`. `event_header_root_hex`,
`event_root_hex`, `event_id`, and the signature values change because
`event_header_root` now commits `sender_localpart` and `sender_domain` as
separate leaves instead of a single `sender` leaf.

### Cryptographic proof responses

When `proof` is requested in `fields` and the queried room version supports
split canonicalization, a server SHOULD include proof material for provable
requested fields inside the `proofs` sidecar object keyed by the corresponding
`event_id`. A room version adopting this format also extends the queryable
`fields` set with header leaves not exposed in hint-only mode, such as
`state_key` and `redacts`, since a field must be returnable to be provable.

Each `proofs` entry explicitly maps the proven fields to their Merkle paths,
provides any required top-level component hashes needed to reconstruct
`event_root`, and includes the event signature:

```json
"proofs": {
  "$missing_event_A": {
    "leaf_paths": {
      "prev_events": [],
      "sender_domain": [
        { "side": "right", "hash": "base64url_sha3_256_hash" },
        { "side": "right", "hash": "base64url_sha3_256_hash" },
        { "side": "left", "hash": "base64url_sha3_256_hash" }
      ],
      "origin_server_ts": [
        { "side": "left", "hash": "base64url_sha3_256_hash" },
        { "side": "right", "hash": "base64url_sha3_256_hash" },
        { "side": "right", "hash": "base64url_sha3_256_hash" }
      ]
    },
    "top_level_hashes": {
      "auth_events_hash": "base64url_sha3_256_hash",
      "content_hash": "base64url_sha3_256_hash",
      "other_signed_fields_hash": "base64url_sha3_256_hash"
    },
    "signatures": {
      "example.org": {
        "ed25519:key": "signature_base64"
      }
    }
  }
}
```

The example above discloses `sender_domain` but not `sender_localpart`;
`sender_localpart`'s hash is simply absorbed into one of the sibling hashes in
`sender_domain`'s `leaf_paths` entry, the same way any other undisclosed header
leaf would be. This is the selective-disclosure case this split exists for: a
verifier can confirm which server sent the event, and check signature
entitlement, without ever learning or requesting the sender's localpart.

To verify the authenticity of a field all the way to the root, the requester
performs the following steps:

1. Canonicalize each returned field and compute its domain-separated leaf hash.
2. Apply each step in `leaf_paths`, computing the parent inner hash using the
   provided left or right sibling, to reconstruct `event_header_root` or the
   relevant top-level component hash. For top-level components (`prev_events`,
   `auth_events`, content, and other signed fields), the path list is empty and
   the leaf hash is used directly.
3. Combine the reconstructed component with the remaining hashes in
   `top_level_hashes` to compute the master `event_root`. `top_level_hashes`
   MUST contain every component hash not reconstructed from a proof in the same
   response.
4. Verify that the event ID matches `"$" || unpadded_base64url(event_root)`.
5. If the proof is being used as authorship evidence, verify that the
   `sender_domain` leaf is disclosed or otherwise proven, then verify that the
   event signature is from that server name according to the room version's
   signing rules. `sender_domain` alone is sufficient for this check;
   `sender_localpart` does not need to be disclosed or proven to establish
   signature entitlement.

If a required hash is missing or any hash check fails, verification fails. By
chaining hashes upward, the server only needs to send missing neighbor hashes in
the proof, and the verifier recomputes the root locally.

A proof which omits `sender_domain` can authenticate disclosed data against a
known event ID, but does not by itself prove that the signing server is the
server entitled to sign for the event's sender. This distinction matters for
proof consumers outside the backwards-DAG walk, where the event ID may not have
been learned from a previously verified event.

The `signatures` object is not committed to `event_root`; like existing Matrix
signed JSON, signatures are excluded from the signed hash input. Intermediaries
can therefore strip signatures or append additional signatures without changing
the event ID. The signature map keys identify which server keys to try for
verification, but entitlement still comes from the expected server name given by
a proven or disclosed `sender_domain` field, not from `sender_localpart`.

The following side-by-side example DAG shows the metadata-query opportunity. The
left side illustrates the legacy fetch-and-verify path over the chain. The right
side shows the same DAG shape when Merkle proofs let the verifier check topology
metadata before deciding which full PDUs to fetch:

```text
Legacy: fetch-and-verify each hop         Merkleized: prove metadata first

    [known anchor]                                [known anchor]
          |                                             |
         e1                                            e1
        /  \                                          /  \
      e2    e3                                      e2    e3
        \   /                                        \    /
         e4                                            e4
          |                                             |
         e6 tip                                        e6 tip
          |                                             |
          v                                             v
  fetch full PDU / event                  fetch sparse metadata + Merkle proof
  verify Ed25519 signature                recompute root locally from sibling hashes
  verify hashes + auth rules              if root matches, choose next fetches
  repeat for every ancestor               fetch full PDUs before acceptance
```

If you prefer Mermaid, the same comparison can be rendered as two separate
subgraphs, but the ASCII layout above is the least ambiguous when the goal is a
literal left-right comparison.

Redaction semantics are deferred to the future room-version MSC that adopts
split canonicalization. That room version MUST define whether `content_hash`
commits to the full unredacted content, the redacted event representation, or
separate full and redacted content commitments. This topology proof format only
proves the selected metadata leaves and their inclusion in `event_root`; it does
not by itself authorize disclosure of redacted content or change Matrix
redaction rules.

### Marginal value for gap repair

Merkleized topology proofs verify the committed metadata they disclose, but they
are not required for Part I's current gap-repair workflow. A requester must
still fetch the full PDU before accepting an event, because it needs `content`,
`auth_events`, event hashes, signatures, auth rules, and state-resolution
inputs. A proof of `prev_events` therefore adds proof bytes and verification
work without removing the eventual full-event fetch. The remaining
malicious-peer case is early abandonment of a fabricated branch, which this MSC
handles with request work budgets, `limited`, `edge_errors`, and local
hint-reputation heuristics.

The stronger motivation for this sketch is selective disclosure: proving one
field to a party who is not entitled to the whole event, proving topology
without revealing `content`, proving which server sent an event without
revealing the sender's localpart (see `sender_localpart` / `sender_domain`
above), or proving absence for fixed header leaves where a missing optional
field is committed as canonical `null` at a known leaf position. This
construction does not prove absence for arbitrary fields folded into
`other_signed_fields_hash` without revealing the corresponding signed-field set.
Those use cases need their own room-version work before they can become
normative.

---

## Future extensions

Future room versions may extend the proof fields or add more independently
provable metadata fields. Any extension MUST keep the event-root partition
unambiguous: every signed, identity-relevant event field must be committed to
exactly once, and verifiers must be able to determine which leaf position proves
or withholds each independently provable field.

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

The split-canonicalization sketch introduces storage overhead if a server stores
the top-level hashes `prev_events_hash`, `auth_events_hash`,
`event_header_root`, `content_hash`, `other_signed_fields_hash`, and
`event_root`.

Using SHA3-256, each hash is 32 bytes, so storing these six hashes adds 192
bytes of raw hash material per event before database row, index, and encoding
overhead. For a 2 KiB event, this raw hash material is approximately 9.4% of the
event size; for a 5 KiB event, it is approximately 3.8%. Implementations can
recompute proof paths on demand; caching intermediate Merkle nodes or proof
indexes is optional and would increase this overhead.

### Cacheability

Event topology is immutable. Once an event's `prev_events`, `auth_events`,
`depth`, and event ID are known, those values do not change. Responses for
stable, authorization-equivalent queries are therefore cacheable in a way that
linear `/backfill` responses are not: a cached sparse topology answer can remain
useful even when later room history advances.

### Empirical benchmarking

Implementations SHOULD benchmark this proof profile against their specific event
store and federation workload. Recommended metrics include total bytes
transferred, number of round trips, database rows read, full event JSON decode
count, CPU time, active RAM usage, wall-clock latency, and success rate for gap
repair path selection.

## Relationship to other proposals

This room-version sketch is the native-verifiability counterpart to Part I's
sparse query endpoint. It does not define push gossip, session state, set
digests, or bulk event repair; it only defines how a future room version could
make selected metadata independently provable once a query response chooses to
carry proof material.

Part II's signed overlay is the deployable, responder-scoped alternative for
current room versions. This Part III sketch is stronger but requires a future
room version because the metadata commitment must participate in event identity.

[MSC4242: State DAGs](https://github.com/matrix-org/matrix-spec-proposals/pull/4242)
changes the room model by adding state-DAG edges and authorization semantics in
a new room version. A room version adopting this sketch would need to define
whether state-DAG edges are additional independently provable metadata leaves,
and how they interact with state resolution.

## Security considerations

The major risks are incorrect field partitioning, metadata leaks through
selective disclosure, large repeated proof requests, and buggy proof validation
causing incorrect repair attempts. These are mitigated by hard local limits,
normal federation authorization, rate-limiting, explicit field partitioning, and
event-ID verification against `event_root`. Even with independently provable
metadata, a server should still fetch and verify full events before accepting
them, repairing state, or considering a gap resolved unless a later room-version
MSC defines a narrower operation that requires only the proven metadata.

### Bandwidth consumption

This MSC does expose a bandwidth-consumption surface for servers which implement
the endpoint. Authenticated federation peers could issue repeated large bounded
topology queries, so implementations should apply the same conservative
response-size and rate-limit controls described above.

This is not unique to this proof profile: `/event`, `/backfill`,
`/get_missing_events`, and `/state_ids` already expose heavier bandwidth
surfaces.

The intended use case here is real-world gap repair: inbound transactions,
backfill attempts, and auth-chain recovery often need to know a few edges or
candidate servers before deciding which full events to fetch. Returning compact
metadata can reduce total bandwidth compared to fetching full PDUs or state sets
blindly. This can also support operator-initiated repair tooling.

Servers should still treat this as an optional endpoint with hard response-size
limits, per-origin rate limits, and conservative defaults. If a deployment does
not see federation repair value from this query shape, it can decline to expose
the endpoint.

### Hint validation and reputation

Because current room versions cannot independently verify topological hints
without fetching the full event, requesting servers are exposed to potential
misdirection from responding nodes. To mitigate this without strictly
standardizing a global reputation system, implementations should rely on local
heuristics.

Requesting servers SHOULD track topology hints they later verify against full
events. If a responding server repeatedly returns metadata contradicted by
verified event payloads, the requester MAY deprioritize that server for future
topology queries, apply local rate limits, or ignore its topology hints for a
limited period.

Fields such as `sender`, `type`, `depth`, `prev_events`, and `auth_events` are
directly verifiable against `event_root` in room versions that adopt this
sketch. `candidate_servers` is not event-intrinsic and is not part of this
native proof model; a poor candidate may simply be stale or unavailable rather
than provably false.

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
- [MSC4186: Simplified Sliding Sync](4186-simplified-sliding-sync.md), as prior
  art for selective, client-chosen field/query shapes in Matrix.
- [MSC2836: Twitter-style Threading](https://github.com/matrix-org/matrix-spec-proposals/pull/2836),
  as prior art for bounded traversal of Matrix event relationships over
  federation.
- [MSC2716: Incrementally Importing History](https://github.com/matrix-org/matrix-spec-proposals/pull/2716),
  as related background for historical DAG gaps and inserted history chunks.
- [MSC4242: State DAGs](https://github.com/matrix-org/matrix-spec-proposals/pull/4242),
  as related work for representing state progression separately from the message
  event DAG.
- [Polkadot Fellowship RFC-0078: Merkleized Metadata][polkadot-rfc-0078] as
  prior art for committing to metadata with a root hash while revealing only the
  pieces needed by the verifier.
- [Crosby and Wallach, Efficient Data Structures for Tamper-Evident
  Logging][crosby-wallach] as foundational work on the security model and proof
  semantics for tamper-evident logs, including logarithmic membership and
  consistency proofs and authenticated query results over logged event
  attributes.
- [RFC 6962, Section 2.1](https://datatracker.ietf.org/doc/html/rfc6962#section-2.1)
  for the Merkle tree construction used by `event_header_root`.

[polkadot-rfc-0078]:
  https://polkadot-fellows.github.io/RFCs/approved/0078-merkleized-metadata.html
[crosby-wallach]:
  https://static.usenix.org/event/sec09/tech/full_papers/crosby.pdf
