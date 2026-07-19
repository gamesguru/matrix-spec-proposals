# MSC45XX: Topological peek/query API via sparse fieldsets and Merkleized metadata

Currently the Matrix protocol relies on fetching entire events to perform
backfills or otherwise retrieve previous or missing events. Often we do not know
the shape of the graph we are traversing, or whether it is a dead end.

This proposal seeks to remedy such inefficiencies and blockades by allowing
homeservers to return customized queries of highly granular data, including:

- `prev_event` edges, up to a recursion limit.
- `origin` for a missing event (potentially useful for retrieving it).

## Proposal

A new federation endpoint is added:

```http
POST /_matrix/federation/unstable/org.matrix.msc45xx/topology/query
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
    "depth": 50,
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
            "origin": "example.org",
            "prev_events": ["$prev_1", "$prev_2"]
        },
        "$prev_1": {
            "origin": "elsewhere.example",
            "prev_events": ["$prev_0"]
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
- `depth`: the maximum number of recursive hops requested.
- `fields`: the exact metadata fields requested.

The initial response fields for each event are:

- `prev_events`: known previous-event edges.
- `auth_events`: known auth-event edges.
- `origin`: the best known origin server for the event.
- `depth`: the event depth, if known.
- `type`: the event type, if known.
- `state_key`: the state key, if known and applicable.
- `sender`: the sender, if known.

Servers may omit fields they do not know, do not store efficiently, or are not
willing to disclose to the requester.

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
normal room access checks or history visibility policy.

### Merkleized metadata

For current room versions, this endpoint cannot prove an arbitrary topology
claim without fetching the full event payload. Modern event IDs commit to the
canonical JSON, but a server must fetch the full event to verify its hash.

This means the immediately deployable version of this API should treat returned
metadata as authenticated but untrusted routing information.

A future room version could make the metadata independently provable by
splitting the event hash into Merkle-like leaves, for example:

- one leaf for topology metadata such as `prev_events`, `auth_events`, `sender`,
  and `depth`;
- one leaf for content and other non-topological event data;
- a root hash used as the event ID commitment.

In such a room version, a response could include the requested metadata leaf and
the sibling hashes needed to prove that the metadata is committed to by the
event ID, without revealing the full event content.

## Security considerations

The major risks are:

- recursive queries being used for CPU, memory, or database exhaustion;
- metadata leaks about rooms, participants, or historical graph shape;
- malicious servers returning false topology to misroute repair attempts.

These are mitigated by hard local limits, normal federation authorization,
rate-limiting, and treating responses as hints. A server should still fetch and
verify full events before accepting any event, repairing state, or considering a
gap resolved.

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
