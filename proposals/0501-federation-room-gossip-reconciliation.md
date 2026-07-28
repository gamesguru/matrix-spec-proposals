# MSC0501: Efficient gossip-based room federation missed PDU reconciliation

Matrix federation today is "push and hope" — when event delivery fails or
servers miss or drop transactions, the DAG develops permanent holes. Each server
drops a different random subset of events, creating unpredictable gaps that no
existing endpoint can efficiently detect or repair.

When servers undergo extended downtime, they have trouble catching up after a
cold boot; no homeserver implementation "forward fills" a complete or orderly
DAG timeline.

This proposal introduces three lightweight endpoints allowing federated servers
to efficiently monitor for event set divergence and reconcile it, without
requiring full state map comparisons or new room versions.

**Companion documents.** The digest algebra — field, hash derivation, sketch
encoding, decoder contract, and capacity budgets — is specified separately in
MSC0500 (`algebraic_v1` digest profile). The design rationale, rejected
alternatives, and operational tuning guidance are in the MSC0501 architecture
note. This document specifies the protocol and its wire contract.

## Background

### The problem: federation gaps, silent data loss

Federation data loss occurs through several well-documented mechanisms:

1. Servers may never receive events due to DNS routing or other federation
   connection issues (sometimes due to deliberate _de_-federation).
2. Servers after prolonged downtime may be marked as permanently backed off.
   Unless they engage with a room and send an event, they may remain excised
   from any new activity.
3. Other transient bugs in state resolution or database logic can cause an event
   to mistakenly be soft-failed or otherwise skipped during traversal.

The result is that servers in the same room can have divergent views of the same
DAG, leading to membership differences, missing messages, and inconsistent state
(different inputs to state resolution in general produce different outputs).

### Why existing endpoints are insufficient

<!-- markdownlint-disable MD013 -->

| Endpoint                            | Limitation                                                                                                     |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `GET /backfill/{roomId}`            | Depth-ordered linear walk; cannot target specific gaps; useless for missing events in the middle of the DAG    |
| `POST /get_missing_events/{roomId}` | BFS walk with a lower depth limit; cannot bridge some gaps; requires knowing the boundary events               |
| `GET /state_ids/{roomId}`           | Returns state event IDs only (not timeline events); `O(S)` comparison; no incremental diffing                  |
| `GET /event/{eventId}`              | Single-event fetch; no bulk mode; requires knowing which events are missing                                    |
| `GET /make_join`                    | Does not meet latency requirements (20-50 ms); requests can timeout (full remote index scan for unknown event) |

<!-- markdownlint-enable MD013 -->

None of these endpoints quickly answer the question: **"Am I missing events in
this room, and if so, which ones?"**

### Design philosophy

This proposal adapts three mechanisms from the gossip protocol literature ([1],
[2]) to Matrix's federated DAG model:

1. **Anti-entropy room digest comparison** — `O(1)` divergence detection.
2. **Pull-based reconciliation** — the lagging server requests what it needs.
3. **Protocol-level idempotency** — repeated reconciliation is harmless.

The wire contract requires group-valued digests for subtractable, extensible
exchanges and downward-closed recovery sets for integration; see the
architecture note for the full argument.

Homeserver implementations maintain a single table or column family, tracking
the resident sketch and strata per room. These structures are computed only from
local data and are reused across peers; the strata projection is included in
each `room_digest` response, while no per-peer cache or remote knowledge is
required.

## Proposal

Three new federation endpoints are introduced under the
`/_matrix/federation/v1/` namespace.

### Capability discovery

Servers advertise support via `GET /_matrix/federation/v1/version`. Support is
advertised in `unstable_features` so that additional `digest_type` values or
endpoint revisions can be added without changing the stable version document.

```json
{
  "unstable_features": {
    "tk.nutra.msc0501.reconciliation": true,
    "algebraic_v1": true
  }
}
```

A server that receives HTTP 404 or 501 from a reconciliation endpoint MUST cache
that peer as unsupported for at least 24 hours and MUST NOT retry during that
period unless an operator explicitly overrides the cache.

### Room digest: `GET /_matrix/federation/v1/room_digest/{roomId}`

Returns a compact, opaque digest summarizing a server's knowledge of a room's
event set. Two servers can compare digests in `O(1)` to determine whether their
event sets have diverged (about 50 ms compute and 20 KB of bandwidth).

**Request:**

```http
GET /_matrix/federation/v1/room_digest/{roomId}
```

**Response:**

```json
{
  "digest": "<base64url_16_byte_accumulator>",
  "digest_type": "algebraic_v1",
  "known_event_count": 81247,
  "frame_id": "<base64url_32_byte_frame_id>",
  "strata": ["<base64url_64_byte_stratum_sketch>", "..."],
  "frame_event_ids": ["$join_anchor"],
  "extremity_event_ids": ["$abc123", "$def456"],
  "depth_range": [1, 93841],
  "origin_server_ts_range": [1609459200000, 1716000000000]
}
```

**Fields:**

<!-- markdownlint-disable MD013 -->

| Field                    | Type               | Required | Description                                                                                                                                |
| ------------------------ | ------------------ | -------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `digest`                 | string             | Yes      | Base64url-encoded 16-byte accumulator over the server's known event identifier set for this room and frame, per MSC0500.                   |
| `digest_type`            | string             | Yes      | The digest profile used. Servers MUST support `algebraic_v1`.                                                                              |
| `known_event_count`      | integer            | Yes      | The total number of event identifiers the server knows for this room and frame: accepted events plus rejected-event tombstones.            |
| `frame_id`               | string             | Yes      | Unpadded base64url identifier of the canonical frame anchor antichain. Requests MUST echo this value when using the digest.                |
| `strata`                 | [string]           | Yes      | The required 32-entry strata estimator, per MSC0500. Each entry is a base64url-encoded 64-byte sketch. (Efficiency/performance gain).      |
| `frame_event_ids`        | [string]           | Yes      | The frame anchor antichain bounding the history this digest covers. Servers MUST compare digests only when they understand the same frame. |
| `extremity_event_ids`    | [string]           | Yes      | The server's current forward extremities (DAG tips) for this room.                                                                         |
| `depth_range`            | [integer, integer] | No       | The minimum and maximum topological depth of events held.                                                                                  |
| `origin_server_ts_range` | [integer, integer] | No       | The earliest and latest `origin_server_ts` of events held.                                                                                 |

<!-- markdownlint-enable MD013 -->

#### The digested population

The digest covers the known event identifier set

$$
K = E_{\mathrm{accepted}} \cup E_{\mathrm{rejected}}.
$$

In addition to the accepted and rejected event sets, soft-failed events are also
in `K`; their soft-fail status specifically is not part of reconciliation.

<!-- Edit marker. -->

Given that population, `digest` and `known_event_count` are the level-0
accumulator and count defined in MSC0500, and `strata` is that profile's strata
estimator. MSC0501 requires the estimator on `room_digest`; other consumers of
MSC0500 MAY use it only when their wire contract includes it. This MSC adds no
arithmetic of its own.

