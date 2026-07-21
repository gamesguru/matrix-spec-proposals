# MSC00DA: BLS signatures and non-interactive aggregation

Matrix federation currently verifies each required event signature
independently. This model, while easy and reliable, scales poorly for massive
workflows. Backfill, full room import, and future state-summary protocols often
need to prove that many origin signatures were checked without forcing every
receiver to repeat every pairing or signature verification step immediately.

This MSC introduces BLS signature aggregation as a key federation signing
capability for room versions that opt in.

Definition: (**non-interactive aggregation**) multiple independently produced
signatures can be combined into compact proofs that a receiver can verify
against the corresponding public keys and messages.

## Proposal

### Algorithm profile

This MSC defines the stable algorithm identifier `bls12-381-g2` for BLS
signatures over the BLS12-381 curve using the IETF hash-to-curve suite for
signatures in `G2` and public keys in `G1`.

The exact ciphersuite for event signatures and aggregate verification is:

```text
BLS_SIG_BLS12381G2_XMD:SHA-256_SSWU_RO_NUL_
```

Server-key proof-of-possession signatures use a custom binding-signature profile
built on the same BLS12-381 G2 parameters, hash-to-curve suite, public-key
group, signature group, and serialization rules. That binding-signature profile
uses the following domain-separation tag:

```text
MATRIX_MSC00DA_BLS12381G2_POP_V1_
```

To create `pop`, the server computes the BLS signing operation over the
canonical proof-of-possession binding object below with its BLS private key
using the `MATRIX_MSC00DA_BLS12381G2_POP_V1_` domain-separation tag. To verify
`pop`, receivers run the corresponding BLS verification operation over the
advertised BLS public key, the same canonical binding object, and the same tag.
Implementations MUST verify `pop` before aggregating signatures from distinct
public keys. This binding-signature profile is separate from the event-signature
ciphersuite above and does not use the standard IETF POP ciphersuite.

### Server keys

Homeservers that support this MSC advertise BLS verify keys through
`/_matrix/key/v2/server` in the same `verify_keys` and `old_verify_keys`
structures used for existing server signing keys.

A BLS verify key object uses the existing `key` field (containing the compressed
BLS12-381 G1 public key, unpadded base64 encoded) and adds:

- `pop`: A proof-of-possession signature over the canonical server-key binding
  object (below), encoded as unpadded base64.

BLS keys use the existing server-key validity semantics. BLS verify keys in
`verify_keys` do not carry a separate expiry field; their effective `expires_ts`
is the enclosing server-key response's `valid_until_ts`. BLS verify keys in
`old_verify_keys` use that entry's `expired_ts` as their effective `expires_ts`.
If an implementation encounters any additional per-key expiry field, it MUST
ignore that field for BLS key acceptance and use the timestamp defined by the
enclosing server-key structure.

The signed proof-of-possession message is the standard canonical representation:

```json
{
  "action": "bls-server-key-proof-of-possession",
  "algorithm": "bls12-381-g2",
  "key_id": "bls12-381-g2:abc123",
  "public_key": "<unpadded-base64-compressed-g1-public-key>",
  "server_name": "example.com"
}
```

Receivers MUST verify `pop` before accepting a BLS key for event-signature or
aggregate-signature verification. A key with a missing, malformed, or invalid
proof of possession MUST be treated as unusable.

### PDU signatures

Room versions that opt in to this MSC MAY allow origin servers to sign PDUs with
BLS. Legacy room versions are unchanged.

For a BLS-signed PDU, the message is the UTF-8 byte sequence of the standard
canonical event JSON (e.g., after removing `signatures` and `unsigned`).

The `signatures` object uses the algorithm key identifier as usual:

```json
{
  "signatures": {
    "example.com": {
      "bls12-381-g2:abc123": "<unpadded-base64-compressed-g2-signature>"
    }
  }
}
```

Receiving servers MUST apply the room version's signature requirements. If the
room version requires BLS signatures, the required origin signature MUST verify
under a valid advertised BLS key. If the room version merely permits BLS
signatures, receivers MAY verify them as advisory signatures while continuing to
require whatever signature algorithm the room version already mandates.

### Aggregate signature object

Protocols that depend on this MSC may carry an aggregate signature object:

```json
{
  "algorithm": "bls12-381-g2",
  "signatures": [
    {
      "event_id": "$event1:example.com",
      "server_name": "example.com",
      "key_id": "bls12-381-g2:abc123",
      "message_hash": "<unpadded-base64-sha256-canonical-event-signing-input>"
    }
  ],
  "aggregate": "<unpadded-base64-compressed-g2-aggregate-signature>"
}
```

The `signatures` array is the verification transcript. Each entry binds one
message, one server name, and one BLS key ID. The `message_hash` is not a
replacement for the event-signing input; it is a compact transcript commitment
that lets receivers identify the message bytes they must reconstruct before
verification.

Receivers MUST reconstruct each event-signing input from the corresponding PDU
and MUST verify that `message_hash` equals `SHA-256(signing_input)`. Receivers
MUST NOT verify an aggregate solely against transmitted hashes.

Aggregate verification succeeds only if all referenced keys are valid for the
event origin at the event's `origin_server_ts`, every proof of possession is
valid, every reconstructed message hash matches the transcript, and the BLS
aggregate signature verifies for the ordered `(public_key, message)` set.

### Room version requirements

This MSC does not by itself create a new room version. A future room-version MSC
MAY choose one of three deployment classes:

- **Advisory BLS:** BLS signatures are accepted and may be aggregated, but the
  existing mandatory event signature remains authoritative.
- **Hybrid BLS:** Events carry both the legacy mandatory signature and a BLS
  signature, allowing receivers to compare behavior during migration.
- **BLS-required:** BLS signatures are the mandatory event signature for that
  room version.

Bulk backfill protocols SHOULD require at least advisory BLS support for any
aggregate proof they accept, but MUST NOT treat aggregate proof success as a
substitute for room-version event authorization.

## Potential issues

BLS verification is pairing-based and substantially more specialized than
Matrix's existing Ed25519 verification path. Implementations need audited
libraries, subgroup checks, proof-of-possession validation, canonical encoding
checks, and strict ciphersuite agreement.

Aggregate signatures also create a failure-localization problem: one invalid
member invalidates the aggregate. Dependent protocols SHOULD include a fallback
path that lets receivers bisect or request unaggregated signatures for the
failing subset.

## Alternatives

The simplest alternative is to keep verifying all historical event signatures
individually. That avoids new cryptography, but gives no compact proof for bulk
workflows and makes high-volume backfill more expensive than necessary.

Another alternative is to use a Merkle tree of ordinary signatures. This gives a
compact commitment to a batch, but it does not reduce signature-verification
cost: every Ed25519 or FN-DSA signature still needs to be verified separately.

## Security considerations

Implementations MUST validate BLS public keys and signatures according to the
selected ciphersuite, including subgroup membership, canonical compressed
encodings, infinity rejection where required, and proof-of-possession checks.

Receivers MUST bind each aggregate member to the Matrix event-signing input,
server name, key ID, and key validity interval. An aggregate over hashes, event
IDs, or unsigned event JSON is not sufficient.

Servers MUST NOT aggregate signatures from keys that have not supplied a valid
proof of possession. Without this defense, rogue-key attacks can make aggregate
verification unsound.

This MSC does not weaken existing event authorization rules. Aggregate
verification proves that a set of signatures is valid; it does not prove that
the events are authorized, ordered, non-conflicting, or acceptable under room
state.

## Dependencies

None. Dependent companion MSCs are strictly descendants, not ancestors.
