# MSC4511A: Bounded Topology and State Queries

Federation repair currently requires fetching full events before a server can
learn the shape of a gap in the DAG. Likewise,
`GET /_matrix/client/v3/rooms/{roomId}/state` returns the complete state
dictionary even when a client needs only selected event types or state keys.

This proposal unifies both surfaces under a single formal **bounded-closure
query primitive**. Homeservers can execute bounded graph traversals and return
sparse metadata hints and routing advice for federation repair, while client
state filtering is evaluated as the depth-zero point-query instance of the same
operator.

For room versions 3 and later, metadata returned over federation remains a hint
that must be verified by fetching full events. This is the intended security
model, not a fallback: gap repair requires the full PDU to validate `content`,
event hashes, signatures, auth rules, and state resolution. The query response
helps servers prioritize which event IDs and peers to try next, while history
repair continues to use the existing verified-event path.

For a room version which defines State DAGs as in MSC4242, `prev_state_events`
is an additional supported edge type. It is traversed with the same depth,
record, and visited-node limits as `prev_events` and `auth_events`. A requester
MUST NOT infer State DAG edges from `auth_events` or `prev_events`, and a server
MUST reject `prev_state_events` with `M_UNSUPPORTED_ROOM_VERSION` when the room
version does not define that relation. State-DAG traversal remains a hint-only
query; it does not replace state resolution or authorize accepting an event.

## Proposal

### The query form

Both the federation topology query and client state filtering are instances of a
single bounded-closure query defined as a 5-tuple $(S, R, b, \phi, \pi)$:

- $S$ — **Seed selector**: an initial set of events in the room, named
  explicitly by event ID or resolved from current room state.
- $R$ — **Edge relations**: a subset of edge types to traverse (`prev_events`,
  `auth_events`, `relates_to`, `redacts`, `prev_state_events`).
- $b$ — **Bound vector**: literal resource and recursion limits (`depth`,
  `records`, `nodes`, `candidate_servers`, `compute_pairs`, `common_ancestors`).
- $\phi$ — **Node predicate (`select`)**: a boolean filter over event properties
  (`types`, `state_keys`) that gates event emission.
- $\pi$ — **Projection**: the output representation mode (`"fields"` for dense
  positional matrices, or `"events"` for full event objects).

#### Traversal semantics

Query evaluation is described by the following bounded frontier fixpoint. The
normative queue, authorization, and accounting rules below determine which
members enter a frontier when a bound is reached:

<!-- markdownlint-disable MD013 -->

$$
V_0 = S
$$

$$
V_{k+1} = V_k \cup \{ t : s \in V_k, (s, t) \in R \} \quad \text{for } k < b.\text{depth}, \, |V_k| < b.\text{nodes}
$$

$$
\text{result} = \pi(\sigma_\phi(V))
$$

Under this algebra:

- The **Federation Topology Profile** is
  $(S_{\text{event\_ids}}, \{\text{prev\_events}, \text{auth\_events}\}, b, \top, \pi_{\text{fields}})$.
- The **Client State Profile** is
  $(S_{\text{state}}, R = \emptyset, b.\text{depth} = 0, \phi_{\text{types} \land \text{state\_keys}}, \pi_{\text{events}})$.

<!-- markdownlint-enable MD013 -->

When the relation set $R$ is empty ($\emptyset$), traversal collapses to depth
0, and the evaluator executes a direct filtered point query without special
casing.

#### Termination and cost bounds

The query form guarantees deterministic termination and bounded protocol-level
work through three invariants. These limits do not promise a uniform bound on
database latency or parsing cost for attacker-controlled event data; servers
retain independent physical response-size and time limits.

1. **Syntactic bound**: Every iteration construct's trip count is a literal
   integer in the request or a server-configured constant, never a value derived
   from queried event data.
2. **Budget monotonicity**: Every visited node and every inspected edge
   reference draws from a non-resettable work budget. `limits.nodes` bounds
   nodes; servers MUST additionally enforce a configured edge-reference budget
   and response-size budget.
