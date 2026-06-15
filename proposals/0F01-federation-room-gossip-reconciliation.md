# MSC0F01: Gossip-Based Federation Room Reconciliation

Matrix federation is "push and hope" — servers send events via `/send` transactions and assume
delivery. When delivery fails (rate limiting, network partitions, spam storms, rejection cascades),
the DAG develops permanent holes. Each server drops a different random subset of events, creating
unique "swiss cheese" patterns that no existing endpoint can efficiently detect or repair.

This proposal introduces a lightweight, gossip-inspired reconciliation protocol that allows federated
servers to efficiently detect DAG divergence and surgically heal data gaps without requiring full
state synchronization or new room versions.

## Background

### The Problem: Silent Data Loss

Federation data loss occurs through several well-documented mechanisms:

1. **Rate limiting** drops inbound `/send` transactions during high-traffic periods (spam storms,
   raids, viral rooms).
2. **Rejection cascades** orphan entire subgraphs — a single malformed event causes every subsequent
   event referencing it via `prev_events` to be rejected.
3. **Auth chain fetch timeouts** during load cause events to be permanently persisted as rejected
   outliers.
4. **Partial state joins** (MSC3706) intentionally defer full state synchronization, but network
   interruptions during the resync phase can leave permanent gaps.

The result is that servers in the same room can have materially different DAGs, leading to membership
divergence, missing messages, and inconsistent state resolution outputs — even when the state
resolution algorithm itself is functioning correctly.

### Why Existing Endpoints Are Insufficient

| Endpoint                            | Limitation                                                                                                                    |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `GET /backfill/{roomId}`            | Depth-ordered linear walk; cannot target specific gaps; useless for missing events in the middle of the DAG                   |
| `POST /get_missing_events/{roomId}` | BFS walk with a hard depth limit (default 10); cannot bridge gaps larger than 10 events; requires knowing the boundary events |
| `GET /state_ids/{roomId}`           | Returns state event IDs only (not timeline events); O(N) comparison; no incremental diffing                                   |
| `GET /event/{eventId}`              | Single-event fetch; no bulk mode; requires knowing which events are missing                                                   |

None of these endpoints answer the fundamental question: **"Am I missing events in this room, and
if so, which ones?"**

### Design Principles

This proposal follows the gossip protocol literature (Demers et al., 1987; Birman, 1999) and adapts
three core mechanisms to Matrix's federated DAG model:

1. **Anti-entropy via digest comparison** — O(1) divergence detection using compact room digests
2. **Pull-based reconciliation** — the lagging server requests exactly the events it needs
3. **Protocol-level idempotency** — repeated reconciliation produces no side effects on an
   already-synchronized pair

## Proposal

Three new federation endpoints are introduced under the `/_matrix/federation/v1/` namespace.

### 1. Room Digest: `GET /_matrix/federation/v1/room_digest/{roomId}`

Returns a compact, opaque digest summarizing a server's knowledge of a room's event graph. Two
servers can compare digests in O(1) to determine whether their DAGs have diverged.

**Request:**

```http
GET /_matrix/federation/v1/room_digest/{roomId}
```

**Response:**

