# MSC0501: Gossip-based room federation PDU reconciliation

Matrix federation is "push and hope" — servers send events via `/send`
transactions and assume delivery. When delivery fails (rate limiting, network
partitions, spam storms, rejection cascades), the DAG develops permanent holes.
Each server drops a different random subset of events, creating unique "swiss
cheese" patterns that no existing endpoint can efficiently detect or repair.

This proposal introduces a lightweight, gossip-inspired reconciliation protocol
that allows federated servers to efficiently detect DAG divergence and
surgically heal data gaps without requiring full state synchronization or new
room versions.

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

| Endpoint                            | Limitation                                                                                                                    |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `GET /backfill/{roomId}`            | Depth-ordered linear walk; cannot target specific gaps; useless for missing events in the middle of the DAG                   |
| `POST /get_missing_events/{roomId}` | BFS walk with a hard depth limit (default 10); cannot bridge gaps larger than 10 events; requires knowing the boundary events |
| `GET /state_ids/{roomId}`           | Returns state event IDs only (not timeline events); O(N) comparison; no incremental diffing                                   |
| `GET /event/{eventId}`              | Single-event fetch; no bulk mode; requires knowing which events are missing                                                   |
| `GET /make_join`                    | Does not meet latency requirements (20-100 ms); requests to lagging server can timeout (full index scan for unknown event)    |

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
    "org.matrix.msc0501.reconciliation": true,
    "org.matrix.msc0501.digest.xxh3_bloom": true
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
  "digest": "<opaque_base64_string>",
  "digest_type": "xxh3_bloom",
  "digest_salt": "<base64url_8_byte_salt>",
  "digest_bits": 32768,
  "digest_depth_floor": 93000,
  "digest_window": 5000,
  "window_event_count": 5000,
  "event_count": 81247,
  "extremity_event_ids": ["$abc123", "$def456"],
  "depth_range": [1, 93841],
  "origin_server_ts_range": [1609459200000, 1716000000000]
}
```

**Fields:**

| Field                    | Type               | Required | Description                                                                                                                             |
| ------------------------ | ------------------ | -------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `digest`                 | string             | Yes      | Base64url-encoded digest of the server's event ID set for this room. See Digest Construction below.                                     |
| `digest_type`            | string             | Yes      | The algorithm used to construct the digest. Servers MUST support `xxh3_bloom`.                                                          |
| `digest_salt`            | string             | Yes      | Base64url-encoded 8-byte salt used when constructing the Bloom filter.                                                                  |
| `digest_bits`            | integer            | Yes      | The bit-length of the Bloom filter. The server dynamically sizes this; see Digest Construction.                                         |
| `digest_depth_floor`     | integer            | Yes      | The minimum topological depth included in the active window.                                                                            |
| `digest_window`          | integer            | Yes      | The number of most-recent events (by topological depth) included in the digest. See Active Window.                                      |
| `window_event_count`     | integer            | Yes      | The number of events actually included in the active window at `digest_depth_floor`.                                                    |
| `event_count`            | integer            | Yes      | The total number of non-outlier events the server holds for this room (including locally rejected events; see Rejected Event Handling). |
| `extremity_event_ids`    | [string]           | Yes      | The server's current forward extremities (DAG tips) for this room.                                                                      |
| `depth_range`            | [integer, integer] | Yes      | The minimum and maximum topological depth of events held.                                                                               |
| `origin_server_ts_range` | [integer, integer] | Yes      | The earliest and latest `origin_server_ts` of events held.                                                                              |

**Digest construction (`xxh3_bloom`):**

The digest is a dynamically-sized Bloom filter constructed as follows:

1. **Determine the Active Window.** Select a depth floor `D` such that the
   events with locally computed topological depth `>= D` cover approximately the
   requested window size `W` (default `W = 5000`). The active window is the set
   of events at or above `D`, including locally rejected events; see Rejected
   Event Handling below. The server reports `D` in `digest_depth_floor` and the
   resulting event count in `window_event_count`. Hashing the entire event
   history is unnecessary because the bottom of the DAG (old history) rarely
   mutates — divergence almost always occurs at the frontier.
2. **Size the filter.** Allocate `m` bits where `m` is the next power of two
   greater than or equal to `ceil(W * 6.235)` (approximately 6.235 bits per
   element), which yields a false-positive rate of ~5% with `k = 4` hash
   functions. Standardizing `m` as a power of two is a strict requirement to
   enable in-place dynamic folding (see
   [Dynamic Filter Folding](#dynamic-filter-folding)). For the default window of
   5000 events, `ceil(5000 * 6.235) = 31,175`, so the server allocates
   `m = 32,768` bits (exactly 4,096 bytes or 4.0 KB). The server reports this
   value in the `digest_bits` field.
3. **Select a salt.** The party constructing the filter selects an 8-byte salt.
   For the `room_digest` endpoint the salt MAY be fixed per cache generation.
   For `room_diff` in `bloom` mode the requesting server MUST generate a fresh,
   unpredictable salt for every request. The salt is transmitted alongside the
   filter as `digest_salt` (unpadded base64url).
4. **Populate the filter.** For each event ID in the active window, compute
   `H = XXH3-128(digest_salt || utf8(event_id))` with seed 0, where `||` is byte
   concatenation. Let `h1` be the low 64 bits of `H` and `h2` be the high 64
   bits, both as unsigned 64-bit integers.
5. Derive `k = 4` bit positions using Kirsch–Mitzenmacher double hashing:
   `position_i = ((h1 + i * h2) mod 2^64) mod m` for `i` in `{0, 1, 2, 3}`. Both
   the addition and the multiplication are performed modulo `2^64` (wrapping, as
   in unsigned 64-bit arithmetic). Because `m` is a power of two, the final
   reduction is equivalent to `& (m - 1)`.
6. Set each `position_i` in the filter. Bit `p` is located at byte index
   `floor(p / 8)`, at bit offset `p mod 8` counted from the least significant
   bit of that byte (`byte[p >> 3] |= 1 << (p & 7)`).
7. Base64url-encode the resulting byte array (unpadded).

The key mathematical constraint is:

> `m = -n * ln(p) / (ln(2))^2`
>
> For `n = 5000` events and `p = 0.05` (5% false positive rate):
> `m = 31,175 bits ≈ 3.8 KB`
>
> For `n = 10000` events and `p = 0.05`: `m = 62,350 bits ≈ 7.6 KB`

Servers MAY adjust the window size and filter dimensions, but MUST NOT advertise
`digest_bits` greater than `2^23` (1 MiB). A requesting server can infer the
filter parameters from the `digest_bits` and `digest_window` fields in the
response. Two servers with different window sizes can still detect divergence —
if their windows overlap, bit differences in the overlapping region indicate
missing events. The active window is a predicate over `digest_depth_floor`, not
an ordering over raw receipt time, so both peers can evaluate it against their
own stores without relying on locally observed arrival order.

The Bloom filter gives O(1) equality comparison, approximate difference
estimation (for example, the popcount of `remote AND NOT local` correlates with
the number of events the local server is missing from the window), and a compact
~4 KB representation regardless of total room size.

**Rejected Event Handling:**

Servers MUST include locally rejected events in the Bloom filter digest. If
rejected events were excluded, a fetch loop would occur: Server B sees that
Server A is "missing" an event (because A excluded it from the filter), returns
it in `/room_diff`, Server A fetches it via `/room_events`, rejects it again,
and the cycle repeats on the next gossip interval.

By including rejected event IDs in the filter, Server B's membership test
returns positive and the event is correctly skipped. This does not affect the
security model — rejected events are only included in the _digest_, not in the
_resolved state_. Additionally, servers MUST maintain a negative cache of event
IDs that were fetched via reconciliation and subsequently rejected. Events in
the negative cache MUST NOT be re-requested for a configurable cooldown period
(RECOMMENDED: 24 hours). This provides defense-in-depth against fetch loops if a
peer reports the same rejected event again because of stale state, inconsistent
filter parameters, or implementation error.

#### Dynamic filter "folding"

To allow comparison of Bloom filters of different sizes (e.g., if Server A uses
$W_a = 5000$, resulting in $m_a = 32,768$ bits, and Server B uses $W_b = 10000$,
resulting in $m_b = 65,536$ bits) without re-hashing raw event IDs,
implementations MUST support dynamic filter folding.

Because $m$ is strictly constrained to be a power of two, a larger Bloom filter
of size $2m$ can be folded in half to match a target size $m$ simply by dividing
the bit-array into two equal halves and performing a bitwise `OR` operation on
them: `folded[i] = filter[i] | filter[i + m]` (indexing in bits, or equivalently
over the byte array with an `m/8` byte offset)

Folding preserves the underlying bit positions, but it does not preserve the
false-positive rate. Each halving approximately doubles the fill ratio, and the
false-positive rate grows super-linearly in fill. Folding is therefore only
appropriate for filter-to-filter comparison and other non-membership uses such
as `popcount(remote AND NOT local)`. It MUST NOT be used to claim that a folded
filter is an equivalent membership-testing filter.

Servers MUST NOT fold a filter by more than one halving, and MUST treat
difference estimates derived from a folded filter as an order-of-magnitude hint
only. Where the two peers' `digest_bits` differ by more than 2×, the comparison
MUST be skipped and the protocol MUST proceed directly to `bloom` mode using
each side's native parameters.

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

The `bloom` mode is a **set reconciliation tool** (the Cassandra approach) that
ignores graph topology entirely and checks raw event set membership. It detects
interior gaps that the merge-base finder is structurally blind to.

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
  "local_event_count": 81000,
  "max_depth_delta": 5000,
  "max_events": 10000,
  "limit": 1000
}
```