3. **Truncation totality**: Every operation has a well-defined result when the
   work budget is exhausted, and truncation is unambiguously signaled to the
   requester via `limited: true`.

#### JSON schema

The canonical request body adheres to the following JSON schema:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["room_id", "seed"],
  "properties": {
    "room_id": { "type": "string" },
    "seed": {
      "type": "object",
      "oneOf": [
        {
          "required": ["event_ids"],
          "properties": {
            "event_ids": {
              "type": "array",
              "items": { "type": "string" },
              "minItems": 1
            }
          },
          "additionalProperties": false
        },
        {
          "required": ["state"],
          "properties": {
            "state": {
              "type": "object",
              "properties": {
                "types": {
                  "type": "array",
                  "items": { "type": "string" },
                  "minItems": 1
                },
                "state_keys": {
                  "type": "array",
                  "items": { "type": "string" },
                  "minItems": 1
                }
              },
              "additionalProperties": false
            }
          },
          "additionalProperties": false
        }
      ]
    },
    "edge_types": {
      "type": "array",
      "items": {
        "type": "string",
        "enum": [
          "prev_events",
          "auth_events",
          "relates_to",
          "redacts",
          "prev_state_events"
        ]
      },
      "uniqueItems": true
    },
    "relates_to_types": {
      "type": "array",
      "items": { "type": "string" },
      "minItems": 1,
      "uniqueItems": true
    },
    "select": {
      "type": "object",
      "properties": {
        "types": {
          "type": "array",
          "items": { "type": "string" },
          "minItems": 1
        },
        "state_keys": {
          "type": "array",
          "items": { "type": "string" },
          "minItems": 1
        }
      },
      "additionalProperties": false
    },
    "limits": {
      "type": "object",
      "properties": {
        "depth": { "type": "integer", "minimum": 0 },
        "records": { "type": "integer", "minimum": 1 },
        "nodes": { "type": "integer", "minimum": 1 },
        "candidate_servers": { "type": "integer", "minimum": 1 },
        "compute_pairs": { "type": "integer", "minimum": 1 },
        "common_ancestors": { "type": "integer", "minimum": 1 }
      },
      "additionalProperties": false
    },
    "projection": {
      "type": "string",
      "enum": ["fields", "events"]
    },
    "fields": {
      "type": "array",
      "items": { "type": "string" },
      "uniqueItems": true,
      "contains": { "const": "event_id" }
    },
    "include": {
      "type": "array",
      "items": {
        "type": "string",
        "enum": ["edge_errors", "start_event_errors", "proofs"]
      },
      "uniqueItems": true
    },
    "compute": {
      "type": "array",
      "items": {
        "type": "string",
        "enum": ["common_ancestor", "hop_distance"]
      },
      "minItems": 1,
      "uniqueItems": true
    },
    "compute_event_pairs": {
      "type": "array",
      "items": {
        "type": "array",
        "items": { "type": "string" },
        "minItems": 2,
        "maxItems": 2
      },
      "minItems": 1
    }
  },
  "additionalProperties": true
}
```

The successful (`200 OK`) response body adheres to the following envelope
schema. Standard Matrix error responses are defined separately. JSON Schema
cannot express the cross-field requirement that every positional row has the
same length and ordering as `event_fields`; that requirement is normative in the
projection rules below.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["events"],
  "properties": {
    "event_fields": {
      "type": "array",
      "items": { "type": "string" },
      "uniqueItems": true,
      "contains": { "const": "event_id" }
    },
    "events": {
      "type": "array",
      "anyOf": [
        { "items": { "type": "array", "items": {} } },
        { "items": { "type": "object" } }
      ]
    },
    "edge_errors": {
      "type": "object",
      "additionalProperties": { "type": "object" }
    },
    "start_event_errors": {
      "type": "object",
      "additionalProperties": { "type": "string" }
    },
    "proofs": {
      "type": "object",
      "additionalProperties": { "type": "object" }
    },
    "computed": { "type": "object" },
    "limited": { "type": "boolean" }
  },
  "additionalProperties": false
}
```