**Rejected event handling.** Servers MUST include locally rejected event IDs as
tombstones in `K`. If rejected events were excluded, a fetch loop would occur:
Server B sees that Server A is "missing" an event, returns it in `/room_diff`,
Server A fetches it via `/room_events`, rejects it again, and the cycle repeats.

When `/room_diff` identifies event IDs missing on Server A, Server A requests
those events via `/room_events`. If Server B only holds a tombstone for a
requested event or if Server A fetches an event PDU and locally rejects it,
Server A MUST record and persist a tombstone entry carrying the event ID and
rejection reason in its local `E_{\mathrm{rejected}}` population (and thus `K`).
If Server A cannot persist a tombstone for a non-transferable rejected event, it
MUST exclude that event ID from `K`.

By including rejected event IDs in `K`, peers converge to `Δ = 0` even when they
disagree about acceptance. If two servers have the same known-event set but
different accepted-event sets, the problem is an authorization, room-version, or
implementation disagreement rather than a data-sync failure. Tombstones SHOULD
retain the event ID and rejection reason, not the full PDU. A tombstone MUST NOT
be garbage-collected while it belongs to a frame the server advertises as
available for reconciliation.

This MSC does not provide a verdict-diff endpoint for `K_rejected` or
`K_softfailed`. Equal digests mean equal known-event-ID sets, not equal
acceptance decisions: a server that accepted an event and a server that rejected
it with a retained tombstone both include the same ID in `K`, so that ID cancels
from the sketch. Comparing rejection or soft-failure status between servers is a
diagnostic API over per-event verdicts, not part of the mechanism defined here.

#### Frames

The digest covers a **frame**: an agreed antichain of event IDs bounding the
history being reconciled. Reconciliation repairs holes inside a frame. Backfill
extends the frame downward. Servers MUST NOT compare digests unless they agree
on the frame.

A frame is mathematically bounded by the causal graph. The frame's digested
population includes only the known events that **causally succeed** (are
topological descendants of) the anchor antichain. Events that causally precede
the anchor, such as pre-join history, are excluded. This topological bound
prevents spoofed timestamps or depths from polluting finalized historical
segments, because the causal lineage is cryptographically sealed by the anchor.

A frame is identified by the sorted, deduplicated `frame_event_ids` antichain.
The order of the array is not significant on the wire, but implementations MUST
canonicalize it before comparing or indexing a digest.

`frame_id` is the unpadded base64url encoding of the 32-byte SHA-256 digest of
the Matrix canonical JSON array containing the canonical sorted
`frame_event_ids`. The room ID is not included because a frame anchor event ID
is already room-bound; implementations MUST nevertheless reject a frame whose
anchors do not belong to the requested room. The same frame ID therefore names
the same frame across servers and can be used as the key for resident state.

**Negotiation.**

1. The requester compares the responder's `frame_event_ids` with its own. An
   exact match selects that frame immediately.
2. If the frames differ, the requester MUST issue an `extremity` diff request
   with `frame_negotiation: true` and include its current `frame_event_ids`. The
   responder MUST compare the supplied anchors against its local DAG and return
   `frame_status` and `negotiated_frame_event_ids`.
3. If the responder can establish a common descendant frame, it returns
   `frame_status: "common"` and the complete sorted anchor antichain. A frame is
   common only when both servers hold every returned anchor and every returned
   anchor is at or below the history boundary represented by both previous
   frames. The responder MUST NOT select a frame merely because it is newer on
   one server.
4. If no common descendant frame can be established, the responder returns
   `frame_status: "none"` and MUST NOT return or compare digests for the
   request. The requester MUST route the room to frame extension or historical
   backfill, using MSC00DB where applicable, rather than treating the frame
   mismatch as an interior hole.

The `room_diff` response MUST include `frame_status` whenever
`frame_negotiation` is requested. `frame_status` is one of `common`, `none`, or
`not_requested`; `negotiated_frame_event_ids` is required for `common` and
omitted otherwise. Once a common frame is selected, both sides MUST compute all
subsequent digests, counts, sketches, and bucket summaries over that exact
frame. An implementation MUST NOT silently substitute its local frame between
the digest and diff requests.

**Lifetime.** Implementations SHOULD maintain resident accumulators keyed by the
canonical frame anchor set rather than only by room ID, under a bounded TTL
and/or LRU policy. Servers MUST NOT assume a negotiated frame remains
permanently available and are not required to track per-peer frame adoption
before evicting it.

If a requester submits a `frame_id` that has been pruned, expired, or is
unknown, the responder MUST reject the request with HTTP 400 or 404 and a
standard Matrix error code such as `M_NOT_FOUND`. The error response MUST
include `frame_status: "none"`:

```json
{
  "errcode": "M_NOT_FOUND",
  "error": "The requested reconciliation frame is unknown or has expired.",
  "frame_status": "none"
}
```

The responder MUST return this signal before comparing any digest. The requester
MUST then abandon algebraic reconciliation for that frame and fall back to
`extremity` mode, frame extension, or historical backfill. If an old frame is
retired, the responder MUST NOT claim that a digest mismatch proves an event-set
divergence.

**Validation is a transport-layer responsibility.** Before invoking the MSC0500
kernel, the responder MUST resolve and validate the requested `frame_id` and
confirm that it matches the frame used by the supplied digest metadata. A
missing, expired, or mismatched frame MUST terminate the request before the
responder computes any residual, subtraction, or bucket summary. The kernel MUST
receive only inputs already validated as belonging to the same frame; it MUST
NOT interpret `frame_status: "none"` or perform frame negotiation itself.

MSC00DB bulk backfill is the complementary boundary-extension mechanism: its
`edges.oldest` response field is an antichain that can become the next frame
anchor after the returned historical segment is validated and ingested. This MSC
reconciles holes above that anchor; MSC00DB moves the anchor downward.

**Authorization.** The requesting server MUST be a participant in the room
(i.e., have at least one joined member). The receiving server MUST verify this
before responding, and MUST respond with HTTP 403 and `M_FORBIDDEN` otherwise.

### Room diff: `POST /_matrix/federation/v1/room_diff/{roomId}`

Given a requesting server's event ID set (or a compact representation thereof),
returns the set of event IDs that the responding server has but the requester
likely does not. This is the "what am I missing?" query.

**Comparison scope.** The default `scope` is `event_set`, which compares `K`
within the negotiated frame. A request MAY instead use `scope: "resolved_state"`
to compare only the event IDs in each server's locally resolved state map at one
common `state_at` event. Both peers MUST agree on `scope` and `state_at` before
comparing digests or sketches.

A resolved-state comparison is still only event-ID/PDU transport: the responder
MUST NOT send `(type, state_key) -> event_id` map entries, and the requester
MUST rerun state resolution locally after admitting any returned PDUs. For
`scope: "resolved_state"`, `state_at` MUST be present in the negotiated frame
and both servers MUST hold and resolve that event under the same room version.
MSC0500 identifier derivation applies to state event IDs exactly as to timeline
event IDs. A peer that cannot establish these conditions MUST reject the request
with `M_INVALID_PARAM` or `M_UNSUPPORTED_ROOM_VERSION` before comparing digests.

