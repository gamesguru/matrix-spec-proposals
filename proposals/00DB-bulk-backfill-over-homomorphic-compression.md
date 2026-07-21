# MSC00DB: Bulk Backfill over Homomorphic Compression

Matrix backfill currently retrieves historical events through APIs optimized for
modest timeline gaps. Very large gaps, archival imports, and post-outage
recovery can require many round trips and repeated validation work. The result
is slow convergence and high bandwidth usage precisely when a server is trying
to recover missing history.

This MSC introduces a bulk backfill workflow that transfers historical timeline
segments as compressed chunks with explicit state and signature commitments. Its
primary capability is **bulk historical ingestion**: a receiver can request a
bounded range of history, stream compressed event payloads, and validate the
batch against compact commitments before admitting the events to normal
authorization and persistence paths.

This proposal depends on
[MSC00DA: BLS Signatures and Non-Interactive Aggregation](./00DA-bls-signatures-non-interactive-aggregation.md)
for optional aggregate signature proofs. It also aligns with the accumulator
model in [MSC4500](./4500-state-accumulators.md), but does not require MSC4500
transaction digests to be deployed for ordinary federation.

## Proposal

### Relationship to existing backfill

This MSC adds a new federation endpoint for bulk historical ingestion. Existing
`/_matrix/federation/v1/backfill/{roomId}` and
`/_matrix/federation/v1/get_missing_events/{roomId}` behavior is unchanged.

Servers MAY use the bulk endpoint when both sides advertise support. A receiver
MUST be able to fall back to existing backfill APIs if the sender does not
support this MSC, cannot serve the requested range, or returns commitments the
receiver cannot verify.

### Capability discovery

Servers advertise support in `/_matrix/federation/v1/version` feature flags:

```json
{
  "unstable_features": {
    "tk.nutra.msc00db.bulk_backfill": true,
    "xzip": true,
    "bls12-381-g2": true
  }
}
```

Support for `tk.nutra.msc00db.bulk_backfill` indicates that the endpoint shape
is understood. Compression algorithms and aggregate signature algorithms are
advertised separately so that future encodings can be added without replacing
the endpoint.

### Endpoint definition

This MSC defines:

```text
POST /_matrix/federation/unstable/tk.nutra.msc00db/bulk_backfill/{roomId}
```

The request body is:

```json
{
  "start": ["$eventA:example.com"],
  "limit": 10000,
  "direction": "backwards",
  "min_depth": 1234,
  "compression": ["xzip"],
  "include_bls_aggregate": true,
  "aggregate_policy": "required",
  "state_commitments": ["lthash16"]
}
```

- `start`: Event IDs at the known edge of the receiver's timeline.
- `limit`: Maximum number of events requested.
- `direction`: `backwards` in this MSC. Future extensions may define forward
  historical repair.
- `min_depth`: Optional lower depth bound.
- `compression`: Ordered list of compression encodings the receiver accepts.
- `include_bls_aggregate`: Whether the receiver wants an MSC00DA aggregate proof
  over the returned PDUs.
- `aggregate_policy`: Optional policy for `bls_aggregate`. If omitted, defaults
  to `preferred`. Valid values are `preferred` and `required`.
- `state_commitments`: Ordered list of state commitment formats the receiver
  accepts.

The response body is:

```json
{
  "room_id": "!room:example.com",
  "encoding": "xzip",
  "chunk_id": "01J2Y4J3M2HG6N6WDFN8H8X3EB",
  "events": "<unpadded-base64-compressed-event-stream>",
  "event_count": 9481,
  "edges": {
    "requested_start": ["$eventA:example.com"],
    "oldest": ["$eventZ:example.org"],
    "newest": ["$eventA:example.com"]
  },
  "content_sha256": "<base64url-sha256-compressed-events>",
  "canonical_events_sha256": "<base64url-sha256-decoded-canonical-event-stream>",
  "state_commitments": {
    "algorithm": "lthash16",
    "before_oldest": {
      "digest": "<hex-blake2b-256-lthash16>",
      "n": 417
    },
    "after_newest": {
      "digest": "<hex-blake2b-256-lthash16>",
      "n": 421
    }
  },
  "bls_aggregate": {
    "algorithm": "bls12-381-g2",
    "signatures": [],
    "aggregate": "<unpadded-base64-compressed-g2-aggregate-signature>"
  }
}
```

The `bls_aggregate` field uses the aggregate signature object defined by
MSC00DA. If `include_bls_aggregate` is omitted or `false`, `bls_aggregate` MAY
be omitted. If `include_bls_aggregate` is `true` and `aggregate_policy` is
`preferred`, the sender SHOULD include `bls_aggregate` when it can produce a
valid aggregate proof over the returned PDUs.

If `include_bls_aggregate` is `true` and `aggregate_policy` is `required`, the
sender MUST include a valid `bls_aggregate` covering every returned PDU. If the
sender cannot produce such a proof, it MUST fail the request with
`400 M_INVALID_PARAM`.

### Event stream

After decompression, the event stream is a deterministic sequence of Matrix PDU
JSON objects. Events MUST be serialized as Matrix Canonical JSON, length
prefixed with an unsigned 32-bit little-endian byte count, and ordered from
newest to oldest for `direction: backwards`.

