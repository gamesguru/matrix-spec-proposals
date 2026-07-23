# MSC0501: Efficient gossip-based room federation missed PDU reconciliation

Matrix federation today is "push and hope" — when event delivery fails or
servers miss or drop transactions, the DAG develops permanent holes. Each server
drops a different random subset of events, creating unpredictable gaps that no
existing endpoint can efficiently detect or repair.

When servers undergo extended downtime, they have trouble catching up after a
cold boot; no homeserver implementation "forward fills" a complete or orderly
DAG timeline.

This proposal introduces a lightweight endpoint allowing federated servers to
efficiently monitor for event set divergence and reconcile via existing
endpoints without requiring full state synchronization or new room versions.

## Background

### The problem: federation gaps, silent data loss

Federation data loss occurs through several well-documented mechanisms:

1. **Rate limiting** drops inbound `/send` transactions during high-traffic
   periods (spam storms, raids, viral rooms).
2. **Rejection cascades** orphan entire subgraphs — a single malformed event
   causes every subsequent event referencing it via `prev_events` to be
   rejected.
3. **Auth chain fetch timeouts** during load cause events to be permanently
   persisted as rejected outliers.
4. **Partial state joins** (MSC3706) intentionally defer full state
   synchronization, but network interruptions during the resync phase can leave
   permanent gaps.

The result is that servers in the same room can have materially different DAGs,
leading to membership divergence, missing messages, and inconsistent state
resolution outputs — even when the state resolution algorithm itself is
functioning correctly.

### Why existing endpoints are insufficient

<!-- markdownlint-disable MD013 -->

| Endpoint                            | Limitation                                                                                                                    |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `GET /backfill/{roomId}`            | Depth-ordered linear walk; cannot target specific gaps; useless for missing events in the middle of the DAG                   |
| `POST /get_missing_events/{roomId}` | BFS walk with a hard depth limit (default 10); cannot bridge gaps larger than 10 events; requires knowing the boundary events |
| `GET /state_ids/{roomId}`           | Returns state event IDs only (not timeline events); O(N) comparison; no incremental diffing                                   |
| `GET /event/{eventId}`              | Single-event fetch; no bulk mode; requires knowing which events are missing                                                   |
| `GET /make_join`                    | Does not meet latency requirements (20-100 ms); requests to lagging server can timeout (full index scan for unknown event)    |

<!-- markdownlint-enable MD013 -->

None of these endpoints answer the fundamental question: **"Am I missing events
in this room, and if so, which ones?"**

### Design philosophy

This proposal follows the gossip protocol literature (Demers et al., 1987;
Birman, 1999) and adapts three core mechanisms to Matrix's federated DAG model:

1. **Anti-entropy via digest comparison** — O(1) divergence detection using
   compact room digests
2. **Pull-based reconciliation** — the lagging server requests exactly the
   events it needs
3. **Protocol-level idempotency** — repeated reconciliation produces no side
   effects on an already-synchronized pair

### Algebraic rationale

The digest choice is the core design decision. A Bloom filter is a homomorphism
into an idempotent monoid: it supports membership tests, but not subtraction.
That single missing operation forces the responder to test its event set
element-by-element, makes false positives silent in the reconciliation
direction, and prevents incremental extension of a failed exchange.

This MSC instead uses group-valued digests. The room accumulator, bucket
summary, and extraction sketch are treated as truncations of one syndrome map:

```text
sigma_k(S) = (sum h(e), sum h(e)^3, ..., sum h(e)^(2k-1))
```

over the minisketch-compatible 64-bit binary field, with a separate 128-bit
accumulator as the integrity anchor. Increasing `k` extends the sketch
additively rather than restarting the exchange. Decode failure is loud: the
requester or responder can check the decoded identifiers against the 128-bit
residual accumulator before trusting the result.

Causality is the second constraint. A set of recovered events can be integrated
only if it is downward-closed relative to the recipient's store. Exact recovery
of `K_B \ K_A` satisfies that condition; a truncated backward walk from the
frontier does not, because it discovers descendants before ancestors. This is
why truncated graph walks are reported as transfer progress, not as proof that
the DAG gap has been repaired.

## Proposal

Three new federation endpoints are introduced under the
`/_matrix/federation/v1/` namespace.

### Capability discovery

Servers advertise support for this MSC via `GET /_matrix/federation/v1/version`.
Support is advertised in `unstable_features` so that additional `digest_type`
values or endpoint revisions can be added without changing the stable version
document.

```json
{
  "unstable_features": {
    "tk.nutra.msc0501.reconciliation": true,
    "tk.nutra.msc0501.digest.algebraic_v1": true
  }
}
```

A server that receives HTTP 404 or 501 from a reconciliation endpoint MUST cache
that peer as unsupported for at least 24 hours and MUST NOT retry during that
period unless an operator explicitly overrides the cache.

### Room digest: `GET /_matrix/federation/v1/room_digest/{roomId}`

Returns a compact, opaque digest summarizing a server's knowledge of a room's
event graph. Two servers can compare digests in O(1) to determine whether their
DAGs have diverged.

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
| `digest`                 | string             | Yes      | Base64url-encoded 16-byte accumulator over the server's known event identifier set for this room and frame. See Digest Construction below. |
| `digest_type`            | string             | Yes      | The algorithm used to construct the digest. Servers MUST support `algebraic_v1`.                                                           |
| `known_event_count`      | integer            | Yes      | The total number of event identifiers the server knows for this room and frame: accepted events plus rejected-event tombstones.            |
| `strata`                 | [string]           | No       | Optional 32-entry strata estimator. Each entry is a base64url-encoded 64-byte sketch containing `s1` through `s15` for one stratum.        |
| `frame_event_ids`        | [string]           | Yes      | The frame anchor antichain bounding the history this digest covers. Servers MUST compare digests only when they understand the same frame. |
| `extremity_event_ids`    | [string]           | Yes      | The server's current forward extremities (DAG tips) for this room.                                                                         |
| `depth_range`            | [integer, integer] | Yes      | The minimum and maximum topological depth of events held.                                                                                  |
| `origin_server_ts_range` | [integer, integer] | Yes      | The earliest and latest `origin_server_ts` of events held.                                                                                 |