**Two modes.** The endpoint supports two diff modes because Matrix federation
produces two fundamentally different classes of data loss:

- **Frontier lag ("clean" divergence).** A server goes offline, gets
  rate-limited, or falls behind. It misses a contiguous branch of events from
  the DAG tip. Its extremities are stale, but its interior DAG is intact. This
  is a Git branch that is behind upstream — the delta is a clean, linear range
  between the local and remote tips.

- **Interior gaps ("Swiss cheese" divergence).** A server drops random
  individual events due to rate limiting, rejection cascades, or auth chain
  fetch timeouts, but continues to receive subsequent events via state-resyncs.
  Its extremities may match the remote server's, but its interior DAG has holes.
  This has no Git analogue — Git's content-addressable storage guarantees that
  possessing a commit implies possessing all ancestors.

`extremity` mode is a merge-base finder optimized for frontier lag. It walks
backward from divergent extremities to find the most recent common ancestor,
returning the missing delta in O(delta) time.

`sketch` mode is exact set reconciliation over `K`. It ignores graph topology
while extracting event identifiers in the symmetric difference.

**Request:**

```http
POST /_matrix/federation/v1/room_diff/{roomId}
```

```json
{
  "mode": "extremity",
  "scope": "event_set",
  "frame_id": "<base64url_32_byte_frame_id>",
  "local_extremity_event_ids": ["$abc123", "$def456"],
  "have_event_ids": [
    "$known_depth_90000",
    "$known_depth_89500",
    "$known_depth_88000",
    "$known_depth_84000"
  ],
  "frame_negotiation": true,
  "frame_event_ids": ["$join_anchor"],
  "local_known_event_count": 81000,
  "max_depth_delta": 5000,
  "max_events": 10000,
  "limit": 1000
}
```

**Or, in `sketch` mode:**

```json
{
  "mode": "sketch",
  "scope": "resolved_state",
  "state_at": "$state_point:example.com",
  "frame_id": "<base64url_32_byte_frame_id>",
  "local_digest": "<base64url_16_byte_accumulator>",
  "digest_type": "algebraic_v1",
  "local_known_event_count": 81000,
  "frame_event_ids": ["$join_anchor"],
  "requests": [{ "depth": 0, "prefix": 0, "capacity": 32 }],
  "local_sketches": ["<base64url_syndrome_sketch>"],
  "limit": 1000
}
```

#### Dynamic tree request schema

The `requests` member in `sketch` mode is a JSON array of request objects with
the following shape:

```json
{
  "type": "object",
  "required": ["depth", "prefix", "capacity"],
  "additionalProperties": false,
  "properties": {
    "depth": {
      "type": "integer",
      "minimum": 0,
      "maximum": 32
    },
    "prefix": {
      "type": "integer",
      "minimum": 0
    },
    "capacity": {
      "type": "integer",
      "minimum": 1,
      "maximum": 32
    }
  }
}
```

For a request `R = (depth, prefix, capacity)`, `prefix` MUST be strictly less
than `2^depth`. The array of `requests` MUST be in canonical key-space range
order, and each request MUST form an antichain with every other request in the
same array. Implementations MUST reject any request list that is out of order,
contains duplicates, or contains an ancestor-descendant pair before attempting
subtraction.

The canonical range for a request `R = (d, p)` is:

$$
\text{start}(R) = p \cdot 2^{32-d}
$$

$$
\text{end}(R) = (p + 1) \cdot 2^{32-d}
$$

A request sequence `[R_0, R_1, \dots, R_{N-1}]` is valid only if:

$$
\text{end}(R_i) \le \text{start}(R_{i+1}) \quad \text{for all } 0 \le i < N - 1
$$

This condition is normative. Implementations MAY validate it in place in `O(N)`
time and `O(1)` memory when the wire order is already canonical.

**Fields (request):**

<!-- markdownlint-disable MD013 -->

| Field                       | Type     | Required                | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| --------------------------- | -------- | ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `mode`                      | string   | Yes                     | One of `extremity` or `sketch`. Determines how the diff is computed.                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `scope`                     | string   | No                      | `event_set` (default), or `resolved_state`. The latter compares resolved state event IDs at `state_at` and is never a state-map adoption mechanism.                                                                                                                                                                                                                                                                                                                                                                      |
| `state_at`                  | string   | If scope=resolved_state | Common event ID at which both servers resolve state. It MUST be in the negotiated frame.                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `local_extremity_event_ids` | [string] | If mode=extremity       | The requesting server's current forward extremities. Included in the `have` set for the merge-base walk.                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `have_event_ids`            | [string] | If mode=extremity       | A sparse sample of event IDs the requester already has, used as stop conditions for the merge-base walk. See below.                                                                                                                                                                                                                                                                                                                                                                                                      |
| `frame_negotiation`         | bool     | No                      | If true, the responder negotiates a common frame and returns `frame_status`; use it when the advertised frame arrays differ.                                                                                                                                                                                                                                                                                                                                                                                             |
| `frame_event_ids`           | [string] | If frame negotiation    | The requester's current canonical frame anchor antichain. Required when `frame_negotiation` is true; also required in `sketch` mode.                                                                                                                                                                                                                                                                                                                                                                                     |
| `frame_id`                  | string   | If mode=sketch          | Exact identifier of the frame used to construct the digest, sketch, and counts. The responder MUST reject an unknown or expired ID.                                                                                                                                                                                                                                                                                                                                                                                      |
| `local_digest`              | string   | If mode=sketch          | The requesting server's 16-byte accumulator for the negotiated frame.                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `digest_type`               | string   | If mode=sketch          | The digest profile used. MUST be `algebraic_v1` for this MSC.                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `local_known_event_count`   | integer  | If mode=sketch          | The requesting server's known-event count for the negotiated frame.                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `requests`                  | [object] | If mode=sketch          | A list of dynamic-tree extraction requests. Each entry has `depth` (integer, `0..32`), `prefix` (integer, `0..2^depth-1`, the leading `depth` bits of `h_64`), and positive `capacity` (MUST NOT exceed 32). Entries MUST be transmitted in canonical key-space range order; duplicates MUST be rejected before subtraction. Entries MUST form an antichain — no entry's range may contain another's — and MUST be rejected before subtraction otherwise. The sum of `capacity` across all entries MUST NOT exceed 4096. |
| `local_sketches`            | [string] | If mode=sketch          | Base64url-encoded syndrome sketches of the requester's known-event set, one per entry in `requests`, in the same order. A length mismatch against `requests` MUST be rejected before subtraction.                                                                                                                                                                                                                                                                                                                        |
| `max_depth_delta`           | integer  | No                      | Extremity mode only. Positive integer. The maximum topological depth distance the peer is allowed to walk. Default 5000, max 50000.                                                                                                                                                                                                                                                                                                                                                                                      |
| `max_events`                | integer  | No                      | Extremity mode only. Positive integer. The maximum number of event IDs the peer is allowed to inspect before stopping. Default 10000, max 50000.                                                                                                                                                                                                                                                                                                                                                                         |
| `limit`                     | integer  | No                      | Positive integer. Maximum number of event IDs to return. Default 1000, max 10000.                                                                                                                                                                                                                                                                                                                                                                                                                                        |

