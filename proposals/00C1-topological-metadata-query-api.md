# MSC45XX: Topological peek/query API via sparse fieldsets

Currently the Matrix protocol relies on fetching entire events to perform
backfills or otherwise retrieve previous or missing events. Often we do not know
the shape of the graph we are traversing, or whether it is a dead end.

This proposal seeks to remedy such inefficiencies and blockades by allowing
homeservers to return routing hints as customized queries of highly granular
data, including:

- `prev_events` edges, up to a recursion limit.
- `origin` for a missing event (potentially useful for retrieving it).

## Proposal

A new federation endpoint is added:

```http
POST /_matrix/federation/unstable/tk.nutra.topology_query
```

The endpoint accepts a bounded query over one or more starting events. The
requesting homeserver chooses the edge types it wants to walk, how deep it wants
to recurse, and which small metadata fields it wants back.

For example:

```json
{
    "room_id": "!room:example.org",
    "start_event_ids": ["$missing_event_A", "$missing_event_B"],
    "edge_types": ["prev_events"],
    "max_depth": 50,
    "fields": ["prev_events", "origin"],
    "compute": ["common_ancestor", "hop_distance"],
    "compute_event_pairs": [["$missing_event_A", "$missing_event_B"]]
}
```

This asks the responding server to walk backwards through `prev_events`, up to
50 hops, returning only previous-event edges and origin hints.

The response is intentionally sparse:

```json
{
    "events": {
        "$missing_event_A": {
            "prev_events": ["$prev_1", "$prev_2"],
            "origin": "example.org"
        },
        "$missing_event_B": {
            "prev_events": ["$prev_1"],
            "origin": "elsewhere.example"
        }
    },
    "computed": {
        "common_ancestor": ["$prev_1"],
        "hop_distance": [3]
    },
    "limited": true
}
```

The returned metadata is a hint, not a replacement for fetching and verifying
events. A homeserver can use the hints to decide which servers and event IDs to
try next, then verify full events through normal Matrix rules once it retrieves
them.

### Query shape

The initial query fields are:

- `room_id`: the room being queried.
- `start_event_ids`: event IDs to start from.
- `edge_types`: one or more of `prev_events` or `auth_events`.
- `max_depth`: the maximum number of recursive hops requested.
- `fields`: the exact metadata fields requested.
- `compute`: optional graph facts to compute over the same bounded traversal.
- `compute_event_pairs`: event ID pairs to use for computed graph facts.

The initial response fields for each event are:

- `prev_events`: known previous-event edges.
- `auth_events`: known auth-event edges.
- `origin`: the best known origin server for the event.
- `type`: the event type, if known.
- `state_key`: the state key, if known and applicable.
- `sender`: the sender, if known.
- `proof`: Merkle proof material, only for room versions which define split
  canonicalization.

The response maps each event ID to an object containing the fields returned for
that event. Servers may omit fields they do not know, do not store efficiently,
or are not willing to disclose to the requester.

### Traversal

Traversal is breadth-first. The starting events are included in the response at
recursion depth `0`. Events reached by following one requested edge are at depth
`1`, and so on.

Each event ID is visited at most once, even if it is reachable through multiple
paths or multiple edge types. This also handles accidental or malicious cycles:
an already-seen event is not queued again.

Within the same recursion depth, events should be processed in bytewise
lexicographic order by event ID. This gives stable results when a response is
limited. Because event IDs may be hashes, this is not intended to prefer the
most recent or most useful branch. It is only a deterministic truncation rule.

The server applies limits in this order:

- reject malformed request fields before traversal;
- stop before returning more than the effective maximum event record count;
- do not follow edges past the requested or server-configured recursion depth;
- stop before exceeding the server's response-size or processing-time limits.

If any limit, visibility check, wrong-room event, unknown event, response-size
cap, or timeout prevents the server from returning data it otherwise would have
walked, it sets `limited` to `true`. If several conditions apply, `limited` is
still just `true`; this proposal does not require exposing which limit was hit.

### Computed graph queries

Responding servers MAY support small computed graph queries in addition to raw
metadata fields. These queries are bounded by the same recursion, record, time,
authorization, and room-boundary limits as normal traversal.

Computed graph queries operate on `compute_event_pairs`. Each entry is a
two-element list of event IDs. If `compute` is present, `compute_event_pairs`
MUST also be present and non-empty. Malformed pairs, pairs with fewer or more
than two event IDs, or pairs containing malformed event IDs cause the request to
fail with `M_INVALID_PARAM`.

The initial computed query names are:

- `common_ancestor`: given two event IDs, return the nearest event ID known to
  the responding server which is reachable from both events by following the
  selected edge types. If the search is limited before a common ancestor is
  found, the result is `null`; the server MUST NOT return a partial local
  ancestor as if it were final.
- `hop_distance`: given two event IDs, return the shortest known hop distance
  between them when following the selected edge types, or `null` if no path is
  found within the effective recursion limit.

Computed queries only walk events which belong to the requested room and are
visible to the requester. Hidden history-visibility branches are pruned, not
replaced with opaque markers. If pruning affects the answer, the server sets
`limited` to `true`.