**Or, in `bloom` mode:**

```json
{
  "mode": "bloom",
  "local_digest": "<base64_bloom_filter>",
  "digest_type": "xxh3_bloom",
  "digest_salt": "<base64url_8_byte_salt>",
  "digest_bits": 32768,
  "digest_depth_floor": 93000,
  "digest_window": 5000,
  "local_event_count": 81000,
  "limit": 1000
}
```

**Fields (request):**

| Field                       | Type     | Required          | Description                                                                                                                                      |
| --------------------------- | -------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `mode`                      | string   | Yes               | One of `extremity` or `bloom`. Determines how the diff is computed.                                                                              |
| `local_extremity_event_ids` | [string] | If mode=extremity | The requesting server's current forward extremities. Included in the `have` set for the merge-base walk.                                         |
| `have_event_ids`            | [string] | If mode=extremity | A sparse sample of event IDs the requester already has, used as stop conditions for the merge-base walk. See below.                              |
| `local_digest`              | string   | If mode=bloom     | The requesting server's Bloom filter digest.                                                                                                     |
| `digest_type`               | string   | If mode=bloom     | The digest algorithm used.                                                                                                                       |
| `digest_salt`               | string   | If mode=bloom     | The 8-byte salt used to construct `local_digest`.                                                                                                |
| `digest_bits`               | integer  | If mode=bloom     | The bit-length of `local_digest`. MUST be a power of two, at most `2^23`.                                                                        |
| `digest_depth_floor`        | integer  | If mode=bloom     | The minimum topological depth included in the active window used to construct `local_digest`.                                                    |
| `digest_window`             | integer  | If mode=bloom     | The active-window size used to build `local_digest`.                                                                                             |
| `local_event_count`         | integer  | Yes               | The requesting server's total event count for this room.                                                                                         |
| `max_depth_delta`           | integer  | No                | Extremity mode only. Positive integer. The maximum topological depth distance the peer is allowed to walk. Default 5000, max 50000.              |
| `max_events`                | integer  | No                | Extremity mode only. Positive integer. The maximum number of event IDs the peer is allowed to inspect before stopping. Default 10000, max 50000. |
| `limit`                     | integer  | No                | Positive integer. Maximum number of event IDs to return. Default 1000, max 10000.                                                                |

