# MSC 00E4: Notary provenance for post-quantum server keys

This draft contains the notary-scoped publication challenge, notary observation,
and TLS 1.3 compact provenance material split from MSC45XX. The body below is
intentionally copied nearly verbatim and still needs normal MSC integration
text.

##### Notary-scoped publication challenges

Notaries MAY offer a challenge endpoint that lets an origin bind additional
publication work to a named notary before the notary attests to the key:

```http
POST /_matrix/key/unstable/tk.nutra.msc45xx/publication_challenge
```

Request body:

```json
{
    "server_name": "example.com",
    "key_id_sha256": "<unpadded-base64url-sha256>",
    "key_metadata_sha256": "<unpadded-base64url-sha256>",
    "server_key_package_sha256": "<unpadded-base64url-sha256>"
}
```

Response body:

```json
{
    "algorithm": "tk.nutra.msc45xx.pow.cuckoo-cycle-42-29-sha256",
    "challenge": "<unpadded-base64url-random>",
    "challenge_id": "<opaque-string>",
    "expires_ts": 1798848000000,
    "issuer": "notary.example",
    "resource": {
        "action": "fn-dsa-key-publication-notary-challenge",
        "server_name": "example.com",
        "key_id_sha256": "<unpadded-base64url-sha256>",
        "key_metadata_sha256": "<unpadded-base64url-sha256>",
        "server_key_package_sha256": "<unpadded-base64url-sha256>",
        "issuer": "notary.example"
    },
    "signatures": {
        "notary.example": {
            "ed25519:auto": "<base64-ed25519-signature>",
            "fn-dsa-512:<short_id>": "<base64-fn-dsa-signature>"
        }
    }
}
```

The challenge request MUST carry a valid `Authorization: X-Matrix` header. The
notary MUST reject the request with `403 M_FORBIDDEN` if the authenticated
origin does not exactly match `server_name` in the request body. If the origin
already has a published FN-DSA key, the request SHOULD also carry a valid
`X-Matrix-PQC` header, but this header cannot be required for first publication.
Notaries MUST rate-limit challenge issuance and return `429 M_LIMIT_EXCEEDED`
when rate limits are exceeded. Unsupported notaries return `404 M_UNRECOGNIZED`;
malformed digest fields, unsupported algorithms, or malformed request bodies
return `400 M_INVALID_PARAM`.

The `challenge` and `challenge_id` values MUST each contain at least 128 bits of
entropy from a cryptographically secure source and MUST use the base64url
alphabet. `challenge_id` MUST be 1-128 characters. `expires_ts` SHOULD be no
more than 15 minutes after challenge issuance. The origin MUST continue serving
the exact `/_matrix/key/v2/server` response whose signing-object hash is
committed by `server_key_package_sha256` until challenge completion or expiry.
If a notary fetches a response whose signing-object hash differs, the challenge
is void and the origin must request a new challenge.

`server_key_package_sha256` is the unpadded base64url-encoded SHA-256 digest of
the Matrix Canonical JSON representation of the origin's
`/_matrix/key/v2/server` response after removing only `signatures` and
`unsigned`. A notary-scoped challenge MUST NOT be inserted into that server-key
object. The origin solves the proof against the signed challenge object and
submits the solution to the notary out-of-band from the server-key object using:

```http
POST /_matrix/key/unstable/tk.nutra.msc45xx/publication_challenge/complete
```

Completion request body:

```json
{
    "challenge_object": { "...": "the notary-signed challenge object" },
    "proof": {
        "algorithm": "tk.nutra.msc45xx.pow.cuckoo-cycle-42-29-sha256",
        "nonce": 8137226,
        "solution": [123, 456, 789, "..."]
    }
}
```

The completion request MUST carry a valid `Authorization: X-Matrix` header from
the origin and a valid `X-Matrix-PQC` header signed by the FN-DSA key whose
`key_id_sha256` is committed in the challenge. The notary MUST verify the
challenge object's signatures, verify that `resource.issuer` exactly matches the
notary's own server name, verify that the challenge has not expired, verify the
proof against the exact challenge object, fetch the origin's server-key
response, and check that the committed `server_name`, `key_id_sha256`,
`key_metadata_sha256`, and `server_key_package_sha256` values match the fetched
key package. Completion failures return `400 M_INVALID_PARAM` for malformed or
invalid proofs, `403 M_FORBIDDEN` for authentication or keyholder mismatch,
`410 M_INVALID_PARAM` for expired challenges, and `429 M_LIMIT_EXCEEDED` when
rate limits are exceeded. A notary MUST NOT accept the same `challenge_id` twice
within the challenge lifetime.

