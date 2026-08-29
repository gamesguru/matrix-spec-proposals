# MSC4511A: Bounded Topology and State Queries

Currently the Matrix protocol relies on fetching entire events to perform
backfills or otherwise retrieve previous or missing events. Often we do not know
the shape of the graph we are traversing, whether it is a dead end, or whether
two branches reconnect at a known common ancestor. When a server encounters a
gap in the DAG, the current federation API provides limited ways to discover
which events reference the gap, which servers sent or received them, or which
servers are otherwise likely to have the missing event before fetching full
events.

Similarly, on the Client-Server API, clients fetching room state via
`GET /_matrix/client/v3/rooms/{roomId}/state` must retrieve the entire room
state dictionary even when they only require a few specific state event types
(such as `m.room.name` or specific application-scoped state).

This proposal unifies both surfaces under a single formal **bounded-closure
query primitive**. Homeservers can execute bounded graph traversals and return
sparse metadata hints, routing advice, and computed graph facts for federation
repair, while client state filtering is evaluated as the depth-zero point query
instance of the same operator.

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
  `records`, `nodes`, `compute_pairs`, `common_ancestors`, `candidate_servers`).
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
        "compute_pairs": { "type": "integer", "minimum": 1 },
        "common_ancestors": { "type": "integer", "minimum": 1 },
        "candidate_servers": { "type": "integer", "minimum": 1 }
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
      "uniqueItems": true
    },
    "compute_event_pairs": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "array",
        "items": { "type": "string" },
        "minItems": 2,
        "maxItems": 2
      }
    }
  },
  "additionalProperties": false
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
      "items": {
        "oneOf": [{ "type": "array", "items": {} }, { "type": "object" }]
      }
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
- `relates_to`: follows the target event referenced in `m.relates_to`.
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
- `sender_domain`: server domain portion of `sender`, split at the first `:`
  character. For example, `@alice:example.org:8448` yields `example.org:8448`.
  If `sender` is not a valid user ID, both `sender` and `sender_domain` MUST be
  `null`.
- `type`: event type string.
- `state_key`: event state key string (if a state event).
- `candidate_servers`: list of server names recommended for routing/repair
  (advisory hints).
- `origin_server_ts`: timestamp of event origin.
- `depth`: integer depth of the event.
- `rejected`: boolean indicating local rejection status (server hint).
- `soft_failed`: boolean indicating local soft-fail status (server hint).
- `content`: full canonical event content dictionary. **Allowed in Client
  Profile only; strictly forbidden in Federation Profile.**

`room_id` is deliberately omitted from dense `fields`: since queries are scoped
to a single `room_id`, emitting it in every row is redundant dead weight.

If a server does not know a dense field, does not store it efficiently, or
declines to disclose it, it emits `null` in that position. Every event row MUST
have exactly the length and field order of `event_fields`. Diagnostic sidecars
(`edge_errors`, `start_event_errors`, `proofs`) are not dense fields: they are
returned only when named in `include`, as maps keyed by event ID. A server MUST
NOT repeat a requested dense field in `event_fields`.

#### Folded compute layer (`compute`)

Computed graph facts (`common_ancestor`, `hop_distance`) are not separate
algorithmic subroutines; they are derived facts over labeled closures:

- `hop_distance(a, b)`: the depth-of-first-visit of $b$ in the closure seeded at
  $\{a\}$ under edge relation set $R$.
- `common_ancestor(a, b)`: the maximal elements of $V(a) \cap V(b)$ under the
  closure's edge relation set $R$. An ancestor $u \in V(a) \cap V(b)$ is maximal
  if there is no $v \in V(a) \cap V(b)$ such that $v$ reaches $u$ along $R$
  (antichain reduction).

`compute_event_pairs` specifies the pairs $[a, b]$ to evaluate. If `compute` is
present, `compute_event_pairs` MUST be present and non-empty. Malformed pairs
cause the request to fail with `M_INVALID_PARAM`.

`common_ancestor` is defined only when `edge_types` is a non-empty subset of
`prev_events`, `auth_events`, and `prev_state_events`. A request that combines
it with `relates_to` or `redacts` MUST be rejected with `M_INVALID_PARAM`: those
links are navigable references, not ancestry relations.

Compute evaluations draw directly from the request's shared `limits.nodes`
currency. If the node budget is exhausted before maximality can be confirmed for
a `common_ancestor` pair, the server MUST return `null` for that pair and set
`limited: true`; it MUST NOT emit a partial non-maximal ancestor set.

If a server does not implement computed graph queries, it MUST reject requests
containing `compute` with `M_UNRECOGNIZED` (or the distinct code
`tk.nutra.msc4511.unsupported_compute`) rather than generic `M_INVALID_PARAM`,
allowing the requester to distinguish lack of implementation support from a
malformed query payload.

#### Limits and cost model (`limits`)

Resource limits are consolidated in the `limits` object:

```json
"limits": {
  "depth": 50,
  "records": 1000,
  "nodes": 5000,
  "compute_pairs": 10,
  "common_ancestors": 10,
  "candidate_servers": 5
}
```

The unified cost model establishes:

- `nodes`: the **single non-resettable work currency** shared across graph
  traversal, seed resolution, and all compute invocations.
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
| `compute_pairs`     | Rejected               | `20`                       | Cap on compute pair invocations   |
| `common_ancestors`  | Rejected               | `20`                       | Max maximal ancestors per pair    |
| `candidate_servers` | Rejected               | `10`                       | Routing hint disclosure cap       |