<!-- markdownlint-enable MD013 -->

**Digest construction (`algebraic_v1`):**

The digest is a group-valued accumulator over the known event identifier set
`K = Acc ∪ Rej`, where `Acc` is the accepted event set and `Rej` is the set of
locally rejected event tombstones. Soft-failed events are stored events and are
therefore in `K`; their soft-fail status is not part of reconciliation.

For room versions 3 and later, event IDs are already derived from SHA-256 event
hashes. Implementations derive short identifiers directly from the decoded event
ID hash, using the room-version-specific event-ID alphabet:

- room versions 1 and 2: hash the UTF-8 event ID string with SHA-256;
- room version 3: decode the event ID reference hash as unpadded standard
  Base64;
- room versions 4 and later: decode the event ID reference hash as unpadded
  URL-safe Base64.

```text
h_128(e) = first 128 bits of decoded_event_id_hash(e)
h_64(e)  = first  64 bits of decoded_event_id_hash(e)
```

The "first" bits above are the leading bytes of the SHA-256 byte string in
network byte order. `h_128(e)` is the first 16 bytes. `h_64(e)` is the first 8
bytes interpreted as an unsigned big-endian integer. Because minisketch set
elements are nonzero, if the first 8-byte chunk is zero the implementation MUST
use the next nonzero 8-byte chunk of the decoded SHA-256 hash; if all four
chunks are zero, it MUST use the integer value 1. The 64-bit field is
`GF(2)[x] / (x^64 + x^4 + x^3 + x + 1)`. `h_64` values are mapped to field
elements by treating bit `i` of the integer as the coefficient of `x^i`.
`algebraic_v1` sketches MUST be byte-for-byte compatible with libminisketch
field size 64 for the same inserted `h_64` values. Syndrome coordinates are
serialized in increasing odd-power order, `s1, s3, s5, ...`; each coordinate is
serialized as an unsigned 64-bit little-endian integer. The big-endian hash
parsing and little-endian sketch-coordinate serialization are both normative.

Room versions whose event IDs are not hash-derived MUST either hash their event
IDs with SHA-256 before truncation or be excluded from the negotiated frame.
This MSC does not use XXH3 or another auxiliary hash for `algebraic_v1`.

The level-0 room digest is:

$$
\begin{aligned}
\mathrm{digest} &= \bigoplus_{e \in K \cap \mathrm{Frame}} h_{128}(e) \\
\\
\mathrm{known\_event\_count} &= |K \cap \mathrm{Frame}|
\end{aligned}
$$

Here $$\bigoplus$$ denotes bitwise XOR over all selected 128-bit values.

The digest is encoded as 16 raw bytes using unpadded base64url. Insertion and
removal are the same operation: XOR the same `h_128(e)` value into the
accumulator and increment or decrement `known_event_count`.

The count residual `c = abs(local.known_event_count - remote.known_event_count)`
is an exact measurement of `Δ = |symmetric difference of K_A and K_B|` when
divergence is one-sided, which is the common lagging-server case. If both the
digest and `known_event_count` match for the same frame, the two known-event
sets agree except with negligible probability from an accidental 128-bit
accumulator collision.

Implementations MAY additionally maintain a 32-strata estimator. Stratum `i`
contains the same odd syndrome coordinates `s1` through `s15` as an extraction
sketch, but only for events whose `h_64(e)` has exactly `i` trailing zero bits;
stratum 31 also includes all values with 31 or more trailing zero bits. When two
servers exchange `strata`, they XOR corresponding stratum sketches and inspect
the highest nonempty residual stratum to estimate the symmetric-difference size
before choosing between direct sketch extraction, bucket localization, or frame
repair. The estimator is advisory: it MUST NOT override the frame check or the
128-bit residual verification of a decoded difference.

**Rejected Event Handling:**

Servers MUST include locally rejected event IDs as tombstones in `K`. If
rejected events were excluded, a fetch loop would occur: Server B sees that
Server A is "missing" an event, returns it in `/room_diff`, Server A fetches it
via `/room_events`, rejects it again, and the cycle repeats.

By including rejected event IDs in `K`, peers can converge to `Δ = 0` even when
they disagree about acceptance. If two servers have the same known-event set but
different accepted-event sets, the problem is an authorization, room-version, or
implementation disagreement rather than a data-sync failure. Rejected tombstones
SHOULD retain the event ID and rejection reason, not the full PDU. Until frame
negotiation defines synchronized frame advancement, rejected tombstones that are
inside any active reconciliation frame MUST NOT be garbage-collected.

This MSC does not provide a verdict-diff endpoint for `K_rejected` or
`K_softfailed`. Equal `algebraic_v1` digests mean equal known-event-ID sets, not
equal acceptance decisions. A server that accepted an event and a server that
rejected the same event with a retained tombstone both include the same event ID
in `K`, so that ID cancels from the reconciliation sketch. If operators need to
compare rejection or soft-failure status between servers, that is a diagnostic
API over per-event verdicts, not part of the set-reconciliation mechanism
defined here.

**Frames:**

The digest covers a frame: an agreed antichain of event IDs that bounds the
history being reconciled. Reconciliation repairs holes inside a frame. Backfill
extends the frame downward. Servers MUST NOT compare `algebraic_v1` digests
unless they agree on the frame. Full frame negotiation is left as future work in
this draft, but differing frames have a defined fallback: if two servers report
different frames, they MUST NOT compare the reported digests; they SHOULD use
`extremity` mode to discover a common anchor. If both servers hold one side's
later frame anchor, they MAY reconcile using that later frame. Implementations
SHOULD maintain resident accumulators keyed by frame anchor rather than only by
room ID, so the small number of common join generations in a room can be
compared without scanning the whole store.

