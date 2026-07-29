# MSC0502: Federation EDU State Reconciliation

Matrix federation delivers Ephemeral Data Units (EDUs) via the same `/send`
transaction mechanism as PDUs. When transactions are dropped — due to rate
limiting, network partitions, or server restarts — EDU state silently diverges.
Unlike PDUs, EDUs have no DAG, no persistence guarantee, and no retry mechanism.
A dropped EDU is lost forever.

This proposal introduces a lightweight state-snapshot reconciliation protocol
for EDU streams, allowing federated servers to detect and repair stale ephemeral
state without polling individual user accounts.

## Background

### The Problem: Silent Ephemeral State Divergence

EDU delivery failures produce user-visible symptoms that are difficult to
diagnose:

1. **Stale presence.** A user appears permanently offline to remote servers
   because the `m.presence` EDU announcing their online status was dropped. The
   remote server has no mechanism to discover that its cached presence is wrong.

2. **Missing read receipts.** A user's read position appears frozen in remote
   clients because `m.receipt` EDUs were dropped during a network partition. The
   conversation shows a permanently stale read marker.

3. **Broken encryption.** Device list updates (`m.device_list_update`) are
   critical for Olm/Megolm session management. A missed device list change means
   remote servers continue encrypting to stale device keys, producing
   undecryptable messages (UTDs) with no automatic recovery.

4. **Stale typing indicators.** Typing notifications (`m.typing`) are inherently
   short-lived, but a missed "stopped typing" EDU can leave a ghost typing
   indicator for minutes or until the next full sync.

### Why This Is Different From PDU Reconciliation

MSC0501 addresses PDU divergence in the room DAG. PDUs are:

- **Persistent** — they are stored permanently and form a cryptographically
  linked graph.
- **Append-only** — new events reference previous events; the history only
  grows.
- **Set-reconcilable** — version-vector comparison can identify missing state
  snapshots for a given EDU stream.

EDUs are fundamentally different:

- **Ephemeral** — they represent _current state_, not history. Only the latest
  value matters.
- **Last-writer-wins** — a newer presence update supersedes an older one. There
  is no merge conflict.
- **Scoped differently** — some EDUs are per-user-global (presence, device
  lists), others are per-user-per-room (receipts, typing).

This means EDU reconciliation does not need graph traversal or Bloom filters. It
needs **version-vector comparison** — a mechanism to ask "is your snapshot of
user X's state newer than mine?"

## Proposal

One new federation endpoint is introduced, plus a lightweight digest mechanism
for triggering reconciliation.

### 1. EDU State Digest: `GET /_matrix/federation/v1/edu_digest`

Returns a compact summary of the responding server's EDU state versions for
users that the requesting server shares rooms with. This allows the requester to
identify which users have stale state locally.

**Request:**

```http
GET /_matrix/federation/v1/edu_digest
```

**Query Parameters:**

<!-- markdownlint-disable MD013 -->

| Parameter  | Type   | Required | Description                                                                                      |
| :--------- | :----- | :------- | :----------------------------------------------------------------------------------------------- |
| `edu_type` | string | Yes      | The EDU type to query. See Supported EDU Types.                                                  |
| `since`    | string | No       | Opaque pagination token returned as `next_batch` by a previous response for the same `edu_type`. |
| `limit`    | int    | No       | Max user entries to return. Default 100, max 1000.                                               |

<!-- markdownlint-enable MD013 -->

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

<!-- markdownlint-disable MD013 -->

| Field                          | Type    | Required | Description                                                                                 |
| ------------------------------ | ------- | -------- | ------------------------------------------------------------------------------------------- |
| `users`                        | object  | Yes      | Sorted map of userID-to-version metadata.                                                   |
| `users.*.version`              | integer | Yes      | Monotonically increasing version counter for user's EDU state. See Version Semantics below. |
| `users.*.content_hash`         | string  | Yes      | Consistency checksum for the current EDU content body.                                      |
| `users.*.rooms`                | object  | No       | Per-room EDU snapshots for user-room-scoped types such as receipts.                         |
| `users.*.rooms.*.version`      | integer | No       | Monotonically increasing version counter for a user-room EDU state.                         |
| `users.*.rooms.*.content_hash` | string  | No       | Consistency checksum for a user-room EDU content body.                                      |
| `next_batch`                   | string  | No       | Pagination token. If present, more users are available.                                     |
| `edu_type`                     | string  | Yes      | The EDU type this digest covers.                                                            |

<!-- markdownlint-enable MD013 -->

**Version Semantics:**

The `version` field MUST be a monotonically increasing integer that advances
every time the user's EDU state of the given type changes. Servers MUST NOT rely
solely on `origin_server_ts` as the version, as it is sensitive to clock skew.

Instead, the version acts as a per-user, per-EDU-type monotonic counter:

- The server MUST maintain a strict counter per user/EDU-type.
- When state changes, the server MUST set `new_version = previous_version + 1`.
- `origin_server_ts` MAY be stored in the EDU content body for debugging or
  presentation, but it MUST NOT drive version selection.