<!-- markdownlint-enable MD013 -->

Request limits are clamped to the server's configured maximum. If traversal or
compute is truncated by any limit or budget exhaustion, the response sets
`limited: true`.

The public Client State Profile filter accepts only `types` and `state_keys`.
`compute`, `compute_event_pairs`, `include`, `fields`, `edge_types`, and
`projection` are invalid in that profile and MUST be rejected with
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
    "compute_pairs": 10,
    "common_ancestors": 10,
    "candidate_servers": 5
  },
  "fields": ["event_id", "prev_events", "sender", "type", "candidate_servers"],
  "include": ["edge_errors"],
  "compute": ["common_ancestor", "hop_distance"],
  "compute_event_pairs": [["$missing_event_A", "$prev_1"]]
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

The `content` field is **strictly forbidden** in the Federation Profile. Serving
event bodies over `/topology_query` would collapse the endpoint into an
unauthenticated `/backfill` and destroy the bandwidth-saving hint architecture.
`candidate_servers` represents the responder's local routing beliefs and MUST
NOT be exposed to clients.

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

The response is the standard JSON array of canonical state event objects.
Clients receive authoritative state directly from their own homeserver; no
Merkle proof verification is required for C2S flows.

#### Consuming example: Issue #2019 state filtering

[Issue #2019: Ability to specify a filter for `/_matrix/client/v3/rooms/{roomId}/state`](https://github.com/matrix-org/matrix-spec/issues/2019)
is a direct consumer of this profile. An integration such as Hookshot, which
needs selected application state but does not run `/sync`, can request only the
event types and state keys it understands instead of downloading the entire room
state and discarding most of it. A follow-on C2S MSC resolving that issue can
adopt the depth-zero rewrite above while retaining the standard `/state`
response shape; it need not expose federation traversal, compute, sidecars, or
the general query grammar to clients.

---

### Future extensions & deferred features

#### Deferral of predicate-gated traversal (`traverse`)

Predicate-gated expansion (`traverse`) is intentionally deferred from v1.
Filtering which edges are traversed changes the reachable closure $V$, which
interacts destructively with `compute` (a common ancestor over a pruned graph is
not a true merge base) and overlaps with history-visibility pruning. By
restricting $\phi$ (`select`) to an emission filter over unpruned closures, v1
achieves complete unification of federation and C2S surfaces with zero traversal
ambiguity.

#### Forward recursive queries

Future extensions may define forward recursion (following inverse `prev_events`
or `auth_events` references) to aid witness discovery and cache hunting. Such
extensions must specify forward indexing structures, traversal ordering, and
hard visit budgets.

---

### Packaging and deployment strategy

The architectural unification and deployment vehicles are decoupled:

1. **Semantic Unification**: MSC4511A defines the comprehensive query primitive
   $(S, R, b, \phi, \pi)$ and shared evaluator semantics.
2. **Independent Shipping**: The Client State Profile can be shipped as an
   independent, lightweight C2S MSC referencing MSC4511A's grammar. Issue #2019
   is its motivating consumer example. This allows immediate client performance
   gains without coupling to federation traversal reviews or room-version
   upgrades.

---

### Performance characteristics and benchmarking

#### Asymptotic bandwidth analysis

For a traversal visiting $N$ events:

- Full-event retrieval transfers $O(N \cdot S_{\text{event}})$ bytes.
- Sparse topology queries transfer $O(N \cdot S_{\text{meta}})$ bytes.

With $S_{\text{event}} \approx 1\text{--}5\text{ KiB}$ and
$S_{\text{meta}} \approx 80\text{--}300\text{ bytes}$, bandwidth reductions of
70% to 98% are achieved for DAG exploration and merge-base discovery.

#### Storage and indexing

Homeservers answer queries from existing event stores supplemented by standard
indexes over `(room_id, event_id)`, `prev_events`, `auth_events`, `type`, and
`state_key`. No cryptographic tree generation or new storage formats are
required for MSC4511A.

---

### Relationship to other proposals

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
- [MSC2695: Get event by ID over federation](https://github.com/matrix-org/matrix-spec-proposals/pull/2695):
  MSC4511A identifies which missing PDUs to fetch via MSC2695.
- [Matrix Spec Issue #2019](https://github.com/matrix-org/matrix-spec/issues/2019):
  Directly satisfied by MSC4511A's Client State Profile.

---

### Security considerations

1. **Resource exhaustion**: Mitigated by strict syntactic bounds, monotonic
   `limits.nodes` currencies, and server-clamped execution budgets.
2. **Metadata disclosure**: Conflating local absence with permission denials via
   `not_available` prevents leaking history visibility boundaries.
3. **Misdirection resistance**: Requesting homeservers track hint accuracy
   against verified PDUs and temporarily rate-limit peers that return false
   topology metadata.

---

### References

- [Matrix Server-Server API](https://spec.matrix.org/latest/server-server-api/)
- [Matrix Client-Server API](https://spec.matrix.org/latest/client-server-api/)
- [Matrix Room Version 12](https://spec.matrix.org/latest/rooms/v12/)
- [MSC4242: State DAGs](https://github.com/matrix-org/matrix-spec-proposals/pull/4242)
- [MSC4511C: Merkleized Room Version Upgrade](4511-c-merkleized-room-version-upgrade.md)