If more than one pair is supplied, the server computes each requested graph fact
for each pair independently. The `computed` object maps each requested compute
name to a list of results aligned with `compute_event_pairs`. If either event in
a pair is unknown, wrong-room, or not visible to the requester, the result for
that pair is `null` and `limited` is set to `true`.

These results are hints. They MUST NOT be used as proof that two branches are
authentically related without fetching and verifying the relevant events, unless
the room version provides Merkleized topology proofs for the path.

### Limits

Responding servers MUST enforce local limits regardless of what the requester
asks for. At minimum, implementations should have admin-configurable limits for:

- maximum recursion depth,
- maximum returned event records,
- maximum number of start events,
- maximum response size,
- request rate per origin server.

If a response is truncated because of one of these limits, the server sets
`limited` to `true`.

### Authorization

The responding server MUST only return topology metadata which the requesting
server is allowed to learn over federation. This endpoint should not bypass
normal room access checks, membership checks, or history visibility policy.

The `room_id` is part of the query boundary. The responding server MUST validate
that each `start_event_ids` entry is a well-formed event ID before processing
the request. Malformed event IDs cause the request to fail with
`M_INVALID_PARAM`.

For every start event and every event discovered during traversal, the
responding server MUST validate that the event belongs to the requested
`room_id` before returning metadata for it or following its edges. Unknown
events and wrong-room events are omitted from the response. If any requested or
discovered event is omitted for being unknown, wrong-room, or not visible to the
requester, the response MUST set `limited` to `true`.

The responding server MUST NOT return topology metadata for an event if it would
not be allowed to serve the corresponding full event to the requester.

This means the answer can differ by room and event. A joined server can normally
query visible history for the room. An invited server should only receive
metadata that would already be visible through invite-stripped state or other
invite-legal federation flows. A non-joined server should not get private room
topology merely because it knows an event ID. For world-readable history, the
server may answer consistently with the room's history visibility rules, but
should still avoid disclosing fields beyond what the requester asked for.

Where the relevant historical state is known, visibility should be evaluated at
the event being queried, not only against current room state. If the responding
server cannot reconstruct the relevant historical state, it may fall back to
current room policy only when that fallback is at least as restrictive as the
known historical policy. Otherwise it should omit the event or use the degraded
repair mode below.

If the requester is not allowed to access the room at all, for example because
no user on the requesting server is joined or otherwise permitted by the room's
federation rules, the server MUST reject the request with `M_FORBIDDEN`. If the
requester is allowed to access the room but some branches, events, or fields are
not visible, the server omits those branches, events, or fields and sets
`limited` to `true`. Hidden branches are not replaced with opaque markers,
because such markers would still leak graph shape.

However, when the responding server cannot determine full-event visibility due
to local partial-state, missing-auth, or repair-in-progress conditions, it MAY
return only the minimum routing fields needed for repair, such as `origin` and
edge event IDs, provided the requester is already joined to the room. It MUST
NOT return `sender`, `type`, `state_key`, content-derived fields, or proof
material in this degraded mode. If current membership is also uncertain, the
responding server should use the most restrictive known membership or event-auth
view it can reconstruct for the queried event. If it cannot establish joined
membership, it must omit the event or reject the request with `M_FORBIDDEN`.

### Room versions

This endpoint only applies to room versions 3 and later.

Older room versions have different event ID and reference shapes. In particular,
room versions 1 and 2 use server-assigned event IDs and include hashes in
`prev_events` / `auth_events` entries rather than using the bare event ID shape
used by later room versions. Supporting those versions would either require
version-specific response shapes or lossy stripping of hashes.

Servers MUST reject requests for older room versions with
`M_UNSUPPORTED_ROOM_VERSION`. This keeps the first version of the endpoint
simple and avoids pretending that old event formats provide the same trust
properties as hash-based event IDs.

The metadata returned by this endpoint is still a hint. Even in room versions
with hash-based event IDs, a server must fetch the full event payload to verify
the claimed topology against the event hash.

## Split canonicalization and Merkleized metadata

To make topology metadata independently provable without fetching the full event
payload, this MSC defines split canonicalization for a new room version.

The new room version modifies event hashing to generate an `event_root` from
isolated metadata leaves:

- `prev_events_hash`: canonical hash of the event's `prev_events`;
- `auth_events_hash`: canonical hash of the event's `auth_events`;
- `event_header_root`: Merkle root over routing and authorship fields:
  `room_id`, `sender`, `type`, `state_key`, and `origin_server_ts`;
- `content_hash`: canonical hash of the remaining event body;
- `event_root`: the root hash committing to the above components.

The hash algorithm is SHA-256. Each hash input is domain-separated:

- Leaf hash:
  `SHA256("tk.nutra.msc45xx.leaf.v1" || field_name || "\x00" || canonical_value)`.
- Inner hash: `SHA256("tk.nutra.msc45xx.node.v1" || left_hash || right_hash)`.
- Root hash:
  `SHA256("tk.nutra.msc45xx.root.v1" || prev_events_hash || auth_events_hash || event_header_root || content_hash)`.