<!-- markdownlint-enable MD013 -->

**Response:**

```json
{
  "missing_event_ids": ["$ghi789", "$jkl012", "$mno345"],
  "requester_only_short_ids": ["base64url_8_byte_h64"],
  "expected_requester_side_accumulator": "<base64url_16_byte_accumulator>",
  "remote_known_event_count": 81247,
  "remote_extremity_event_ids": ["$abc123", "$pqr678"],
  "frame_id": "<base64url_32_byte_frame_id>",
  "frame_status": "not_requested",
  "sketch_status": "decoded",
  "truncated": false
}
```

**Fields (response):**

<!-- markdownlint-disable MD013 -->

| Field                                 | Type     | Required                | Description                                                                                                                                                                                                                                                     |
| ------------------------------------- | -------- | ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `missing_event_ids`                   | [string] | Yes                     | Event IDs that the responding server has but the requesting server does not. Returned IDs are exact in `sketch` mode when `sketch_status` is `decoded`, and in `extremity` mode when `truncated` is false; the list is complete only when `truncated` is false. |
| `requester_only_short_ids`            | [string] | No                      | In `sketch` mode, decoded 8-byte `h_64` values that appear to be held only by the requester, encoded as unpadded base64url. This lets the requester verify and optionally push reverse repairs.                                                                 |
| `expected_requester_side_accumulator` | string   | No                      | In `sketch` mode, `residual_digest XOR accumulator(responder_side)`, encoded as a 16-byte unpadded base64url value. The requester verifies this against the full event IDs it resolves from `requester_only_short_ids`.                                         |
| `remote_known_event_count`            | integer  | Yes                     | The responding server's known-event count for the negotiated frame.                                                                                                                                                                                             |
| `remote_extremity_event_ids`          | [string] | Yes                     | The responding server's current forward extremities.                                                                                                                                                                                                            |
| `frame_id`                            | string   | Yes                     | The exact frame identifier used for the response.                                                                                                                                                                                                               |
| `frame_status`                        | string   | If negotiation          | `common`, `none`, or `not_requested`. A `common` response selects `negotiated_frame_event_ids` for subsequent digest and diff operations.                                                                                                                       |
| `negotiated_frame_event_ids`          | [string] | If frame_status=common  | The complete canonical frame anchor antichain selected by negotiation.                                                                                                                                                                                          |
| `scope`                               | string   | Yes                     | The comparison scope used by the response: `event_set` or `resolved_state`.                                                                                                                                                                                     |
| `state_at`                            | string   | If scope=resolved_state | The common DAG point used for local state resolution.                                                                                                                                                                                                           |
| `inline_pdus`                         | [PDU]    | No                      | Optional full PDUs for `missing_event_ids`. Each PDU MUST be independently checked against its event ID; positional correspondence MUST NOT be trusted.                                                                                                         |
| `sketch_status`                       | string   | No                      | `decoded`, `capacity_exceeded`, or `not_applicable`. Present for `sketch` mode. `capacity_exceeded` on a given `(depth, prefix)` node is the trigger for requesting its two children; see "Dynamic tree extraction," below.                                     |
| `truncated`                           | bool     | Yes                     | Whether the result is incomplete — because `limit` was reached, a walk bound was reached, or the bounding checks failed. See Handling truncation.                                                                                                               |

<!-- markdownlint-enable MD013 -->

When `inline_pdus` is present, the responder MUST include only PDUs whose event
IDs occur in `missing_event_ids` and MUST enforce the same per-event and total
response-size limits as `room_events`. The requester MUST independently validate
each inline PDU's event ID, room, hashes, signatures, authorization, and
ancestry; it MUST NOT rely on array position or the responder's claimed
association. If an inline PDU is absent, oversized, malformed, or fails
validation, the requester MUST fetch that event through `room_events` or the
ordinary federation event endpoints. Inline PDUs do not alter the state-map
projection directly.

#### Mode selection

Servers SHOULD select the diff mode based on the `room_digest` comparison:

- If the remote digest and `known_event_count` both match for the same frame,
  the peers are synchronized for that frame and no diff is needed.
- If the remote server's `extremity_event_ids` contain event IDs the local
  server does not recognize, the requester MAY use `extremity` mode to discover
  the repair frontier, but MUST treat `truncated: true` as non-repair progress
  until the walk reaches known ancestry.
- Otherwise, or after frontier repair, use `sketch` mode, provisioning the
  initial depth-0 request's `capacity` per the MSC0500 budget from the count
  residual `c = abs(local_known_event_count - remote_known_event_count)`.

#### `extremity` mode

Once two servers determine their digests disagree, the protocol does not
immediately request all missing data, which invites accidental backfill abuse.
It first locates where the two DAG views diverged, so that only the divergent
delta is transferred.

This mode is modeled on Git's packfile negotiation (`upload-pack`): the
requester sends "have" hints at exponentially increasing distances from its
tips, and the responder walks backward from its own tips that the requester does
not know, through `prev_events`, until it reaches an event the requester already
has. That event is the **merge-base** — the most recent common ancestor of the
two DAG views. Events above the merge-base are reconciliation candidates; events
at or below it are stable shared history.

An unbounded graph walk here is a denial-of-service vector. A large room with
partial-state joins, rejected branches, and missing auth chains is not a clean
tree; it is a damaged DAG with holes, and blind traversal lets a hostile peer
trigger expensive walks that rediscover old history. Every walk is therefore
bounded by `max_depth_delta` and `max_events`. If either field is omitted, the
responder MUST apply the default from the request field table. A responder MUST
stop as soon as any bound is reached and report `truncated: true` rather than
silently escalating to deeper traversal. Reconciliation is allowed to be
incomplete, but it MUST NEVER become unbounded.

The responding server computes the diff as follows:

1. Build the `have` set: the union of `local_extremity_event_ids` and
   `have_event_ids`. The combined `have` set MUST NOT exceed 256 entries;
   requests exceeding this MUST be rejected with HTTP 400.
2. **Pre-flight validation:** Look up which `have` events exist in the local
   store (a batch of point lookups). If zero `have` events are recognized,
   immediately return an empty result with `truncated: true` rather than walking
   the DAG. This prevents a malicious requester from forcing a maximal walk by
   sending fabricated `have` event IDs.
3. **Topological bounding check (O(1)):** Compute
   `delta = local_extremity_depth - max(local_depth_of_valid_have_events)`,
   where `local_extremity_depth` is the maximum depth of the responder's own
   forward extremities. If `delta > max_depth_delta`, return an empty result
   with `truncated: true`.
