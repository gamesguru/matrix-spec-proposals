# MSC4511 Part C: Merkleized Metadata and Causal Search Tree

This companion to [Part A](4511-topological-metadata-query-api.md) sketches how
a future room version could make selected topology metadata independently
provable by committing it into event identity. Current room versions should use
Part A as hint-only; archived Part B provides responder-scoped attestations
without a room-version change.

## Unstable prefix

<!-- markdownlint-disable MD013 -->

| Proposed final identifier | Purpose      | Development identifier |
| ------------------------- | ------------ | ---------------------- |
| `room_version`            | room version | `tk.nutra.msc4511.12`  |

<!-- markdownlint-enable MD013 -->

## Proposal

To make selected event metadata independently verifiable, this MSC sketches a
split canonicalization design for future room versions to opt into.

A compatible future room version modifies event hashing to generate an
`event_root` from isolated metadata leaves:

- `prev_events_hash`: canonical hash of the event's `prev_events`;
- `auth_events_hash`: canonical hash of the event's `auth_events`;
- `prev_state_events_hash`: canonical hash of the event's `prev_state_events`,
  when the room version defines State DAGs; otherwise this component is the
  canonical hash of `null`;
- `event_header_root`: Merkle root over routing and authorship fields:
  `room_id`, `sender_localpart`, `sender_domain`, `type`, `state_key`,
  `redacts`, `depth`, and `origin_server_ts`;
- `redacted_content_hash`: canonical hash of the event body fields that strictly
  survive redaction according to the room version's redaction algorithm;
- `ephemeral_content_hash`: canonical hash of the event body fields that are
  stripped during redaction;
- `content_hash`: the inner Merkle hash combining `redacted_content_hash` and
  `ephemeral_content_hash`, computed with the `inner_hash` construction below.
  This is distinct from the legacy event `hashes` field unless a future
  room-version MSC explicitly maps them together;
- `other_signed_fields_hash`: canonical hash of every remaining signed event
  field that is not included in `prev_events_hash`, `auth_events_hash`,
  `event_header_root`, or `content_hash`, strictly excluding the `signatures`
  and `unsigned` dictionaries;
- `event_root`: the root hash committing to the above components.

The event also contains a required signed `causal_set` field, described below.
It is included in `other_signed_fields_hash`, and is therefore committed by
`event_root` and the event ID without changing the partition above.

The future room version MUST define this partition so every signed,
identity-relevant event field is committed to exactly once. Two events which
differ in any signed field that contributes to event identity, including
`redacts`, MUST NOT derive the same `event_root` or event ID.

`sender_localpart` and `sender_domain` MUST be committed as two independent
header leaves rather than one combined `sender` leaf, using the same
first-`:`-boundary split defined in
[Part A](4511-topological-metadata-query-api.md) for the hint-mode
`sender_domain` field: the local part is everything between the leading `@` and
the first `:`, and the domain is everything after it. A room version adopting
this format MUST reject events whose `sender` does not parse under that grammar
before deriving `event_root`, since an unparsable `sender` would otherwise have
no defined split. Splitting the leaf this way is required, not merely
convenient: with a single `sender` leaf, any proof that discloses authorship
information necessarily discloses the full MXID, including the localpart. With
`sender_domain` committed separately, a prover can disclose and prove only the
sending server's identity, and a verifier can check signature entitlement,
without either party handling the sender's localpart at all. The sender's full
MXID remains recoverable and provable by disclosing both leaves together as
`"@" || sender_localpart || ":" || sender_domain`, so no authorship information
is lost, only made separable.

The hash algorithm is `SHA3-256`. Each hash input is domain-separated:

```text
leaf_hash =
  SHA3-256("msc4511:leaf:v1" || field_name || "\x00" || canonical_value)

inner_hash =
  SHA3-256("msc4511:node:v1" || left_hash || right_hash)

redacted_content_hash =
  SHA3-256("msc4511:leaf:v1" || "redacted_content" || "\x00" ||
           canonical_redacted_value)

ephemeral_content_hash =
  SHA3-256("msc4511:leaf:v1" || "ephemeral_content" || "\x00" ||
           canonical_ephemeral_value)

content_hash =
  SHA3-256("msc4511:node:v1" || redacted_content_hash || ephemeral_content_hash)

event_root =
  SHA3-256("msc4511:root:v1" || prev_events_hash || auth_events_hash ||
           event_header_root || content_hash || other_signed_fields_hash)
```