This preserves a single canonical origin key package signing object: the object
fetched directly from the origin and the object redistributed by a notary remain
equivalent after removing `signatures` and `unsigned` and applying Matrix
Canonical JSON serialization, and therefore hash to the same
`server_key_package_sha256`. A notary response may add its own signatures or
observation records, so raw wire JSON and full response objects are not expected
to be byte-for-byte identical. Notary-specific challenge IDs, issuance
timestamps, proof receipt timestamps, TLS provenance, and audit notes MUST be
carried outside the origin key object, either in notary-signed observation
records or transport metadata. They MUST NOT affect the canonical hash or
signature input of the origin key package.

Notary-scoped challenges are advisory provenance. A notary MAY require them as a
local policy before emitting its own attestation, but other receivers MUST NOT
reject an otherwise-valid FN-DSA key publication solely because this
notary-specific challenge metadata is absent, delayed, too fast, or too slow.

#### Notary expectations and key validity

Key notaries (`/_matrix/key/v2/query`) MUST include FN-DSA keys and their
corresponding signatures in responses when present on the queried server.
Notaries MUST validate the remote server's FN-DSA self-signature for the queried
`server_name` — and MUST recompute and validate the hash-derived `short_id`
against the advertised key body, and MUST verify a valid proof-of-work as
specified in [Key publication Proof-of-Work](#key-publication-proof-of-work) —
before attesting to the key; if any check fails, the notary MUST NOT include
that FN-DSA key in its response. Notary responses are themselves signed objects;
notaries that support this MSC MUST include FN-DSA signatures on their
responses.

A notary MUST NOT add, remove, reorder, or rewrite any member of the origin key
object other than adding entries under `signatures`. As a conformance check,
`server_key_package_sha256` recomputed from a notary-redistributed key object
after removing `signatures` and `unsigned` MUST equal the digest recomputed from
a direct origin fetch of the same response snapshot.

##### Notary observations

Notaries MAY include `notary_observations` records describing how they observed
an FN-DSA key. These records are signed provenance metadata: they let operators
and later auditors verify that a named notary claims to have fetched a specific
server key over HTTPS at a specific time and under a specific TLS certificate
context.

Notary observations are strictly advisory. They are not a substitute for Matrix
server-key validation and MUST NOT change acceptance semantics.

```json
{
    "notary_observations": [
        {
            "notary_server_name": "notary.example",
            "observed_server_name": "example.com",
            "observed_at": 1798848000000,
            "fetch_uri": "https://example.com/_matrix/key/v2/server",
            "transport": "https",
            "tls": {
                "leaf_spki_sha256": "<unpadded-base64url-sha256>",
                "leaf_cert_sha256": "<unpadded-base64url-sha256>",
                "tls_13_provenance": {
                    "transcript_hash_algorithm": "sha256",
                    "handshake_transcript_hash": "<unpadded-base64url-hash>",
                    "certificate_verify_signature_scheme": "ecdsa_secp256r1_sha256",
                    "server_certificate_verify_signature": "<unpadded-base64url-signature>"
                }
            },
            "key_id_sha256": "<unpadded-base64url-sha256>",
            "server_key_package_sha256": "<unpadded-base64url-sha256>",
            "notary_challenge": {
                "challenge_id": "<opaque-string>",
                "challenge_sha256": "<unpadded-base64url-sha256>",
                "proof_sha256": "<unpadded-base64url-sha256>",
                "challenge_issued_at": 1798847988000,
                "pow_observed_at": 1798848000000,
                "pow_verified_at": 1798848000100
            },
            "valid_until_ts": 1798848000000,
            "signatures": {
                "notary.example": {
                    "ed25519:auto": "<base64-ed25519-signature>",
                    "fn-dsa-512:<short_id>": "<base64-fn-dsa-signature>"
                }
            }
        }
    ]
}
```

The observation signature input is the following byte string:

```text
len16("matrix:notary-key-observation:v1") ||
"matrix:notary-key-observation:v1" ||
len16(notary_server_name) ||
notary_server_name ||
len16(observed_server_name) ||
observed_server_name ||
uint64_be(observed_at) ||
len16(fetch_uri) ||
fetch_uri ||
len16(transport) ||
transport ||
len16(leaf_spki_sha256) ||
leaf_spki_sha256 ||
len16(leaf_cert_sha256) ||
leaf_cert_sha256 ||
uint8(tls_13_provenance_present) ||
if tls_13_provenance_present:
    len16(tls_13_provenance_sha256) ||
    tls_13_provenance_sha256 ||
len16(key_id_sha256) ||
key_id_sha256 ||
len16(server_key_package_sha256) ||
server_key_package_sha256 ||
uint8(notary_challenge_present) ||
if notary_challenge_present:
    len16(notary_challenge_sha256) ||
    notary_challenge_sha256 ||
uint64_be(valid_until_ts)
```

Formatting definitions:

- `len16(x)` is the two-byte big-endian length of the UTF-8 byte string `x`,
  followed immediately by `x`.
- `uint64_be(ts)` is an unsigned 64-bit big-endian millisecond timestamp.
- `leaf_spki_sha256` is the unpadded base64url-encoded SHA-256 digest of the TLS
  leaf certificate's SubjectPublicKeyInfo DER.
- `leaf_cert_sha256` is the unpadded base64url-encoded SHA-256 digest of the
  full TLS leaf certificate DER observed by the notary during its HTTPS fetch.
- `server_key_package_sha256` is the unpadded base64url-encoded SHA-256 digest
  of the observed origin server-key package's Matrix Canonical JSON
  representation after removing only `signatures` and `unsigned`.
- When `notary_challenge` is present, `notary_challenge_sha256` is the unpadded
  base64url-encoded SHA-256 digest of the Matrix Canonical JSON representation
  of the `notary_challenge` object. The `challenge_sha256` and `proof_sha256`
  fields inside that object are the unpadded base64url-encoded SHA-256 digests
  of the notary-issued challenge object and the origin-submitted proof object,
  respectively, after Matrix Canonical JSON serialization.

Notary and verifier constraints:

- A notary MUST NOT emit an observation unless it performed the described fetch
  itself.
- Verifiers MAY validate and store these records for diagnostics, audits, and
  operator review.
- When validating an observation, a verifier MUST verify the notary's signature
  using the notary's Matrix server signing key.
- When validating an observation, a verifier MUST check that
  `observed_server_name` matches the queried `server_name`.
- When validating an observation, a verifier MUST check that `key_id_sha256` and
  `valid_until_ts` match the observed key response described by the record.
- When validating an observation, a verifier MUST check that
  `server_key_package_sha256` matches the observed key response described by the
  record.
- When `notary_challenge` is present, a verifier MAY use its digests and
  timestamps to audit the sequence from notary challenge issuance to proof
  observation and verification. These fields are advisory and MUST NOT change
  automated key acceptance semantics.

##### TLS 1.3 compact provenance

`tls_13_provenance` MUST be omitted for non-TLS-1.3 fetches. When
`tls_13_provenance` is present, `transport` MUST be `https`, `leaf_spki_sha256`
and `leaf_cert_sha256` MUST be present, and `tls_13_provenance_sha256` is the
unpadded base64url-encoded SHA-256 digest of the following byte string:

```text
len16("matrix:tls13-provenance:v1") ||
"matrix:tls13-provenance:v1" ||
len16(transcript_hash_algorithm) ||
transcript_hash_algorithm ||
len16(handshake_transcript_hash) ||
handshake_transcript_hash ||
len16(certificate_verify_signature_scheme) ||
certificate_verify_signature_scheme ||
len32(server_certificate_verify_signature) ||
server_certificate_verify_signature
```

Formatting definitions:

- `len32(x)` is the four-byte big-endian length of the byte string `x`, followed
  immediately by `x`.
- `handshake_transcript_hash` is the literal cryptographic hash of the TLS 1.3
  Handshake Context up to but excluding the server `CertificateVerify` message,
  as defined by RFC 8446 Section 4.4.1, encoded as unpadded base64url.
- The `handshake_transcript_hash` calculation uses raw TLS Handshake messages
  and excludes TLS record-layer headers.
- `transcript_hash_algorithm` MUST be the hash algorithm of the negotiated TLS
  1.3 cipher suite.
- `certificate_verify_signature_scheme` MUST be a TLS `SignatureScheme` registry
  name.
- `server_certificate_verify_signature` is the literal TLS 1.3
  `CertificateVerify` signature, encoded as unpadded base64url.
- This compact construction intentionally does not carry the full handshake
  transcript.

Validation requirements:

- A verifier or auditor validating compact `tls_13_provenance` MUST obtain a TLS
  leaf certificate matching `leaf_cert_sha256` and `leaf_spki_sha256`, for
  example from Certificate Transparency logs, out-of-band certificate evidence,
  or a retained notary audit bundle.
- The verifier MUST verify `server_certificate_verify_signature` according to
  the TLS 1.3 `CertificateVerify` construction for the server context in RFC
  8446 Section 4.4.3, using the signed content formed from 64 bytes of `0x20`,
  the ASCII context string `TLS 1.3, server CertificateVerify`, a single `0x00`
  byte, and the stated `handshake_transcript_hash`.
- Verifiers SHOULD validate that the obtained leaf certificate chains to the
  WebPKI and was valid for `observed_server_name` at `observed_at`, including
  Certificate Transparency evidence where available.

Trust and enforcement boundaries:

- Compact TLS 1.3 provenance proves only that the notary presents evidence that
  the holder of the obtained TLS certificate private key produced a valid
  `CertificateVerify` signature over the stated transcript hash.
- Compact TLS 1.3 provenance does not prove that the TLS evidence was captured
  at `observed_at`; it proves only that the certificate key holder signed the
  stated transcript hash at some time. Freshness beyond the notary's signed
  assertion requires retained transcript evidence or a stronger transcript
  verification system.
- Compact TLS 1.3 provenance does not prove that the HTTP response body was
  faithfully reported by the notary; proving payload fidelity without trusting
  the notary requires a separate TLS transcript-verification system such as
  TLSNotary or DECO.
- Because the compact form carries only the transcript hash, it does not by
  itself let a later auditor inspect or recompute the handshake transcript,
  confirm the SNI value, confirm a notary challenge, or confirm other handshake
  contents.
- A notary that wants independently auditable transcript contents MAY retain or
  publish the full TLS Handshake messages, or equivalent transcript evidence, in
  an out-of-band audit bundle. Such transcript evidence MUST use the same RFC
  8446 Section 4.4.1 Handshake Context definition and MUST exclude TLS
  record-layer headers.
- `tls_13_provenance` remains advisory provenance metadata and MUST NOT affect
  automated key acceptance, event acceptance, or state resolution.
- Non-normatively, compact TLS 1.3 provenance is intended as low-cost forensic
  evidence attached to a signed notary observation. The notary's own signature
  binds the observation timestamp, observed server name, certificate
  fingerprints, key fingerprint, and TLS provenance digest to that notary's
  identity; the TLS evidence then helps distinguish an actual TLS-origin
  observation from a purely invented certificate claim. It is not intended to
  make homeservers parse TLS handshakes, enforce freshness automatically, or
  treat the notary observation as machine-verifiable proof of HTTP payload
  fidelity.

FN-DSA keys follow identical validity semantics to Ed25519 keys: a signature
made by `fn-dsa-512:<short_id>` is valid if the key was valid at the time of the
signed operation. Retired FN-DSA keys appear in `old_verify_keys` with an
`expired_ts`. The `valid_until_ts` field governs cache lifetime for the entire
key response, identically to existing behavior.

##### Self-signed expiry claim carriage

Notaries MAY include valid `m.server_key.expiry.v1` expiry claims observed from
the origin or from federation gossip alongside their key-query responses. A
notary MUST verify the claim's signatures, `server_name`, and `key_id_sha256`
binding before redistributing it. Notary redistribution does not make the claim
notary-specific: receivers continue to authenticate the claim by its own
signatures and use the smallest observed `not_valid_after_ts` for the closed key
as specified by MSC45XX.

Any third-party attestation metadata a server or notary chooses to additionally
track (e.g. historic corroboration records, reputation signals) is advisory
only. Servers MUST NOT reject events, keys, or state based solely on missing,
false, suspicious, flimsy, or otherwise invalid attestation metadata. Such
metadata MUST NOT affect automated verification, acceptance, or state
resolution. If it is to influence behavior at all, that MUST occur only through
explicit admin override or manual operator intervention. Automated state
resolution MUST continue to operate normally, remain backwards compatible with
legacy federation traffic, and converge according to the established network
rules.

Because a server's very first FN-DSA key observation is TOFU and authenticates
only via the existing Ed25519 trust model (see
[Server key trust model](#server-key-trust-model)), it is vulnerable to an
attacker positioned on that specific fetch path — a targeted, localized
man-in-the-middle rather than a global compromise. Implementations MUST NOT
treat contradictory notary observations, by themselves, as sufficient to reject
or invalidate an otherwise valid first observation: doing so would let any
false, compromised, or stale notary create a denial-of-service condition against
legitimate bootstrap. This MSC therefore leaves first-observation acceptance
semantics aligned with the existing Matrix server-key trust model; it does not
define any cross-source consensus or conflict-resolution mechanism for FN-DSA
bootstrap.