4. Identify forward extremities the responder has that are NOT in the `have` set
   — these are the "want" events (tips unknown to the requester).
5. Walk backwards from those unknown tips via `prev_events`, collecting event
   IDs not in the `have` set.
6. **Stop conditions:** For each branch, stop when it reaches an event ID that
   IS in the `have` set (the merge-base for that branch — events at or before it
   are excluded, as the requester already has them), or when the branch's depth
   falls more than `max_depth_delta` below `local_extremity_depth`.
7. **Safety limit:** If the walk inspects `max_events` event IDs before all
   branches terminate, it MUST stop and the response MUST set `truncated: true`.
8. Return the collected event IDs in reverse topological order, up to `limit`.

Servers MUST reject zero, negative, non-integer, or over-cap `max_depth_delta`,
`max_events`, and `limit` values with HTTP 400. Servers MUST enforce
`max_depth_delta <= 50000`, `max_events <= 50000`, and `limit <= 10000`. Servers
SHOULD also maintain per-peer, per-room accounting of inspected events over a
rolling window (for example, 60 seconds) and reject requests that would exceed a
cumulative budget (RECOMMENDED: 100,000 inspected events per peer per room per
minute), so that many small requests cannot each walk just under the per-request
limit.

**Constructing the `have` set (requesting server).** The requester builds
`have_event_ids` as a sparse, exponentially-spaced sample of event IDs it
already possesses, working backwards from its extremities:

1. Start from each local extremity and walk backwards via `prev_events`.
2. Sample event IDs at exponentially increasing depth intervals: the first
   event, then 1 step back, 2 steps, 4, 8, 16, and so on.
3. Stop sampling after 32 samples per extremity, or when the walk reaches the
   room's create event.

This produces at most approximately 32×E event IDs (where E is the number of
extremities, typically 1–5), totaling 32–160 event IDs, giving dense coverage
near the frontier where divergence is most likely and sparse coverage deep in
the DAG where both servers are likely synchronized. The logarithmic sample count
is a property of this bounded per-extremity traversal, not a universal
convergence bound; actual work depends on DAG depth, extremity count, merge-base
location, and the configured limits. In practice the responder often finds a
merge-base within the first few hundred events of its backward walk.

#### `sketch` mode

The responding server:

1. Validates that `digest_type` is `algebraic_v1`, `local_digest` is exactly 16
   decoded bytes, `requests` is a well-formed array of
   `(depth, prefix, capacity)` entries in canonical key-space range order with
   no duplicates and no entry's range containing another's (an antichain), each
   entry's `capacity` does not exceed 32, the sum of `capacity` across
   `requests` does not exceed 4096, and the request frame matches the
   responder's digest frame.
2. Computes `residual_digest = remote_digest XOR local_digest`.
3. Computes `c = abs(remote_known_event_count - local_known_event_count)`. If
   `residual_digest` is zero and `c` is zero, returns an empty decoded response.
4. Validates that `local_sketches` has the same length as `requests`, and that
   each sketch is exactly `8 * capacity` bytes for its corresponding entry.
5. For each entry in `requests`, produces a syndrome sketch over the subset of
   its own known-event set whose `h_64(e)` has `prefix` as its leading `depth`
   bits, at that entry's `capacity`. Per MSC0500, this subset MUST be
   materialized via an `h_64`-sorted index (a range slice), not a
   full-population scan per request.
6. Subtracts each of the requester's sketches from its corresponding
   responder-side sketch and decodes the symmetric difference as 64-bit short
   identifiers, per MSC0500.
7. Partitions the decoded short identifiers into `responder_side` and
   `requester_side`. For `responder_side`, the responder resolves each short ID
   to a full event ID it holds and computes the 128-bit accumulator over those
   full event IDs. For `requester_side`, it returns only the short IDs, because
   it cannot compute the corresponding accumulator without the full event IDs.
8. Returns responder-side full IDs as `missing_event_ids`, requester-side short
   IDs as `requester_only_short_ids`, and
   `expected_requester_side_accumulator = residual_digest XOR accumulator(responder_side)`.
   The requester resolves the short IDs it holds, verifies their 128-bit
   accumulator against `expected_requester_side_accumulator`, and MAY use those
   events as reverse repair candidates for the responder.
9. If any entry's sketch exceeds its capacity, sets
   `sketch_status: "capacity_exceeded"` and `truncated: true`.

**Dynamic tree extraction.** A `capacity_exceeded` result for a given
`(depth, prefix)` node is the trigger for localization, not a terminal failure.
The requester issues a further `sketch` request with two new `requests` entries
at `depth + 1`, for prefixes `2 * prefix` and `2 * prefix + 1` — the overflowing
node's two children — instead of raising that node's own capacity. A child that
itself overflows is split the same way, one depth deeper. Because each split
strictly partitions its parent's population, recursion terminates: worst case at
`depth = 32`, where the profile's depth cap is reached. No precondition beyond
the usual frame agreement is required for this — every result is independently
verified against the 128-bit accumulator per MSC0500, not inferred from graph
structure, so there is nothing analogous to a probabilistic fallback's
extremity-convergence requirement.

The requester SHOULD use the strata-estimated `d̂` from `room_digest` to size the
initial depth-0 request's _capacity_ (bounded by the per-entry cap of 32, above)
— not its depth. This spec always starts a `sketch` exchange at depth 0 and
splits one level at a time on `capacity_exceeded`. This is an efficiency choice,
not a correctness one: an under-provisioned node produces
`sketch_status: "capacity_exceeded"` for that node specifically, which is
exactly the trigger for the next split, not a lost result.

**This has a latency cost that is worth stating in concrete terms — and a
per-branch depth count understates it.** Each round is capacity-bounded: the
aggregate cap (4096) limits any single round to at most `4096 / 32 = 128` new
node-decodes. A node only stops needing further splitting once its local count
is ≤32, so fully localizing a difference of size `d` requires roughly `d / 32`
successful node-decodes in total — and at most 128 of those fit in one round.
That gives a round-count floor of `d / 4096`, independent of how many depth
levels are involved: **~123 rounds at `d = 500,000`, ~2,442 at
`d = 10,000,000`.** A depth count alone (`log2(d/32)` ≈ 14 and ≈18 respectively)
understates this badly: it only holds while the frontier is narrower than the
aggregate cap allows, which stops being true once the frontier passes 128 nodes
— around depth 7. Each round is gated on the previous response, so at typical
federation RTT (50–200 ms) this is many seconds to tens of seconds for the
differences dynamic tree extraction is meant to handle.

Choosing a smarter starting depth from `d̂` cannot fix this: the best a different
starting point can do is skip the ramp-up below the 128-node aggregate ceiling —
at most ~7 rounds, against a floor already in the hundreds. The floor is a
throughput bound (total decodes ÷ per-round decode cap), not a latency bound
(how many depth levels are walked), and no starting-depth choice changes total
decode throughput. This spec therefore does not define d̂-driven initial depth:
the ~7-round saving it could offer is not worth the added spec surface against a
floor it cannot move.