During development, implementations use `tk.nutra.msc45xx.*` domain separators.
Before stabilization, these MUST be replaced with the final room-version
identifier.

### Header tree construction

The `event_header_root` is constructed as a binary Merkle tree. Header leaves
are ordered bytewise by field name. Missing optional fields use the canonical
JSON value `null`; present fields use their standard Matrix canonical JSON
encoding.

Because the number of header leaves is not guaranteed to be a power of two,
implementations MUST construct `event_header_root` using the Merkle tree
algorithm defined in
[RFC 6962, Section 2.1](https://datatracker.ietf.org/doc/html/rfc6962#section-2.1).

### Event IDs and signatures

The event ID is derived directly from the root:
`"$" || unpadded_base64url(event_root)`.

The origin server's Ed25519 signature covers the canonical signed envelope
containing this root:

```json
{
    "room_id": "!room:example.org",
    "room_version": "msc45xx",
    "event_root": "unpadded_base64url_sha256_hash"
}
```

This keeps normal federation lightweight. A `/send` PDU can still look
functionally like an ordinary PDU; the receiving server computes the split
hashes locally when verifying the event.

### Cryptographic proof responses

When the queried room version supports split canonicalization, a server MAY
include proof material for requested fields. For example, a response proving
`prev_events` returns the canonical `prev_events` leaf, the top-level sibling
hashes needed to reconstruct `event_root`, and the origin signature. Those
top-level siblings include `auth_events_hash`, `event_header_root`, and
`content_hash` unless they are separately proven in the same response.

Sibling paths are represented from leaf to root as an array of objects:

```json
[
    { "side": "right", "hash": "base64url_sha256_hash" },
    { "side": "left", "hash": "base64url_sha256_hash" }
]
```

To verify topology without the payload, the requester canonicalizes the returned
field, computes its domain-separated leaf hash, applies each sibling in order to
reconstruct either the header root or the event root, reconstructs `event_root`
using the other provided top-level sibling hashes, checks that the event ID is
derived from that root, then verifies the origin server's Ed25519 signature over
the signed envelope. If a top-level sibling hash is not provided and not
reconstructed from another proof in the same response, verification fails.

## Future extensions

Future room versions may extend the proof fields, add more independently
provable leaves, or alter the domain separators during stabilization.

## Relationship to other proposals

This proposal is a lower-level, targeted, pull-based metadata primitive. A
set-reconciliation or gossip protocol could use it as a lookup step after
detecting divergence.

This proposal does not define push gossip, session state, set digests, or bulk
event repair. If another reconciliation proposal defines those higher-level
flows, this endpoint should compose underneath it rather than compete with its
wire format.

## Security considerations

The major risks are:

- recursive queries being used for CPU, memory, or database exhaustion;
- repeated valid-looking queries being used for bandwidth consumption;
- metadata leaks about rooms, participants, or historical graph shape;
- malicious servers returning false topology to misroute repair attempts.

These are mitigated by hard local limits, normal federation authorization,
rate-limiting, and treating responses as hints. A server should still fetch and
verify full events before accepting any event, repairing state, or considering a
gap resolved.

### Bandwidth consumption

This MSC does expose a bandwidth-consumption surface for servers which implement
the endpoint. An attacker who is already able to make authenticated federation
requests could ask for large bounded topology responses repeatedly.

This is not unique to this endpoint: `/event`, `/backfill`,
`/get_missing_events`, and `/state_ids` already expose heavier bandwidth
surfaces. The intended use case here is real-world gap repair: inbound
transactions, backfill attempts, and auth-chain recovery often need to know a
few edges or origins before deciding which full events to fetch. Returning
compact metadata can reduce total bandwidth compared to fetching full PDUs or
state sets blindly.

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

Implementations should decay these penalties over time to prevent transient
corruption or partial-state desyncs from permanently poisoning a peer. Such
reputation data MUST NOT cause the requester to reject a valid event which
passes normal Matrix authorization and event verification.

## References

- [Matrix Server-Server API](https://spec.matrix.org/v1.16/server-server-api/)
  for `/event`, `/backfill`, `/get_missing_events`, `/state_ids`, federation
  authorization, and the existing PDU flow this proposal tries to avoid
  overusing.
- [Matrix room version 7](https://spec.matrix.org/v1.16/rooms/v7/) for current
  event ID hashing, `prev_events`, `auth_events`, `origin`, `depth`, and event
  format behavior.
- [MSC4186: Simplified Sliding Sync](4186-simplified-sliding-sync.md), as prior
  art for selective, client-chosen field/query shapes in Matrix.
- [Polkadot Fellowship RFC-0078: Merkleized Metadata](https://polkadot-fellows.github.io/RFCs/approved/0078-merkleized-metadata.html)
  as prior art for committing to metadata with a root hash while revealing only
  the pieces needed by the verifier.
- [RFC 6962, Section 2.1](https://datatracker.ietf.org/doc/html/rfc6962#section-2.1)
  for the Merkle tree construction used by `event_header_root`.