MSC00DB bulk backfill is the complementary boundary-extension mechanism: its
`edges.oldest` response field is an antichain that can become the next frame
anchor after the returned historical segment is validated and ingested. This MSC
then reconciles holes above that anchor; MSC00DB fetches contiguous history to
move the anchor downward.

**Authorization:**

The requesting server MUST be a participant in the room (i.e., have at least one
joined member). The receiving server MUST verify this before responding. If the
requesting server is not in the room, the server MUST respond with HTTP 403 and
error code `M_FORBIDDEN`.

### Room diff: `POST /_matrix/federation/v1/room_diff/{roomId}`

Given a requesting server's event ID set (or a compact representation thereof),
returns the set of event IDs that the responding server has but the requester
likely does not. This is the "what am I missing?" query.

The endpoint supports two diff modes because Matrix federation produces two
fundamentally different classes of data loss:

- **Frontier lag ("clean" divergence).** A server goes offline, gets
  rate-limited, or falls behind. It misses a contiguous branch of events from
  the DAG tip. The server's extremities are stale, but its interior DAG is
  intact. This is identical to a Git branch that is behind upstream — the delta
  is a clean, linear range between the local and remote tips.

- **Interior gaps ("Swiss cheese" divergence).** A server drops random
  individual events due to rate limiting, rejection cascades, or auth chain
  fetch timeouts, but continues to receive subsequent events via state-resyncs.
  The server's extremities may match the remote server's, but its interior DAG
  has holes. This has no Git analogue — Git's content-addressable storage
  guarantees that possessing a commit implies possessing all ancestors.

The `extremity` mode is a **merge-base finder** (the Git approach) optimized for
frontier lag. It walks backward from divergent extremities to find the most
recent common ancestor, returning exactly the missing delta in O(delta) time.

The `sketch` mode is an exact **set reconciliation tool** over `K = Acc ∪ Rej`.
It ignores graph topology while extracting event identifiers in the symmetric
difference. Exact extraction matters in a causal DAG: a recovery set is
integrable only when it is downward-closed relative to what the requester
already has.

**Request:**

```http
POST /_matrix/federation/v1/room_diff/{roomId}
```

```json
{
  "mode": "extremity",
  "local_extremity_event_ids": ["$abc123", "$def456"],
  "have_event_ids": [
    "$known_depth_90000",
    "$known_depth_89500",
    "$known_depth_88000",
    "$known_depth_84000"
  ],
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
  "local_digest": "<base64url_16_byte_accumulator>",
  "digest_type": "algebraic_v1",
  "local_known_event_count": 81000,
  "frame_event_ids": ["$join_anchor"],
  "sketch_capacity": 64,
  "local_sketch": "<base64url_syndrome_sketch>",
  "buckets": null,
  "bucket_count": 256,
  "include_bucket_summary": false,
  "limit": 1000
}
```

**Fields (request):**

<!-- markdownlint-disable MD013 -->

| Field                       | Type     | Required          | Description                                                                                                                                               |
| --------------------------- | -------- | ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `mode`                      | string   | Yes               | One of `extremity` or `sketch`. Determines how the diff is computed.                                                                                      |
| `local_extremity_event_ids` | [string] | If mode=extremity | The requesting server's current forward extremities. Included in the `have` set for the merge-base walk.                                                  |
| `have_event_ids`            | [string] | If mode=extremity | A sparse sample of event IDs the requester already has, used as stop conditions for the merge-base walk. See below.                                       |
| `local_digest`              | string   | If mode=sketch    | The requesting server's 16-byte `algebraic_v1` accumulator for the negotiated frame.                                                                      |
| `digest_type`               | string   | If mode=sketch    | The digest algorithm used. MUST be `algebraic_v1` for this MSC.                                                                                           |
| `local_known_event_count`   | integer  | If mode=sketch    | The requesting server's known-event count for the negotiated frame.                                                                                       |
| `frame_event_ids`           | [string] | If mode=sketch    | The frame anchor antichain used for both the local digest and responder digest.                                                                           |
| `sketch_capacity`           | integer  | If mode=sketch    | Requested extraction capacity `k`. Unbucketed sketches MUST NOT exceed 64 on the wire.                                                                    |
| `local_sketch`              | string   | If mode=sketch    | Base64url-encoded syndrome sketch of the requester's known-event set for the requested frame, `sketch_capacity`, and optional bucket selection.           |
| `buckets`                   | [object] | No                | Bucket subset for localized sketch mode. Each entry has `bucket_id` in `0..255` and positive `capacity`. Entries MUST be sorted by ascending `bucket_id`. |
| `bucket_count`              | integer  | No                | Bucket count `b` for optional localization summaries. If present, MUST be 256 in this MSC.                                                                |
| `include_bucket_summary`    | bool     | No                | Whether the requester wants bucket accumulators and counts for two-sided localization. Default false.                                                     |
| `max_depth_delta`           | integer  | No                | Extremity mode only. Positive integer. The maximum topological depth distance the peer is allowed to walk. Default 5000, max 50000.                       |
| `max_events`                | integer  | No                | Extremity mode only. Positive integer. The maximum number of event IDs the peer is allowed to inspect before stopping. Default 10000, max 50000.          |
| `limit`                     | integer  | No                | Positive integer. Maximum number of event IDs to return. Default 1000, max 10000.                                                                         |

<!-- markdownlint-enable MD013 -->

**Response:**

```json
{
  "missing_event_ids": ["$ghi789", "$jkl012", "$mno345"],
  "requester_only_short_ids": ["base64url_8_byte_h64"],
  "expected_requester_side_accumulator": "<base64url_16_byte_accumulator>",
  "remote_known_event_count": 81247,
  "remote_extremity_event_ids": ["$abc123", "$pqr678"],
  "sketch_status": "decoded",
  "bucket_summary": null,
  "truncated": false
}
```

**Fields (response):**

<!-- markdownlint-disable MD013 -->

