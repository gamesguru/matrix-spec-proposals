# MSC45XX: Topological peek/query API via sparse fieldsets

Currently the Matrix protocol relies on fetching entire events to perform
backfills or otherwise retrieve previous or missing events. Often we do not know
the shape of the graph we are traversing, or whether it is a dead end.

This proposal seeks to remedy such inefficiencies and blockades by allowing
homeservers to return customized queries of highly granular data, including:

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
    "start_event_ids": ["$missing_event"],
    "edge_types": ["prev_events"],
    "max_depth": 50,
    "fields": ["prev_events", "origin"]
}
```

This asks the responding server to walk backwards through `prev_events`, up to
50 hops, returning only previous-event edges and origin hints.

The response is intentionally sparse:

```json
{
    "events": {
        "$missing_event": {
            "prev_events": ["$prev_1", "$prev_2"],
            "origin": "example.org"
        },
        "$prev_1": {
            "prev_events": ["$prev_0"],
            "origin": "elsewhere.example"
        }
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

The initial response fields for each event are:

- `prev_events`: known previous-event edges.
- `auth_events`: known auth-event edges.
- `origin`: the best known origin server for the event.
- `type`: the event type, if known.
- `state_key`: the state key, if known and applicable.
- `sender`: the sender, if known.

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

## Future extensions

Computed graph queries such as `common_ancestor` and `delta_depth` are useful
follow-up work. They can help a server decide whether two branches reconnect
within a bounded walk, or estimate how many events might be needed to bridge a
gap. They are deliberately not part of the initial endpoint because common
ancestors are not always unique in a DAG, and computing them safely needs its
own tie-breaking and leakage analysis.

A future room version could also make topology metadata independently provable
by changing event hashing rules. One possible direction is split
canonicalization: separate hashes for `prev_events`, `auth_events`, a small
event header, and content, with an event root used for the event ID and signed
by the origin server. That proof system belongs in a dedicated room-version MSC.
This MSC only defines the hinting API for existing hash-based room versions.

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