All concatenations above are byte concatenations: domain-separation strings and
`field_name` are UTF-8 bytes; `\x00` is a single `0x00` byte; `canonical_value`
is the UTF-8 encoding of the canonical JSON value; and
`left_hash`/`right_hash`/component hashes are the raw 32-byte hash outputs.

The top-level component hashes `prev_events_hash`, `auth_events_hash`, and
`other_signed_fields_hash` are computed with the leaf-hash construction above,
using the field names `prev_events`, `auth_events`, and `other_signed_fields`
respectively. `content_hash` is instead the `inner_hash` combination of
`redacted_content_hash` and `ephemeral_content_hash`, each of which is itself a
leaf hash over the room version's redaction-surviving and redaction-stripped
event body fields respectively, as shown above.

The domain-separation strings use the stable MSC identifier `msc4511` and are
part of the event ID derivation. Implementations MUST NOT use the unstable
endpoint namespace or an implementation-local identifier for these domain
separators, because changing the identifier changes the derived `event_root` and
event ID.

### Header tree construction

The `event_header_root` is constructed as a binary Merkle tree. Header leaves
are ordered bytewise by field name. Missing optional fields, such as `redacts`,
use the canonical JSON value `null`; present fields use their standard Matrix
canonical JSON encoding.

Because the number of header leaves is not guaranteed to be a power of two,
implementations MUST construct `event_header_root` using the Merkle tree
algorithm defined in
[RFC 6962, Section 2.1](https://datatracker.ietf.org/doc/html/rfc6962#section-2.1),
substituting the domain-separated leaf and inner hash constructions defined
above for the RFC's `0x00`- and `0x01`-prefixed hashes. Only the tree shape (the
largest-power-of-two split rule and its recursion) is taken from RFC 6962; no
padding leaves are used.

<!-- RFC 6962 tree shape: dyadic interval decomp over ordered leaves. -->

### Event IDs and signatures

The event ID is derived directly from the root:
`"$" || unpadded_base64url(event_root)`.

The event signature covers the canonical signed envelope containing this root:

```json
{
  "room_id": "!room:example.org",
  "room_version": "<room_version>",
  "event_root": "unpadded_base64url_sha3_256_hash"
}
```

An individual proof is self-contained only when it carries this complete
envelope together with the signature and the signing server identity. The
verifier MUST canonicalize the envelope with Matrix Canonical JSON, using the
exact `room_id`, `room_version`, and `event_root` values shown, remove no
additional fields, and verify the signature over the resulting UTF-8 bytes.
Implementations MUST NOT infer `room_id` or `room_version` from an outer query
or accept a proof that omits an envelope member; a deployment that uses trusted
request context instead MUST specify that context as part of its room-version
profile.

For room versions adopting this format, a future room-version MSC MUST specify
how the root signature interacts with, or replaces, existing event authorization
and verification rules. This keeps the proposal focused on the topology query
API and defers signature migration mechanics to the room-version proposal.

### Causal sparse Merkle sum trie

Every event `E` MUST commit to the set of event IDs in its strict causal past.
For a room version with State DAGs, `prev_state_events` are additional causal
predecessor edges and are included in the same set-union recurrence:

$$
C(E) = \bigcup_{P \in \operatorname{prev\_events}(E) \cup
                 \operatorname{prev\_state\_events}(E)}
       \left(C(P) \cup \{P\}\right).
$$

The current event is excluded, avoiding self-reference: its event ID can commit
to the root of `C(E)` because every member ID is already known. At a merge, the
population is the set union of the predecessor populations, not concatenation or
arithmetic addition; an event reachable through multiple predecessors occurs
once.

`C(E)` is represented as a persistent 256-level sparse Merkle trie. The search
key is the 32-byte digest encoded by a room-version event ID, interpreted from
most-significant bit to least-significant bit. A key's bit at depth `d` selects
the left (`0`) or right (`1`) child. This makes the structure a Merkle search
tree without imposing chronological order on the Matrix DAG.

