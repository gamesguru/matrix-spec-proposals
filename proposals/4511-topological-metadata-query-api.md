# MSC4511: Topological peek/query API with sparse fieldsets and Merkleized metadata

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

## Proposal

A new federation endpoint is added:

```http
POST /_matrix/federation/unstable/tk.nutra.msc4511/topology_query
```

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
- `edge_types`: one or more of `prev_events` or `auth_events`.
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
- `sender`: the event sender, if known.
- `sender_domain`: the server name (domain) portion of `sender`, if known. This
  field exists because, as of room version 11, the top-level `origin` property
  is no longer protected from redaction and is not committed event metadata in
  any modern room version
  ([room version 11 redaction changes](https://spec.matrix.org/latest/rooms/v11/#redactions));
  a requester that only wants the sending server's domain, not the full MXID,
  can request `sender_domain` instead of `sender`. See below for the split rule.
- `type`: the event type, if known.
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
  split canonicalization. Requested via the `proof` field name.

Unrecognized `edge_types` entries cause the request to fail with
`M_INVALID_PARAM`, because silently ignoring them would change traversal
semantics without the requester knowing.

If the server does not support computed graph queries at all, it rejects any
request containing `compute` with `M_UNRECOGNIZED`, as described below. If the
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

Fields expected to be highly sparse or bulky, such as `proof`, `edge_errors`,
and `start_event_errors`, are returned in sidecar maps (`proofs`, `edge_errors`,
and `start_event_errors`) rather than in the positional `events` rows. This
ensures servers do not have to emit explicit `null` slots for sparse data. A
server MUST only include a sidecar map if the corresponding logical field was
requested in `fields` (e.g. `proof` for `proofs`), and MUST only include entries
with applicable data to return. A requester MUST ignore unrecognized field names
while preserving positional alignment for fields it understands.

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
[Split canonicalization and Merkleized metadata](#split-canonicalization-and-merkleized-metadata-opt-in-sketch)
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

When requested via `fields`, a server MAY include `edge_errors` explaining why
certain edge targets were not followed. Servers MUST omit `edge_errors` unless
it is requested in `fields`. The initial reason codes are:

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
containing the `compute` field with `M_UNRECOGNIZED`. This ensures the requester
can gracefully fall back to raw edge traversal rather than silently failing to
receive expected computations.

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

## Sidecar commitment sketch

One room-agnostic option is to leave the PDU format and `event_id` derivation
unchanged, and attach a separate commitment only to the sparse fields returned
by this query. The commitment would live alongside the query response, not
inside the event itself, so current room versions would keep their existing
hashing and signature rules.

A responder that supports this overlay returns the sparse metadata requested by
`fields` and a sidecar commitment proving that the returned slice is exactly the
slice it committed to. This does not make the PDU self-splitting and does not
change event identity. It only gives the requester a way to verify the sparse
response without waiting for a full-event fetch.

### Overlay commitment construction

The overlay commitment is computed per returned event as follows:

- canonicalize the disclosed response fields as a map from field name to Matrix
  canonical JSON value;
- order the field names bytewise;
- compute a leaf hash for each disclosed field with
  `SHA3-256("msc4511:overlay-leaf:v1" || field_name || "\x00" || canonical_value)`;
- combine the ordered leaf hashes into a binary Merkle tree using
  `SHA3-256("msc4511:overlay-node:v1" || left_hash || right_hash)` for inner
  nodes;
- compute the sidecar commitment root as
  `SHA3-256("msc4511:overlay-root:v1" || event_id || field_count || merkle_root)`,
  where `event_id` is the returned event ID, `field_count` is the number of
  disclosed fields encoded as an unsigned integer in network byte order, and
  `merkle_root` is the root hash of the disclosed-field tree.

All concatenations above are byte concatenations: domain-separation strings and
`field_name` are UTF-8 bytes; `\x00` is a single `0x00` byte; `canonical_value`
is the UTF-8 encoding of the canonical JSON value; and
`left_hash`/`right_hash`/component hashes are the raw 32-byte hash outputs.

The disclosed-field tree uses the same binary Merkle shape as the native sketch
below: the largest-power-of-two split rule at each level, with no padding
leaves.

### Overlay proof responses

The response can carry a per-event sidecar map such as `proofs`, where each
entry contains the disclosed field values, the sibling hashes needed to
recompute the disclosed-field Merkle root, and the final `overlay_commitment`
for that event:

```json
"proofs": {
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
    "overlay_commitment": "base64url_sha3_256_hash"
  }
}
```

The `leaf_paths` object maps each disclosed field name to the sibling hashes
needed to rebuild the Merkle root for that field set. For fields committed
directly at the top level, the path is empty and the leaf hash is used as-is.
For fields inside the disclosed slice, the sibling list is ordered from the leaf
level upward to the root.

Because the overlay commits only to whichever fields the responder chose to
disclose in a given response, it already gets sender-domain-only selective
disclosure for free by disclosing `sender_domain` instead of `sender` — no
room-version change is required. This is unlike the native
split-canonicalization sketch below, where `sender_localpart` and
`sender_domain` must be fixed as separate committed leaves ahead of time,
because the leaf set there is part of event identity rather than chosen per
response.

### Overlay verification

To verify the authenticity of the disclosed slice, the requester performs the
following steps:

1. Canonicalize each returned field and compute its domain-separated leaf hash.
2. Apply each step in `leaf_paths`, computing the parent inner hash using the
   provided left or right sibling, to reconstruct the disclosed-field
   `merkle_root`.
3. Combine the reconstructed root with `event_id` and the field count to compute
   the master `overlay_commitment`.
4. Verify that the computed commitment matches the `overlay_commitment` in the
   response.

If a required hash is missing or any hash check fails, verification fails. By
chaining hashes upward, the server only needs to send missing sibling hashes in
the proof, and the requester recomputes the commitment locally.

This makes the overlay useful in two places:

- for room versions that want stronger verification of sparse query replies
  without changing event identity;
- for deployments that want to evaluate a proof layer before deciding whether a
  later room-version MSC is worth standardizing.

The overlay also preserves the current repair workflow. If a server only needs
to find likely bridge points, candidate peers, or a merge base, it can still use
the sparse query as a hint. If it also wants cryptographic assurance about the
returned slice, it can verify the sidecar commitment without waiting for a full
event fetch.

The important part here is the boundary: commitment over disclosed query output,
not commitment that redefines the event itself.

The main trade-off is that this is response-scoped rather than event-intrinsic.
That makes it easy to deploy incrementally, but it also means the proof is only
as durable and cacheable as the query response that carried it. It is a good fit
for sparse repair and operator workflows, but it does not replace a native
event-level commitment model if the protocol later wants the proof to be part of
event identity.

## Split canonicalization and Merkleized metadata (opt-in sketch)

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
`event_root`, since an unparseable `sender` would otherwise have no defined
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
  "field_root_fields": {
    "event_id": "$b:example.org",
    "depth": 7,
    "rejected": false,
    "prev_events_hash": "sha256:abc"
  },
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
    "event_root": "734aaf66da440dfbbe445bfe7874014983beafe7682b456f40973f7e8e0a2e4d",
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
field_root_hex = 08e7c748acbe75a855a5c1420ea3d5948a765509f27d132796bfbaecbe8c3fae
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

`field_root_hex`, `prev_events_hash_hex`, `auth_events_hash_hex`,
`content_hash_hex`, and `other_signed_fields_hash_hex` are unchanged from the
pre-split vectors, since none of those inputs reference `sender`.
`event_header_root_hex`, `event_root_hex`, `event_id`, and the signature values
change because `event_header_root` now commits `sender_localpart` and
`sender_domain` as separate leaves instead of a single `sender` leaf.

### Cryptographic proof responses

When `proof` is requested in `fields` and the queried room version supports
split canonicalization, a server MAY include proof material for provable
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

The following side-by-side example DAG shows the short-circuiting opportunity.
The left side illustrates the legacy fetch-and-verify path over the chain. The
right side shows the same DAG shape when Merkle proofs let the verifier stop
after proving the branch back to a trusted anchor:

```text
Legacy: fetch-and-verify each hop         Merkleized: prove branch, stop early

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
  verify hashes + auth rules              if root matches, stop here
  repeat for every ancestor               no need to fetch every ancestor
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
are not required for this endpoint's current gap-repair workflow. A requester
must still fetch the full PDU before accepting an event, because it needs
`content`, `auth_events`, event hashes, signatures, auth rules, and
state-resolution inputs. A proof of `prev_events` therefore adds proof bytes and
verification work without removing the eventual full-event fetch. The remaining
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

## Future extensions

Future room versions may extend the proof fields or add more independently
provable metadata fields.

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
