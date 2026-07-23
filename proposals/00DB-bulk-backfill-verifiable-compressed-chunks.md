# MSC00DB: Bulk Backfill with Verifiable Compressed Chunks

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

The chunked compression profile in this MSC is transport encoding, not
homomorphic compression. Optional integrity profiles can be layered on top: this
MSC aligns with the accumulator model in
[MSC4500](./4500-state-accumulators.md), and
[MSC00DA](./00DA-bls-signatures-non-interactive-aggregation.md) can define a
severable BLS aggregate extension for the same endpoint. Neither MSC4500 nor
MSC00DA is required for ordinary bulk backfill.

## Proposal

### Relationship to existing backfill

This MSC adds a new federation endpoint for bulk historical ingestion. Existing
`/_matrix/federation/v1/backfill/{roomId}` and
`/_matrix/federation/v1/get_missing_events/{roomId}` behavior is unchanged.

Servers MAY use the bulk endpoint when both sides advertise support. A receiver
MUST be able to fall back to existing backfill APIs if the sender does not
support this MSC, cannot serve the requested range, or returns commitments the
receiver cannot verify.

This proposal is distinct from
[MSC4016: Streaming and resumable E2EE file transfer with random access](https://github.com/matrix-org/matrix-spec-proposals/pull/4016).
MSC4016 concerns encrypted media/file payloads, random access, resumable upload,
and per-block AEAD authentication. Its open Merkle-tree question is about
binding encrypted file chunks so a media server cannot reorder or substitute
chunks while still satisfying local block checks. MSC00DB concerns federated
history backfill of already-signed Matrix PDUs. Chunk hashes in this proposal
are transport integrity checks for compressed event batches; they do not
authenticate secret media contents, do not replace event hashes or signatures,
and do not make chunk order authoritative for the room DAG.

### Relationship to MSC0501 frames

This MSC extends a history frame downward. In MSC0501 terminology, the
`edges.oldest` array in a successful bulk response is an antichain frame anchor:
after the receiver validates and ingests the returned segment, it can reconcile
above that new floor with MSC0501's set-reconciliation endpoint. Conversely,
MSC0501 repairs holes inside an already agreed frame, while this MSC fetches a
contiguous historical range to move the frame boundary.

### Capability discovery

Servers advertise support in `/_matrix/federation/v1/version` feature flags:

```json
{
  "unstable_features": {
    "tk.nutra.msc00db.bulk_backfill": true,
    "tk.nutra.msc00db.xzip": true,
    "tk.nutra.msc00da.bls12-381-g2": true
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
  "resume": {
    "transfer_id": "01J2Y4J3M2HG6N6WDFN8H8X3EB",
    "first_chunk": 12
  },
  "compression": ["tk.nutra.msc00db.xzip"],
  "include_bls_aggregate": true,
  "aggregate_policy": "required",
  "state_commitments": ["lthash16"]
}
```

- `start`: Event IDs at the known edge of the receiver's timeline.
- `limit`: Maximum number of events requested. Receivers MUST abort decoding and
  reject the response if the decoded event count exceeds `limit`.
- `direction`: `backwards` in this MSC. Future extensions may define forward
  historical repair.
- `min_depth`: Optional lower depth bound.
- `resume`: Optional resumability hint. If present, `transfer_id` identifies a
  previous response and `first_chunk` is the zero-based chunk index from which
  the sender should resume. Senders MAY reject unknown or expired transfer IDs
  with `400 M_INVALID_PARAM`.
- `compression`: Ordered list of compression encodings the receiver accepts. The
  sender MUST choose `encoding` from this list.
- `include_bls_aggregate`: Whether the receiver wants an MSC00DA aggregate proof
  over the returned PDUs.
- `aggregate_policy`: Optional policy for `bls_aggregate`. If omitted, defaults
  to `preferred`. Valid values are `preferred` and `required`.
- `state_commitments`: Ordered list of state commitment formats the receiver
  accepts. If the sender includes `state_commitments` in the response, its
  `algorithm` MUST be selected from this list.

If `aggregate_policy` is `required`, `include_bls_aggregate` MUST be `true`.
Senders MUST reject a request with `400 M_INVALID_PARAM` if `aggregate_policy`
is `required` and `include_bls_aggregate` is omitted or `false`.

If no mutually supported compression encoding exists, the sender MUST fail the
request with `400 M_INVALID_PARAM` and the receiver SHOULD fall back to ordinary
backfill. If no mutually supported state-commitment format exists, the sender
MUST omit `state_commitments` unless a future profile makes state commitments
mandatory.

The response has content type `application/octet-stream`. The body is not
Base64-wrapped in JSON. It starts with a 32-bit little-endian manifest length,
followed by that many bytes of Matrix canonical JSON response manifest, followed
by the raw compressed chunk stream for the selected `encoding`:

```text
manifest_len uint32-le
manifest      manifest_len bytes of Matrix canonical JSON
chunk_stream  remaining bytes
```

The manifest has this shape:

```json
{
  "room_id": "!room:example.com",
  "encoding": "tk.nutra.msc00db.xzip",
  "transfer_id": "01J2Y4J3M2HG6N6WDFN8H8X3EB",
  "first_chunk": 0,
  "chunk_count": 19,
  "event_count": 9481,
  "edges": {
    "requested_start": ["$eventA:example.com"],
    "oldest": ["$eventZ:example.org"],
    "newest": ["$eventA:example.com"]
  },
  "content_sha256": "<unpadded-standard-base64-sha256-compressed-event-stream>",
  "canonical_events_sha256": "<unpadded-standard-base64-sha256-decoded-canonical-event-stream>",
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
    "signatures": [
      {
        "event_id": "$eventA:example.com",
        "server_name": "example.com",
        "key_id": "bls12-381-g2:abc123",
        "message_hash": "<unpadded-base64-sha256-canonical-event-signing-input>"
      }
    ],
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

Receivers MUST reject a response before admitting any events if `encoding` is
not one of the `compression` values requested in the request, if a supplied
`state_commitments.algorithm` is not one of the `state_commitments` values
requested in the request, or if the request's `aggregate_policy` is `required`
and `bls_aggregate` is absent.

`transfer_id` is scoped to the responding server, room ID, request range, and
encoding. A receiver MAY resume a dropped transfer by repeating the request with
`resume.first_chunk` set to the first missing chunk. Senders SHOULD keep
transfer IDs resumable for at least 10 minutes, but MAY expire them earlier
under resource pressure. A resumed response contains the suffix beginning at
`first_chunk`, but its manifest describes the complete transfer: `chunk_count`,
`event_count`, `edges`, state commitments, and the global hashes retain their
original full-transfer meaning. The receiver MUST retain the previously
verified prefix and combine it with the resumed suffix before checking the
global hashes, final event count, and boundary commitments. The expected number
of chunks in a resumed suffix is `chunk_count - first_chunk`.

### Event stream

After decompression, the event stream is a deterministic sequence of Matrix PDU
JSON objects. Events MUST be serialized as Matrix Canonical JSON, length
prefixed with an unsigned 32-bit little-endian byte count, and ordered from
newest to oldest for `direction: backwards`.

The decoded canonical event stream is the concatenation of those length-prefixed
canonical JSON byte strings. `canonical_events_sha256` is computed over that
decoded stream and encoded as unpadded standard Base64. `content_sha256` is
computed over `chunk_stream`, excluding the manifest length and manifest bytes,
and encoded as unpadded standard Base64.
`canonical_events_sha256` duplicates the per-chunk `decoded_sha256` coverage,
but is retained as defense-in-depth over the complete decoded event sequence and
event order.

Receivers MUST verify both `content_sha256` and `canonical_events_sha256` over
the complete transfer before parsing events for authorization. For a resumed
response, this means combining the retained verified prefix with the received
suffix first. Receivers MUST count events while decoding, abort if the complete
decoded event count exceeds the request `limit`, and verify that the final
decoded count equals `event_count` and is less than or equal to `limit` before
persisting any event from the response.

### `xzip` compression profile

This MSC uses the identifier `tk.nutra.msc00db.xzip` for the initial
experimental compressed chunk profile.

The `tk.nutra.msc00db.xzip` profile is intentionally scoped to transport
encoding. It MUST NOT change Matrix event JSON, event IDs, event hashes, room
DAG semantics, or event authorization. A receiver that does not understand
`tk.nutra.msc00db.xzip` simply cannot decode that response and MUST retry with
another advertised encoding or fall back to ordinary backfill.

A `tk.nutra.msc00db.xzip` stream is required to be:

- deterministic for a given decoded event stream and encoder version;
- splittable into independently verifiable chunks;
- bounded-memory to decode, with an advertised maximum window size;
- non-authoritative for protocol semantics, meaning decoded Matrix PDUs remain
  the only objects admitted to the room DAG.

The byte-level coding tables are intentionally left unstable in this draft.
Advertising `tk.nutra.msc00db.xzip` means the server supports the unstable wire
profile below:

```text
magic              = "MSC00DBXZIP"        ; 11 ASCII bytes
version            = uint16-le
max_window_size    = uint32-le            ; bytes
chunk_count        = uint32-le
chunk              = decoded_size uint32-le
                   compressed_size uint32-le
                   decoded_sha256 32 bytes
                   compressed_bytes compressed_size bytes
```

The only version defined by this draft is `1`. Senders MUST put the encoder
version and maximum decompression window size in the stream header. Receivers
MUST reject streams with an unknown `version`, a `max_window_size` greater than
their local configured limit, malformed chunk framing, or a chunk whose decoded
bytes do not match its `decoded_sha256`. Each chunk is decoded and verified
independently before its events are passed to the event-stream decoder. Chunk
indices are zero-based and refer to this stream order. A resumed response whose
manifest `first_chunk` is nonzero contains the same stream format starting at
that chunk index.

### Receiver contract

Receiving servers MUST treat bulk backfill as an optimization over existing PDU
processing, not as a new trust path. After validating the response envelope, the
receiver MUST:

1. Decode the event stream.
2. Abort if more than `limit` events are decoded, or if the final decoded count
   does not equal `event_count`.
3. Verify event IDs and content hashes according to the room version.
4. Verify required event signatures according to the room version.
5. If the request's `aggregate_policy` is `required`, reject responses without
   `bls_aggregate`; if `bls_aggregate` is present, verify it according to the
   severable BLS aggregate extension defined by MSC00DA.
6. Run normal authorization rules for every event before admitting it.
7. Persist only events that pass the same acceptance rules as ordinary
   federation backfill.

A valid bulk commitment, compression checksum, or BLS aggregate MUST NOT cause
an otherwise invalid event to be accepted.

### State commitments

When the sender supports the `lthash16` state commitment from MSC4500, it SHOULD
include `before_oldest` and `after_newest` commitments. `before_oldest` commits
to state after `edges.oldest` as resolved by the sender. `after_newest` commits
to state after `edges.newest` as resolved by the sender. These commitments let
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
the decoded event count exceeds `limit`, no mutually supported response format
is available, or the sender cannot serve the range, the receiver SHOULD fall
back to existing backfill endpoints.

Senders SHOULD cap `limit`, compressed response size, decoded response size, and
CPU time per request. If a request is too large, the sender SHOULD return
`M_LIMIT_EXCEEDED` with a suggested smaller limit.

## Potential issues

This MSC touches federation transport, storage pressure, compression, and
chunk-integrity plumbing. BLS aggregation is intentionally severable into
MSC00DA so pairing-cryptography review does not gate the compressed backfill API
itself, but the implementation remains substantial.

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

BLS aggregate proofs, when supported through the MSC00DA extension, are
optimization proofs only. They do not replace event authorization, room-version
signature requirements, or state resolution.

State commitments can reveal coarse information about room state size and
boundary state. Servers SHOULD apply the same access controls they apply to
ordinary backfill and MUST NOT serve bulk history to a server that would not be
allowed to request the same events through existing federation APIs.

## Unstable prefix

Until accepted into the Matrix specification, implementations MUST use:

- Endpoint prefix:
  `/_matrix/federation/unstable/tk.nutra.msc00db/bulk_backfill/{roomId}`
- Feature flag: `tk.nutra.msc00db.bulk_backfill`
- Compression identifier: `tk.nutra.msc00db.xzip`

## Dependencies

This MSC has no hard dependency on MSC00DA. MSC00DA can define the optional BLS
aggregate extension for the `bls_aggregate` field.

This MSC can use [MSC4500](./4500-state-accumulators.md) state commitments when
available, but ordinary event validation remains authoritative.