**Response:**

```json
{
  "probably_missing_event_ids": ["$ghi789", "$jkl012", "$mno345"],
  "remote_event_count": 81247,
  "remote_extremity_event_ids": ["$abc123", "$pqr678"],
  "truncated": false
}
```

**Fields (response):**

| Field                        | Type     | Required | Description                                                                                                                                                                                                             |
| ---------------------------- | -------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `probably_missing_event_ids` | [string] | Yes      | Event IDs that the responding server has but the requesting server likely does not. In `bloom` mode, Bloom false positives can hide some missing events from this list; in `extremity` mode the returned IDs are exact. |
| `remote_event_count`         | integer  | Yes      | The responding server's total event count.                                                                                                                                                                              |
| `remote_extremity_event_ids` | [string] | Yes      | The responding server's current forward extremities.                                                                                                                                                                    |
| `truncated`                  | bool     | Yes      | Whether the result is incomplete — because `limit` was reached, a walk bound was reached, or the bounding checks failed. See Handling Truncation.                                                                       |

**Diff Computation — Mode Selection:**

Servers SHOULD select the diff mode based on the `room_digest` comparison:

- If the remote server's `extremity_event_ids` contain event IDs the local
  server does not recognize → use `extremity` mode (frontier lag; the merge-base
  walk will find the delta).
