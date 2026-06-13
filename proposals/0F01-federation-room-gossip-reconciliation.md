# MSC0F01: Gossip-Based Federation Reconciliation (PDUs and EDUs)

Matrix federation delivers both persistent room events (PDUs) and ephemeral data units (EDUs) via a "push and hope" transaction model—servers send payloads via `POST /_matrix/federation/v1/send/{txnId}` and assume delivery. When delivery fails due to rate limiting, network partitions, server restarts, spam storms, or rejection cascades, data silently diverges.

- **PDUs** develop permanent holes in the room directed acyclic graph (DAG), resulting in unique "swiss cheese" patterns that degrade state resolution and client synchronization.
- **EDUs** are lost entirely, leaving servers with permanently stale presence, frozen read receipts, or broken encryption (stale device list updates).

This proposal introduces a lightweight, gossip-inspired reconciliation framework under a unified namespace (`org.matrix.msc0f01`). It provides federated servers with a highly efficient anti-entropy mechanism to detect divergence and surgically heal both persistent room history and ephemeral user states without requiring full state synchronization or new room versions.

---

## Background

### The Problem: Silent Room PDU Loss

Room DAG data loss occurs through several well-documented mechanisms:

1. **Rate limiting** drops inbound transactions during high-traffic periods (spam storms, raids, viral rooms).
2. **Rejection cascades** orphan entire subgraphs—a single malformed event causes every subsequent event referencing it via `prev_events` to be rejected.
3. **Auth chain fetch timeouts** during load cause events to be permanently persisted as rejected outliers.
4. **Partial state joins** (MSC3706) intentionally defer full state synchronization, but network interruptions during the resync phase can leave permanent gaps.

The result is that servers in the same room can have materially different DAGs, leading to membership divergence, missing messages, and inconsistent state resolution outputs—even when the state resolution algorithm itself is functioning correctly.

### The Problem: Silent Ephemeral State Divergence (EDUs)

Because EDUs have no DAG, no persistence guarantee, and no retry mechanism, dropped transactions result in silent, permanent ephemeral state divergence:

1. **Stale presence.** A user appears permanently offline to remote servers because the `m.presence` EDU announcing their online status was dropped.
2. **Missing read receipts.** A user's read position appears frozen in remote clients because `m.receipt` EDUs were dropped during a network partition.
3. **Broken encryption.** Device list updates (`m.device_list_update`) are critical for Olm/Megolm session management. A missed device list change means remote servers continue encrypting to stale device keys, producing undecryptable messages (UTDs).
4. **Stale typing indicators.** Typing notifications (`m.typing`) are inherently short-lived, but a missed "stopped typing" EDU can leave a ghost typing indicator for minutes.

### Why Existing Endpoints Are Insufficient

| Endpoint                            | Scope | Limitation                                                                                                                    |
| ----------------------------------- | ----- | ----------------------------------------------------------------------------------------------------------------------------- |
| `GET /backfill/{roomId}`            | PDU   | Depth-ordered linear walk; cannot target specific gaps; useless for missing events in the middle of the DAG                   |
| `POST /get_missing_events/{roomId}` | PDU   | BFS walk with a hard depth limit (default 10); cannot bridge gaps larger than 10 events; requires knowing the boundary events |
| `GET /state_ids/{roomId}`           | PDU   | Returns state event IDs only (not timeline events); O(N) comparison; no incremental diffing                                   |
| `GET /event/{eventId}`              | PDU   | Single-event fetch; no bulk mode; requires knowing which events are missing                                                   |

None of these endpoints answer the fundamental question: **"Am I missing events/states in this room, and if so, which ones?"**

### Why PDU and EDU Reconciliation Differ (Data Models)

MSC0F01 addresses PDU divergence in the room DAG. PDUs are:

- **Persistent**—they are stored permanently and form a cryptographically linked graph.
- **Append-only**—new events reference previous events; the history only grows.
- **Set-reconcilable**—Bloom filters and merge-base walks can identify missing entries in the graph.

EDUs are fundamentally different:

- **Ephemeral**—they represent _current state_, not history. Only the latest value matters.
- **Last-writer-wins**—a newer presence update supersedes an older one. There is no merge conflict.
- **Scoped differently**—some EDUs are per-user-global (presence, device lists), others are per-user-per-room (receipts, typing).