Each node also commits to its exact subtree cardinality, making the structure a
Merkle sum trie. Counts are unsigned 64-bit integers. An implementation MUST
reject construction if an addition overflows; it MUST NOT wrap or saturate. The
hashes are:

```text
causal_leaf(key) =
  SHA3-256("msc4511:causal-leaf:v1" || key)

causal_node(depth, left_hash, left_count, right_hash, right_count) =
  SHA3-256("msc4511:causal-node:v1" || u16be(depth) ||
           left_hash || u64be(left_count) ||
           right_hash || u64be(right_count))
```

`depth` ranges from 0 at the root to 255 above the leaves. Empty hashes are
defined recursively from a distinguished empty leaf:

```text
empty[256] = SHA3-256("msc4511:causal-empty-leaf:v1")
empty[d] = causal_node(d, empty[d+1], 0, empty[d+1], 0)
```

An occupied leaf has count 1. An internal node's count is exactly
`left_count + right_count`. The empty set has root `empty[0]` and count 0.
Implementations MAY omit empty children physically and share unchanged nodes
between event roots; these storage choices do not alter the canonical root.

The event carries:

```json
"causal_set": {
  "algorithm": "msc4511.sparse_merkle_sum_v1",
  "root": "unpadded_base64url_sha3_256_hash",
  "count": 1234
}
```

The declared `count` MUST equal the count committed at the root. It is redundant
for integrity but allows population sizing without opening the root node.

The counts in this structure have deliberately narrow meanings. The root and
every internal node count the distinct event IDs in the causal-set population;
they do not count edges, paths, or predecessor references. `depth` remains the
event's scalar topological metadata and is committed in `event_header_root`; it
is not a population count. The cardinalities of `prev_events`, `auth_events`,
and, where applicable, `prev_state_events` are already bound by hashing their
canonical JSON arrays, whose lengths are part of the encoded values. Separate
cardinality fields for those lists would be redundant. An LtHash or other
commutative digest of the causal set is also not included: the sparse Merkle
root already supplies equality, search, proofs, and recursive diff, while the
sum counts supply authenticated cardinality. A future optimization MAY add a
domain-separated commutative digest, but it would be an auxiliary equality hint,
not a replacement for the root or its proof rules.

#### Room-version validity

Committing an arbitrary root does not prove that it represents the event's
causal past. A room version adopting this structure MUST make the recurrence for
`C(E)` an event-validity rule:

- an event with no `prev_events` commits the empty root and count zero;
- an event with one predecessor commits that predecessor's causal set after
  inserting the predecessor event ID;
- an event with multiple predecessors commits the exact set union of every
  predecessor causal set and every predecessor event ID.

A receiver MUST recompute this transition from locally held trie nodes or verify
an authenticated transition/union witness against every predecessor root. A
signature over `event_root` does not replace this check. If required predecessor
roots or witness nodes are unavailable, validation is deferred while they are
fetched, just as event processing already waits for required event and auth
data; the receiver MUST NOT accept an unchecked `causal_set` root as valid.

For a linear transition, the witness is a standard sparse-trie update path for
inserting the predecessor ID. For a merge, a witness recursively supplies nodes
where predecessor roots differ and permits equal roots to short-circuit. Every
opened branch is rehashed to each predecessor root and to the claimed union
root. The witness MUST demonstrate set union and duplicate elimination; merely
listing predecessor roots proves neither.

#### Search and proof operations

Part A MAY request proofs relative to a named anchor event `E`. A responder can
provide:

- **inclusion:** the event ID is a leaf in `C(E)`;
- **non-inclusion:** the key-directed path terminates in a canonical empty
  subtree;
- **prefix range:** a subtree root and count at a requested key-prefix;
- **recursive diff:** two holders compare corresponding subtree hashes and
  descend only where they differ;
- **multiproof:** shared siblings for several inclusion, non-inclusion, or
  prefix queries are transmitted once.

Every proof MUST identify the anchor event, algorithm, expected root, and root
count. Sibling entries carry both hash and count. Verifiers recompute every
internal hash and count to the anchor's committed root. Runs of canonical empty
siblings MAY be compressed as `(start_depth, length)`; decompression MUST yield
the exact `empty[d]` values above. A responder MUST NOT claim completeness from
a truncated proof.

