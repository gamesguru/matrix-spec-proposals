# MSC4511 Part A: Topological Metadata Query API

Currently the Matrix protocol relies on fetching entire events to perform
backfills or otherwise retrieve previous or missing events. Often we do not know
the shape of the graph we are traversing, whether it is a dead end, or whether
two branches reconnect at a known common ancestor. When a server encounters a
gap in the DAG, the current federation API provides limited ways to discover
which events reference the gap, which servers sent or received them, or which
servers are otherwise likely to have the missing event before fetching full
events.

This proposal seeks to reduce these inefficiencies and traversal failures by
allowing homeservers to return routing hints as customized queries of highly
granular metadata and bounded graph facts, including:

- `prev_events` / `auth_events` edge event IDs, up to a recursion limit.
- candidate servers which may have useful data for the returned event or branch.
- whether a known edge target is outside the requested room, unavailable from
  this server, or not followed because the response was truncated.
- graph shape hints and bounded computed facts, such as common ancestors, hop
  distances, and per-event branching factor from returned edges.

For room versions 3 and later, returned metadata remains a hint that must be
verified by fetching full events. This is the intended security model, not a
weaker fallback: gap repair needs the full PDU at the end anyway in order to
validate `content`, `auth_events`, event hashes, signatures, auth rules, and
state resolution. The query response helps a server decide which event IDs and
peer servers to try next, but accepting or repairing history still happens
through the existing verified-event path. This proposal also sketches an opt-in
future room-version extension for Merkleized event metadata, allowing selected
metadata fields to be independently verified without fetching the full event
payload.

For a room version which defines State DAGs as in MSC4242, `prev_state_events`
is an additional supported edge type. It is traversed with the same depth,
record, and visited-node limits as `prev_events` and `auth_events`. A requester
MUST NOT infer State DAG edges from `auth_events` or `prev_events`, and a server
MUST reject `prev_state_events` with `M_UNSUPPORTED_ROOM_VERSION` when the room
version does not define that relation. State-DAG traversal remains a hint-only
query; it does not replace state resolution or authorize accepting an event.

## Proposal

A new federation endpoint is added:

```http
POST /_matrix/federation/unstable/tk.nutra.msc4511/topology_query
```

### Capability discovery

Servers advertise support for this endpoint via
`GET /_matrix/federation/v1/version`. Support is advertised in
`unstable_features` so that peers can avoid probing servers which do not
implement the topology query at all.

```json
{
  "unstable_features": {
    "tk.nutra.msc4511.topology_query": true,
    "tk.nutra.msc4511.computed_graph_queries": true
  }
}
```

The `tk.nutra.msc4511.overlay_attestations` flag is advertised separately in
[Part B](4511-part-b-merkle-overlay-backwards-compat.md) for overlay proofs. The
`tk.nutra.msc4511.computed_graph_queries` flag advertises the optional `compute`
extension in this part. Future room-version commitments described in
[Part C](4511-part-c-merkleized-room-version-upgrade.md) are gated by
room-version negotiation instead of a federation capability flag.

A server that does not advertise this flag SHOULD be treated as not supporting
the `/topology_query` endpoint. Receivers SHOULD avoid repeated probes to
unsupported peers; a `501 Not Implemented` response, or a `404` response with
`M_UNRECOGNIZED` or a non-Matrix body, SHOULD be cached as an unsupported signal
for at least 24 hours unless an operator explicitly overrides the cache. The
cache MUST be invalidated on any observed change to the peer's `/version`
document.

The endpoint accepts a bounded query over one or more starting events. The
requesting homeserver chooses the edge types it wants to walk, how deep it wants
to recurse, and which specific metadata fields it wants back.

For example:

```json
{
  "room_id": "!room:example.org",
  "start_event_ids": ["$missing_event_A", "$missing_event_B"],
  "edge_types": ["prev_events"],
  "max_depth": 50,
  "max_event_records": 1000,
  "max_nodes_visited": 5000,
  "max_compute_event_pairs": 10,
  "max_common_ancestors": 10,
  "max_candidate_servers_per_event": 5,
  "fields": [
    "event_id",
    "prev_events",
    "sender",
    "type",
    "candidate_servers",
    "edge_errors"
  ],
  "compute": ["common_ancestor", "hop_distance"],
  "compute_event_pairs": [["$missing_event_A", "$prev_1"]]
}
```

This asks the responding server to walk backwards through `prev_events`, up to
50 hops, returning previous-event edges, sender/type hints, candidate-server
routing hints, and requested edge errors.