Therefore, while PDU reconciliation requires graph traversal and Bloom filters, EDU reconciliation uses **version-vector comparison** to determine which server possesses a newer snapshot of a user's state.

### Design Principles

This proposal follows the gossip protocol literature (Demers et al., 1987; Birman, 1999) and adapts three core mechanisms:

1. **Anti-entropy via digest comparison**—O(1) divergence detection using compact room and EDU digests.
2. **Pull-based reconciliation**—the lagging server requests exactly the events or user states it needs.
3. **Protocol-level idempotency**—repeated reconciliation produces no side effects on already-synchronized peers.

---

## Proposal

Five new federation endpoints are introduced under the `/_matrix/federation/v1/` namespace.

### 1. Room Digest: `GET /_matrix/federation/v1/room_digest/{roomId}`

Returns a compact, opaque digest summarizing a server's knowledge of a room's event graph. Two servers can compare digests in O(1) to determine whether their DAGs have diverged.

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

1. **Determine the Active Window.** Select the `W` most recent events by topological depth held for this room (including locally rejected events; see Rejected Event Handling below). The default window size is `W = 5000`. The server reports this value in the `digest_window` field. Hashing the entire event history is unnecessary because the bottom of the DAG (old history) rarely mutates—divergence almost always occurs at the frontier.
2. **Size the filter.** Allocate `m` bits where `m = ceil(W * 6.235)` (approximately 6.235 bits per element), which yields a false positive rate of ~5% with `k = 4` hash functions. For the default window of 5000 events, this produces a filter of `m = 31,175` bits (~3.8 KB). The server reports this value in the `digest_bits` field.
3. **Populate the filter.** For each event ID in the active window, compute two independent hash values using XXH3-128, seeded with the constants `0x00` and `0x01` respectively.
4. Use double hashing to derive `k=4` bit positions from the two hash values: `position_i = (h1 + i * h2) mod m` for `i` in `{0, 1, 2, 3}`.
5. Set those bits in the filter.
6. Base64url-encode the resulting byte array (unpadded).

The key mathematical constraint is:

> `m = -n * ln(p) / (ln(2))^2`
>
> For `n = 5000` events and `p = 0.05` (5% false positive rate): `m = 31,175 bits ≈ 3.8 KB`
>
> For `n = 10000` events and `p = 0.05`: `m = 62,350 bits ≈ 7.6 KB`

Servers MAY adjust the window size and filter dimensions. A requesting server can infer the filter parameters from the `digest_bits` and `digest_window` fields in the response. Two servers with different window sizes can still detect divergence—if their windows overlap, bit differences in the overlapping region indicate missing events.

The Bloom filter approach allows:

- O(1) equality comparison (if filters are identical, the active windows are very likely synchronized).
- Approximate set difference estimation (popcount of `local AND NOT remote` correlates with the number of locally-missing events in the active window).
- Compact representation (~4 KB for the common case, regardless of total room size).

**Rejected Event Handling:**

Servers MUST include locally rejected events in the Bloom filter digest. If rejected events were excluded, a fetch loop would occur: Server B sees that Server A is "missing" an event (because A excluded it from the filter), returns it in `/room_diff`, Server A fetches it via `/room_events`, rejects it again, and the cycle repeats on the next gossip interval.

By including rejected event IDs in the filter, Server B's membership test returns positive and the event is correctly skipped. This does not affect the security model—rejected events are only included in the _digest_, not in the _resolved state_. Additionally, servers MUST maintain a negative cache of event IDs that were fetched via reconciliation and subsequently rejected. Events in the negative cache MUST NOT be re-requested for a configurable cooldown period (RECOMMENDED: 24 hours). This provides defense-in-depth against fetch loops even if the Bloom filter test produces a false negative for a rejected event ID.

**Authorization:**

The requesting server MUST be a participant in the room (i.e., have at least one joined member). The receiving server MUST verify this before responding. If the requesting server is not in the room, the server MUST respond with HTTP 403 and error code `M_FORBIDDEN`.

---

### 2. Room Diff: `POST /_matrix/federation/v1/room_diff/{roomId}`

Given a requesting server's event ID set (or a compact representation thereof), returns the set of event IDs that the responding server has but the requester likely does not. This is the "what am I missing?" query.