```json
{
  "digest": "<opaque_base64_string>",
  "digest_type": "xxh3_bloom",
  "digest_bits": 32768,
  "digest_window": 5000,
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
| `digest_bits`            | integer            | Yes      | The bit-length of the Bloom filter. The server dynamically sizes this; see Digest Construction.                                         |
| `digest_window`          | integer            | Yes      | The number of most-recent events (by topological depth) included in the digest. See Active Window.                                      |
| `event_count`            | integer            | Yes      | The total number of non-outlier events the server holds for this room (including locally rejected events; see Rejected Event Handling). |
| `extremity_event_ids`    | [string]           | Yes      | The server's current forward extremities (DAG tips) for this room.                                                                      |
| `depth_range`            | [integer, integer] | Yes      | The minimum and maximum topological depth of events held.                                                                               |
| `origin_server_ts_range` | [integer, integer] | Yes      | The earliest and latest `origin_server_ts` of events held.                                                                              |

**Digest Construction (`xxh3_bloom`):**

The digest is a dynamically-sized Bloom filter constructed as follows:

1. **Determine the Active Window.** Select the `W` most recent events by topological depth held
   for this room (including locally rejected events; see Rejected Event Handling below). The
   default window size is `W = 5000`. The server reports this value in the `digest_window` field.
   Hashing the entire event history is unnecessary because the bottom of the DAG (old history)
   rarely mutates — divergence almost always occurs at the frontier.
2. **Size the filter.** Allocate `m` bits where `m` is the next power of two greater than or
   equal to `ceil(W * 6.235)` (approximately 6.235 bits per element), which yields a
   false-positive rate of ~5% with `k = 4` hash functions. Standardizing `m` as a power of two is
   a strict requirement to enable in-place dynamic folding (see [Dynamic Filter Folding](#dynamic-filter-folding)).
   For the default window of 5000 events, `ceil(5000 * 6.235) = 31,175`, so the server allocates
   `m = 32,768` bits (exactly 4,096 bytes or 4.0 KB). The server reports this value in the
   `digest_bits` field.
3. **Populate the filter.** For each event ID in the active window, compute two independent hash
   values using XXH3-128, seeded with the constants `0x00` and `0x01` respectively.
4. Use double hashing to derive `k=4` bit positions from the two hash values:
   `position_i = (h1 + i * h2) mod m` for `i` in `{0, 1, 2, 3}`.
5. Set those bits in the filter.
6. Base64url-encode the resulting byte array (unpadded).

The key mathematical constraint is:

> `m = -n * ln(p) / (ln(2))^2`
>
> For `n = 5000` events and `p = 0.05` (5% false positive rate): `m = 31,175 bits ≈ 3.8 KB`
>
> For `n = 10000` events and `p = 0.05`: `m = 62,350 bits ≈ 7.6 KB`

Servers MAY adjust the window size and filter dimensions. A requesting server can infer the filter
parameters from the `digest_bits` and `digest_window` fields in the response. Two servers with
different window sizes can still detect divergence — if their windows overlap, bit differences in
the overlapping region indicate missing events.

**Rejected Event Handling:**

Servers MUST include locally rejected events in the Bloom filter digest. If rejected events were
excluded, a fetch loop would occur: Server B sees that Server A is "missing" an event (because A
excluded it from the filter), returns it in `/room_diff`, Server A fetches it via `/room_events`,
rejects it again, and the cycle repeats on the next gossip interval.

By including rejected event IDs in the filter, Server B's membership test returns positive and the
event is correctly skipped. This does not affect the security model — rejected events are only
included in the _digest_, not in the _resolved state_. Additionally, servers MUST maintain a
negative cache of event IDs that were fetched via reconciliation and subsequently rejected. Events
in the negative cache MUST NOT be re-requested for a configurable cooldown period (RECOMMENDED:
24 hours). This provides defense-in-depth against fetch loops even if the Bloom filter test
produces a false negative for a rejected event ID.

### Dynamic Filter Folding

To allow comparison of Bloom filters of different sizes (e.g., if Server A uses $W_a = 5000$,
resulting in $m_a = 32,768$ bits, and Server B uses $W_b = 10000$, resulting in $m_b = 65,536$
bits) without re-hashing raw event IDs, implementations MUST support dynamic filter folding.

Because $m$ is strictly constrained to be a power of two, a larger Bloom filter of size $2m$
can be folded in half to match a target size $m$ simply by dividing the bit-array into two
equal halves and performing a bitwise `OR` operation on them:
`folded_bits[i] = bits[i] | bits[i + half_bytes]`

This mathematical projection is perfectly sound because $hash \pmod m$ maps to the exact same bit
position as $(hash \pmod{2m}) \pmod m$. This enables instant, in-memory filter down-sampling
with zero cryptographic overhead.

**Authorization:**

The requesting server MUST be a participant in the room (i.e., have at least one joined member).
The receiving server MUST verify this before responding. If the requesting server is not in the room,
the server MUST respond with HTTP 403 and error code `M_FORBIDDEN`.

### 2. Room Diff: `POST /_matrix/federation/v1/room_diff/{roomId}`

Given a requesting server's event ID set (or a compact representation thereof), returns the set of
event IDs that the responding server has but the requester likely does not. This is the "what am I
missing?" query.

The endpoint supports two diff modes because Matrix federation produces two fundamentally
different classes of data loss:

- **Frontier lag ("clean" divergence).** A server goes offline, gets rate-limited, or falls
  behind. It misses a contiguous branch of events from the DAG tip. The server's extremities
  are stale, but its interior DAG is intact. This is identical to a Git branch that is behind
  upstream — the delta is a clean, linear range between the local and remote tips.

- **Interior gaps ("Swiss cheese" divergence).** A server drops random individual events due
  to rate limiting, rejection cascades, or auth chain fetch timeouts, but continues to receive
  subsequent events via state-resyncs. The server's extremities may match the remote server's,
  but its interior DAG has holes. This has no Git analogue — Git's content-addressable storage
  guarantees that possessing a commit implies possessing all ancestors.

The `extremity` mode is a **merge-base finder** (the Git approach) optimized for frontier lag.
It walks backward from divergent extremities to find the most recent common ancestor, returning
exactly the missing delta in O(delta) time.

The `bloom` mode is a **set reconciliation tool** (the Cassandra approach) that ignores graph
topology entirely and checks raw event set membership. It detects interior gaps that the
merge-base finder is structurally blind to.

**Request:**

```http
POST /_matrix/federation/v1/room_diff/{roomId}
```

```json
{
  "mode": "extremity",
  "local_extremity_event_ids": ["$abc123", "$def456"],
  "have_event_ids": ["$known_depth_90000", "$known_depth_89500", "$known_depth_88000", "$known_depth_84000"],
  "local_event_count": 81000,
  "limit": 1000
}
```

**Or, in `bloom` mode:**

```json
{
  "mode": "bloom",
  "local_digest": "<base64_bloom_filter>",
  "digest_type": "xxh3_bloom",
  "local_event_count": 81000,
  "limit": 1000
}
```

**Fields (request):**

| Field                       | Type     | Required          | Description                                                                                                         |
| --------------------------- | -------- | ----------------- | ------------------------------------------------------------------------------------------------------------------- |
| `mode`                      | string   | Yes               | One of `extremity` or `bloom`. Determines how the diff is computed.                                                 |
| `local_extremity_event_ids` | [string] | If mode=extremity | The requesting server's current forward extremities ("want" — what it's trying to reach).                           |
| `have_event_ids`            | [string] | If mode=extremity | A sparse sample of event IDs the requester already has, used as stop conditions for the merge-base walk. See below. |
| `local_digest`              | string   | If mode=bloom     | The requesting server's Bloom filter digest.                                                                        |
| `digest_type`               | string   | If mode=bloom     | The digest algorithm used.                                                                                          |
| `local_event_count`         | integer  | Yes               | The requesting server's total event count for this room.                                                            |
| `max_depth_walk`            | integer  | No                | Maximum events to walk in `extremity` mode before giving up. Default 10000, max 50000.                              |
| `limit`                     | integer  | No                | Maximum number of event IDs to return. Default 1000, max 10000.                                                     |

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

| Field                        | Type     | Required | Description                                                                                                                                                                 |
| ---------------------------- | -------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `probably_missing_event_ids` | [string] | Yes      | Event IDs that the responding server has but the requesting server likely does not. In `bloom` mode these may include false positives; in `extremity` mode these are exact. |
| `remote_event_count`         | integer  | Yes      | The responding server's total event count.                                                                                                                                  |
| `remote_extremity_event_ids` | [string] | Yes      | The responding server's current forward extremities.                                                                                                                        |
| `truncated`                  | bool     | Yes      | Whether the result was truncated due to `limit`. If true, the requesting server should make additional requests.                                                            |

**Diff Computation — Mode Selection:**

Servers SHOULD select the diff mode based on the `room_digest` comparison:

- If the remote server's `extremity_event_ids` contain event IDs the local server does not
  recognize → use `extremity` mode (frontier lag; the merge-base walk will find the delta).
- If the remote server's `extremity_event_ids` all match locally, but `event_count` differs →
  use `bloom` mode (interior gap; extremities match but events are missing inside the DAG).
- If both extremities diverge AND event counts differ → use `extremity` mode first (to resolve
  the frontier), then `bloom` mode (to patch interior gaps).

**Diff Computation — `extremity` mode:**

In `extremity` mode, the responding server performs a **merge-base walk** modeled on Git's
packfile negotiation protocol:

1. **Topological Bounding Check ($O(1)$):** Before starting, the responder MUST perform a pre-flight depth check to prevent CPU-exhaustion DoS attacks.
   - The requester provides `have_event_ids` (a sparse sample of known ancestors).
   - The responder finds the `local_depth` of these events.
   - `delta = local_extremity_depth - max(local_depth_of_valid_have_events)`
   - If `delta > max_depth_walk`, the responder MUST immediately return an empty result with `truncated: true`. This guarantees the server only ever walks bounded, recent history.
2. Build the `have` set: the union of `local_extremity_event_ids` and `have_event_ids`. These
   represent events the requester already possesses. The combined `have` set MUST NOT exceed
   256 entries; requests exceeding this MUST be rejected with HTTP 400.
3. Identify forward extremities the responder has that are NOT in the `have` set — these are the
   "want" events (unknown tips from the requester's perspective).
4. Walk backwards from those unknown extremities via `prev_events`, collecting event IDs.
5. **Stop condition:** For each branch of the walk, stop when the walk reaches an event ID that
   IS in the `have` set. This event is the **merge-base** for that branch — the most recent
   common ancestor between the two servers' DAGs. Events at or before the merge-base are NOT
   included in the result (the requester already has them).
6. **Safety limit:** If the walk visits `max_depth_walk` events without finding any event in the
   `have` set, the walk is terminated and the response MUST set `truncated: true`.
7. Returns the collected event IDs in reverse topological order, up to `limit`.

The `max_depth_walk` parameter prevents CPU exhaustion. Servers MUST enforce
`max_depth_walk <= 50000`. The default is `5000`. Servers SHOULD also maintain a per-peer,
per-room accounting of total walk depth consumed over a rolling window (e.g., 60 seconds) and
reject requests that would exceed a cumulative budget (RECOMMENDED: 100,000 events per peer
per room per minute).

**Constructing the `have` set (requesting server):**

The requesting server constructs `have_event_ids` as a sparse, exponentially-spaced sample of
event IDs it already possesses, working backwards from its extremities:

1. Start from each local extremity and walk backwards via `prev_events`.
2. Sample event IDs at exponentially increasing depth intervals: the first event, then 1 step
   back, 2 steps, 4 steps, 8 steps, 16 steps, etc.
3. Stop sampling after 32 samples per extremity, or when the walk reaches the room's create event.

This produces approximately 32×E event IDs (where E is the number of extremities, typically 1–5),
totaling 32–160 event IDs. The exponential spacing ensures:

- Dense coverage near the frontier (where divergence is most likely)
- Sparse coverage deep in the DAG (where both servers are likely synchronized)
- O(log N) total samples for a DAG of depth N
- The responder is highly likely to find a merge-base within the first few hundred events of
  its backward walk, making the algorithm O(delta) in practice — proportional to the number of
  missing events, not the total room size

In `bloom` mode, the responding server:

1. Tests each of its event IDs (within the active window) against the requester's Bloom filter.
2. Event IDs that are NOT in the filter are probably missing from the requester.
3. Returns those event IDs up to `limit`, ordered by topological depth (oldest first).

**Authorization:**

Same as `room_digest` — the requesting server MUST be a participant in the room.

### 3. Bulk Event Fetch: `POST /_matrix/federation/v1/room_events/{roomId}`

Given a set of event IDs, returns the full events and their auth chain events in topological order,
suitable for direct insertion into the local store.

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

Events in both `events` and `auth_chain_events` MUST be returned in topological order such that for
any event E, all events referenced by E's `auth_events` and `prev_events` appear earlier in the
combined list (auth_chain_events concatenated with events). This allows the requesting server to
process events in a single pass without dependency resolution.

**Authorization:**

Same as `room_digest`. Additionally, the responding server MUST NOT return events that the requesting
server would not be allowed to see (e.g., events sent after the requesting server's last member left
the room, per existing history visibility rules).

### Reconciliation Protocol

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
         │    local_extremity_event_ids: [...] }   │
         │────────────────────────────────────────>│
         │                                         │
         │  200 OK { probably_missing: [...] }     │
         │<────────────────────────────────────────│
         │                                         │
         │  POST /room_events/{roomId}             │
         │  { event_ids: [...],                    │
         │    include_auth_chain: true }            │
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

**Short-circuit optimization:** If the `room_digest` response shows identical `extremity_event_ids`
and `event_count` values, the requesting server MAY skip the diff and event fetch phases entirely.

### Gossip Scheduling

Servers SHOULD implement periodic gossip-based reconciliation for active rooms. The recommended
strategy is:

1. **Trigger-based gossip:** When a server detects potential divergence (e.g., a state resolution
   produces an unexpected result, or a received event references unknown `prev_events`), it SHOULD
   immediately initiate reconciliation with the event's origin server.

2. **Periodic anti-entropy:** Servers SHOULD periodically select a random subset of active rooms and
   a random peer for each, and perform the digest comparison phase. The recommended interval is:
   - Every 60 seconds for rooms with recent activity (events in the last 5 minutes)
   - Every 300 seconds for rooms with moderate activity (events in the last hour)
   - Every 3600 seconds for idle rooms

3. **Peer selection:** For each reconciliation round, the server SHOULD select peers using a
   weighted random strategy, preferring:
   - Servers that originated the most recent events (most likely to be ahead)
   - Servers that previously returned divergent digests (known to have different data)
   - Backbone/hub servers with high availability (most likely to have complete DAGs)

4. **Back-off:** If a peer consistently returns identical digests (no divergence), the server SHOULD
   exponentially back off the reconciliation interval for that peer/room pair, up to a maximum of
   24 hours. Any new event received in the room resets the back-off.

### ETag Optimization for Digest Polling

To minimize bandwidth for digest polling, the `room_digest` endpoint supports conditional requests:

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

The ETag MUST NOT be derived from the Bloom filter digest (which would require computing the full
filter just to evaluate the conditional request, defeating the purpose of a fast 304 check).
Instead, the ETag MUST be computed as:

> `Base64(room_xor_sum || XXH3-64(sorted(extremity_event_ids)))`

- The `room_xor_sum` is computed as the XOR-sum of all event IDs currently in the room's event store. This is commutative and associative, allowing it to be updated in O(1) during event persistence or purging.
- The `sorted(extremity_event_ids)` part ensures frontier divergence is detected.

Because the Matrix DAG is append-only, if the `room_xor_sum` and the extremities are identical,
the underlying event set is mathematically guaranteed to be identical. This allows the server to
evaluate the ETag in O(E) where E is the number of extremities (typically 1–5), without touching
the event store or computing the Bloom filter.

If the computed ETag matches the `If-None-Match` header, the server MUST return HTTP 304 with no
body. This reduces the reconciliation polling cost to a single HTTP round-trip with a ~50 byte
response for rooms that are already synchronized.

## Potential issues

### Performance Under Adversarial Conditions

A malicious server could abuse the reconciliation endpoints to cause resource exhaustion:

- **Digest computation cost:** Computing the Bloom filter requires iterating over all event IDs in
  the room. For rooms with 100K+ events, this could be expensive. Servers SHOULD cache the digest
  and invalidate it only when new events are persisted.

- **Diff amplification:** A malicious server could send a nearly-empty Bloom filter, causing the
  responding server to return its entire event ID list. The `limit` parameter caps this, and servers
  SHOULD rate-limit diff requests per room per peer.

- **Bulk fetch abuse:** The `room_events` endpoint returns full PDUs, which could be large. The
  500-event-per-request cap and standard federation rate limiting mitigate this.

### Active Window Trade-offs

The active window approach (digesting only the top `W` events by depth) means that divergence in
old history is invisible to the Bloom filter. This is an intentional trade-off:

- Divergence in old history is rare (the DAG bottom is stable once fully replicated)
- The `extremity` diff mode catches frontier divergence regardless of the window
- If deep-history reconciliation is needed, the server can increase `digest_window` or fall back
  to a full `/state_ids` comparison
- The dynamic filter sizing (`m ≈ 6.235 * W` bits) guarantees a consistent ~5% false positive rate regardless
  of window size, preventing the saturation problem entirely

### Consistency During Active Rooms

If a room is actively receiving events during reconciliation, the digest/diff/fetch sequence may
return stale data. This is acceptable — gossip protocols are inherently eventually consistent, and
the next reconciliation round will catch up. Servers MUST NOT block event processing during
reconciliation.

### Interaction with Partial State Joins

Servers in the process of a partial state join (MSC3706) SHOULD NOT initiate reconciliation for
that room until the full state resync is complete. They MAY respond to incoming reconciliation
requests with the events they have, but SHOULD set a response header
`X-Matrix-Partial-State: true` to indicate that their digest/diff is incomplete.

## Alternatives

### Using `/make_join` as a Reconciliation Probe

(See performance guidance writeup for alternative architecture discussion).

### Full Merkle Tree Synchronization

A more sophisticated approach would use Merkle trees over the event ID space (similar to
Cassandra's anti-entropy repair). Each server would maintain a Merkle tree where leaves are event
IDs and internal nodes are hashes of their children. Two servers could then efficiently identify
divergent subtrees in O(log N) rounds.

This was rejected for the initial proposal because:

1. It requires persistent auxiliary data structures (the Merkle tree) that must be maintained
   across restarts
2. The interactive multi-round protocol is more complex to implement and reason about
3. The Bloom filter + extremity-walk approach achieves similar practical efficiency for the common
   case (small divergences) with much lower implementation complexity
4. Merkle tree reconciliation can be introduced as a future `digest_type` without changing the
   protocol structure

### Server-Initiated Push Reconciliation

Instead of pull-based reconciliation, servers could proactively push digests to peers when their
DAG advances (rumor-mongering). This was rejected because:

1. It creates O(servers²) traffic in active rooms
2. It requires all servers to process incoming digests even when they are already synchronized
3. Pull-based reconciliation naturally rate-limits itself — a server only reconciles when it
   chooses to, and only with one peer at a time

## Security considerations

### Information Disclosure

The `room_digest` endpoint reveals metadata about a server's event store: event count, depth range,
timestamp range, and forward extremities. This metadata could be used to fingerprint server
implementations or estimate room activity patterns. However:

- This information is already implicitly available through existing endpoints (`/state_ids`,
  `/backfill`, `/event`)
- Access is restricted to servers that are participants in the room
- The Bloom filter digest does not reveal individual event IDs (only membership in the set)

### Denial of Service

The reconciliation endpoints add new attack surface for resource exhaustion. Mitigations:

- **Rate limiting:** Servers MUST apply per-peer, per-room rate limiting to all three endpoints.
  Recommended: 1 request per 10 seconds per room per peer for `room_digest` and `room_diff`;
  1 request per 30 seconds for `room_events`.
- **Digest caching:** The Bloom filter SHOULD be computed lazily and cached, with cache invalidation
  on new event persistence. This amortizes the O(N) computation cost.
- **Response caps:** The `limit` parameter on `room_diff` and the 500-event cap on `room_events`
  bound the maximum response size.

### Replay and Poisoning

A malicious server could return fabricated events in `room_events` responses. This is mitigated by
the same mechanisms that protect existing federation endpoints:

- All returned PDUs MUST have valid signatures from their origin servers
- All returned PDUs MUST pass hash verification (event ID = hash of content)
- The requesting server MUST apply standard auth checks before persisting events
- Events that fail any of these checks MUST be discarded without affecting local state

### Amplification Attacks via Bloom Filter Manipulation

A malicious requesting server could send a Bloom filter with all bits set to 0, causing the
responding server to believe the requester has no events and return its entire event ID set. The
`limit` parameter caps the response size. Additionally, servers SHOULD compare the
`local_event_count` in the request with the filter's apparent fullness — a count of 80,000 events
with an empty filter is clearly inconsistent and SHOULD be rejected with HTTP 400.

### Interaction with Server ACLs

Servers MUST respect `m.room.server_acl` when responding to reconciliation requests. If the
requesting server is denied by the room's ACL, the responding server MUST return HTTP 403 with
error code `M_FORBIDDEN`, identical to the behavior for other federation endpoints.

## Unstable prefix

The following mapping will be used for identifiers in this MSC during development:

| Proposed final identifier                     | Purpose         | Development identifier                                                 |
| --------------------------------------------- | --------------- | ---------------------------------------------------------------------- |
| `/_matrix/federation/v1/room_digest/{roomId}` | endpoint        | `/_matrix/federation/unstable/org.matrix.msc0f01/room_digest/{roomId}` |
| `/_matrix/federation/v1/room_diff/{roomId}`   | endpoint        | `/_matrix/federation/unstable/org.matrix.msc0f01/room_diff/{roomId}`   |
| `/_matrix/federation/v1/room_events/{roomId}` | endpoint        | `/_matrix/federation/unstable/org.matrix.msc0f01/room_events/{roomId}` |
| `xxh3_bloom`                                  | digest type     | `org.matrix.msc0f01.xxh3_bloom`                                        |
| `X-Matrix-Partial-State`                      | response header | `X-Matrix-Unstable-Partial-State`                                      |

## Dependencies

This MSC has no hard dependencies on other unaccepted MSCs.

It is designed to complement:

- [MSC3706](https://github.com/matrix-org/matrix-spec-proposals/pull/3706) (Partial state in send_join) — reconciliation can complete what partial joins leave incomplete
- [MSC4297](https://github.com/matrix-org/matrix-spec-proposals/pull/4297) (State Resolution v2.1) — reconciliation repairs the data gaps that V2.1 cannot address algorithmically