**The floor implies a hard precondition, not just a documented cost.**
`d̂ × per_node_cap` rounds are needed regardless of strategy, so a requester
whose round budget cannot cover `d̂` MUST NOT begin a `sketch` exchange for that
difference at all. Concretely: a requester MUST compare `d̂` (or the exact count
residual `c`, if available) against `round_cap * 4096` — using this MSC's round
cap of 20 (see "Amplification via oversized sketches," below), that ceiling is
**~82,000 elements** — and MUST route to `extremity` mode, backfill, or frame
extension instead of `sketch` mode when `d̂` exceeds it. This is the load-bearing
check: it stops a peer from starting a round sequence it cannot finish, rather
than letting it discover that dozens of rounds in. See "Scope" in MSC0500 for
the corresponding profile-level guidance.

#### Causal closure and truncation

A recovery set can be integrated iff it is downward-closed relative to the
requester's existing store and the returned identifiers have retrievable,
validated PDUs. Exact set recovery satisfies this for the whole difference only
when the frame's retained event bodies and required auth-chain dependencies are
available; a rejected tombstone records knowledge of an ID but cannot itself be
integrated as an event. A truncated backward walk from the responder's frontier
does not: it collects descendants before ancestors, so its minimal returned
events may still have parents in neither the requester's store nor the returned
set. Such a walk can transfer useful bytes, but it does not repair the DAG until
it reaches known ancestry. Responders MUST therefore report `truncated: true`,
and requesters MUST NOT treat a truncated walk as resolving the gap.

**Handling truncation (requesting server).** On `truncated: true`, the requester
MUST NOT immediately retry an identical request. If the response is non-empty,
the requester SHOULD fetch and persist the returned events, then re-run the diff
with an updated `have` sample. If progress stalls or the response is empty, the
bounds were insufficient: the requester MAY retry with larger
`max_depth_delta`/`max_events` (up to the caps), and otherwise SHOULD fall back
to `sketch` mode or existing `/backfill`, applying back-off between attempts.

**Authorization.** Same as `room_digest` — the requesting server MUST be a
participant in the room.

### Membership divergence handling

If a request is rejected because the responding and requesting servers disagree
about room membership, the rejection MUST be distinguishable from a generic
authorization failure. A plain `M_FORBIDDEN` deadlocks recovery: the requester
cannot learn which membership event it needs in order to reconcile the
membership view that caused the denial.

In that case, the responding server SHOULD return a structured error body naming
the membership event IDs it used to make the decision, limited to events about
the requesting server's own membership or other authorization-relevant events
already known to the requester:

```json
{
  "errcode": "M_MEMBERSHIP_DIVERGENCE",
  "error": "membership view differs between peers",
  "membership_event_ids": ["$membership_event_1", "$membership_event_2"]
}
```

This does not leak additional private room history: the event IDs are only for
membership events already implicated in the authorization decision, and they
give the requester a concrete target for subsequent reconciliation.

### Bulk event fetch: `POST /_matrix/federation/v1/room_events/{roomId}`

Given a set of event IDs, returns the full events and their auth chain events in
topological order, suitable for direct insertion into the local store.

**Request:**

```http
POST /_matrix/federation/v1/room_events/{roomId}
```

```json
{
  "event_ids": ["$ghi789", "$jkl012", "$mno345"],
  "include_auth_chain": true,
  "known_event_ids": ["$abc123", "$def456"]
}
```

**Fields (request):**

<!-- markdownlint-disable MD013 -->

| Field                | Type     | Required | Description                                                                                                                                                                                               |
| -------------------- | -------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `event_ids`          | [string] | Yes      | The event IDs to fetch. Maximum 500 per request.                                                                                                                                                          |
| `include_auth_chain` | bool     | No       | If true, the response includes auth chain events that the requesting server might not have. Default true.                                                                                                 |
| `known_event_ids`    | [string] | No       | Event IDs the requesting server already has. When walking auth chains, the responding server SHOULD stop at events in this set (the graph intersection), avoiding redundant transfer. Default empty list. |

<!-- markdownlint-enable MD013 -->

**Response:**

```json
{
    "events": [
        { "...PDU..." },
        { "...PDU..." }
    ],
    "auth_chain_events": [
        { "...PDU..." }
    ],
    "missing_event_ids": [
      "$unknown999"
    ],
    "rejected_tombstones": [
      { "event_id": "$rejected123", "reason": "auth_failed" }
    ]
}
```

**Fields (response):**

<!-- markdownlint-disable MD013 -->

| Field                 | Type     | Required | Description                                                                                                                                                 |
| --------------------- | -------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `events`              | [PDU]    | Yes      | The requested events, in topological order (dependencies before dependants). Each event is a full, signed PDU.                                              |
| `auth_chain_events`   | [PDU]    | Yes      | Auth chain events for the returned events that are not in the `events` list. Also in topological order. Empty if `include_auth_chain` was false.            |
| `missing_event_ids`   | [string] | Yes      | Event IDs from the request that the responding server does not have.                                                                                        |
| `rejected_tombstones` | [object] | Yes      | Rejected event IDs known to the responder but not available as PDUs. Each entry contains `event_id` and a stable rejection `reason`. Empty when none apply. |

<!-- markdownlint-enable MD013 -->

**Event ordering.** Events in both `events` and `auth_chain_events` MUST be
returned in topological order. For any event E, each referenced `auth_events` or
`prev_events` event that is included in the response MUST appear earlier in the
combined list (`auth_chain_events` concatenated with `events`). References to
events identified by the requester's `known_event_ids` MAY be omitted from the
response and need not appear in the combined list. The requester MUST still
verify that those dependencies are present locally before admitting E.

**Authorization.** Same as `room_digest`. Additionally, the responding server
MUST NOT return events that the requesting server would not be allowed to see
(e.g. events sent after the requesting server's last member left the room, per
existing history visibility rules).

The requester MUST persist each `rejected_tombstones` entry in its frame-scoped
known-event store before the exchange completes. Tombstone reasons are
diagnostic metadata and are not used to resolve room state. A responder MUST
return a tombstone instead of repeatedly returning the same rejected ID as a
fetchable event; this prevents permanent digest mismatches and fetch loops.

### Reconciliation protocol

```text
    Server A (lagging)                        Server B (ahead)
         │                                         │
         │  GET /room_digest/{roomId}              │
         │────────────────────────────────────────>│
         │                                         │
         │  200 OK { digest, extremities, ... }    │
         │<────────────────────────────────────────│
         │                                         │
         │  [Compare: digests differ?              │
         │   counts differ? extremities unknown?]  │
         │                                         │
         │  POST /room_diff/{roomId}               │
         │  { mode: "sketch",                      │
         │    local_digest: "...",                 │
         │    local_known_event_count: 81000,      │
         │    requests: [{depth: 0, prefix: 0,     │
         │                capacity: 32}],          │
         │    local_sketches: ["..."] }            │
         │────────────────────────────────────────>│
         │                                         │
         │  200 OK { missing_event_ids: [...] }    │
         │<────────────────────────────────────────│
         │                                         │
         │  POST /room_events/{roomId}             │
         │  { event_ids: [...],                    │
         │    include_auth_chain: true }           │
         │────────────────────────────────────────>│
         │                                         │
         │  200 OK { events: [...],                │
         │           auth_chain_events: [...] }    │
         │<────────────────────────────────────────│
         │                                         │
         │  [Verify signatures, persist events,    │
         │   re-resolve state at extremities]      │
         │                                         │
```