The sum is not a substitute for search or hashing. It provides authenticated
cardinality for subtrees, useful for sizing reconciliation work and rejecting a
malformed proof whose child counts do not sum to its parent. Equal counts do not
imply equal populations; equality still requires the root hash.

#### Construction and merge cost

Adding one causal predecessor to an existing linear history path-copies at most
256 trie nodes, independent of population size. A multi-predecessor event must
compute the exact set union of its predecessor tries before inserting the
predecessor IDs. Equal subtree roots short-circuit; work is proportional to the
unequal structure visited, not necessarily to the number of predecessors or to
the symmetric difference in adversarially shaped cases. Implementations MUST NOT
derive a merge root by hashing predecessor roots together, because that would
commit to history shape and multiplicity rather than the causal event set.

The root authenticates a population; it does not guarantee that a server still
stores every witness node or PDU needed to answer a proof. A server unable to
materialize a requested proof returns the ordinary Part A limited/unavailable
result. Authorization and history-visibility checks still apply before revealing
event IDs or proof paths.

Requiring this root changes room-creation and federation availability. A server
cannot create or fully validate a merge event while lacking the predecessor trie
material needed to establish the union. Implementations should retain trie nodes
or compressed union witnesses alongside events for at least as long as they
expect to serve the corresponding history. This is a deliberate room-version
trade-off, not a backwards-compatible optimization for existing rooms.

### Draft test vectors

The following vectors are non-normative implementation regression vectors for
the split-canonicalization sketch above. They are generated by
`cmd/merkle-vectors` in the `gomatrixcrypto` reference repository and are
included to make the `msc4511:*:v1` domain-separation strings, Matrix Canonical
JSON inputs, RFC 6962 tree shape, component ordering, missing optional header
fields, and unpadded base64url event ID encoding easy to cross-check while this
MSC is still unstable.

These vectors predate the required `causal_set` field and test only the
split-canonicalization primitive; a complete conforming event vector for this
revision MUST also commit `causal_set` through `other_signed_fields_hash`. The
causal sparse Merkle sum trie vectors below, generated by the reference
`cmd/causal-vectors` command added to `gomatrixcrypto` alongside this revision,
exercise the empty, single-predecessor, merge-union, inclusion, and
non-inclusion cases so the trie construction can be cross-checked independently
of a full event vector.

The sample inputs are:

```json
{
  "event_header_root_fields": {
    "room_id": "!room:example.org",
    "sender_localpart": "alice",
    "sender_domain": "example.org",
    "type": "m.room.message",
    "state_key": null,
    "redacts": null,
    "depth": 42,
    "origin_server_ts": 123456789
  },
  "prev_events": ["$a:example.org"],
  "auth_events": ["$auth:example.org"],
  "content": {
    "body": "hello",
    "msgtype": "m.text"
  },
  "other_signed_fields": {
    "origin": "example.org"
  },
  "signature_envelope": {
    "event_root": "4ccc880527fe5f97d27a04105bb55e6c6e75d87928e54a6cd2973c224802ce91",
    "room_id": "!room:example.org",
    "room_version": "tk.nutra.msc4511.12"
  }
}
```

The `origin` value above is included only as sample signed input for the test
vector's `other_signed_fields_hash`. It does not define `origin` as a queryable
field for this MSC. It is an arbitrary signed property used to demonstrate hash
absorption only.

The `signature_envelope` value above is the sample canonical signed envelope for
the stated `event_root`. It is signed with the sample Ed25519 key below to make
the draft vector self-contained.

The generated outputs are:

```text
[msc4511-merkle]
event_header_root_hex = db91cc8e8d3eb0d13885c32f28dbd4215a111081383e25263749c65d9bf8bc37
prev_events_hash_hex = fe8934c852d5a646390f3734f99911606c40f4f8ca7fe4065814081e2fb1faef
auth_events_hash_hex = 2309b8433c96de36d4a55cfb263f3f3131a0874324a9bda59bfd9e73e3846ea1
content_hash_hex = 8bfc6857f7a86d45b263c551057d052dfa73ef29dee6e842c90d12143abec729
other_signed_fields_hash_hex = 272428680275d80a8b02254dbbbe13e93af0153a6e8d80746d7d95dd1df48d59
event_root_hex = 4ccc880527fe5f97d27a04105bb55e6c6e75d87928e54a6cd2973c224802ce91
event_id = $TMyIBSf-X5fSegQQW7VebG512Hko5Ups0pc8IkgCzpE
event_signature_public_key_base64 = LYZrYjxYptzTRzEYBZzYMMEfX/2yYYqQ+RCw62Hmsz4
event_signature_base64 = 592xXLqbyExpxL1Te7zobls1Gh+IYYbliYCN3jTTn2Ny0kRnFGCEc22Sh/ifTCh/IDsJWVnmRFgrWA7JAqchBA
```