---

### Query specification and semantics

#### Seed selector (`seed`)

The starting event set $S$ is specified via the tagged union `seed`. Exactly one
member MUST be present:

- `"seed": { "event_ids": ["$event_id_1", "$event_id_2", ...] }` — Explicit
  starting event IDs.
- `"seed": { "state": { "types": [...], "state_keys": [...] } }` — Resolves the
  seed from current room state matching the given `types` and `state_keys`.

A request containing both `event_ids` and `state`, or omitting `seed` entirely,
MUST be rejected with `M_INVALID_PARAM`.

Empty arrays in `seed.state` or `select` MUST be rejected with
`M_INVALID_PARAM`.

In v1 of this specification, `state` seed resolution is strictly restricted to
the **current room state**. Requesters MUST NOT specify historical state anchors
(e.g., `at: {event_id}`). State resolution at arbitrary historical events
requires on-demand state reconstruction and auth-chain traversal whose
computational cost is not a function of the request's declared literals.
Excluding historical state anchors preserves the fundamental invariant that all
traversal work is bounded statically by $b$.

#### Edge relations (`edge_types`)

The `edge_types` list defines the edge relations $R$ to follow during traversal:

- `prev_events`: follows directed DAG predecessor edges.
- `auth_events`: follows authentication predecessor edges.
- `relates_to`: follows the target event referenced in `m.relates_to`. When
  `relates_to_types` is present, only relations whose `rel_type` is in that
  non-empty list are followed; it does not change the reported `relates_to`
  value.
- `redacts`: follows the target event referenced in the top-level `redacts`
  property of `m.room.redaction` events.
- `prev_state_events`: follows State DAG predecessor edges in room versions
  defining MSC4242. If requested in a room version that does not define State
  DAGs, the server MUST reject the request with `M_UNSUPPORTED_ROOM_VERSION`.

Unrecognized edge types MUST cause the request to fail with `M_INVALID_PARAM` to
prevent silent semantic divergence.

#### Node predicate (`select`)

The `select` object specifies the emission filter $\phi$:

- `types`: array of event type patterns. A trailing `*` acts as a prefix match
  (e.g., `uk.half-shot.hookshot.*`); `*` matches all event types. Trailing `*`
  is always interpreted as a wildcard operator with no escape syntax; event
  types literally ending in `*` are unrepresentable. An interior `*` (for
  example, `m.*.foo`) is literal and has no wildcard semantics.
- `state_keys`: array of exact state keys. State keys are always exact matches.

Predicates within each array are ORed; clauses across different fields are
ANDed. An omitted field or empty `{}` matches all visited events. Arrays in
`select` represent mathematical sets; duplicate entries are harmless and
ignored.

`in_past_of` is deliberately not part of v1. Its negative result requires a
second, potentially incomplete ancestry evaluation. A room version adopting
MSC4511C MAY define a separate proof request for that operation.

**Emission-only semantics:** The `select` predicate acts strictly as an
**emission filter**, not a traversal gate. A node in $V$ that does not satisfy
$\phi$ is still visited, still charged against the non-resettable `limits.nodes`
budget, and still expanded along $R$. Reachability semantics are invariant to
the projection filter, guaranteeing that different requesters querying the same
graph structure receive consistent, comparable closures.

#### Projection modes (`projection` and `fields`)

The `projection` field selects the output encoding:

1. `"fields"` (default for federation topology queries): Returns dense
   positional records in `events`, aligned with the `event_fields` header array.
2. `"events"` (default for client state queries): Returns an array of full
   canonical event objects in `events`.