**Short-circuit optimization.** If the `room_digest` response shows an identical
`digest` and `known_event_count` for the same frame, the requesting server MAY
skip the diff and event fetch phases entirely. This is the common no-difference
path and costs 16 accumulator bytes plus the count field.

### Gossip scheduling

Servers SHOULD implement periodic gossip-based reconciliation for active rooms.
Normative requirements:

- Implementations MUST apply randomized jitter of ±15% to all scheduling
  intervals, including backed-off intervals, to prevent cluster-wide thundering
  herds after large homeserver restarts or partition recovery.
- Peers SHOULD be selected using a weighted random strategy with a uniform
  floor: `Pr(select i) = (1 - epsilon) * weight_i + epsilon / N`, with
  `epsilon >= 0.05` unless local policy has a measured reason to choose
  otherwise. The uniform floor is not a fairness heuristic; it is the condition
  that prevents hub-weighted peer choice from becoming an eclipse surface.
- Servers SHOULD select at least `f = 3` peers per round for active rooms unless
  local policy requires a lower rate.
- When a server detects potential divergence — a state resolution producing an
  unexpected result, or a received event referencing unknown `prev_events` — it
  SHOULD immediately initiate reconciliation with the event's origin server.
- If a peer returns identical digests across 3 consecutive polls, the server
  SHOULD exponentially back off that peer/room pair, to a maximum of 24 hours.
  Any new event in the room resets the back-off.

Recommended polling intervals, peer weighting inputs, and the
eclipse-probability analysis behind `f` and `epsilon` are in the architecture
note.

### `ETag` optimization

To minimize bandwidth for digest polling, `room_digest` supports conditional
requests:

```http
GET /_matrix/federation/v1/room_digest/{roomId}
If-None-Match: "algv1:abc123def456"
```

```http
HTTP/1.1 304 Not Modified
ETag: "algv1:abc123def456"
```

The ETag is derived from the incrementally maintained level-0 accumulator and
the current extremity frontier:

```text
unpadded_base64url(digest || implementation_defined_frontier_hash[0:8])
```

`digest` is the 16-byte accumulator over `K` for the frame. The frontier
component is defense-in-depth: even if two different known-event sets collide in
the accumulator, differing frontiers still change the ETag.

The frontier hash is implementation-defined because ETags are opaque and are
only compared against the same responder's later `If-None-Match` value. Servers
wanting byte-stable behavior across implementations SHOULD use Matrix canonical
JSON over bytewise-sorted `extremity_event_ids`, hashed with SHA-256, truncated
to the first 8 bytes.

If the computed ETag matches `If-None-Match`, the server MUST return HTTP 304
with no body. The server evaluates the conditional request in O(E), where E is
the number of extremities (typically 1–5), using the incrementally maintained
accumulator — without touching the event store or computing a sketch. This
reduces polling for a synchronized room to a single round-trip with a ~50 byte
response.

The ETag is a cache-validation hint, not a synchronization guarantee: a `304`
means only that the responder's view has not changed since the requester last
observed it. It does not imply the two servers agree. This statement concerns
accidental collisions only; malicious peers are covered under Accumulator
integrity, below.

Requesting servers SHOULD cache the peer's ETag together with their own local
accumulator at the time of caching. They MUST NOT send `If-None-Match` if their
own accumulator has changed since the ETag was cached, or if the most recent
reconciliation round for that peer and room terminated with `truncated: true`.
To avoid indefinite stalling on a quiescent peer, the requesting server MUST
force an unconditional digest comparison at least once every 16 consecutive
`304` responses per peer and room.

## Potential issues

### Performance resilience

- **Digest computation cost.** The accumulator and resident strata estimator are
  maintained incrementally when events are persisted or purged, per MSC0500.
  Dynamic-tree node sketches are computed on demand, not maintained resident.
  Implementations that do not maintain the resident structure may need to scan
  room history to answer `sketch` requests and SHOULD apply stricter rate
  limits.
- **Diff amplification.** A malicious requester can overstate `requests`
  capacities, force repeated tree splits, or ask for large `limit` values.
  Servers MUST reject a `sketch` request whose aggregate `requests` capacity
  exceeds 4096, and MUST cap `limit`, response bytes, and per-peer CPU time.
- **Bulk fetch abuse.** `room_events` returns full PDUs, which can be large. The
  500-event cap and standard federation rate limiting mitigate this.

### Frame negotiation

Frame negotiation is bounded by the `extremity` request limits and point lookups
for the supplied anchors. Repeated leave/rejoin cycles may produce an antichain
rather than a single anchor; the negotiated value is therefore always the
complete anchor array. Retention and history purging can make the common frame
unavailable, in which case the protocol deliberately reports
`frame_status: "none"` and hands the room to frame extension or backfill.

### Consistency in active rooms

If a room is actively receiving events during reconciliation, the
digest/diff/fetch sequence may return stale data. This is acceptable — gossip
protocols are eventually consistent, and the next round catches up. Servers MUST
NOT block event processing during reconciliation.

### Interaction with partial state joins

Servers in the process of a partial state join (MSC3706) SHOULD NOT initiate
reconciliation for that room until the full state resync is complete. They MAY
respond to incoming requests with the events they have, but MUST NOT use a
normal `algebraic_v1` digest for the fully joined frame. They SHOULD return HTTP
409 with `M_PARTIAL_STATE`, or advertise a distinct partial-state frame and set
`X-Matrix-Partial-State: true` to indicate that their digest/diff is incomplete.

## Alternatives

The design space — `/make_join` probing, full Merkle synchronization, Bloom
filters, mandatory RIBLT, and server-initiated push — is analysed in the
architecture note. Summary of the conclusions:

- **`/make_join` as a probe** requires no spec changes but reveals only
  extremity divergence, not interior gaps, and abuses an endpoint designed for a
  different purpose.
- **Full Merkle synchronization** treats reconciliation as a set-membership
  problem when it is a graph repair problem; it also requires persistent
  auxiliary state and adds write amplification. It remains available later as a
  separate `digest_type`.
- **Bloom filters** are rejected outright: they are not group-valued, so peers
  cannot subtract them, false positives are silent, and a larger filter restarts
  rather than extends an exchange. A salted, extremity-gated `bloom_v1` fallback
  was drafted and discarded in favor of dynamic tree extraction, which handles
  the same heavy-tail case without giving up exactness.