The decoded canonical event stream is the concatenation of those length-prefixed
canonical JSON byte strings. `canonical_events_sha256` is computed over that
decoded stream.

Receivers MUST verify both `content_sha256` and `canonical_events_sha256` before
parsing events for authorization.

### `xzip` compression profile

This MSC uses the name `xzip` for the initial experimental homomorphic
compression profile.

The `xzip` profile is intentionally scoped to transport encoding. It MUST NOT
change Matrix event JSON, event IDs, event hashes, room DAG semantics, or event
authorization. A receiver that does not understand `xzip` simply cannot decode
that response and MUST retry with another advertised encoding or fall back to
ordinary backfill.

An `xzip` stream is required to be:

- deterministic for a given decoded event stream and encoder version;
- splittable into independently verifiable chunks;
- bounded-memory to decode, with an advertised maximum window size;
- non-authoritative for protocol semantics, meaning decoded Matrix PDUs remain
  the only objects admitted to the room DAG.

The byte-level `xzip` coding tables are intentionally left unstable in this
draft. Implementations experimenting with `xzip` MUST version the encoder inside
the compressed stream and MUST reject unknown encoder versions.

### Receiver contract

Receiving servers MUST treat bulk backfill as an optimization over existing PDU
processing, not as a new trust path. After validating the response envelope, the
receiver MUST:

1. Decode the event stream.
2. Verify event IDs and content hashes according to the room version.
3. Verify required event signatures according to the room version.
4. If `bls_aggregate` is present, verify it according to MSC00DA.
5. Run normal authorization rules for every event before admitting it.
6. Persist only events that pass the same acceptance rules as ordinary
   federation backfill.

A valid bulk commitment, compression checksum, or BLS aggregate MUST NOT cause
an otherwise invalid event to be accepted.

### State commitments

When the sender supports the `lthash16` state commitment from MSC4500, it SHOULD
include `before_oldest` and `after_newest` commitments. These commitments let
the receiver compare the imported segment against known local state boundaries
or perform follow-up accumulator queries when a mismatch is detected.

State commitments are advisory unless a future room version makes them
mandatory. A receiver MUST NOT reject an otherwise valid event solely because an
optional state commitment is absent. A receiver SHOULD reject the bulk response
and fall back to ordinary backfill if a supplied state commitment is malformed
or internally inconsistent with the decoded events.

### Failure recovery

A receiver MUST reject a response with `M_INVALID_SIGNATURE` if a supplied
`bls_aggregate` is malformed, is inconsistent with the returned PDUs, or fails
aggregate signature verification. The receiver SHOULD retry with a smaller
`limit` to localize the invalid subset. If decoding fails, hashes do not match,
or the sender cannot serve the range, the receiver SHOULD fall back to existing
backfill endpoints.

Senders SHOULD cap `limit`, compressed response size, decoded response size, and
CPU time per request. If a request is too large, the sender SHOULD return
`M_LIMIT_EXCEEDED` with a suggested smaller limit.

## Potential issues

This MSC touches federation transport, storage pressure, compression, and
cryptographic proof plumbing. Splitting BLS aggregation into MSC00DA keeps the
pairing-cryptography review separate from the backfill API review, but the
implementation remains substantial.

Large compressed chunks can create denial-of-service risk if receivers allocate
based on claimed decoded sizes or defer validation until after parsing. The
receiver contract therefore requires bounded decoding and hash verification
before normal event processing.

## Alternatives

One alternative is to extend existing `/backfill` with optional compression
fields. That avoids a new endpoint, but makes legacy behavior harder to reason
about and complicates fallback logic.

Another alternative is to require ordinary HTTP compression only. That helps
bandwidth, but does not provide chunk-level commitments, aggregate signature
proofs, or state-boundary commitments for large historical ingestion.

## Security considerations

Bulk backfill responses are untrusted input. Receivers MUST enforce compressed
and decompressed size limits, streaming decode limits, canonical JSON checks,
event hash checks, signature checks, and normal authorization before
persistence.

Compression MUST NOT be used as an oracle over secrets. The event payloads in
this endpoint are historical PDUs already available through federation backfill;
servers MUST NOT mix secret material into the compressed stream.

BLS aggregate proofs are optimization proofs only. They do not replace event
authorization, room-version signature requirements, or state resolution.

State commitments can reveal coarse information about room state size and
boundary state. Servers SHOULD apply the same access controls they apply to
ordinary backfill and MUST NOT serve bulk history to a server that would not be
allowed to request the same events through existing federation APIs.

## Unstable prefix

Until accepted into the Matrix specification, implementations MUST use:

- Endpoint prefix:
  `/_matrix/federation/unstable/tk.nutra.msc00db/bulk_backfill/{roomId}`
- Feature flag: `tk.nutra.msc00db.bulk_backfill`

## Dependencies

This MSC depends on
[MSC00DA](./00DA-bls-signatures-non-interactive-aggregation.md) for BLS
aggregate signatures.

This MSC can use [MSC4500](./4500-state-accumulators.md) state commitments when
available, but ordinary event validation remains authoritative.