When `projection` is `"fields"`, `fields` specifies the exact dense fields to
project. The `fields` list MUST contain `event_id` and MUST NOT contain
duplicate field names. Duplicates in `fields` MUST be rejected with
`M_INVALID_PARAM` before traversal because positional decoding in `events`
requires 1:1 alignment with `event_fields`.

When `projection` is `"events"`, the request MUST omit `fields` and the response
MUST omit `event_fields`. The returned `events` array MUST be strictly
homogeneous: it contains only positional arrays for `"fields"` projection or
only event objects for `"events"` projection. A request that includes `fields`
with `"events"` projection MUST be rejected with `M_INVALID_PARAM`, and a mixed
array MUST NOT be emitted.

Dense fields available for projection include:

- `event_id`: the event identifier (always required in `fields`).
- `prev_events`: list of previous-event edge IDs.
- `auth_events`: list of auth-event edge IDs.
- `prev_state_events`: list of State DAG edge IDs (MSC4242 room versions only).
- `relates_to`: relation target object `{"event_id": "...", "rel_type": "..."}`.
- `redacts`: target event ID redacted by this event.
- `sender`: full MXID of the event sender.
- `sender_localpart`: localpart portion of `sender`, split at the first `:`.
- `sender_domain`: server domain portion of `sender`, split at the first `:`
  character. For example, `@alice:example.org:8448` yields `example.org:8448`. A
  server MUST NOT split at the last `:`. If `sender` is not a valid user ID,
  both derived fields MUST be `null`; whenever `sender` and either derived field
  are returned, the value MUST agree with this first-boundary split.
- `type`: event type string.
- `state_key`: event state key string (if a state event).
- `candidate_servers`: list of server names recommended for routing/repair
  (advisory hints).
- `origin_server_ts`: timestamp of event origin.
- `depth`: integer depth of the event.
- `rejected`: boolean indicating local rejection status (server hint).
- `soft_failed`: boolean indicating local soft-fail status (server hint).
- `hop_count`: non-negative integer. The minimum number of selected edges on the
  first BFS path from any seed to this event, as observed during the responder's
  traversal; seed events have `hop_count` `0`. This is a traversal observation
  over the responder's available graph, not a proof of globally shortest-path
  distance through history the responder could not inspect. `hop_count` MUST NOT
  be requested with `seed.state`; servers MUST reject such requests with
  `M_INVALID_PARAM`. Because `select` is an emission filter and not a traversal
  gate, `hop_count` reflects distance to the event itself regardless of whether
  intermediate events were emitted.

`room_id` is deliberately omitted from dense `fields`: since queries are scoped
to a single `room_id`, emitting it in every row is redundant dead weight.

If a server does not know a dense field, does not store it efficiently, or
declines to disclose it, it emits `null` in that position. Every event row MUST
have exactly the length and field order of `event_fields`. Diagnostic sidecars
(`edge_errors`, `start_event_errors`, `proofs`) are not dense fields: they are
returned only when named in `include`, as maps keyed by event ID. A server MUST
NOT repeat a requested dense field in `event_fields`. Dense fields unavailable
for every returned event MAY be omitted from `event_fields` except `event_id`.
Requesters MUST ignore unrecognized field names while preserving positional
alignment. A room version or extension MAY define additional queryable fields;
its request schema extends the v1 core schema, so the core schema's open top
level does not make unnamespaced members part of this specification.

#### Derived graph facts (`compute`)

`compute` requests optional derived facts over labelled bounded closures.
Support is advertised with the `tk.nutra.msc4511.computed_graph_queries`
capability. A server which does not advertise it MUST reject a request
containing `compute` or `compute_event_pairs` with `M_UNRECOGNIZED`.

`compute` and `compute_event_pairs` MUST either both be present or both be
absent. Each pair has exactly two event IDs. The first ID of each pair is an
implicit explicit seed for that pair's labelled sub-closure; it is not required
to occur in `seed.event_ids`. Pair closures use the selected edge types and the
same authorization, room-boundary, depth, node, time, and response budgets as
the raw closure. The request-wide node budget is never reset between raw
traversal or pairs.