| Field                                 | Type     | Required | Description                                                                                                                                                                                                                                                     |
| ------------------------------------- | -------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `missing_event_ids`                   | [string] | Yes      | Event IDs that the responding server has but the requesting server does not. Returned IDs are exact in `sketch` mode when `sketch_status` is `decoded`, and in `extremity` mode when `truncated` is false; the list is complete only when `truncated` is false. |
| `requester_only_short_ids`            | [string] | No       | In `sketch` mode, decoded 8-byte `h_64` values that appear to be held only by the requester, encoded as unpadded base64url. This lets the requester verify and optionally push reverse repairs.                                                                 |
| `expected_requester_side_accumulator` | string   | No       | In `sketch` mode, `residual_digest XOR accumulator(responder_side)`, encoded as a 16-byte unpadded base64url value. The requester verifies this against the full event IDs it resolves from `requester_only_short_ids`.                                         |
| `remote_known_event_count`            | integer  | Yes      | The responding server's known-event count for the negotiated frame.                                                                                                                                                                                             |
| `remote_extremity_event_ids`          | [string] | Yes      | The responding server's current forward extremities.                                                                                                                                                                                                            |
| `sketch_status`                       | string   | No       | `decoded`, `capacity_exceeded`, or `not_applicable`. Present for `sketch` mode.                                                                                                                                                                                 |
| `bucket_summary`                      | object   | No       | Optional bucket accumulator/count summary for two-sided localization. Present only when requested and supported.                                                                                                                                                |
| `truncated`                           | bool     | Yes      | Whether the result is incomplete — because `limit` was reached, a walk bound was reached, or the bounding checks failed. See Handling Truncation.                                                                                                               |

<!-- markdownlint-enable MD013 -->

**Diff Computation — Mode Selection:**

Servers SHOULD select the diff mode based on the `room_digest` comparison:

- If the remote digest and `known_event_count` both match for the same frame,
  the peers are synchronized for that frame and no diff is needed.
- If the remote server's `extremity_event_ids` contain event IDs the local
  server does not recognize, the requester MAY use `extremity` mode to discover
  the repair frontier, but MUST treat `truncated: true` as non-repair progress
  until the walk reaches known ancestry.
- Otherwise, or after frontier repair, use `sketch` mode. Provision
  `sketch_capacity` from the count residual
  `c = abs(local_known_event_count - remote_known_event_count)`: in the common
  one-sided lag case, `c` equals the exact difference size. Add headroom for
  concurrently arriving events, for example
  `ceil(1.5 * c) + 4 + ceil(observed_event_rate_per_second * estimated_RTT)`.
  The requester sends its own syndrome sketch at that capacity so the responder
  can subtract and decode the residual. If decode fails, retry with a larger
  capacity or request `bucket_summary` to localize a two-sided difference.

**Diff Computation — `extremity` mode:**

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
bounded by two optional request parameters: `max_depth_delta` (the maximum
topological depth distance the responder may walk) and `max_events` (the maximum
number of event IDs it may inspect). If either field is omitted, the responder
MUST apply the default from the request field table. A responder MUST stop as
soon as any bound is reached and report `truncated: true` rather than silently
escalating to deeper history traversal. Reconciliation is allowed to be
incomplete, but it MUST NEVER become unbounded.

The responding server computes the diff as follows:

1. Build the `have` set: the union of `local_extremity_event_ids` and
   `have_event_ids`. These represent events the requester already possesses. The
   combined `have` set MUST NOT exceed 256 entries; requests exceeding this MUST
   be rejected with HTTP 400.
2. **Pre-flight validation:** Look up which `have` events exist in the local
   store (a batch of point lookups). If zero `have` events are recognized,
   immediately return an empty result with `truncated: true` rather than walking
   the DAG. This prevents a malicious requester from forcing a maximal walk by
   sending fabricated `have` event IDs that don't exist in the responder's DAG.
3. **Topological bounding check (O(1)):** Compute
   `delta = local_extremity_depth - max(local_depth_of_valid_have_events)`,
   where `local_extremity_depth` is the maximum depth of the responder's own
   forward extremities. If `delta > max_depth_delta`, return an empty result
   with `truncated: true`. This guarantees the responder only ever walks
   bounded, recent history.
4. Identify forward extremities the responder has that are NOT in the `have` set
   — these are the "want" events (tips unknown to the requester).
5. Walk backwards from those unknown tips via `prev_events`, collecting event
   IDs not in the `have` set.
6. **Stop conditions:** For each branch of the walk, stop when it reaches an
   event ID that IS in the `have` set (the merge-base for that branch — events
   at or before it are excluded from the result, as the requester already has
   them), or when the branch's depth falls more than `max_depth_delta` below
   `local_extremity_depth`.
7. **Safety limit:** If the walk inspects `max_events` event IDs before all
   branches terminate, it MUST stop and the response MUST set `truncated: true`.
8. Return the collected event IDs in reverse topological order, up to `limit`.

Servers MUST reject zero, negative, non-integer, or over-cap `max_depth_delta`,
`max_events`, and `limit` values with HTTP 400. Servers MUST enforce
`max_depth_delta <= 50000`, `max_events <= 50000`, and `limit <= 10000`. Servers
SHOULD also maintain per-peer, per-room accounting of inspected events over a
rolling window (for example, 60 seconds) and reject requests that would exceed a
cumulative budget (RECOMMENDED: 100,000 inspected events per peer per room per
minute). The cumulative budget prevents an attacker from issuing many small
requests that each walk just under the per-request limit.

**Handling Truncation (requesting server):**

On `truncated: true`, the requester MUST NOT immediately retry an identical
request. If the response is non-empty, the requester SHOULD fetch and persist
the returned events, then re-run the diff with an updated `have` sample. If
progress stalls or the response is empty, the bounds were insufficient: the
requester MAY retry with larger `max_depth_delta`/`max_events` (up to the caps),
and otherwise SHOULD fall back to `sketch` mode or existing `/backfill`,
applying back-off between attempts.

**Constructing the `have` set (requesting server):**