`prev_events_hash_hex`, `auth_events_hash_hex`, `content_hash_hex`, and
`other_signed_fields_hash_hex` are unchanged from the pre-split vectors, since
none of those inputs reference `sender`. `event_header_root_hex`,
`event_root_hex`, `event_id`, and the signature values change because
`event_header_root` now commits `sender_localpart` and `sender_domain` as
separate leaves instead of a single `sender` leaf.

#### Draft causal sparse Merkle sum trie vectors

The sample inputs use three arbitrary 32-byte digests standing in for event-ID
keys `A`, `B`, and `C` (`0xa1`, `0xb2`, and `0xc3` repeated to fill 32 bytes,
respectively), and a fourth digest `D` (`0xd4` repeated) that is never inserted:

```text
[msc4511-causal-vectors]
empty_root_hex = 293689eda818133b4186c211acd350e9abec011a5d90fbfb3234321d76596093
empty_count = 0
single_predecessor_root_hex = 4c74b37b5d98971c00b15ba01c1cccdc15015fda9a5f03fc4a34f72bcf9e2493
single_predecessor_count = 1
merge_union_root_hex = c9507c08800822e798105c0a8d5ba48bdb71b9917f956240f32caada3eae9c47
merge_union_count = 3
inclusion_query_key_hex = b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2
inclusion_result = true
non_inclusion_query_key_hex = d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4
non_inclusion_result = false
```

- **Empty:** an event with no `prev_events` commits `empty_root_hex`
  (`empty[0]`) and count zero.
- **Single-predecessor:** an event with one predecessor `A` commits the causal
  set produced by inserting `A` into the empty set.
- **Merge-union:** two divergent single-predecessor branches, one having
  inserted `A` then `B`, the other `A` then `C`, are merged. The merge event's
  `causal_set` is their exact set union: `{A, B, C}`, count 3, not 4 — the
  shared key `A` is not double-counted, and the union root is identical to
  inserting `A`, `B`, and `C` into the empty set directly in any order.
- **Inclusion:** `B` is a member of the merged set; a responder proves inclusion
  with the key-directed path to `causal_leaf(B)`.
- **Non-inclusion:** `D` was never inserted into either branch or the merge; a
  responder proves non-inclusion by exhibiting a key-directed path that
  terminates in a canonical empty subtree.

### Cryptographic proof responses

When `proof` is requested in `fields` and the queried room version supports
split canonicalization, a server SHOULD include proof material for provable
requested fields inside the `proofs` sidecar object keyed by the corresponding
`event_id`. A room version adopting this format also extends the queryable
`fields` set with header leaves not exposed in hint-only mode, such as
`state_key` and `redacts`, since a field must be returnable to be provable.

Each `proofs` entry explicitly maps the proven fields to their Merkle paths,
provides any required top-level component hashes needed to reconstruct
`event_root`, and includes the event signature:

```json
"proofs": {
  "$missing_event_A": {
    "leaf_paths": {
      "prev_events": [],
      "sender_domain": [
        { "side": "right", "hash": "base64url_sha3_256_hash" },
        { "side": "right", "hash": "base64url_sha3_256_hash" },
        { "side": "left", "hash": "base64url_sha3_256_hash" }
      ],
      "origin_server_ts": [
        { "side": "left", "hash": "base64url_sha3_256_hash" },
        { "side": "right", "hash": "base64url_sha3_256_hash" },
        { "side": "right", "hash": "base64url_sha3_256_hash" }
      ]
    },
    "top_level_hashes": {
      "auth_events_hash": "base64url_sha3_256_hash",
      "content_hash": "base64url_sha3_256_hash",
      "other_signed_fields_hash": "base64url_sha3_256_hash"
    },
    "signatures": {
      "example.org": {
        "ed25519:key": "signature_base64"
      }
    }
  }
}
```