The request uses `fields`, while the sparse response returns `event_fields` as a
positional header so the `events` rows can be decoded unambiguously even when
sidecar maps such as `edge_errors` are present.

The response is intentionally sparse:

```json
{
  "event_fields": [
    "event_id",
    "prev_events",
    "sender",
    "type",
    "candidate_servers"
  ],
  "events": [
    [
      "$missing_event_A",
      ["$prev_1", "$prev_2"],
      "@alice:example.org",
      "m.room.message",
      ["example.org"]
    ],
    [
      "$missing_event_B",
      ["$prev_1"],
      "@bob:elsewhere.example",
      "m.room.member",
      ["elsewhere.example", "example.net"]
    ],
    [
      "$prev_1",
      ["$prev_0"],
      "@alice:example.org",
      "m.room.message",
      ["example.org"]
    ]
  ],
  "edge_errors": {
    "$missing_event_A": {
      "prev_events": {
        "$foreign_event": "wrong_room",
        "$unavailable_event": "not_available",
        "$later_event": "truncated"
      }
    }
  },
  "computed": {
    "common_ancestor": [["$prev_1"]],
    "hop_distance": [1]
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
- `edge_types`: one or more of `prev_events`, `auth_events`, or
  `prev_state_events` when defined by the room version.
- `max_depth`: the maximum number of recursive hops requested.
- `max_event_records`: the maximum number of event records returned.
- `max_nodes_visited`: the maximum number of distinct events visited while
  serving raw traversal and all computed graph query pairs in the request.
- `max_compute_event_pairs`: the maximum number of event ID pairs accepted in
  `compute_event_pairs`.
- `max_common_ancestors`: the maximum number of `common_ancestor` results
  returned for each event pair.
- `max_candidate_servers_per_event`: the maximum number of candidate server
  names returned for each event.
- `fields`: the exact metadata fields requested. This list MUST include
  `event_id`.
- `compute`: optional graph facts to compute over the same bounded traversal.
- `compute_event_pairs`: ordered event ID pairs that each computed graph fact
  operates on.

The `fields` list MUST include `event_id` and MUST NOT contain duplicate field
names. A server MUST reject a request which omits `event_id` from `fields`, or
which contains duplicate `fields` entries, with `M_INVALID_PARAM` before
traversal.

The initial dense response fields available for the `events` rows are:

- `event_id`: the event ID for the returned metadata row. This field is required
  in `fields` and is always returned.
- `room_id`: the room the event belongs to. This is always the requested room,
  since wrong-room events are never returned as records.
- `prev_events`: known previous-event edges.
- `auth_events`: known auth-event edges.
- `prev_state_events`: known State DAG edges, only for room versions which
  define them.
- `sender`: the event sender, if known.
- `sender_domain`: the server name (domain) portion of `sender`, if known. This
  field exists because, as of room version 11, the top-level `origin` property
  is no longer protected from redaction and is not committed event metadata in
  any modern room version
  ([room version 11 redaction changes](https://spec.matrix.org/latest/rooms/v11/#redactions));
  a requester that only wants the sending server's domain, not the full MXID,
  can request `sender_domain` instead of `sender`. See below for the split rule.
- `type`: the event type, if known.
- `state_key`: the event state key, if the event is a state event.
- `candidate_servers`: a list of server names which the responding server
  believes may have useful data for this event or branch.
- `origin_server_ts`: the event timestamp, if known.
- `depth`: the event depth, if known.
- `rejected`: whether the responding server has locally rejected the event, if
  known.
- `soft_failed`: whether the responding server has locally soft-failed the
  event, if known.

The initial sparse response fields returned as sidecar maps are:

- `edge_errors`: non-followed edge targets keyed first by source event ID, then
  by edge type, then by target event ID to reason code.
- `start_event_errors`: non-returned start events keyed by start event ID to
  reason code.
- `proofs`: Merkle proof material, only for future room versions which opt into
  split canonicalization as described in Part C. Requested via the `proof` field
  name.
- `overlay_proofs`: signed responder attestations for room-version-agnostic
  metadata commitments as described in Part B. Requested via the `overlay_proof`
  field name and only available when the responder advertises
  `tk.nutra.msc4511.overlay_attestations` in `/_matrix/federation/v1/version`.

Unrecognized `edge_types` entries cause the request to fail with
`M_INVALID_PARAM`, because silently ignoring them would change traversal
semantics without the requester knowing.

`prev_state_events` is recognized only when the requested room version defines
the State DAG relation. Otherwise the server MUST reject the request with
`M_UNSUPPORTED_ROOM_VERSION`, rather than silently treating the edge as empty.

If the server does not support computed graph queries at all, it rejects any
request containing `compute` with `M_INVALID_PARAM`, as described below. If the
server does support computed graph queries, unrecognized `compute` names cause
the request to fail with `M_INVALID_PARAM`.

Unrecognized `fields` entries are ignored, which is indistinguishable from a
server declining to disclose a known field and keeps the field set
forward-extensible.

A room version or extension MAY define additional queryable fields. Unrecognized
`fields` entries remain ignored unless a room version or extension specifies
otherwise.

Future extensions may also define an arbitrary query language over event fields,
including field projection, predicates over event JSON, and recursive relations,
provided the extension specifies deterministic evaluation, authorization
behavior, resource limits, and failure semantics.

To minimize parser allocations and uncompressed payload size for
high-cardinality queries, the response encodes dense event metadata as
`event_fields` plus `events`, not as bulky per-event objects. `event_fields` is
a list of field names, and each entry in `events` is a list of values
corresponding positionally to those names. `event_fields` MUST include
`event_id`, because valid requests are required to include `event_id` in
`fields`. Each field name, including `event_id`, MUST appear at most once in
`event_fields`. An event entry MUST have exactly the same length as
`event_fields`.

If a server does not know a dense value, does not store it efficiently, or is
not willing to disclose it to the requester, it returns `null` in that field
position for the affected event. Dense fields that are unavailable for every
returned event MAY be omitted entirely from `event_fields`, except for
`event_id`. Servers MUST NOT rely on per-event object-key omission semantics in
`events`.

Fields expected to be highly sparse or bulky, such as `proof`, `overlay_proof`,
`edge_errors`, and `start_event_errors`, are returned in sidecar maps (`proofs`,
`overlay_proofs`, `edge_errors`, and `start_event_errors`) rather than in the
positional `events` rows. This ensures servers do not have to emit explicit
`null` slots for sparse data. A server MUST only include a sidecar map if the
corresponding logical field was requested in `fields` (e.g. `proof` for
`proofs`, or `overlay_proof` for `overlay_proofs`), and MUST only include
entries with applicable data to return. A requester MUST ignore unrecognized
field names while preserving positional alignment for fields it understands.

The `rejected` and `soft_failed` fields describe the responding server's local
event-processing result. They are hints only, may differ between servers, and
are not independently provable event metadata.

This MSC deliberately does not define an `origin` event field. Room version 11
removed `origin` (along with `membership` and `prev_state`) from the set of
top-level properties protected from redaction, formalizing what was already true
in practice: `origin` carries no defined meaning in modern room versions. (A
stale `origin` field lingered in the Matrix specification's own PDU JSON example
well after room version 11 shipped; that was a documentation bug fixed
separately in spec release v1.12, unrelated to any room-version behavior
change.) For current room versions the sender domain is available through the
queryable `sender_domain` field, derived from `sender`. Routing advice is
represented by `candidate_servers` instead, because that field is explicitly a
local belief about where repair requests may be productive, not a claim about
who signed the event.

For room versions 3 and later, `sender_domain` MUST be derived by splitting
`sender` at the first `:` character: the local part is everything between the
leading `@` and that first `:`, and the domain is everything after it. This
matches the Matrix user identifier grammar, under which the domain is a server
name that may itself contain a `:` (for an explicit port), so splitting at the
first `:` rather than the last `:` is required to recover the correct boundary.
A server MUST NOT split at the last `:`, and MUST treat a `sender` value that
does not parse as a well-formed user ID as unknown, returning `null` for both
`sender` and `sender_domain` rather than a partial or best-effort split.
Implementations SHOULD compute `sender_domain` directly from stored event data
rather than re-parsing `sender` on every response, but the two fields MUST
remain consistent: whenever both are returned for the same event,
`sender_domain` MUST equal the domain component of `sender` under the splitting
rule above. This split is purely a query-time convenience for current room
versions; see
[Part C: Split canonicalization and Merkleized metadata](4511-part-c-merkleized-room-version-upgrade.md)
for the independently provable analogue in a future room version.

### Traversal

Traversal is breadth-first. The starting events are included in the response at
recursion depth `0`. Events reached by following one requested edge are at depth
`1`, and so on.

Each event ID is visited at most once, even if it is reachable through multiple
paths or multiple edge types. This also prevents cycles from causing repeated
work: an already-seen event is not queued again.

Within the same recursion depth, servers SHOULD process events with known event
`depth` in `(depth descending, event_id ascending)` order. Events without known
depth are ordered after events with known depth, by event ID ascending. This
keeps the frontier near the query tips when a walk is truncated, which is the
part most likely to help find a merge base or a useful repair path. The event ID
tiebreaker is bytewise lexicographic over the UTF-8 encoding of the event ID
string. This is a deterministic truncation rule over a hint field; requesters
still verify fetched events before relying on them.

The server applies limits in this order:

- reject malformed request fields before traversal;
- reject requests with more than the effective maximum number of start events
  before looking up any events;
- stop before returning more than the effective maximum event record count;
- do not follow edges past the requested or server-configured recursion depth;
- stop queueing or inspecting new events before visiting more than the effective
  maximum number of distinct events;
- stop before exceeding the server's response-size or processing-time limits.

If a traversal, response-size, time, or work budget prevents the server from
returning data it otherwise would have walked, it sets `limited` to `true`. If
several such conditions apply, `limited` is still just `true`. Unknown,
inaccessible, hidden, and wrong-room edge targets do not by themselves set
`limited` when `edge_errors` is requested; those conditions are represented by
the applicable edge error code. If `edge_errors` was not requested and an
unknown, inaccessible, hidden, or wrong-room edge target causes a row to be
omitted, the server MUST set `limited` to `true` to signal that the response is
not a complete walk.

When requested via `fields`, a server SHOULD include `edge_errors` explaining
why certain edge targets were not followed. Servers MUST omit `edge_errors`
unless it is requested in `fields`. The initial reason codes are:

- `wrong_room`: the target event is known to belong to a different room.
- `not_available`: the target event appears to be an in-room edge target, but
  the responding server will not return it to this requester. This deliberately
  conflates unknown, locally missing, inaccessible, and hidden events.
- `truncated`: the target event was not inspected or followed because the
  traversal, response-size, time, or work budget was exhausted.

The same reason codes are used for `start_event_errors`. A server SHOULD return
`start_event_errors` when requested and one or more `start_event_ids` cannot be
returned as event records. If `start_event_errors` was not requested and a start
event is omitted for being unknown, wrong-room, inaccessible, or hidden, the
server MUST set `limited` to `true`.

For example:

```json
{
  "edge_errors": {
    "$missing_event_A": {
      "prev_events": {
        "$foreign_event": "wrong_room",
        "$unavailable_event": "not_available",
        "$later_event": "truncated"
      }
    }
  }
}
```

The server MUST NOT include the other room's ID or any metadata from the
wrong-room event. Additionally, a server MUST only apply the `wrong_room` label
if it would be allowed to serve the target event to the requester under the
target event's own room's authorization and history-visibility rules; otherwise
the edge target is treated as unknown and omitted. Without this restriction, the
label would disclose whether the responding server holds an arbitrary event ID
from an unrelated, possibly private, room.

For in-room edge targets, the server SHOULD return `not_available` rather than
silently omitting the edge target when it cannot or will not return that target
to the requester. The code intentionally does not distinguish local absence from
history-visibility denial or other access restrictions. The requester learns
only that this branch is unproductive from this responding server. The edge
target's event ID was already disclosed by a visible source event's
`prev_events` or `auth_events` list, so this does not disclose a new unrelated
room identifier.

Servers SHOULD return `truncated` for edge targets which would otherwise have
been eligible for traversal but were not inspected because an effective limit or
local work budget was reached. This distinguishes "ask a different server" from
"ask the same server with a deeper or less expensive query".

If `edge_errors` or `start_event_errors` are not requested, availability
distinctions are unavailable. The requester can still use `limited: true` to
detect that traversal was cut short by an effective limit or local work budget,
or that the server omitted an event without returning a detailed reason code.

Implementations SHOULD maintain indexes for `prev_events`, `auth_events`, and
known forward extremities per room. These indexes allow the endpoint to answer
bounded reverse-edge queries and help local repair logic choose useful starting
events without scanning full event JSON. Forward extremities are not returned by
this endpoint because they describe a server's local view of the room DAG
boundary, rather than metadata committed to an individual event.

### Computed graph queries

Responding servers MAY support small computed graph queries in addition to raw
metadata fields. These queries are bounded by the same recursion, record, time,
authorization, and room-boundary limits as normal traversal.

If a server does not support computed graph queries, it MUST reject requests
containing the `compute` field with `M_INVALID_PARAM`. This ensures the
requester can gracefully fall back to raw edge traversal rather than silently
failing to receive expected computations.

Computed graph queries operate on `compute_event_pairs`. Each entry is a
two-element list of event IDs. If `compute` is present, `compute_event_pairs`
MUST also be present and non-empty. If `compute_event_pairs` is present without
`compute`, the server MUST reject the request with `M_INVALID_PARAM`. Malformed
pairs, pairs with fewer or more than two event IDs, or pairs containing
malformed event IDs cause the request to fail with `M_INVALID_PARAM`.

If the number of `compute_event_pairs` entries exceeds the effective
`max_compute_event_pairs` limit, the server MUST reject the request with
`M_INVALID_PARAM` before performing any event lookup, authorization check, graph
traversal, or computed query processing.

Servers MUST enforce a request-wide aggregate work budget covering raw traversal
and all computed graph query processing. At minimum, this budget MUST include
the effective `max_nodes_visited` cap and a processing-time limit.
Implementations SHOULD account for local storage work, such as database reads,
when setting those limits or applying additional local caps. Raw traversal and
computed queries consume the same request-wide `max_nodes_visited` budget; the
budget is not reset when computed query processing begins. The effective cap is
the lower of `max_nodes_visited` and the responding server's local limit.

When more than one `compute_event_pairs` entry is supplied, the server MUST
process pairs in request order. The effective `max_nodes_visited` budget is
shared across the whole request rather than reset for each pair. If the
remaining budget is exhausted before a pair's result can be determined, that
pair's affected computed result is `null`, any later affected results are also
`null`, and the response sets `limited` to `true`. If a request-wide time or
local work budget is exhausted after processing has started, the server returns
the partial response it can produce, sets affected computed results to `null`,
and sets `limited` to `true`.

The initial computed query names are:

- `common_ancestor`: given two event IDs, return the common ancestors which are
  maximal in the selected edge graph. A common ancestor is maximal if it is not
  reachable from another common ancestor by following the selected edge types.
  This follows Git's merge-base semantics: a pair may have more than one best
  common ancestor, and returning a non-maximal ancestor can mislead repair by
  pointing behind the useful merge base. The result is a list ordered by
  `(depth descending, event_id ascending)` where depth is known, with
  unknown-depth entries ordered after known-depth entries by event ID. If more
  maximal common ancestors are found than the effective `max_common_ancestors`
  limit allows, the server returns the first `max_common_ancestors` entries in
  that order and sets `limited` to `true`. If no common ancestor is found within
  the effective recursion limit, the result is an empty list. If the search is
  limited before any bounded maximal set can be determined, the result is
  `null`; the server MUST NOT return a partial ancestor set as if maximality had
  been established.

- `hop_distance`: given two event IDs, return the shortest directed hop distance
  from the first event to the second event following the selected edge types. If
  no directed path is found within the effective recursion limit, the result is
  `null`. If the search is limited (for example by `max_nodes_visited`,
  processing-time, or response-size limits) before a result can be determined,
  the result is `null` and the server MUST set `limited` to `true`.

Future computed query names or query-language extensions MUST specify their
inputs, outputs, limit behavior, authorization behavior, and deterministic
ordering. Extensions that introduce arbitrary predicates, recursive rule
evaluation, joins over arbitrary event fields, or a general-purpose query
language MUST also define hard evaluation budgets and truncation behavior, since
those features carry database-style recursive query execution risks.

Computed queries only walk events which belong to the requested room and are
visible to the requester. Hidden history-visibility branches are pruned, not
replaced with opaque markers. If pruning affects the answer, the server sets
`limited` to `true`.

If more than one pair is supplied, the server computes each requested graph fact
for each pair independently. The `computed` object maps each requested compute
name to a list of results aligned with `compute_event_pairs`. If either event in
a pair is unknown, wrong-room, or not visible to the requester, the result for
that pair is `null`. This does not by itself set `limited`.

These results are hints. They MUST NOT be used as proof that two branches are
authentically related without fetching and verifying the relevant events, unless
the room version provides Merkleized topology proofs for the path.

### Limits

Responding servers MUST enforce local limits regardless of what the requester
asks for. The effective limit is the lower of the requester-provided limit and
the server's configured local limit. If the requester omits an optional limit,
the server's configured local default applies.

At minimum, implementations MUST enforce these limits:

- maximum recursion depth;
- maximum returned event records;
- maximum distinct events visited while serving raw or computed graph queries;
- maximum number of computed graph query pairs;
- maximum common ancestors returned per computed graph query pair;
- maximum candidate servers returned per event;
- maximum number of start events;
- maximum response body size;
- maximum processing time;
- request rate per origin server.

The following request limits are optional. If present, they MUST be positive
integers:

- `max_depth`;
- `max_event_records`;
- `max_nodes_visited`;
- `max_compute_event_pairs`;
- `max_common_ancestors`;
- `max_candidate_servers_per_event`.

Omitting a limit uses the server's configured default, which may be lower than
its configured maximum. There is no request syntax for unlimited traversal;
negative values such as `-1` are invalid.

If a request limit is `0`, negative, or not an integer, the server MUST reject
the request with `M_INVALID_PARAM`. A request limit larger than the server's
configured maximum is not an error: consistent with the effective-limit rule
above, it is clamped to that maximum. Clamping by itself does not set `limited`;
`limited` is only set if the effective limit actually truncates the response.

If the number of `start_event_ids` entries exceeds the responding server's
effective maximum start-event count, the server MUST reject the request with
`M_INVALID_PARAM` before performing any event lookup, authorization check, or
graph traversal. This rejection does not produce a partial response and
therefore has no `limited` flag.

If the number of `compute_event_pairs` entries exceeds the effective
`max_compute_event_pairs` limit, the server MUST reject the request with
`M_INVALID_PARAM` before performing any event lookup, authorization check, or
graph traversal. This rejection does not produce a partial response and
therefore has no `limited` flag.

Implementations SHOULD use conservative defaults no higher than:

- `max_depth`: 500;
- `max_event_records`: 1000;
- `max_nodes_visited`: 5000;
- `max_compute_event_pairs`: 20;
- `max_common_ancestors`: 20;
- `max_candidate_servers_per_event`: 10;
- maximum start events: 20;
- maximum response body size: 1 MiB;
- maximum processing time: 3 seconds.

Implementations MAY use lower local defaults or absolute maxima. If a response
is truncated because of an effective limit, the server sets `limited` to `true`.

### Authorization

The responding server MUST only return sparse metadata which the requesting
server is allowed to learn over federation. This endpoint should not bypass
normal room access checks, membership checks, or history visibility policy.

The `room_id` is part of the query boundary. The responding server MUST validate
that each `start_event_ids` entry is a well-formed event ID before processing
the request. Malformed event IDs cause the request to fail with
`M_INVALID_PARAM`.

For every start event and every event discovered during traversal, the
responding server MUST validate that the event belongs to the requested
`room_id` before returning metadata for it or following its edges. Unknown
events and wrong-room events are omitted from the response as event records. If
`edge_errors` was requested, the server MAY label a known wrong-room edge target
as `wrong_room` on the source event, subject to the disclosure restriction in
the traversal section, but MUST NOT follow that edge or return metadata from the
wrong-room event. If any requested or discovered event is omitted for being
unknown, wrong-room, or not visible to the requester, the server SHOULD describe
the omission with `start_event_errors` or `edge_errors` when the applicable
field was requested. If the applicable error field was not requested, the server
MUST set `limited` to `true`.

This mirrors, for this query API, the same wrong-room hazard that
[MSC4307](https://github.com/matrix-org/matrix-spec-proposals/pull/4307) closes
at the core auth-rule level (rejecting `auth_events` whose `room_id` does not
match the event being authorized): an `auth_events` or `prev_events` edge can
point at an event from a different room, and both the base protocol and this
traversal endpoint must refuse to treat that edge as same-room data.

The responding server MUST NOT return event metadata if it would not be allowed
to serve the corresponding full event to the requester.

The `candidate_servers` field is also governed by this rule, but it requires
additional care because it can reveal participation and routing metadata. A
server MAY derive candidate servers from sources such as the event sender's
domain, servers which transmitted the event to this server, servers which
previously answered for nearby events in the branch, or servers known to be in
the room around the event's depth. A server SHOULD return only a small ordered
set of candidates it considers useful for repair, bounded by
`max_candidate_servers_per_event`, and SHOULD omit candidates whose disclosure
would reveal private membership or history information beyond what the requester
could otherwise learn. The sender domain is a useful candidate, but it only
identifies who is expected to sign the event; it does not prove that server
still has the event, is reachable, or is the best repair source. The field is a
routing belief, not event metadata committed by the PDU, and requesters MUST
treat it as advisory.

Servers SHOULD order `candidate_servers` by descending local confidence. The
field earns its place when it reflects who is likely to have the data, not
merely who signed the event. A response which always returns only the sender
domain is likely to reproduce common backfill dead ends when that server is
dead, defederated, or has purged history. Future extensions may add provenance
tags for candidate entries, such as whether a candidate is the sender domain, a
server which transmitted the event to this server, or a server known to be in
the room around the event's depth.

This means the answer can differ by room and event. A joined server can normally
query visible history for the room. An invited server should only receive
metadata that would already be visible through invite-stripped state or other
invite-legal federation flows. A non-joined server should not get private room
metadata merely because it knows an event ID. For world-readable history, the
server may answer consistently with the room's history visibility rules, but
should still avoid disclosing fields beyond what the requester asked for.

Where the relevant historical state is known, visibility should be evaluated at
the event being queried, not only against current room state. If the responding
server cannot reconstruct the relevant historical state, it may fall back to
current room policy only when that fallback is at least as restrictive as the
known historical policy. Otherwise it should omit the event.

If the requester is not allowed to access the room at all, for example because
no user on the requesting server is joined or otherwise permitted by the room's
federation rules, the server MUST reject the request with `M_FORBIDDEN`. If the
requester is allowed to access the room but some branches, events, or fields are
not visible, the server omits those branches, events, or fields. When the
omission affects a requested start event or traversed edge and the applicable
error field was not requested, the server sets `limited` to `true`. Hidden
branches are not replaced with opaque markers, because such markers would still
leak graph shape.

### Room versions

This endpoint applies to room versions 3 and later in a hint-only capacity.

Older room versions (1 and 2) use server-assigned event IDs and include hashes
in `prev_events` / `auth_events` entries rather than using bare event IDs.
Servers MUST reject requests for older room versions with
`M_UNSUPPORTED_ROOM_VERSION` to avoid version-specific response shapes and lossy
hash stripping.

The metadata returned by this endpoint for room versions 3 and later is strictly
a **hint**. A server must still fetch the full event payload to verify the
claimed topology against the event hash, validate signatures, run auth rules,
and perform state resolution. This endpoint therefore provides the same final
verification guarantees as existing federation repair flows while reducing round
trips and wasted full-PDU fetches.

---

## Future extensions

Future extensions may add more computed graph facts or additional traversal
relations while keeping this endpoint hint-only for current room versions.

### Forward recursive queries

Future extensions might define forward recursive queries over the same event
graph. In this context, "forward" means following the inverse of a stored edge:

- forward `prev_events` recursion follows events whose `prev_events` list the
  frontier event.
- forward `auth_events` recursion follows events whose `auth_events` list the
  frontier event.

Because events store backward references, a responding server computes forward
traversal from local indexes over `prev_events` and `auth_events`.
Implementations might also use local forward-extremity indexes or equivalent
adjacency caches to avoid performing full table walks or full event JSON scans.

The practical motivation is cache-hunting and witness discovery when an origin
server is unavailable: a forward walk helps identify the later events or peers
most likely to have observed the referenced resource.

Any forward recursive query extension would need to specify:

- whether it walks one or more forward edge types, and how those types map to
  the underlying stored relations, associations, or mentions;
- whether traversal proceeds breadth-first, or via another deterministic order;
- whether events can be revisited, and how cycles are handled;
- visibility and room-boundary rules and authorization;
- limits to recursion depth, returned records, and visited nodes;
- how truncation is reported, including whether `limited` is set and whether an
  `edge_errors` sidecar is offered.

Absent such an extension, this MSC does not define forward recursion semantics.
The current endpoint remains a reverse walk over `prev_events` and
`auth_events`, plus bounded computed facts derived from that reverse walk.

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

The base sparse-query endpoint does not require new per-event cryptographic
storage. Implementations can answer from existing event JSON plus indexes over
`prev_events`, `auth_events`, event ID, sender, type, state key, depth, and
origin timestamp. Optional caches for forward adjacency, candidate-server hints,
or common-ancestor computation are implementation details and should be sized by
local workload.

### Cacheability

Event topology is immutable. Once an event's `prev_events`, `auth_events`,
`depth`, and event ID are known, those values do not change. Responses for
stable, authorization-equivalent queries are therefore cacheable in a way that
linear `/backfill` responses are not: a cached sparse topology answer can remain
useful even when later room history advances. Responder-local fields such as
`rejected`, `soft_failed`, and `candidate_servers` can change over time and
should be cached more conservatively.

### Empirical benchmarking

Implementations SHOULD benchmark this endpoint against their specific event
store and federation workload. Recommended metrics include total bytes
transferred, number of round trips, database rows read, full event JSON decode
count, CPU time, active RAM usage, wall-clock latency, and success rate for gap
repair path selection.

## Relationship to other proposals

This proposal is a lower-level, targeted, pull-based metadata primitive. A
set-reconciliation or gossip protocol could use it as a lookup step after
detecting divergence.

This proposal does not define push gossip, session state, set digests, or bulk
event repair. If another reconciliation proposal defines those higher-level
flows, this endpoint should compose underneath it rather than compete with its
wire format.

This proposal is also complementary to
[MSC4242: State DAGs](https://github.com/matrix-org/matrix-spec-proposals/pull/4242).
State DAGs split state progression from the message event DAG; this endpoint
provides a sparse query primitive that can compose with state-DAG edges. A
state-DAG extension of this query shape can add efficient filters by event
`type` and `state_key`, allowing a server to ask targeted questions such as
"which membership-state branch contains this user?" without fetching full state
events or scanning unrelated state keys.

This proposal also overlaps conceptually with several existing DAG traversal and
repair MSCs, but sits at a different layer:

- [MSC4000: Forwards fill](https://github.com/matrix-org/matrix-spec-proposals/pull/4000)
  adds a mirror of `/backfill` for fetching successor PDUs. MSC4511 does not
  return the next slice of room history as a transaction. It returns bounded
  graph metadata, edge errors, candidate servers, and optional computed facts so
  a server can decide which events or peers to query next before fetching full
  PDUs through existing mechanisms.

- [MSC4370: Federation endpoint for retrieving current extremities](https://github.com/matrix-org/matrix-spec-proposals/pull/4370)
  exposes the current forward extremities a server would use as `prev_events` at
  request time. MSC4511 is not limited to current extremities: it can start from
  arbitrary known or missing event IDs, walk selected edge types, and return
  sparse per-event metadata for gap repair and historical traversal.

- [MSC4242: State DAGs](https://github.com/matrix-org/matrix-spec-proposals/pull/4242)
  changes the room model by adding `prev_state_events` edges and authorization
  semantics in a new room version. MSC4511 is intentionally additive for
  existing room versions: returned metadata is a routing and diagnostic hint
  unless a future room version adds independently verifiable metadata
  commitments. It does not replace state resolution or make metadata alone
  sufficient to accept history.

- [MSC2695: Get event by ID over federation](https://github.com/matrix-org/matrix-spec-proposals/pull/2695)
  fetches a single full PDU by event ID over federation. MSC4511 is the
  traversal step that answers _which_ event IDs to fetch: it returns sparse
  per-event metadata and edge context, and leaves fetching the full PDU to
  existing mechanisms such as MSC2695.

- [MSC2316: Federation queries to aid with database recovery](https://github.com/matrix-org/matrix-spec-proposals/pull/2316)
  defines a recovery protocol for a server that has lost its copy of the DAG.
  MSC4511 is not itself a recovery protocol, but its bounded edge queries are a
  natural pull primitive for such recovery: a recovering server can ask a remote
  peer for the edges and candidate servers it needs before fetching full events.

- [MSC2391: Efficient point-queries for room state over federation](https://github.com/matrix-org/matrix-spec-proposals/pull/2391)
  is the state-side analog: a granular alternative to bulk `/state` and
  `/state_ids` transfers for asking a targeted state question. MSC4511 targets
  DAG topology rather than state, and is recursive over selected edge types
  rather than a single point lookup.

None of these provides a recursive sparse query over DAG edges: arbitrary start
point, selectable edge types, bounded traversal, and sparse per-event metadata
plus candidate servers. MSC4511 is the piece these other proposals assume or
compose around rather than provide.

## Security considerations

The major risks are:

- recursive queries hanging or entering unbounded traversals;
- large repeated queries increasing bandwidth or processing load;
- metadata leaks about rooms, participants, or historical graph shape;
- buggy or misrepresented topology output causing incorrect repair attempts.

These are mitigated by hard local limits, normal federation authorization,
rate-limiting, and treating responses as hints unless the room version provides
verifiable Merkle topology proofs. A server should still fetch and verify full
events before accepting them, repairing state, or considering a gap resolved.

### Bandwidth consumption

This MSC does expose a bandwidth-consumption surface for servers which implement
the endpoint. Authenticated federation peers could issue repeated large bounded
topology queries, so implementations should apply the same conservative
response-size and rate-limit controls described above.

This is not unique to this endpoint: `/event`, `/backfill`,
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