- If the remote server's `extremity_event_ids` all match locally, but
  `window_event_count` differs at the shared `digest_depth_floor` → use `bloom`
  mode (interior gap; extremities match but events are missing inside the DAG).
- If both extremities diverge AND event counts differ → use `extremity` mode
  first (to resolve the frontier), then `bloom` mode (to patch interior gaps).

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
and otherwise SHOULD fall back to `bloom` mode or existing `/backfill`, applying
back-off between attempts.

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

**Diff Computation — `bloom` mode:**

In `bloom` mode, the responding server:

1. Validates the filter: `digest_bits` MUST be a power of two no greater than
   `2^23` and MUST equal eight times the decoded byte length of `local_digest`;
   otherwise the request MUST be rejected with HTTP 400.
2. Selects its `W` most recent events by topological depth, where `W` is the
   smaller of its own digest window and the request's `digest_window`.
   Restricting the test to the smaller window avoids spuriously reporting events
   as "missing" merely because they fall outside the requester's digest window.
3. Tests each selected event ID against the requester's filter, computing bit
   positions modulo `digest_bits`.
4. Event IDs that are NOT in the filter are probably missing from the requester.
5. Returns those event IDs up to `limit`, ordered by topological depth (oldest
   first).

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

| Field                | Type     | Required | Description                                                                                                                                                                                               |
| -------------------- | -------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `event_ids`          | [string] | Yes      | The event IDs to fetch. Maximum 500 per request.                                                                                                                                                          |
| `include_auth_chain` | bool     | No       | If true, the response includes auth chain events that the requesting server might not have. Default true.                                                                                                 |
| `known_event_ids`    | [string] | No       | Event IDs the requesting server already has. When walking auth chains, the responding server SHOULD stop at events in this set (the graph intersection), avoiding redundant transfer. Default empty list. |

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

| Field               | Type     | Required | Description                                                                                                                                      |
| ------------------- | -------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `events`            | [PDU]    | Yes      | The requested events, in topological order (dependencies before dependants). Each event is a full, signed PDU.                                   |
| `auth_chain_events` | [PDU]    | Yes      | Auth chain events for the returned events that are not in the `events` list. Also in topological order. Empty if `include_auth_chain` was false. |
| `missing_event_ids` | [string] | Yes      | Event IDs from the request that the responding server does not have.                                                                             |

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
         │   extremities unrecognized?]            │
         │                                         │
         │  POST /room_diff/{roomId}               │
         │  { mode: "extremity",                   │
         │    local_extremity_event_ids: [...],    │
         │    have_event_ids: [...],               │
         │    max_depth_delta: 5000,               │
         │    max_events: 10000 }                  │
         │────────────────────────────────────────>│
         │                                         │
         │  200 OK { probably_missing: [...] }     │
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