Pairs MUST be processed in request order. `computed` maps each requested fact
name to an array aligned with `compute_event_pairs`. If a budget is exhausted,
the affected result and every later affected result are `null` and `limited` is
`true`. If either member is unknown, wrong-room, or invisible, its result is
`null`; this alone does not set `limited`.

- `common_ancestor` returns the maximal common ancestors of the two pair
  members. Results are ordered `(depth descending, event_id ascending)`, with
  unknown-depth events after known-depth events. Results exceeding
  `limits.common_ancestors` are truncated in that order and set `limited`. If a
  bounded maximal set cannot be determined, the result is `null`, never a
  partial set.
- `hop_distance` returns the shortest directed distance from the first member to
  the second through selected edges, or `null` if none is found within the
  effective depth. It is also `null` with `limited: true` when the answer cannot
  be determined before budget exhaustion.

Derived facts are hints and MUST NOT be treated as proof of an authenticated
relationship without fetching and validating the relevant events.

#### Limits and cost model (`limits`)

Resource limits are consolidated in the `limits` object:

```json
"limits": {
  "depth": 50,
  "records": 1000,
  "nodes": 5000,
  "candidate_servers": 5
}
```

The unified cost model establishes:

- `nodes`: the **single non-resettable work currency** shared across graph
  traversal and seed resolution.
- `records`: terminal cap on the number of emitted event records.
- `depth`: syntactic recursion bound on frontier expansion.
- `bytes` / `ms`: server-enforced physical response size and wall-clock time
  caps.

<!-- markdownlint-disable MD013 -->

| Limit               | Client Profile Default | Federation Profile Default | Cost Model Role                   |
| :------------------ | :--------------------- | :------------------------- | :-------------------------------- |
| `depth`             | `0` (fixed)            | `500`                      | Syntactic recursion bound         |
| `records`           | `1000`                 | `1000`                     | Terminal emission cap             |
| `nodes`             | `1000`                 | `5000`                     | Shared non-resettable work budget |
| `candidate_servers` | Rejected               | `10`                       | Routing hint disclosure cap       |
| `compute_pairs`     | Rejected               | `20`                       | Derived-fact pair cap             |
| `common_ancestors`  | Rejected               | `20`                       | Per-pair ancestor-result cap      |

<!-- markdownlint-enable MD013 -->

Responding servers MUST enforce local maxima for seed event count, depth,
records, nodes, compute pairs, common ancestors, candidate servers, response
body size, processing time, and request rate per origin. The effective request
limit is clamped to the configured maximum; there is no unlimited syntax. A
request with more seed IDs than the effective maximum MUST fail with
`M_INVALID_PARAM` before lookup or traversal. `depth` MAY be `0`; every other
requestable limit MUST be at least `1`. `bytes` and `ms` are server-enforced,
not request members. Clamping alone does not set `limited`; it is `true` when a
limit, timeout, or unreported omission truncates work or output.

Implementations SHOULD use conservative defaults no higher than 20 seed IDs,
depth 500, 1000 records, 5000 nodes, 20 compute pairs, 20 common ancestors, 10
candidate servers per event, a 1 MiB response body, and 3 seconds of processing
time. Implementations MAY use lower defaults and stricter per-origin rate
limits.

The public Client State Profile filter accepts only `types` and `state_keys`.
`include`, `fields`, `edge_types`, `relates_to_types`, `projection`, `compute`,
and `compute_event_pairs` are invalid in that profile and MUST be rejected with
`M_INVALID_PARAM` if present. Client-controlled `limits` are likewise not part
of the profile: depth is fixed at zero by the rewrite, and record and work
limits are server-owned.

---

### Federation Topology Profile

The federation profile exposes the bounded topology query over federation to
assist homeservers in DAG repair and backfill path selection.

#### Federation endpoint