- **RIBLT** is in the same group-valued family as PinSketch and would preserve
  exact recovery, but requires its own wire format (signed counts, overflow
  bounds, chunk authentication, a termination rule) and a second decoder to be
  safe against an adversarial peer. Dynamic tree extraction (see "Dynamic tree
  extraction" above) reuses the depth-0 sketch mechanism and decoder instead,
  recursively subdividing only overflowing nodes, and stays exact with no second
  decoder and no capacity guess.
- **Push reconciliation** creates O(servers²) traffic and forfeits the natural
  self-rate-limiting of pull.

## Security considerations

### Information disclosure

`room_digest` reveals metadata about a server's event store: event count, depth
range, timestamp range, and forward extremities. This could be used to
fingerprint implementations or estimate room activity. However, this information
is already implicitly available through `/state_ids`, `/backfill`, and `/event`;
access is restricted to room participants; and the accumulator does not reveal
individual event IDs. Optional bucket summaries reveal only coarse bucket counts
and accumulator differences.

### Denial of service

- **Rate limiting.** Servers MUST apply per-peer, per-room rate limiting to all
  three endpoints. Recommended: 1 request per 10 seconds per room per peer for
  `room_digest` and `room_diff`; 1 request per 30 seconds for `room_events`.
- **Digest caching.** The accumulator and resident bucket summaries SHOULD be
  maintained incrementally. Implementations that compute sketches by scanning
  the event store SHOULD use stricter request budgets.
- **Response caps.** The `limit` parameter on `room_diff` and the 500-event cap
  on `room_events` bound maximum response size.
- **Walk bounds.** The `extremity` mode bounds and the per-peer cumulative
  inspection budget are normative; see that section.

### Replay and poisoning

A malicious server could return fabricated events in `room_events` responses.
This is mitigated by the mechanisms that already protect existing federation
endpoints: all returned PDUs MUST have valid origin signatures, MUST pass hash
verification, and MUST pass standard auth checks before persistence. Events
failing any check MUST be discarded without affecting local state.

### Amplification via oversized sketches

Servers MUST reject a `sketch` request whose aggregate `requests` capacity
exceeds 4096, whose any single entry's capacity exceeds 32, oversized decoded
responses, and requests exceeding per-peer or per-room CPU budgets. The
per-entry cap bounds decode CPU for a single node; the aggregate cap bounds
total wire size and work across the exchange — neither substitutes for the
other, since a single `{depth: 0, capacity: 4096}` entry would otherwise pass an
aggregate-only check while costing roughly 16,384x the decode budget of a
capacity-32 node. Servers SHOULD reject requests whose `local_known_event_count`
is grossly inconsistent with the supplied accumulator history or negotiated
frame.

Servers MUST also cap the number of `requests` entries per round and the
cumulative per-peer count of tree-split rounds for a given reconciliation
attempt at 20, so a peer cannot force unbounded recursive fan-out by repeatedly
requesting refinement of nodes that do not actually overflow. This cap is not
arbitrary: it is the same `round_cap * 4096 ≈ 82,000`-element ceiling that
"sketch mode," above, derives from decode throughput, restated here as the
enforcement side of that MUST NOT precondition. A well-behaved requester never
reaches this cap, because it already refused to start past the same ceiling;
this bound exists for peers that skip that check or misestimate `d̂`.

### Depth manipulation

The topological bounding checks rely on event depth, which is derived from
attacker-influenced event content. When evaluating `max_depth_delta`, servers
SHOULD use their locally computed topological ordering (e.g. stream ordering or
recomputed depth) rather than trusting the `depth` field of received events.

### Accumulator integrity

XOR accumulators are fault-detecting, not authenticators; MSC0500 states the
linear-algebra limit precisely. Nothing in this MSC relies on the accumulator
being binding against a malicious peer. It is an integrity anchor for accidental
decode failure and benign desync, while returned PDUs still MUST be verified by
event ID, hashes, signatures, and authorization rules. Deployments needing
transferable accumulator evidence should await an LtHash-style `digest_type`;
MSC4511 uses Ed25519-signed overlay attestations for responder accountability.

### Interaction with server ACLs

Servers MUST respect `m.room.server_acl` when responding to reconciliation
requests. If the requesting server is denied by the room's ACL, the responding
server MUST return HTTP 403 with `M_FORBIDDEN`, identically to other federation
endpoints.

## Unstable prefix

<!-- markdownlint-disable MD013 -->

| Proposed final identifier                     | Purpose         | Development identifier                                               |
| --------------------------------------------- | --------------- | -------------------------------------------------------------------- |
| `/_matrix/federation/v1/room_digest/{roomId}` | endpoint        | `/_matrix/federation/unstable/tk.nutra.msc45xx/room_digest/{roomId}` |
| `/_matrix/federation/v1/room_diff/{roomId}`   | endpoint        | `/_matrix/federation/unstable/tk.nutra.msc45xx/room_diff/{roomId}`   |
| `/_matrix/federation/v1/room_events/{roomId}` | endpoint        | `/_matrix/federation/unstable/tk.nutra.msc45xx/room_events/{roomId}` |
| `algebraic_v1`                                | digest type     | `algebraic_v1`                                                       |
| `X-Matrix-Partial-State`                      | response header | `X-Matrix-Unstable-Partial-State`                                    |

<!-- markdownlint-enable MD013 -->

## Dependencies

This MSC depends on MSC0500 (`algebraic_v1` digest profile) for its digest
construction. It has no hard dependencies on other unaccepted MSCs.

It is designed to complement:

- [MSC3706](https://github.com/matrix-org/matrix-spec-proposals/pull/3706)
  (Partial state in send_join) — reconciliation can complete what partial joins
  leave incomplete
- [MSC4297](https://github.com/matrix-org/matrix-spec-proposals/pull/4297)
  (State Resolution v2.1) — reconciliation repairs the data gaps that V2.1
  cannot address algorithmically
- MSC4500 (State accumulators) — passive per-transaction state digests give
  active rooms free divergence detection; on mismatch, enumeration and healing
  are delegated to this proposal's `room_diff` and `room_events`. Rooms whose
  recent inbound transactions carry matching accumulator digests MAY back off
  periodic anti-entropy polling accordingly
- MSC00DB (Bulk backfill) — the complementary boundary-extension mechanism; see
  Frames
- MSC0502 (Federation EDU state reconciliation) — the ephemeral-state
  counterpart, using version-vector comparison instead of graph reconciliation

## References

[1] A. Demers, D. Greene, C. Hauser, W. Irish, J. Larson, S. Shenker, H.
Sturgis, D. Swinehart, and D. Terry, "Epidemic Algorithms for Replicated
Database Maintenance," _Proceedings of the Sixth Annual ACM Symposium on
Principles of Distributed Computing_, 1987. Anti-entropy and rumor-mongering.

[2] K. P. Birman, M. Hayden, O. Ozkasap, Z. Xiao, M. Babu, and Y. Minsky,
"Bimodal Multicast," _ACM Transactions on Computer Systems_, 17(2), 1999. Gossip
under partial failure.