**Short-circuit optimization:** If the `room_digest` response shows identical
`extremity_event_ids` and `window_event_count` values at the shared
`digest_depth_floor`, the requesting server MAY skip the diff and event fetch
phases entirely.

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
   peers using a weighted random strategy, preferring:
   - Servers that originated the most recent events (most likely to be ahead)
   - Servers that previously returned divergent digests (known to have different
     data)
   - Backbone/hub servers with high availability (most likely to have complete
     DAGs)

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
If-None-Match: "xxh3:abc123def456"
```

**Response (no change):**

```http
HTTP/1.1 304 Not Modified
ETag: "xxh3:abc123def456"
```

The ETag MUST NOT be derived from the Bloom filter digest (which would require
computing the full filter just to evaluate the conditional request, defeating
the purpose of a fast 304 check). Instead, the ETag MUST be computed as:

> `unpadded_base64url(room_xor_sum || XXH3-64(sorted(extremity_event_ids)))`

- `room_xor_sum` is the bitwise XOR of `XXH3-128(event_id)` over all event IDs
  currently in the room's event store — a fixed 16-byte value. XOR is
  commutative and associative, so the sum is maintained incrementally in O(1)
  whenever an event is persisted or purged.
- The `sorted(extremity_event_ids)` component is defense-in-depth: even if two
  different event sets collide in `room_xor_sum`, differing frontiers still
  change the ETag.

If both components match, the two event sets are identical except with
negligible probability (an accidental collision of XORed 128-bit hashes). The
ETag is a cache-validation hint, not a synchronization guarantee: it only means
the responder's view of the room has not changed since the requester last
observed it. A `304` does not imply the two servers agree. The server evaluates
the conditional request in O(E), where E is the number of extremities (typically
1–5), using the incrementally maintained `room_xor_sum` — without touching the
event store or computing the Bloom filter.

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

## Potential issues

### Performance resilience

As with any federation endpoint, execution time and resource usage are concerns.

- **Digest computation cost:** Computing the Bloom filter requires iterating
  over all event IDs in the room. For rooms with 100K+ events, this could be
  expensive. Servers SHOULD cache the digest and invalidate it only when new
  events are persisted.

- **Diff amplification:** A malicious server could send a nearly-empty Bloom
  filter, causing the responding server to return its entire event ID list. The
  `limit` parameter caps this, and servers SHOULD rate-limit diff requests per
  room per peer.

- **Bulk fetch abuse:** The `room_events` endpoint returns full PDUs, which
  could be large. The 500-event-per-request cap and standard federation rate
  limiting mitigate this.

### Active window trade-offs

The active window approach (digesting only the top `W` events by depth) means
that divergence in old history is invisible to the Bloom filter. This is an
intentional trade-off:

- Divergence in old history is rare (the DAG bottom is stable once fully
  replicated)
- The `extremity` diff mode catches frontier divergence regardless of the window
- If deep-history reconciliation is needed, the server can increase
  `digest_window` or fall back to a full `/state_ids` comparison
- The dynamic filter sizing (`m ≈ 6.235 * W` bits) guarantees a consistent ~5%
  false positive rate regardless of window size, preventing the saturation
  problem entirely

### Consistency in active/hot rooms

If a room is actively receiving events during reconciliation, the
digest/diff/fetch sequence may return stale data. This is acceptable — gossip
protocols are inherently eventually consistent, and the next reconciliation
round will catch up. Servers MUST NOT block event processing during
reconciliation.

### Interaction with "Partial State" joins

Servers in the process of a partial state join (MSC3706) SHOULD NOT initiate
reconciliation for that room until the full state resync is complete. They MAY
respond to incoming reconciliation requests with the events they have, but
SHOULD set a response header `X-Matrix-Partial-State: true` to indicate that
their digest/diff is incomplete.

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
4. The proposed Bloom filter plus bounded extremity walk already captures the
   common-case benefit of quickly narrowing the repair frontier, without
   mandating a full room-level Merkle structure.
5. Merkle-based reconciliation can still be introduced later as a separate
   `digest_type` or related optimization if there is a strong need for it.

### Why not invertible bloom filters?

Invertible Bloom Lookup Tables (IBLTs) are an attractive alternative because
they can recover missing event IDs directly from the digest exchange when the
set difference is small. While this is a useful optimization, it is the wrong
primitive to mandate for baseline room reconciliation.

Matrix federation failures are rarely limited to small-delta events. The
scenarios this proposal is designed to survive include spam storms, network
partitions, rejected-event cascades, and partial-state resync failures. Under
these conditions, the difference set can easily exceed the decode capacity of an
invertible filter. Once the decode capacity is exceeded, the filter fails
sharply, requiring the protocol to fall back to a bounded graph walk anyway.

Therefore, this proposal relies on standard Bloom filters for cheap divergence
detection and membership testing, followed by a bounded merge-base walk to
locate the actual repair frontier. Future MSCs MAY define an `ibl_bloom`
`digest_type` value as an optimization for small differences, but baseline
correctness MUST NOT depend on invertible-filter decoding success.

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
- The Bloom filter digest does not reveal individual event IDs (only membership
  in the set)

### Denial of service

The reconciliation endpoints add new attack surface for resource exhaustion.
Mitigations:

- **Rate limiting:** Servers MUST apply per-peer, per-room rate limiting to all
  three endpoints. Recommended: 1 request per 10 seconds per room per peer for
  `room_digest` and `room_diff`; 1 request per 30 seconds for `room_events`.
- **Digest caching:** The Bloom filter SHOULD be computed lazily and cached,
  with cache invalidation on new event persistence. This amortizes the O(N)
  computation cost.
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

### Amplification attacks (via bloom filter modification)

A malicious requesting server could send a Bloom filter with all bits set to 0,
causing the responding server to believe the requester has no events and return
its entire event ID set. The `limit` parameter caps the response size.
Additionally, servers SHOULD compare the `local_event_count` in the request with
the filter's apparent fullness — a count of 80,000 events with an empty filter
is clearly inconsistent and SHOULD be rejected with HTTP 400.

### Depth manipulation

The topological bounding checks and the active window rely on event depth, which
is derived from attacker-influenced event content. When evaluating
`max_depth_delta` and selecting the active window, servers SHOULD use their
locally computed topological ordering (e.g., stream ordering or recomputed
depth) rather than trusting the `depth` field of received events.

### Interaction with server ACLs

Servers MUST respect `m.room.server_acl` when responding to reconciliation
requests. If the requesting server is denied by the room's ACL, the responding
server MUST return HTTP 403 with error code `M_FORBIDDEN`, identical to the
behavior for other federation endpoints.

## Unstable prefix

The following mapping will be used for identifiers in this MSC during
development:

| Proposed final identifier                     | Purpose         | Development identifier                                               |
| --------------------------------------------- | --------------- | -------------------------------------------------------------------- |
| `/_matrix/federation/v1/room_digest/{roomId}` | endpoint        | `/_matrix/federation/unstable/tk.nutra.msc45xx/room_digest/{roomId}` |
| `/_matrix/federation/v1/room_diff/{roomId}`   | endpoint        | `/_matrix/federation/unstable/tk.nutra.msc45xx/room_diff/{roomId}`   |
| `/_matrix/federation/v1/room_events/{roomId}` | endpoint        | `/_matrix/federation/unstable/tk.nutra.msc45xx/room_events/{roomId}` |
| `xxh3_bloom`                                  | digest type     | `xxh3_bloom`                                                         |
| `X-Matrix-Partial-State`                      | response header | `X-Matrix-Unstable-Partial-State`                                    |

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