The endpoint supports two diff modes because Matrix federation produces two fundamentally different classes of data loss:

- **Frontier lag ("clean" divergence).** A server goes offline, gets rate-limited, or falls behind. It misses a contiguous branch of events from the DAG tip. The server's extremities are stale, but its interior DAG is intact. This is identical to a Git branch that is behind upstream—the delta is a clean, linear range between the local and remote tips.
- **Interior gaps ("Swiss cheese" divergence).** A server drops random individual events due to rate limiting, rejection cascades, or auth chain fetch timeouts, but continues to receive subsequent events via state-resyncs. The server's extremities may match the remote server's, but its interior DAG has holes. This has no Git analogue—Git's content-addressable storage guarantees that possessing a commit implies possessing all ancestors.

The `extremity` mode is a **merge-base finder** (the Git approach) optimized for frontier lag. It walks backward from divergent extremities to find the most recent common ancestor, returning exactly the missing delta in O(delta) time.

The `bloom` mode is a **set reconciliation tool** (the Cassandra approach) that ignores graph topology entirely and checks raw event set membership. It detects interior gaps that the merge-base finder is structurally blind to.

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
| `local_extremity_event_ids` | [string] | If mode=extremity | The requesting server's current forward extremities ("want"—what it's trying to reach).                             |
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

- If the remote server's `extremity_event_ids` contain event IDs the local server does not recognize → use `extremity` mode (frontier lag; the merge-base walk will find the delta).
- If the remote server's `extremity_event_ids` all match locally, but `event_count` differs → use `bloom` mode (interior gap; extremities match but events are missing inside the DAG).
- If both extremities diverge AND event counts differ → use `extremity` mode first (to resolve the frontier), then `bloom` mode (to patch interior gaps).

**Diff Computation — `extremity` mode:**

In `extremity` mode, the responding server performs a **merge-base walk** modeled on Git's packfile negotiation protocol:

1. Build the `have` set: the union of `local_extremity_event_ids` and `have_event_ids`. These represent events the requester already possesses. The combined `have` set MUST NOT exceed 256 entries; requests exceeding this MUST be rejected with HTTP 400.
2. **Pre-flight validation:** Before starting the walk, the responder SHOULD check whether _any_ event ID in the `have` set exists in its local store (a batch of point-lookups). If zero `have` events are recognized, the responder SHOULD immediately return an empty `probably_missing_event_ids` with `truncated: true` rather than walking the DAG. This prevents a malicious requester from forcing a 50,000-event walk by sending fabricated `have` event IDs that don't exist in the responder's DAG.
3. Identify forward extremities the responder has that are NOT in the `have` set—these are the "want" events (unknown tips from the requester's perspective).
4. Walk backwards from those unknown extremities via `prev_events`, collecting event IDs.
5. **Stop condition:** For each branch of the walk, stop when the walk reaches an event ID that IS in the `have` set. This event is the **merge-base** for that branch—the most recent common ancestor between the two servers' DAGs. Events at or before the merge-base are NOT included in the result (the requester already has them).
6. **Safety limit:** If the walk visits `max_depth_walk` events without finding any event in the `have` set, the walk is terminated and the response MUST set `truncated: true`. This prevents CPU exhaustion when the requester's `have` set has no overlap with the responder's DAG (e.g., the requester has been offline for weeks and its sparse sample is too sparse).
7. Returns the collected event IDs in reverse topological order, up to `limit`.

The `max_depth_walk` parameter prevents CPU exhaustion. Servers MUST enforce `max_depth_walk <= 50000`. The default is `5000`. Servers SHOULD also maintain a per-peer, per-room accounting of total walk depth consumed over a rolling window (e.g., 60 seconds) and reject requests that would exceed a cumulative budget (RECOMMENDED: 100,000 events per peer per room per minute). This prevents an attacker from sending many small requests that each walk just under the per-request limit.

**Constructing the `have` set (requesting server):**

The requesting server constructs `have_event_ids` as a sparse, exponentially-spaced sample of event IDs it already possesses, working backwards from its extremities:

1. Start from each local extremity and walk backwards via `prev_events`.
2. Sample event IDs at exponentially increasing depth intervals: the first event, then 1 step back, 2 steps, 4 steps, 8 steps, 16 steps, etc.
3. Stop sampling after 32 samples per extremity, or when the walk reaches the room's create event.

This produces approximately 32×E event IDs (where E is the number of extremities, typically 1–5), totaling 32–160 event IDs. The exponential spacing ensures:

- Dense coverage near the frontier (where divergence is most likely).
- Sparse coverage deep in the DAG (where both servers are likely synchronized).
- O(log N) total samples for a DAG of depth N.
- The responder is highly likely to find a merge-base within the first few hundred events of its backward walk, making the algorithm O(delta) in practice—proportional to the number of missing events, not the total room size.

This is directly analogous to Git's `upload-pack` protocol, where the client sends `have <commit-id>` lines at exponentially increasing distances from HEAD until the server responds with `ACK <commit-id>` indicating the merge-base.

**Diff Computation — `bloom` mode:**

In `bloom` mode, the responding server:

1. Tests each of its event IDs (within the active window) against the requester's Bloom filter.
2. Event IDs that are NOT in the filter are probably missing from the requester.
3. Returns those event IDs up to `limit`, ordered by topological depth (oldest first).

**Authorization:**

Same as `room_digest`—the requesting server MUST be a participant in the room.

---

### 3. Bulk Event Fetch: `POST /_matrix/federation/v1/room_events/{roomId}`

Given a set of event IDs, returns the full events and their auth chain events in topological order, suitable for direct insertion into the local store.

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

Events in both `events` and `auth_chain_events` MUST be returned in topological order such that for any event E, all events referenced by E's `auth_events` and `prev_events` appear earlier in the combined list (`auth_chain_events` concatenated with `events`). This allows the requesting server to process events in a single pass without dependency resolution.

**Authorization:**

Same as `room_digest`. Additionally, the responding server MUST NOT return events that the requesting server would not be allowed to see (e.g., events sent after the requesting server's last member left the room, per existing history visibility rules).

---

### 4. EDU State Digest: `GET /_matrix/federation/v1/edu_digest`

Returns a compact summary of the responding server's EDU state versions for users that the requesting server shares rooms with. This allows the requester to identify which users have stale state locally.

**Request:**

```http
GET /_matrix/federation/v1/edu_digest
```

**Query Parameters:**

| Parameter  | Type   | Required | Description                                                            |
| ---------- | ------ | -------- | ---------------------------------------------------------------------- |
| `edu_type` | string | Yes      | The EDU type to query. See Supported EDU Types.                        |
| `since`    | string | No       | An opaque pagination token from a previous response. For initial sync, |
|            |        |          | omit this parameter.                                                   |
| `limit`    | int    | No       | Maximum number of user entries to return. Default 100, max 1000.       |

**Response:**

```json
{
  "users": {
    "@alice:example.com": {
      "version": 1716000042,
      "content_hash": "xxh3:a1b2c3d4"
    },
    "@bob:example.com": {
      "version": 1716000099,
      "content_hash": "xxh3:e5f6g7h8"
    }
  },
  "next_batch": "opaque_token_123",
  "edu_type": "m.presence"
}
```

**Fields (response):**

| Field                  | Type    | Required | Description                                                 |
| ---------------------- | ------- | -------- | ----------------------------------------------------------- |
| `users`                | object  | Yes      | Map of user ID to version metadata.                         |
| `users.*.version`      | integer | Yes      | Monotonically increasing version counter for this user's    |
|                        |         |          | EDU state. Typically `origin_server_ts` of the last update. |
| `users.*.content_hash` | string  | Yes      | Hash of the current EDU content. Allows detecting changes   |
|                        |         |          | even if version counters drift.                             |
| `next_batch`           | string  | No       | Pagination token. If present, more users are available.     |
| `edu_type`             | string  | Yes      | The EDU type this digest covers.                            |

**Version Semantics:**

The `version` field MUST be a monotonically increasing integer that advances every time the user's EDU state of the given type changes. Servers SHOULD use `origin_server_ts` (milliseconds since epoch) as the version. If two updates occur within the same millisecond, the server MUST increment the version beyond the previous value.

The `content_hash` is an XXH3-64 hash of the canonical JSON representation of the EDU content body. This serves as a tiebreaker—if two servers have the same `version` for a user but different `content_hash` values, their state has diverged and the one with the higher version wins.

**Scoping:**

The responding server MUST only include users that share at least one room with the requesting server. The set of shared users is determined by the current room membership state—the same scope as existing EDU delivery.

**Authorization:**

The requesting server MUST be authenticated via standard federation authentication. No room-level authorization is required since EDU state is scoped to users, not rooms.

---

### 5. EDU State Fetch: `POST /_matrix/federation/v1/edu_state`

Given a set of user IDs, returns the current EDU state for those users. This is the "give me the latest" endpoint.

**Request:**

```http
POST /_matrix/federation/v1/edu_state
```

```json
{
  "edu_type": "m.presence",
  "user_ids": ["@alice:example.com", "@bob:example.com"]
}
```

**Fields (request):**

| Field      | Type     | Required | Description                                          |
| ---------- | -------- | -------- | ---------------------------------------------------- |
| `edu_type` | string   | Yes      | The EDU type to fetch.                               |
| `user_ids` | [string] | Yes      | User IDs to fetch state for. Maximum 200 per request |
|            |          |          | to prevent abuse.                                    |

**Response:**

```json
{
  "edu_type": "m.presence",
  "states": {
    "@alice:example.com": {
      "version": 1716000042,
      "content": {
        "presence": "online",
        "last_active_ago": 5000,
        "status_msg": "Working on MSCs"
      }
    },
    "@bob:example.com": {
      "version": 1716000099,
      "content": {
        "presence": "unavailable",
        "last_active_ago": 300000
      }
    }
  },
  "unknown_user_ids": []
}
```

**Fields (response):**

| Field              | Type     | Required | Description                                      |
| ------------------ | -------- | -------- | ------------------------------------------------ |
| `edu_type`         | string   | Yes      | The EDU type.                                    |
| `states`           | object   | Yes      | Map of user ID to current state.                 |
| `states.*.version` | integer  | Yes      | Version counter matching the `edu_digest` value. |
| `states.*.content` | object   | Yes      | The full EDU content body.                       |
| `unknown_user_ids` | [string] | Yes      | User IDs from the request that the server does   |
|                    |          |          | not have state for.                              |

**Supported EDU Types:**

The following EDU types are eligible for state reconciliation:

| EDU Type               | Scope         | Reconciliation Strategy                                                      |
| ---------------------- | ------------- | ---------------------------------------------------------------------------- |
| `m.presence`           | Per-user      | Compare version; fetch if remote is newer                                    |
| `m.receipt`            | Per-user-room | Compare version per room; fetch if newer                                     |
| `m.device_list_update` | Per-user      | Compare version; fetch full device list                                      |
| `m.direct_to_device`   | Per-user      | NOT reconcilable (point-to-point, at-most-once delivery; cannot be replayed) |
| `m.typing`             | Per-user-room | NOT reconcilable (inherently transient; stale within seconds)                |

**Per-user-room scoping (receipts):**

For EDU types scoped to a user-room pair, the `edu_digest` response includes a nested structure:

```json
{
  "users": {
    "@alice:example.com": {
      "rooms": {
        "!room1:example.com": {
          "version": 1716000042,
          "content_hash": "xxh3:a1b2c3d4"
        },
        "!room2:example.com": {
          "version": 1716000050,
          "content_hash": "xxh3:b2c3d4e5"
        }
      }
    }
  }
}
```

For receipts, the `version` SHOULD be the `origin_server_ts` of the event that the receipt points to (not the receipt's own timestamp), ensuring that receipts always advance monotonically with the room timeline.

---

## Reconciliation Protocols

### Room PDU Reconciliation Flow

The PDU reconciliation loop allows a server to find and heal historical gaps:

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

**Short-circuit optimization:** If the `room_digest` response shows identical `extremity_event_ids` and `event_count` values, the requesting server MUST skip the diff and event fetch phases entirely.

---

### User EDU Reconciliation Flow

The EDU reconciliation loop allows a server to refresh expired ephemeral states:

```text
    Server A (stale state)                Server B (origin)
         │                                     │
         │  GET /edu_digest                    │
         │  ?edu_type=m.presence               │
         │────────────────────────────────────>│
         │                                     │
         │  200 OK { users: {                  │
         │    "@alice": { version: 42 },       │
         │    "@bob": { version: 99 } } }      │
         │<────────────────────────────────────│
         │                                     │
         │  [Compare local versions:           │
         │   @alice local=42 remote=42 → skip  │
         │   @bob local=50 remote=99 → stale]  │
         │                                     │
         │  POST /edu_state                    │
         │  { user_ids: ["@bob"] }             │
         │────────────────────────────────────>│
         │                                     │
         │  200 OK { states: {                 │
         │    "@bob": { version: 99,           │
         │      content: { ... } } } }         │
         │<────────────────────────────────────│
         │                                     │
         │  [Update local state for @bob]      │
         │                                     │
```

---

## Gossip Scheduling

To prevent unnecessary network overhead, servers SHOULD implement independent, staggered gossip schedules reflecting the urgency of each content type:

### 1. Room PDU Gossip

- **Trigger-based gossip:** When a server detects potential divergence (e.g., state resolution produces an unexpected result, or a received event references unknown `prev_events`), it SHOULD immediately initiate PDU reconciliation with the event's origin server.
- **Periodic anti-entropy:** Servers SHOULD periodically select a random active room and a random peer in that room, and perform the digest comparison phase. The recommended interval is:
  - Every 60 seconds for rooms with recent activity (events in the last 5 minutes).
  - Every 300 seconds for rooms with moderate activity (events in the last hour).
  - Every 3600 seconds for idle rooms.

### 2. User EDU Gossip

EDU reconciliation is scheduled independently from PDU reconciliation, using intervals reflecting the volatility and importance of each EDU type:

- **`m.device_list_update`** — Reconcile every 30 seconds. Device list updates are critical for E2EE; their reconciliation is prioritized to prevent undecryptable messages (UTDs).
- **`m.presence`** — Reconcile every 60 seconds. Presence is highly visible to end users and should be updated quickly.
- **`m.receipt`** — Reconcile every 300 seconds. Read receipt delays are cosmetic but not functionally harmful.

### 3. Peer Selection and Back-off

For both PDU and EDU reconciliation, servers SHOULD:

- Prefer peer selection strategies that target:
  - Servers that originated the most recent updates (most likely to be ahead).
  - Servers that previously returned divergent digests (known to have different data).
  - High-availability backbone/hub servers (most likely to have complete history).
- **Back-off:** If a peer consistently returns identical digests (no divergence) across 3 consecutive polls, the server SHOULD exponentially double the reconciliation interval for that peer up to a maximum of 24 hours (for PDUs) or 3600 seconds (for EDUs). Any newly received event/EDU from that peer resets the back-off.

---

## ETag Optimizations

Both digest endpoints support conditional requests (`If-None-Match`) to reduce steady-state cost to a single O(1) evaluation.

### PDU Room Digest ETag

The `room_digest` ETag MUST NOT be derived from the Bloom filter digest (which would require expensive filter generation). Instead, the ETag MUST be computed as:

> `XXH3-64(sorted(extremity_event_ids) || event_count)`

Because the Matrix DAG is append-only, if the extremities and the event count are identical, the underlying event set is mathematically guaranteed to be identical. This allows O(E) evaluation (where E is typically 1–5 extremities).

**Why this bridges both failure modes:** The ETag design is deliberately constructed so that _both_ frontier lag and interior gaps produce a cache miss. If a server falls behind, its extremities will differ, changing the ETag. If a server has Swiss cheese gaps but matching extremities, its `event_count` will be lower, also changing the ETag. This guarantees that the digest polling phase always detects divergence, triggering the appropriate diff mode.

### EDU Digest ETag

The `edu_digest` ETag is computed as:

> `XXH3-64(max(all user versions for this edu_type))`

Because versions are monotonically increasing, if the maximum version has not changed, no user's state has changed. This allows the server to evaluate the ETag in O(1) if it maintains a running maximum for that EDU type.

If the computed ETag matches the `If-None-Match` header, the server MUST return HTTP 304 with no body, reducing polling cost to a ~50 byte response for synchronized peers.

---

## Potential Issues

### Performance Under Adversarial Conditions (PDUs)

A malicious server could abuse the reconciliation endpoints to cause resource exhaustion:

- **Digest computation cost:** Computing the Bloom filter requires iterating over all active event IDs in the room. Servers SHOULD compute the Bloom filter lazily and cache the result, invalidating the cache only when new events are persisted.
- **Diff amplification:** A malicious server could send an empty Bloom filter, forcing the responder to return its entire event ID set. The `limit` parameter caps this, and servers MUST rate-limit diff requests per peer per room.
- **Bulk fetch abuse:** The `room_events` endpoint returns full PDUs. This is mitigated by the 500-event-per-request cap and rate limits.

### Scale with Large User Bases (EDUs)

A large homeserver (e.g., matrix.org) may share hundreds of thousands of users with a peer. Initial sync of `edu_digest` could require many round-trips.

- **Mitigation:** Servers SHOULD cache the remote digest and only re-request pages that have changed (using the `since` token). For steady-state operation, most polls will result in HTTP 304 or small page diffs.

### Clock Skew (EDUs)

Using `origin_server_ts` as the version counter is sensitive to clock skew. If a server's clock jumps backward, it could produce a lower version for a newer state update, causing remote servers to ignore it.

- **Mitigation:** Servers MUST ensure that the version is always strictly greater than the previous version for that user/type, regardless of wall clock time. The version is a logical clock that uses physical time as a convenient initial value.

### Consistency During Active Rooms

If a room or user state is active during reconciliation, the digest/diff/fetch sequence may return stale data. This is acceptable—gossip protocols are inherently eventually consistent, and the next reconciliation round will catch up. Servers MUST NOT block event processing during reconciliation.

### Race Conditions During State Changes (EDUs)

If a user's EDU state changes between the `edu_digest` and `edu_state` requests, the fetched state will be newer than what the digest indicated. This is harmless—the requesting server gets a more recent state than expected, which is strictly better than the stale state it had.

### Privacy Implications of Presence Probing

A malicious server could use `edu_digest` to probe whether a specific user is online without sharing a room.

- **Mitigation:** The scoping rule—that only users sharing rooms with the requester are included—MUST be strictly enforced.

### Interaction with Partial State Joins (PDUs and EDUs)

Servers in the process of a partial state join (MSC3706) SHOULD NOT initiate reconciliation for that room until the full state resync is complete. They MAY respond to incoming reconciliation requests with the events/states they have, but SHOULD set the response header `X-Matrix-Unstable-Partial-State: true` to indicate that their digest/diff is incomplete.

---

## Alternatives

### Using `/make_join` as a PDU Reconciliation Probe

An alternative is to abuse the existing `/make_join` endpoint as a zero-mutation DAG probe. By calling `/make_join` with a throwaway user ID, a server can obtain the remote server's current `prev_events` (DAG tips) and `auth_events`.

- **Why Rejected:** This only reveals extremity divergence (not interior gaps), creates spurious join traffic that obscures real joins, does not scale (no ETag/304 support), and abuses an endpoint designed for a different purpose.

### Full Merkle Tree Synchronization (PDUs)

A more sophisticated approach would use Merkle trees over the event ID space (similar to Cassandra's anti-entropy repair). Two servers could identify divergent subtrees in O(log N) rounds.

- **Why Rejected:** It requires maintaining complex persistent auxiliary data structures (the Merkle tree) across restarts and adds multi-round protocol complexity. The Bloom filter + extremity-walk approach achieves similar practical efficiency for the common case (small divergences) with much lower complexity.

### Server-Initiated Push Reconciliation

Instead of pull-based reconciliation, servers could proactively push digests to peers when their DAG or user states advance (rumor-mongering).

- **Why Rejected:** It creates O(servers²) traffic in active rooms and forces all servers to process incoming digests even when fully synchronized. Pull-based reconciliation naturally rate-limits itself.

### Piggybacking EDUs on `/send` Transactions

Instead of dedicated endpoints, EDU state could be reconciled by including "state refresh" EDUs in regular `/send` transactions.

- **Why Rejected:** It wastes bandwidth by re-sending state that the remote server already has, couples refresh frequency to transaction frequency, and prevents selective fetching of stale users.

### Per-Room EDU Sync

An alternative would scope EDU reconciliation to individual rooms.

- **Why Rejected:** Presence and device lists are per-user, not per-room. A per-room approach would require redundant queries for users in multiple shared rooms, whereas the version-vector approach naturally handles per-user state with a single digest query per peer.

---

## Security Considerations

### Information Disclosure

The digest endpoints reveal metadata about a server's event store and user activity. However:

- This information is already implicitly available through existing endpoints (`/state_ids`, `/backfill`, `/event`) or normal room traffic.
- Access is restricted strictly to servers that are participants in a shared room.
- The Bloom filter digest does not reveal individual event IDs, only membership in the set.

### Denial of Service & Rate Limiting

The reconciliation endpoints add new attack surface for resource exhaustion. Mitigations:

- **Rate limiting:** Servers MUST apply per-peer, per-room rate limiting:
  - `room_digest` and `room_diff`: Recommended 1 request per 10 seconds per room per peer.
  - `room_events`: Recommended 1 request per 30 seconds per peer.
  - `edu_digest`: Recommended 1 request per 10 seconds per EDU type per peer.
  - `edu_state`: Recommended 1 request per 10 seconds per peer.
- **Pagination caps:** The `limit` parameter on `edu_digest` MUST be capped at `limit <= 1000`.
- **Fetch caps:** The `edu_state` endpoint accepts at most 200 user IDs per request. The `room_events` endpoint accepts at most 500 event IDs per request.

### Replay, Poisoning, and Validation

A malicious server could return fabricated events or stale EDU state with artificially high version numbers.

- **PDU Mitigation:** All returned PDUs MUST have valid signatures from their origin servers, pass hash verification (event ID = hash of content), and pass standard auth checks before persistence. Malformed events MUST be discarded.
- **EDU Mitigation:** Servers SHOULD cross-validate the returned content against the `content_hash` from the digest. If the hash does not match, the response MUST be discarded. Additionally, servers SHOULD prefer EDU state received via normal `/send` transactions over reconciliation responses when the `/send` state has a higher version.

### Amplification Attacks via Bloom Filter Manipulation

A malicious requesting server could send a Bloom filter with all bits set to 0, forcing the responder to return its entire event ID set.

- **Mitigation:** The `limit` parameter caps the response size. Additionally, servers SHOULD compare the `local_event_count` in the request with the filter's apparent fullness—a count of 80,000 events with an empty filter is inconsistent and SHOULD be rejected with HTTP 400.

### Interaction with Server ACLs

Servers MUST respect `m.room.server_acl` when responding to PDU reconciliation requests. If the requesting server is denied by the room's ACL, the responding server MUST return HTTP 403 with error code `M_FORBIDDEN`.

---

## Unstable Prefix

The following mapping will be used for identifiers in this MSC during development:

| Proposed final identifier                     | Purpose         | Development identifier                                                 |
| --------------------------------------------- | --------------- | ---------------------------------------------------------------------- |
| `/_matrix/federation/v1/room_digest/{roomId}` | endpoint        | `/_matrix/federation/unstable/org.matrix.msc0f01/room_digest/{roomId}` |
| `/_matrix/federation/v1/room_diff/{roomId}`   | endpoint        | `/_matrix/federation/unstable/org.matrix.msc0f01/room_diff/{roomId}`   |
| `/_matrix/federation/v1/room_events/{roomId}` | endpoint        | `/_matrix/federation/unstable/org.matrix.msc0f01/room_events/{roomId}` |
| `/_matrix/federation/v1/edu_digest`           | endpoint        | `/_matrix/federation/unstable/org.matrix.msc0f01/edu_digest`           |
| `/_matrix/federation/v1/edu_state`            | endpoint        | `/_matrix/federation/unstable/org.matrix.msc0f01/edu_state`            |
| `xxh3_bloom`                                  | digest type     | `org.matrix.msc0f01.xxh3_bloom`                                        |
| `X-Matrix-Partial-State`                      | response header | `X-Matrix-Unstable-Partial-State`                                      |

---

## Dependencies

This MSC has no hard dependencies on other unaccepted MSCs.

It is designed to complement:

- [MSC3706](https://github.com/matrix-org/matrix-spec-proposals/pull/3706) (Partial state in send_join) — reconciliation can complete what partial joins leave incomplete.
- [MSC4297](https://github.com/matrix-org/matrix-spec-proposals/pull/4297) (State Resolution v2.1) — reconciliation repairs the data gaps that V2.1 cannot address algorithmically.