```http
POST /_matrix/federation/unstable/tk.nutra.msc4511/topology_query
```

#### Federation capability discovery

Servers advertise support in `GET /_matrix/federation/v1/version` under
`unstable_features`:

```json
{
  "unstable_features": {
    "tk.nutra.msc4511.topology_query": true,
    "tk.nutra.msc4511.computed_graph_queries": true
  }
}
```

#### Request example

```json
{
  "room_id": "!room:example.org",
  "seed": {
    "event_ids": ["$missing_event_A", "$missing_event_B"]
  },
  "edge_types": ["prev_events"],
  "select": {
    "types": ["*"]
  },
  "limits": {
    "depth": 50,
    "records": 1000,
    "nodes": 5000,
    "candidate_servers": 5
  },
  "fields": [
    "event_id",
    "prev_events",
    "sender",
    "type",
    "hop_count",
    "candidate_servers"
  ],
  "include": ["edge_errors"]
}
```

#### Response example

```json
{
  "event_fields": [
    "event_id",
    "prev_events",
    "sender",
    "type",
    "hop_count",
    "candidate_servers"
  ],
  "events": [
    [
      "$missing_event_A",
      ["$prev_1", "$prev_2"],
      "@alice:example.org",
      "m.room.message",
      0,
      ["example.org"]
    ],
    [
      "$missing_event_B",
      ["$prev_1"],
      "@bob:elsewhere.example",
      "m.room.member",
      0,
      ["elsewhere.example", "example.net"]
    ],
    [
      "$prev_1",
      ["$prev_0"],
      "@alice:example.org",
      "m.room.message",
      1,
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
  "limited": true
}
```

#### Traversal, authorization, and edge errors

Traversal is breadth-first. A server MUST maintain a visited set and MUST visit
an event ID at most once. Each visited event consumes exactly one unit of
`limits.nodes`, whether or not it passes `select`. Each inspected edge reference
consumes one unit of the server's edge-reference budget. When only some targets
fit in the remaining budget, the server MUST retain the first targets in the
ordering below and report the others as `truncated` if `edge_errors` was
requested.

Within the same recursion depth, servers MUST order events by
`(depth descending, event_id ascending)` where depth is known, with
unknown-depth events ordered after known-depth events by event ID ascending.

Every seed and every discovered target MUST be checked against the requested
`room_id` before it is queued, emitted, or expanded. Federation visibility is
evaluated according to the room's history-visibility rules at the event being
served. If the server cannot reconstruct the applicable historical state, it
MUST omit the event unless it can establish that the current policy is equally
or more restrictive. A request with no federation authorization to access the
room MUST fail with `M_FORBIDDEN`.