The requesting server constructs `have_event_ids` as a sparse,
exponentially-spaced sample of event IDs it already possesses, working backwards
from its extremities:

1. Start from each local extremity and walk backwards via `prev_events`.
2. Sample event IDs at exponentially increasing depth intervals: the first
   event, then 1 step back, 2 steps, 4 steps, 8 steps, 16 steps, etc.
3. Stop sampling after 32 samples per extremity, or when the walk reaches the
   room's create event.

This produces approximately 32×E event IDs (where E is the number of
extremities, typically 1–5), totaling 32–160 event IDs. The exponential spacing
ensures:

- Dense coverage near the frontier (where divergence is most likely)
- Sparse coverage deep in the DAG (where both servers are likely synchronized)
- O(log N) total samples for a DAG of depth N
- The responder is highly likely to find a merge-base within the first few
  hundred events of its backward walk, making the algorithm O(delta) in practice
  — proportional to the number of missing events, not the total room size

**Diff Computation — `sketch` mode:**

In `sketch` mode, the responding server:

1. Validates that `digest_type` is `algebraic_v1`, `local_digest` is exactly 16
   decoded bytes, `sketch_capacity` is positive and within the cap, and the
   request frame matches the responder's digest frame. If `buckets` is absent or
   null, `sketch_capacity` MUST NOT exceed 64 on the wire. If `buckets` is
   present, the sketch is computed only over those buckets, in ascending
   `bucket_id` order. The sum of bucket capacities MUST NOT exceed 4096 unless a
   future profile raises the cap.
2. Computes the residual accumulator:
   `residual_digest = remote_digest XOR local_digest`.
3. Computes the count residual:
   `c = abs(remote_known_event_count - local_known_event_count)`. If
   `residual_digest` is zero and `c` is zero, the server returns an empty
   decoded response.
4. Validates that `local_sketch` has length exactly `8 * sketch_capacity` bytes
   for an unbucketed sketch, or the sum of `8 * bucket_capacity` over the
   requested buckets in localized bucket mode.
5. Produces a matching syndrome sketch over the responder's known-event set for
   the requested frame. In unbucketed mode, the sketch has capacity
   `sketch_capacity`. In bucket mode, it is the concatenation of one sketch per
   requested bucket, each with that bucket's requested capacity, in ascending
   `bucket_id` order. Each sketch operates over 64-bit short identifiers
   `h_64(e)`.
6. Subtracts the requester's sketch from the responder's sketch and attempts to
   decode the symmetric difference as 64-bit short identifiers.
7. Partitions the decoded short identifiers into `responder_side` and
   `requester_side`. For `responder_side`, the responder resolves each short ID
   to a full event ID it holds and computes the 128-bit accumulator over those
   full event IDs. For `requester_side`, the responder returns only the short
   IDs because it cannot compute the corresponding 128-bit event-ID accumulator
   without the full event IDs.
8. Returns responder-side full IDs as `missing_event_ids`, requester-side short
   IDs as `requester_only_short_ids`, and
   `expected_requester_side_accumulator = residual_digest XOR accumulator(responder_side)`.
   The requester resolves the short IDs it holds, verifies that their 128-bit
   accumulator matches `expected_requester_side_accumulator`, and MAY use those
   events as reverse repair candidates for the responder.
9. If the sketch capacity is exceeded, the response sets
   `sketch_status: "capacity_exceeded"` and `truncated: true`.

The BCH/PinSketch decoding details are intentionally profiled by the
`algebraic_v1` digest type rather than restated as a new primitive here:
syndrome coordinates are
`sigma_k(S) = (sum h(e), sum h(e)^3, ..., sum h(e)^(2k-1))` over the
minisketch-compatible 64-bit binary field. Even powers are omitted because
Frobenius makes them redundant in characteristic 2. Future profiles MAY define a
rateless IBLT encoding for large or heavy-tailed differences.

If `include_bucket_summary` is true, the responder also returns a bucket summary
using `bucket_count = 256`. Each bucket is selected by the leading 8 bits of
`h_64(e)` and contains a 128-bit bucket accumulator plus a 24-bit count. Bucket
summaries are used only after the count residual is zero or a direct sketch
decode fails, both of which indicate a two-sided difference. They are not sent
on the common one-sided lag path. After receiving a bucket summary, a requester
MAY issue another `sketch` request with `buckets` limited to the differing
buckets and with per-bucket capacities derived from the bucket count residuals.

**Causal closure and truncation:**

A recovery set can be integrated iff it is downward-closed relative to the
requester's existing store. Exact set recovery satisfies this for the whole
difference. A truncated backward walk from the responder's frontier does not: it
collects descendants before ancestors, so its minimal returned events may still
have parents in neither the requester store nor the returned set. Such a walk
can transfer useful bytes, but it does not repair the DAG until it reaches known
ancestry. Responders MUST therefore report `truncated: true`, and requesters
MUST NOT treat a truncated walk as resolving the gap.

**Authorization:**

Same as `room_digest` — the requesting server MUST be a participant in the room.

### Membership divergence handling

If a request is rejected because the responding server and requesting server
disagree about room membership, the rejection MUST be distinguishable from a
generic authorization failure. A plain `M_FORBIDDEN` deadlocks recovery: the
requester cannot learn which membership event it needs in order to reconcile the
membership view that caused the denial.