The example above discloses `sender_domain` but not `sender_localpart`;
`sender_localpart`'s hash is simply absorbed into one of the sibling hashes in
`sender_domain`'s `leaf_paths` entry, the same way any other undisclosed header
leaf would be. This is the selective-disclosure case this split exists for: a
verifier can confirm which server sent the event, and check signature
entitlement, without ever learning or requesting the sender's localpart.

To verify the authenticity of a field all the way to the root, the requester
performs the following steps:

1. Canonicalize each returned field and compute its domain-separated leaf hash.
2. Apply each step in `leaf_paths`, computing the parent inner hash using the
   provided left or right sibling, to reconstruct `event_header_root` or the
   relevant top-level component hash. For top-level components (`prev_events`,
   `auth_events`, content, and other signed fields), the path list is empty and
   the leaf hash is used directly.
3. Combine the reconstructed component with the remaining hashes in
   `top_level_hashes` to compute the master `event_root`. `top_level_hashes`
   MUST contain every component hash not reconstructed from a proof in the same
   response.
4. Verify that the event ID matches `"$" || unpadded_base64url(event_root)`.
5. If the proof is being used as authorship evidence, verify that the
   `sender_domain` leaf is disclosed or otherwise proven, then verify that the
   event signature is from that server name according to the room version's
   signing rules. `sender_domain` alone is sufficient for this check;
   `sender_localpart` does not need to be disclosed or proven to establish
   signature entitlement.

If a required hash is missing or any hash check fails, verification fails. By
chaining hashes upward, the server only needs to send missing neighbor hashes in
the proof, and the verifier recomputes the root locally.

A proof which omits `sender_domain` can authenticate disclosed data against a
known event ID, but does not by itself prove that the signing server is the
server entitled to sign for the event's sender. This distinction matters for
proof consumers outside the backwards-DAG walk, where the event ID may not have
been learned from a previously verified event.

The `signatures` object is not committed to `event_root`; like existing Matrix
signed JSON, signatures are excluded from the signed hash input. Intermediaries
can therefore strip signatures or append additional signatures without changing
the event ID. The signature map keys identify which server keys to try for
verification, but entitlement still comes from the expected server name given by
a proven or disclosed `sender_domain` field, not from `sender_localpart`.

The following side-by-side example DAG shows the metadata-query opportunity. The
left side illustrates the legacy fetch-and-verify path over the chain. The right
side shows the same DAG shape when Merkle proofs let the verifier check topology
metadata before deciding which full PDUs to fetch:

```text
Legacy: fetch-and-verify each hop         Merkleized: prove metadata first

    [known anchor]                                [known anchor]
          |                                             |
         e1                                            e1
        /  \                                          /  \
      e2    e3                                      e2    e3
        \   /                                        \    /
         e4                                            e4
          |                                             |
         e6 tip                                        e6 tip
          |                                             |
          v                                             v
  fetch full PDU / event                  fetch sparse metadata + Merkle proof
  verify Ed25519 signature                recompute root locally from sibling hashes
  verify hashes + auth rules              if root matches, choose next fetches
  repeat for every ancestor               fetch full PDUs before acceptance
```

If you prefer Mermaid, the same comparison can be rendered as two separate
subgraphs, but the ASCII layout above is the least ambiguous when the goal is a
literal left-right comparison.

Redaction execution semantics are still deferred to the future room-version MSC
that adopts split canonicalization: that room version MUST define exactly which
event body fields are `redacted_content` versus `ephemeral_content` under its
redaction algorithm. The commitment shape itself is not deferred: `content_hash`
is always the combination of `redacted_content_hash` and
`ephemeral_content_hash` defined above, so that a server executing a redaction
can drop the ephemeral plaintext while retaining the 32-byte
`ephemeral_content_hash` value, and `event_root`/the event ID remain verifiable
from the surviving `redacted_content` plus the retained `ephemeral_content_hash`
after redaction. This topology proof format only proves the selected metadata
leaves and their inclusion in `event_root`; it does not by itself authorize
disclosure of redacted content or change which fields Matrix redaction rules
strip.