Malformed seed event IDs MUST fail with `M_INVALID_PARAM`. Unknown, wrong-room,
or invisible seeds and targets are omitted. If the applicable requested sidecar
does not report that omission, the response MUST set `limited: true`. Hidden
branches MUST NOT be replaced with opaque markers: those markers leak graph
shape. This room-boundary rule mirrors the cross-room `auth_events` protection
of [MSC4307](https://github.com/matrix-org/matrix-spec-proposals/pull/4307).

Edge targets that cannot be traversed are reported in `edge_errors`:

- `wrong_room`: the target event belongs to a different room. The server MUST
  NOT follow the edge or disclose data from the target room. This code MAY be
  returned only when the requester is authorized to learn that target under the
  target room's history-visibility rules; otherwise the server MUST return
  `not_available`.
- `not_available`: the target appears to be an in-room event, but the responding
  server will not serve it to this requester (conflating absence with access
  restrictions to prevent leaking history visibility).
- `truncated`: the edge target was not inspected because a limit or work budget
  was reached.

#### Federation authorization and trust model

Responses over federation are strictly **hints**. Requesters MUST fetch and
validate full PDUs (content hashes, auth rules, signatures, state resolution)
before accepting events into room history.

`candidate_servers` represents the responder's local routing beliefs and MUST
NOT be exposed to clients.

Candidate servers MAY be derived from the sender domain, a server which sent the
event, peers which answered for nearby events, or servers known to participate
around the event's depth. They MUST be limited by `limits.candidate_servers`,
ordered by descending local confidence, and omitted where disclosure would
reveal private membership or history information. They are advisory routing
beliefs, not PDU metadata. A joined server may receive visible history; an
invited server only invite-legal material; a non-joined server receives no
private room metadata. World-readable history is still subject to the requested
field set and the event-time visibility rule above.

#### Room versions

This endpoint applies to room versions 3 and later only as a hint surface. Room
versions 1 and 2 MUST be rejected with `M_UNSUPPORTED_ROOM_VERSION`: their
hashed predecessor references cannot be represented losslessly as this
endpoint's bare event-ID edges. For room versions 3 and later, requesters MUST
fetch and validate full PDUs before accepting history.

---

### Client State Profile

To prevent transferring unwanted room state, clients can query room state with
fine-grained type and state-key filters.

#### Client endpoint

```http
GET /_matrix/client/v3/rooms/{roomId}/state?filter={url-encoded-json}
```

#### Client capability discovery

```json
{
  "unstable_features": {
    "tk.nutra.msc4511.client_state_filter": true
  }
}
```

#### Semantics as a query rewrite

The client state filter is specified as an exact rewrite into the unified query
form:

```json
{
  "room_id": "!room:example.org",
  "seed": {
    "state": {
      "types": ["m.room.name", "uk.half-shot.hookshot.github.*"],
      "state_keys": ["", "@alice:example.org"]
    }
  },
  "edge_types": [],
  "limits": {
    "depth": 0
  },
  "projection": "events"
}
```

This MSC chooses the GET-only client surface. The Client State Profile exposes
exactly this depth-zero, current-state, full-event instance of the query form;
the general request object is federation-only in v1. The `filter` object's
`types` and `state_keys` use the same predicate grammar and parser as
`seed.state`: they select which current-state entries enter $V_0$, whereas
`select` is a federation emission filter over all of $V$. A client profile
implementation MUST reject an unadvertised endpoint with `M_UNRECOGNIZED`, an
invalid filter with `M_INVALID_PARAM`, preserve the usual state response order,
return `[]` with `200 OK` when no current state matches, and preserve ordinary
client authorization and history-visibility behaviour.

The Client State Profile is not subject to the `records` terminal emission cap:
the existing endpoint has neither a `limited` member nor pagination. It MUST
return every current-state event that matches the filter and is visible to the
requester. The response is the standard JSON array of canonical state event
objects. Clients receive authoritative state directly from their own homeserver;
no Merkle proof verification is required for C2S flows.

#### Consuming example: Issue #2019 state filtering

[Issue #2019: Ability to specify a filter for `/_matrix/client/v3/rooms/{roomId}/state`](https://github.com/matrix-org/matrix-spec/issues/2019)
is a direct consumer of this profile. An integration such as Hookshot, which
needs selected application state but does not run `/sync`, can request only the
event types and state keys it understands instead of downloading the entire room
state and discarding most of it. A follow-on C2S MSC resolving that issue can
adopt the depth-zero rewrite above while retaining the standard `/state`
response shape; it need not expose federation traversal, sidecars, or the
general query grammar to clients.

---

### Future extensions & deferred features

#### Deferral of predicate-gated traversal (`traverse`)

Predicate-gated expansion (`traverse`) is deferred from v1. Pruning edges
changes the reachable closure and can make ancestry and history-visibility
semantics ambiguous. `select` is therefore an emission filter only.

#### Forward recursive queries

Future extensions may define forward recursion over inverse predecessor edges,
but must specify indexing, ordering, and visit bounds.

#### Cacheability

Event IDs, predecessor edges, depth, and sender-derived fields are immutable
once the event is known. Authorization-equivalent sparse answers containing only
those fields MAY be cached. Responder-local fields such as `rejected`,
`soft_failed`, and `candidate_servers` SHOULD be cached conservatively.
Requesters SHOULD negatively cache a peer's unsupported
`tk.nutra.msc4511.topology_query` capability for no longer than 24 hours.

---

## Relationship to other proposals

- [MSC4242: State DAGs](https://github.com/matrix-org/matrix-spec-proposals/pull/4242):
  MSC4511A natively traverses `prev_state_events` edges when supported by the
  room version.
- [MSC4511C: Verifiable Room State and Event Metadata](4511-c-merkleized-room-version-upgrade.md):
  Completes MSC4511A's algebra by materializing the unbounded ancestor closure
  $C(E)$ into authenticated causal tries, allowing a future proof extension to
  answer causal-membership queries cryptographically.
- [MSC4000: Forwards fill](https://github.com/matrix-org/matrix-spec-proposals/pull/4000)
  &
  [MSC4370: Current extremities endpoint](https://github.com/matrix-org/matrix-spec-proposals/pull/4370):
  MSC4511A provides general DAG traversal rather than frontier-only slices.
  Forward extremities are not returned because they describe a server's local
  view of the DAG boundary, not metadata committed to an individual event.
- [MSC2695: Get event by ID over federation](https://github.com/matrix-org/matrix-spec-proposals/pull/2695):
  MSC4511A identifies which missing PDUs to fetch via MSC2695.
- [Matrix Spec Issue #2019](https://github.com/matrix-org/matrix-spec/issues/2019):
  A motivating consumer of the Client State Profile.

- [MSC2316: Federation queries to aid with database recovery](https://github.com/matrix-org/matrix-spec-proposals/pull/2316)
  and
  [MSC2391: Efficient point-queries for room state over federation](https://github.com/matrix-org/matrix-spec-proposals/pull/2391):
  MSC2316 is a recovery protocol and MSC2391 is a state point-query; neither
  provides this endpoint's bounded recursive sparse DAG query.

None of these proposals combines arbitrary known start points, selectable DAG
edges, bounded traversal, sparse per-event metadata, and routing hints. This is
the pull primitive they can compose with, not a replacement for their higher
level repair or state-transfer flows.

---

## Security considerations

1. **Resource exhaustion**: Mitigated by strict syntactic bounds, monotonic
   `limits.nodes` currencies, and server-clamped execution budgets.
2. **Metadata disclosure**: Conflating local absence with permission denials via
   `not_available` prevents leaking history visibility boundaries.
3. **Misdirection resistance**: Requesting homeservers track hint accuracy
   against verified PDUs and temporarily rate-limit peers that return false
   topology metadata. Such local penalties MUST expire after a bounded interval
   and MUST NOT cause rejection of an event which passes normal Matrix
   verification and authorization.

---

## Unstable prefix

<!-- markdownlint-disable MD013 -->

| Surface                 | Unstable identifier                                            |
| :---------------------- | :------------------------------------------------------------- |
| Federation endpoint     | `/_matrix/federation/unstable/tk.nutra.msc4511/topology_query` |
| Federation capability   | `tk.nutra.msc4511.topology_query`                              |
| Derived-fact capability | `tk.nutra.msc4511.computed_graph_queries`                      |
| Client capability       | `tk.nutra.msc4511.client_state_filter`                         |

<!-- markdownlint-enable MD013 -->

## References

- [Matrix Server-Server API](https://spec.matrix.org/latest/server-server-api/)
- [Matrix Client-Server API](https://spec.matrix.org/latest/client-server-api/)
- [Matrix Room Version 12](https://spec.matrix.org/latest/rooms/v12/)
- [MSC4242: State DAGs](https://github.com/matrix-org/matrix-spec-proposals/pull/4242)
- [MSC4511C: Merkleized Room Version Upgrade](4511-c-merkleized-room-version-upgrade.md)