In that case, the responding server SHOULD return a structured error body that
names the membership event IDs it used to make the decision, limited to events
about the requesting server's own membership or other authorization-relevant
events already known to the requester. A suggested shape is:

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
    ]
}
```

**Fields (response):**

<!-- markdownlint-disable MD013 -->

| Field               | Type     | Required | Description                                                                                                                                      |
| ------------------- | -------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `events`            | [PDU]    | Yes      | The requested events, in topological order (dependencies before dependants). Each event is a full, signed PDU.                                   |
| `auth_chain_events` | [PDU]    | Yes      | Auth chain events for the returned events that are not in the `events` list. Also in topological order. Empty if `include_auth_chain` was false. |
| `missing_event_ids` | [string] | Yes      | Event IDs from the request that the responding server does not have.                                                                             |

<!-- markdownlint-enable MD013 -->

**Event Ordering:**

Events in both `events` and `auth_chain_events` MUST be returned in topological
order such that for any event E, all events referenced by E's `auth_events` and
`prev_events` appear earlier in the combined list (auth_chain_events
concatenated with events). This allows the requesting server to process events
in a single pass without dependency resolution.

**Authorization:**

Same as `room_digest`. Additionally, the responding server MUST NOT return
events that the requesting server would not be allowed to see (e.g., events sent
after the requesting server's last member left the room, per existing history
visibility rules).

### Reconciliation protocol

The full reconciliation flow between two servers is:

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
         │    sketch_capacity: 64,                 │
         │    local_sketch: "..." }                │
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

**Short-circuit optimization:** If the `room_digest` response shows an identical
`digest` and `known_event_count` for the same frame, the requesting server MAY
skip the diff and event fetch phases entirely. This is the common no-difference
path and costs 16 accumulator bytes plus the count field.

#### Gossip scheduling

Servers SHOULD implement periodic gossip-based reconciliation for active rooms.
To prevent cluster-wide "thundering herd" reconciliation waves after large
homeserver restarts or network partition recovery, implementations MUST apply a
randomized jitter of ±15% to all scheduling intervals, including backed-off
intervals. The recommended strategy is:

1. **Trigger-based gossip:** When a server detects potential divergence (e.g., a
   state resolution produces an unexpected result, or a received event
   references unknown `prev_events`), it SHOULD immediately initiate
   reconciliation with the event's origin server.

2. **Periodic anti-entropy:** Servers SHOULD periodically select a random subset
   of active rooms and a random peer for each, and perform the digest comparison
   phase. The recommended interval is:
   - Every 60 seconds for rooms with recent activity (events in the last 5
     minutes)
   - Every 300 seconds for rooms with moderate activity (events in the last
     hour)
   - Every 3600 seconds for idle rooms

3. **Peer selection:** For each reconciliation round, the server SHOULD select
   at least `f = 3` peers for active rooms unless local policy requires a lower
   rate. Peers SHOULD be selected using a weighted random strategy with a
   uniform floor: `Pr(select i) = (1 - epsilon) * weight_i + epsilon / N`.
   Implementations SHOULD use `epsilon >= 0.05` unless local policy has a
   measured reason to choose a different security/efficiency point. The weighted
   term prefers:
   - Servers that originated the most recent events (most likely to be ahead)
   - Servers that previously returned divergent digests (known to have different
     data)
   - Backbone/hub servers with high availability (most likely to have complete
     DAGs)

   The uniform floor is not just a fairness heuristic: it is the condition that
   prevents hub-weighted peer choice from becoming an eclipse surface. If `p` is
   the probability that one selected peer is adversarial, then a round with
   fanout `f` is eclipsed with probability `p^f`, `E[T] = 1 / (1 - p^f)` rounds
   are expected before at least one honest peer is sampled, and
   `Pr[T > tau] = p^(f * tau)`. With `epsilon = 0.05`, adversarial weight near
   1, Sybil fraction `mu = 0.1`, and `f = 1`, there is about a 63% chance of
   exceeding 10 rounds; at `f = 3`, about 25%. The algebraic level-0 digest
   makes this fanout affordable.

4. **Back-off:** If a peer returns identical digests (no divergence) across 3
   consecutive polls, the server SHOULD exponentially back off the
   reconciliation interval for that peer/room pair, up to a maximum of 24 hours.
   Any new event received in the room resets the back-off.

#### `ETag` optimization (for digest polling)

To minimize bandwidth for digest polling, the `room_digest` endpoint supports
conditional requests:

**Request with ETag:**

```http
GET /_matrix/federation/v1/room_digest/{roomId}
If-None-Match: "algv1:abc123def456"
```

**Response (no change):**

```http
HTTP/1.1 304 Not Modified
ETag: "algv1:abc123def456"
```

The ETag is derived from the incrementally maintained level-0 accumulator and
current extremity frontier:

```text
unpadded_base64url(digest || implementation_defined_frontier_hash[0:8])
```

- `digest` is the 16-byte `algebraic_v1` accumulator over `K` for the frame.
- The `sorted(extremity_event_ids)` component is defense-in-depth: even if two
  different known-event sets collide in the accumulator, differing frontiers
  still change the ETag.

The frontier hash is implementation-defined because ETags are opaque and are
only compared against the same responder's later `If-None-Match` value. Servers
that want byte-stable behavior across implementations SHOULD use Matrix
canonical JSON over bytewise-sorted `extremity_event_ids`, hashed with SHA-256,
and truncate to the first 8 bytes.

If both components match, the two event sets are identical except with
negligible probability (an accidental collision of XORed 128-bit hashes). The
ETag is a cache-validation hint, not a synchronization guarantee: it only means
the responder's view of the room has not changed since the requester last
observed it. This statement is about accidental collisions only; malicious peers
are covered by the Accumulator forgery security consideration. A `304` does not
imply the two servers agree. The server evaluates the conditional request in
O(E), where E is the number of extremities (typically 1–5), using the
incrementally maintained accumulator — without touching the event store or
computing a set sketch.

If the computed ETag matches the `If-None-Match` header, the server MUST return
HTTP 304 with no body. This reduces the reconciliation polling cost to a single
HTTP round-trip with a ~50 byte response for rooms that are already
synchronized.

Requesting servers SHOULD cache the peer's ETag together with their own local
room accumulator at the time of caching. They MUST NOT send `If-None-Match` if
their own accumulator has changed since the ETag was cached or if the most
recent reconciliation round for that peer and room terminated with
`truncated: true`. To avoid indefinite stalling on a quiescent peer, the
requesting server MUST force an unconditional digest comparison at least once
every 16 consecutive `304` responses per peer and room.

## Resident structure

To make `sketch` mode deployable, implementations SHOULD maintain a resident
per-room syndrome structure:

<!-- markdownlint-disable MD013 -->

| Layer                               | Width             | Size   | Purpose                                      |
| ----------------------------------- | ----------------- | ------ | -------------------------------------------- |
| Integrity accumulator               | 128 bits          | 16 B   | ETag, level-0 agreement, decode verification |
| Bucket accumulators                 | 128 bits × 256    | 4 KiB  | two-sided localization, fault detection      |
| Bucket counts                       | 24 bits × 256     | 768 B  | count residuals and provisioning             |
| Bucket syndromes `s1` through `s15` | 64 bits × 8 × 256 | 16 KiB | fast-path extraction                         |
| Strata estimator                    | 64 bits × 8 × 32  | 2 KiB  | pre-decode difference estimation             |

<!-- markdownlint-enable MD013 -->

Total resident state is approximately 23 KiB per active room. On persisting or
purging event `e`, compute `x = h_64(e)`, choose the bucket from its leading 8
bits, choose the estimator stratum from `x.trailing_zeros()`, compute `x^2`
once, and update `x, x^3, ..., x^15` by repeated multiplication by `x^2`. In
characteristic 2, insertion and removal are the same XOR operation. The
reference implementation measures about 618 ns per resident update with the
portable multiply and about 52 ns with PCLMULQDQ on the benchmarked x86-64
machine; the underlying `GF(2^64)` multiply measured about 77.25 ns portable and
6.50 ns with PCLMULQDQ, with bit-identical results. Either path is small
relative to the database write needed to persist the event.

The resident 64-bit syndrome layer is an optimization. Any decoded difference
MUST be checked against the 128-bit accumulator before the result is trusted.
The 128-bit accumulators are not binding commitments against malicious peers:
because XOR is linear over `GF(2)`, an adversary with event-authoring freedom
can construct nonempty zero-sum subsets by linear algebra. They are still useful
for detecting accidental divergence and bad decodes. Deployments that need
adversarial robustness SHOULD compute transmitted 64-bit sketches with a
per-link salt over the already-localized buckets, while keeping the resident
128-bit accumulators unsalted and shared across peers.

## Potential issues

### Performance resilience

As with any federation endpoint, execution time and resource usage are concerns.

- **Digest computation cost:** The level-0 accumulator and resident bucket
  summaries are maintained incrementally when events are persisted or purged.
  Implementations that do not maintain the resident structure may need to scan
  room history to answer `sketch` requests and SHOULD apply stricter rate
  limits.

- **Diff amplification:** A malicious requester can overstate `sketch_capacity`,
  request bucket summaries repeatedly, or ask for large `limit` values. Servers
  MUST cap unbucketed `sketch_capacity` at 64, MUST reject bucketed requests
  whose aggregate capacity exceeds 4096, and MUST cap `limit`, response bytes,
  and per-peer CPU time.

- **Bulk fetch abuse:** The `room_events` endpoint returns full PDUs, which
  could be large. The 500-event-per-request cap and standard federation rate
  limiting mitigate this.

### Frame negotiation

This MSC defines frame-bound reconciliation but does not fully specify frame
negotiation. The natural frame anchor is the join-point antichain shared by the
two servers, but repeated leave/rejoin cycles, redaction, retention policies,
and history purging can make frame advancement non-trivial. Servers MUST NOT
compare digests across different frames.

### Consistency in active/hot rooms

If a room is actively receiving events during reconciliation, the
digest/diff/fetch sequence may return stale data. This is acceptable — gossip
protocols are inherently eventually consistent, and the next reconciliation
round will catch up. Servers MUST NOT block event processing during
reconciliation.

### Interaction with "Partial State" joins

Servers in the process of a partial state join (MSC3706) SHOULD NOT initiate
reconciliation for that room until the full state resync is complete. They MAY
respond to incoming reconciliation requests with the events they have, but MUST
NOT advertise a normal `algebraic_v1` digest for the fully joined frame. They
SHOULD either omit `tk.nutra.msc0501.digest.algebraic_v1` for that room, return
HTTP 409 with `M_PARTIAL_STATE`, or advertise a distinct partial-state frame and
set a response header `X-Matrix-Partial-State: true` to indicate that their
digest/diff is incomplete.

## Alternatives

### Using `/make_join` as a reconciliation probe

An alternative approach is to abuse the existing `/make_join` endpoint as a
zero-mutation DAG probe. By calling `/make_join` with a throwaway user ID, a
server can obtain the remote server's current `prev_events` (DAG tips) and
`auth_events` without performing any mutations.

This approach has the advantage of requiring no spec changes. However:

1. It only reveals extremity divergence, not interior gaps (events missing from
   the middle of the DAG)
2. It creates spurious `make_join` traffic that obscures real join attempts in
   server logs
3. It does not scale — there is no ETag/conditional-request support, and the
   response includes a full PDU template that must be serialized and discarded
4. It abuses an endpoint designed for a different purpose, creating confusion
   about intent

The gossip reconciliation protocol proposed here addresses all of these
limitations while remaining lightweight enough for periodic polling.

### Full Merkle tree synchronization

A more sophisticated design would use a full Merkle tree over the event ID
space, similar to the anti-entropy repair schemes used by some distributed
databases. In that model, each server would maintain a persistent tree whose
leaves are event IDs and whose internal nodes commit to child hashes. Two
servers could then compare roots and recursively descend into divergent
subtrees.

That design was rejected for this MSC because Matrix room reconciliation is a
graph repair problem, not just a set-membership problem. A Merkle tree can tell
the peers which event IDs differ, but it does not preserve the DAG structure
needed to understand how a missing event attaches to the room history, what its
`prev_events` are, or which auth-chain/state-resolution inputs are relevant.
After a Merkle comparison identifies a missing event, the protocol would still
need a graph walk to recover the topology around it.

This approach was also rejected because:

1. It requires persistent auxiliary state that must be maintained, indexed, and
   recovered across restarts.
2. Every new event would need to update the tree, adding write amplification and
   more contention to already I/O-bound homeservers.
3. The protocol would become interactive and multi-round even for the common
   case of small divergences.
4. The proposed algebraic sketch already captures the common-case benefit of
   quickly extracting small differences, without mandating a full room-level
   Merkle structure.
5. Merkle-based reconciliation can still be introduced later as a separate
   `digest_type` or related optimization if there is a strong need for it.

### Why not Bloom filters?

Bloom filters were rejected for the baseline because they are not group-valued
digests. A Bloom filter can test membership, but two peers cannot subtract their
filters to compute the residual difference. Operationally this means:

1. The responder must test its own event set element-by-element, so cost scales
   with the number of events tested rather than the number of missing events.
2. False positives are silent in the reconciliation direction: the responder
   concludes that the requester already has an event and does not send it.
3. A larger or re-salted filter does not extend a previous exchange; it restarts
   the exchange over the same event population.
4. A windowed filter creates absorbing holes: once a silently masked event falls
   outside the window, no later Bloom round can find it, while the full-frame
   accumulator continues to detect divergence forever.

These are algebraic consequences of using an idempotent monoid rather than a
group. They are not tuning problems.

### Why not make RIBLT mandatory?

Invertible Bloom Lookup Tables (IBLTs) are an attractive alternative because
they can recover missing event IDs directly from the digest exchange when the
set difference is small, and rateless IBLTs remove the need to choose capacity
up front. They are in the same group-valued family as the syndrome sketch, and
future profiles MAY define a rateless encoding for large or heavy-tailed
differences.

This MSC keeps BCH/PinSketch-style syndromes as the baseline because they are
compact, additive, easy to extend, and can be maintained in the resident bucket
array for the small one-sided differences expected to dominate normal federation
repair.

### Server-initiated push reconciliation

Instead of pull-based reconciliation, servers could proactively push digests to
peers when their DAG advances (rumor-mongering). This was rejected because:

1. It creates O(servers²) traffic in active rooms
2. It requires all servers to process incoming digests even when they are
   already synchronized
3. Pull-based reconciliation naturally rate-limits itself — a server only
   reconciles when it chooses to, and only with one peer at a time

## Security considerations

### Information disclosure

The `room_digest` endpoint reveals metadata about a server's event store: event
count, depth range, timestamp range, and forward extremities. This metadata
could be used to fingerprint server implementations or estimate room activity
patterns. However:

- This information is already implicitly available through existing endpoints
  (`/state_ids`, `/backfill`, `/event`)
- Access is restricted to servers that are participants in the room
- The level-0 accumulator does not reveal individual event IDs; optional bucket
  summaries reveal only coarse bucket counts and accumulator differences.

### Denial of service

The reconciliation endpoints add new attack surface for resource exhaustion.
Mitigations:

- **Rate limiting:** Servers MUST apply per-peer, per-room rate limiting to all
  three endpoints. Recommended: 1 request per 10 seconds per room per peer for
  `room_digest` and `room_diff`; 1 request per 30 seconds for `room_events`.
- **Digest caching:** The level-0 accumulator and resident bucket summaries
  SHOULD be maintained incrementally. Implementations that compute sketches by
  scanning the event store SHOULD use stricter request budgets.
- **Response caps:** The `limit` parameter on `room_diff` and the 500-event cap
  on `room_events` bound the maximum response size.

### Replay and "poisoning"

A malicious server could return fabricated events in `room_events` responses.
This is mitigated by the same mechanisms that protect existing federation
endpoints:

- All returned PDUs MUST have valid signatures from their origin servers
- All returned PDUs MUST pass hash verification (event ID = hash of content)
- The requesting server MUST apply standard auth checks before persisting events
- Events that fail any of these checks MUST be discarded without affecting local
  state

### Amplification attacks via oversized sketches

A malicious requesting server could request excessive `sketch_capacity`, large
bucket summaries, or repeated high-`limit` diffs. The `limit` parameter caps the
number of event IDs returned. Servers MUST reject unbucketed `sketch_capacity`
values above 64, bucketed requests whose aggregate capacity exceeds 4096,
oversized decoded responses, oversized bucket summaries, and requests that
exceed per-peer or per-room CPU budgets. Servers SHOULD reject requests whose
`local_known_event_count` is grossly inconsistent with the supplied accumulator
history or negotiated frame.

### Depth manipulation

The topological bounding checks rely on event depth, which is derived from
attacker-influenced event content. When evaluating `max_depth_delta`, servers
SHOULD use their locally computed topological ordering (e.g., stream ordering or
recomputed depth) rather than trusting the `depth` field of received events.

### Accumulator forgery

XOR accumulators are fault-detecting, not authenticators. A malicious peer with
control over which event IDs to include can target accumulator collisions with
linear algebra over `GF(2)`: any set of 129 128-bit values is linearly
dependent, so the attacker can construct a nonempty subset whose accumulator is
zero. Nothing in this MSC relies on the accumulator being binding against such a
peer. It is an integrity anchor for accidental decode failure and benign desync,
while returned PDUs still MUST be verified by event ID, hashes, signatures, and
authorization rules. Deployments that need transferable accumulator evidence MAY
use an LtHash-style root accumulator profile in a future `digest_type`; MSC4511
uses Ed25519-signed overlay attestations for responder accountability.

### Interaction with server ACLs

Servers MUST respect `m.room.server_acl` when responding to reconciliation
requests. If the requesting server is denied by the room's ACL, the responding
server MUST return HTTP 403 with error code `M_FORBIDDEN`, identical to the
behavior for other federation endpoints.

## Unstable prefix

The following mapping will be used for identifiers in this MSC during
development:

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

This MSC has no hard dependencies on other unaccepted MSCs.

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
- MSC0502 (Federation EDU state reconciliation) — the ephemeral-state
  counterpart to this proposal, using version-vector comparison instead of graph
  reconciliation