A server serving a proof for a redacted event includes the retained
`ephemeral_content_hash` in `top_level_hashes` (or a dedicated `content_proofs`
entry) instead of the stripped plaintext, so a verifier can still reconstruct
`content_hash` and `event_root` without ever seeing the censored fields. An
event with no redaction-protected fields, such as an ordinary `m.room.message`,
MAY define `ephemeral_content` as canonical `null`; `redacted_content_hash` and
`ephemeral_content_hash` are computed the same way regardless of whether either
side is empty.

### Marginal value for gap repair

Merkleized topology proofs verify the committed metadata they disclose, but they
are not required for Part A's current gap-repair workflow. A requester must
still fetch the full PDU before accepting an event, because it needs `content`,
`auth_events`, event hashes, signatures, auth rules, and state-resolution
inputs. A proof of `prev_events` therefore adds proof bytes and verification
work without removing the eventual full-event fetch. The remaining
malicious-peer case is early abandonment of a fabricated branch, which this MSC
handles with request work budgets, `limited`, `edge_errors`, and local
hint-reputation heuristics.

The stronger motivation for this sketch is selective disclosure: proving one
field to a party who is not entitled to the whole event, proving topology
without revealing `content`, proving which server sent an event without
revealing the sender's localpart (see `sender_localpart` / `sender_domain`
above), or proving absence for fixed header leaves where a missing optional
field is committed as canonical `null` at a known leaf position. This
construction does not prove absence for arbitrary fields folded into
`other_signed_fields_hash` without revealing the corresponding signed-field set.
Those use cases need their own room-version work before they can become
normative.

---

## Future extensions

Future room versions may extend the proof fields or add more independently
provable metadata fields. Any extension MUST keep the event-root partition
unambiguous: every signed, identity-relevant event field must be committed to
exactly once, and verifiers must be able to determine which leaf position proves
or withholds each independently provable field.

## Performance characteristics and benchmarking