The `content_hash` is an XXH3-64 hash of the canonical JSON representation of
the EDU content body. It is a consistency checksum, not a tie-breaker. If two
servers have the same `version` for a user but different `content_hash` values,
their responses are inconsistent. The requester MUST treat the returned
authoritative value as replacing its cached copy, and MUST NOT use the
lexicographically larger hash to choose a winner.

**Scoping (Privacy):**

The responding server MUST only include users that share at least one room with
the requesting server.

For EDU types with a clear origin server, the responding server MUST be
authoritative for the returned entries. In practice, that means `m.presence` and
`m.device_list_update` entries MUST be sourced from the user's homeserver, and
`m.receipt` / `m.typing` entries MUST be sourced from the authoritative room
server for that stream. Requesters MUST treat entries from a non-authoritative
server as invalid.

**Authorization (Privacy):**

The responding server MUST perform a **strict S2S routing index intersection**.
Before responding, the server MUST intersect the queried users against the
homeserver's materialized S2S routing table (a list of all users sharing at
least one room with the requester). If a user is not in this set, they MUST NOT
be included in the response, preventing metadata leakage.

### 2. EDU State Fetch: `POST /_matrix/federation/v1/edu_state`

Given a set of user IDs, returns the current EDU state for those users. This is
the "give me the latest" endpoint.

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

<!-- markdownlint-disable MD013 -->

| Field      | Type     | Required | Description                                      |
| ---------- | -------- | -------- | ------------------------------------------------ |
| `edu_type` | string   | Yes      | The EDU type to fetch.                           |
| `user_ids` | [string] | Yes      | User IDs to fetch state for. Max 200 per request |
|            |          |          | to prevent abuse.                                |

<!-- markdownlint-enable MD013 -->

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

<!-- markdownlint-disable MD013 -->

| Field              | Type     | Required | Description                                                        |
| ------------------ | -------- | -------- | ------------------------------------------------------------------ |
| `edu_type`         | string   | Yes      | The EDU type.                                                      |
| `states`           | object   | Yes      | Map of user ID to current state.                                   |
| `states.*.version` | integer  | Yes      | Version counter matching the `edu_digest` value.                   |
| `states.*.content` | object   | Yes      | The full EDU content body.                                         |
| `unknown_user_ids` | [string] | Yes      | User IDs from the request that the server does not have state for. |

<!-- markdownlint-enable MD013 -->

### Supported EDU Types

The following EDU types are eligible for state reconciliation:

<!-- markdownlint-disable MD013 -->

| EDU Type               | Scope         | Reconciliation Strategy                   |
| ---------------------- | ------------- | ----------------------------------------- |
| `m.presence`           | Per-user      | Compare version; fetch if remote is newer |
| `m.receipt`            | Per-user-room | Compare version per room; fetch if newer  |
| `m.device_list_update` | Per-user      | Compare version; fetch full device list   |
| `m.direct_to_device`   | Per-user      | NOT reconcilable (point-to-point, at-most |
|                        |               | -once delivery; cannot be replayed)       |
| `m.typing`             | Per-user-room | NOT reconcilable (inherently transient;   |
|                        |               | stale within seconds)                     |

<!-- markdownlint-enable MD013 -->

**Per-user-room scoping (receipts):**

For EDU types scoped to a user-room pair, the `edu_digest` response includes a
nested structure:

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

For receipts, `origin_server_ts` SHOULD be stored in the EDU content body for
the referenced event, but reconciliation MUST use the monotonic `version`
counter only. That keeps receipt ordering visible without reintroducing the
clock-skew problem this MSC avoids elsewhere.

### Reconciliation Protocol

The EDU reconciliation flow is simpler than PDU reconciliation because there is
no graph to traverse — only snapshots to compare:

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

### Gossip Scheduling

EDU reconciliation SHOULD be scheduled independently from PDU reconciliation
(MSC0501), with different intervals reflecting the urgency of each EDU type. To
prevent cluster-wide "thundering herd" synchronization waves during large
homeserver restarts or network partition recovery, implementations MUST apply a
**randomized scheduling jitter of ±15%** to all base scheduling intervals:

1. **`m.presence`** — Reconcile every 60 seconds with each peer. Presence
   staleness is highly visible to end users and should be corrected quickly.

2. **`m.device_list_update`** — Reconcile every 30 seconds. Device list
   staleness directly causes encryption failures and Undecryptable Messages
   (UTDs), which are critical to user trust. Because of this, homeservers MUST
   aggressively prioritize device list reconciliation over other EDU types and
   execute it immediately when a new S2S connection is established.

3. **`m.receipt`** — Reconcile every 300 seconds. Read receipt staleness is
   cosmetically annoying but not functionally harmful.

4. **Back-off** — If a peer's `edu_digest` shows no version changes across 3
   consecutive polls, the server SHOULD double the interval for that peer up to
   a maximum of 3600 seconds (applying the ±15% jitter to the backed-off
   intervals). Any incoming EDU from the peer resets the back-off.

### ETag Optimization

The `edu_digest` endpoint supports conditional requests using the same pattern
as MSC0501:

```http
GET /_matrix/federation/v1/edu_digest?edu_type=m.presence
If-None-Match: "xxh3:deadbeef"
```

The ETag SHOULD be computed as:

> `XXH3-64(canonical_json({edu_type, users, next_batch}))`

Because the hash covers the full representation, any change to a returned user,
room, or pagination cursor changes the ETag. Servers MAY cache the serialized
page, but they MUST NOT derive the ETag from a single maximum version counter.
Conditional requests are therefore most useful on a stable first page; page-
specific `since` requests should be treated as ordinary incremental fetches.

### Capability discovery

Servers advertise support through `GET /_matrix/federation/v1/version`. The
feature is signaled in `unstable_features` as
`tk.nutra.msc0502.edu_reconciliation`.

```json
{
  "unstable_features": {
    "tk.nutra.msc0502.edu_reconciliation": true
  }
}
```

Servers that do not advertise this flag SHOULD be treated as not supporting
`edu_digest` or `edu_state`. Receivers SHOULD avoid repeated probes to
unsupported peers; a `501 Not Implemented` response, or a `404` response with
`M_UNRECOGNIZED` or a non-Matrix body, SHOULD be cached as unsupported for at
least 24 hours unless an operator explicitly overrides the cache. The cache MUST
be invalidated on any observed change to the peer's `/version` document.

## Potential issues

### Scale with Large User Bases

A large homeserver (e.g., matrix.org) may have hundreds of thousands of users
sharing rooms with a given peer. The stream-based `edu_digest` (using `since`
tokens) mitigates this by only fetching incremental changes.

### Privacy Implications of Presence Probing

The `edu_digest` endpoint could be used to probe whether a specific user is
online without being in a shared room.

- **Mitigation:** The responding server MUST perform strict S2S routing index
  intersection to ensure only users sharing at least one room with the
  requesting server are included.

## Alternatives

### Piggybacking on `/send` Transactions

Instead of dedicated endpoints, EDU state could be reconciled by including
"state refresh" EDUs in regular `/send` transactions. The origin server would
periodically re-send the current state for all users, ensuring that even if the
original EDU was dropped, a refresh will eventually arrive.

This was rejected because:

1. It wastes bandwidth by re-sending state that the remote server already has
   (no diffing mechanism).
2. It couples EDU refresh frequency to `/send` transaction frequency, which
   varies wildly between active and idle rooms.
3. It does not allow the requesting server to selectively fetch only the users
   whose state is stale.

### Per-Room EDU Sync

An alternative design would scope EDU reconciliation to individual rooms
(similar to MSC0501's per-room approach). This was rejected because:

1. Presence and device lists are per-user, not per-room. A per-room approach
   would require redundant queries for users in multiple shared rooms.
2. The version-vector approach naturally handles per-user state with a single
   digest query per peer.

### Extending MSC0501

EDU reconciliation could be added as an extension to MSC0501 rather than a
separate proposal. This was rejected because:

1. The data models are fundamentally different (DAG vs. last-writer-wins).
2. The reconciliation algorithms are different (version-vector comparison vs.
   graph traversal).
3. Separate proposals allow independent review and implementation timelines.
4. Gossip scheduling parameters differ significantly between PDUs and EDUs.

## Security considerations

### Information Disclosure

The `edu_digest` endpoint reveals which users are hosted on the responding
server and their EDU activity patterns (version advancement rate). The S2S
routing intersection requirement minimizes this metadata leakage.

### Denial of Service

- **Rate limiting:** Servers MUST apply per-peer rate limiting. Recommended: 1
  `edu_digest` request per 10 seconds per EDU type per peer; 1 `edu_state`
  request per 10 seconds per peer.
- **Pagination caps:** The `limit` parameter on `edu_digest` bounds the response
  size. Servers MUST enforce `limit <= 1000`.
- **User ID caps:** The `edu_state` endpoint accepts at most 200 user IDs per
  request.

### Replay Attacks

A malicious server could return stale EDU state with an artificially high
version number, causing the requesting server to accept outdated state and
ignore future legitimate updates.

Mitigation: servers SHOULD cross-validate the `content_hash` against the
returned content. If the hash does not match, the response MUST be discarded.
Additionally, servers SHOULD prefer EDU state received via normal `/send`
transactions over reconciliation responses when the `/send` state has a higher
version.

## Unstable prefix

The following mapping will be used for identifiers in this MSC during
development:

<!-- markdownlint-disable MD013 -->

| Proposed final identifier             | Purpose         | Development identifier                                     |
| ------------------------------------- | --------------- | ---------------------------------------------------------- |
| `tk.nutra.msc0502.edu_reconciliation` | capability flag | `true`                                                     |
| `/_matrix/federation/v1/edu_digest`   | endpoint        | `/_matrix/federation/unstable/tk.nutra.msc0502/edu_digest` |
| `/_matrix/federation/v1/edu_state`    | endpoint        | `/_matrix/federation/unstable/tk.nutra.msc0502/edu_state`  |

<!-- markdownlint-enable MD013 -->

## Dependencies

This MSC has no hard dependencies on other unaccepted MSCs.