The shared bandwidth and benchmarking analysis for the topology query endpoint
is defined in
[Part A, Performance characteristics and benchmarking](4511-topological-metadata-query-api.md#performance-characteristics-and-benchmarking).
This part documents only what the split-canonicalization sketch adds on top of
that baseline.

### Storage overhead

The split-canonicalization sketch introduces storage overhead if a server stores
the top-level hashes `prev_events_hash`, `auth_events_hash`,
`event_header_root`, `redacted_content_hash`, `ephemeral_content_hash`,
`content_hash`, `other_signed_fields_hash`, and `event_root`.

Using SHA3-256, each hash is 32 bytes, so storing these eight hashes adds 256
bytes of raw hash material per event before database row, index, and encoding
overhead. For a 2 KiB event, this raw hash material is approximately 12.5% of
the event size; for a 5 KiB event, it is approximately 5.0%. A server MUST
retain `ephemeral_content_hash` past redaction execution, since it is otherwise
unrecoverable once the ephemeral plaintext is dropped and is required to
reconstruct `content_hash` and `event_root`. Implementations can recompute proof
paths on demand; caching intermediate Merkle nodes or proof indexes is optional
and would increase this overhead.

The causal trie adds up to 256 fresh path nodes for a linear insertion, though
persistent structural sharing reuses every untouched subtree and canonical empty
node. A naive 32-byte hash plus 8-byte count per level is therefore roughly 10
KiB of raw new path material per event before node encoding or deduplication.
Implementations SHOULD use compressed paths or another canonical sparse-node
encoding, but compression MUST preserve the root construction and proof
semantics above. Multi-predecessor union can write more than one path and must
be benchmarked separately from the linear case.

### Cacheability

The base topology-query cacheability analysis from Part A applies unchanged to
responses carrying proofs: the committed metadata is event-intrinsic and
immutable, so a cached proof-bearing response remains useful as room history
advances.

## Relationship to other proposals

This room-version sketch is the native-verifiability counterpart to Part A's
sparse query endpoint. It defines event-intrinsic field proofs and a causal-set
search root, but not push gossip, session state, or bulk event repair.

Archived [Part B](archive/4511-part-b-merkle-overlay-backwards-compat.md)'s
signed overlay is the deployable, responder-scoped alternative for current room
versions. This Part C sketch is stronger but requires a future room version
because the metadata commitment must participate in event identity.

[MSC4242: State DAGs](https://github.com/matrix-org/matrix-spec-proposals/pull/4242)
changes the room model by adding state-DAG edges and authorization semantics in
a new room version. A room version adopting this sketch would need to define
whether state-DAG edges are additional independently provable metadata leaves,
and how they interact with state resolution.

The room-version naming format used for illustration here follows the
lexicographic room-version convention defined elsewhere in the proposals set;
the exact unstable identifier is a placeholder only.

## Security considerations

The major risks are incorrect field partitioning, metadata leaks through
selective disclosure, large repeated proof requests, and buggy proof validation
causing incorrect repair attempts. These are mitigated by hard local limits,
normal federation authorization, rate-limiting, explicit field partitioning, and
event-ID verification against `event_root`. Even with independently provable
metadata, a server should still fetch and verify full events before accepting
them, repairing state, or considering a gap resolved unless a later room-version
MSC defines a narrower operation that requires only the proven metadata.

### Bandwidth consumption

The bandwidth surface of the topology query endpoint and its mitigations are
covered in
[Part A, Security considerations](4511-topological-metadata-query-api.md#security-considerations);
the response-size limits, per-origin rate limits, and conservative defaults
described there apply unchanged. This proof profile's specific cost is the
sibling-hash material in `proofs`, which scales with the number of proven fields
and proven events.

### Hint validation and reputation

The hint-reputation heuristics defined in
[Part A, Security considerations](4511-topological-metadata-query-api.md#security-considerations)
apply unchanged. Two proof-specific notes apply:

- Fields such as `sender`, `type`, `depth`, `prev_events`, and `auth_events` are
  directly verifiable against `event_root` in room versions that adopt this
  sketch, so contradicted metadata is provably false rather than merely
  suspicious.
- `candidate_servers` is not event-intrinsic and is not part of this native
  proof model; a poor candidate may simply be stale or unavailable rather than
  provably false.

Reputation decay and the rule that such data MUST NOT cause rejection of a valid
event still apply exactly as in Part A.

## References

- [Matrix Server-Server API](https://spec.matrix.org/latest/server-server-api/)
  for `/event`, `/backfill`, `/get_missing_events`, `/state_ids`, federation
  authorization, and the existing PDU flow this proposal tries to avoid
  overusing.
- [Matrix room version 12](https://spec.matrix.org/latest/rooms/v12/) for the
  current default room-version baseline, including event format behavior
  inherited from room version 11, event IDs inherited from room versions 3 and
  later, and v12-specific room ID and state-resolution changes.
- [MSC4186: Simplified Sliding Sync](4186-simplified-sliding-sync.md), as prior
  art for selective, client-chosen field/query shapes in Matrix.
- [MSC2836: Twitter-style Threading](https://github.com/matrix-org/matrix-spec-proposals/pull/2836),
  as prior art for bounded traversal of Matrix event relationships over
  federation.
- [MSC2716: Incrementally Importing History](https://github.com/matrix-org/matrix-spec-proposals/pull/2716),
  as related background for historical DAG gaps and inserted history chunks.
- [MSC4242: State DAGs](https://github.com/matrix-org/matrix-spec-proposals/pull/4242),
  as related work for representing state progression separately from the message
  event DAG.
- [MSC4311: Ensuring the create event is available on invites](4311-stripped-state-create-event.md),
  as nearby room-version/state work for making stripped-state content available
  when room-version transitions need extra validation context.
- [Polkadot Fellowship RFC-0078: Merkleized Metadata][polkadot-rfc-0078] as
  prior art for committing to metadata with a root hash while revealing only the
  pieces needed by the verifier.
- [Crosby and Wallach, Efficient Data Structures for Tamper-Evident
  Logging][crosby-wallach] as foundational work on the security model and proof
  semantics for tamper-evident logs, including logarithmic membership and
  consistency proofs and authenticated query results over logged event
  attributes.
- [RFC 6962, Section 2.1](https://datatracker.ietf.org/doc/html/rfc6962#section-2.1)
  for the Merkle tree construction used by `event_header_root`.

[polkadot-rfc-0078]:
  https://polkadot-fellows.github.io/RFCs/approved/0078-merkleized-metadata.html
[crosby-wallach]:
  https://static.usenix.org/event/sec09/tech/full_papers/crosby.pdf
