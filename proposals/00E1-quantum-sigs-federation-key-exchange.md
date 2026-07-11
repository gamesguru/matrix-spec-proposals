# MSC45XX: Post-quantum server key exchange and HTTP semantics

Matrix federation authentication currently uses `ed25519`. Quantum computers can
theoretically reverse engineer private keys using Shor's algorithm, breaking
elliptic-curve and RSA schemes.

This MSC is the first step of the post-quantum migration: it defines the
post-quantum signature primitive for Matrix, distributes post-quantum server
signing keys across the federation, and upgrades server-to-server HTTP
authentication to be quantum-resistant. It deliberately makes **no changes to
events, PDUs, or room versions** — every mechanism in this proposal can be
deployed immediately by any homeserver, with zero impact on rooms or clients.

PQC PDU signing and the requisite room version upgrade will be addressed in a
separate proposal that builds on the primitives defined here. E2EE device and
cross-signing key management and migration can also be addressed in follow-up
MSCs.

## Proposal

This MSC introduces **FN-DSA**, a 128-bit secure lattice-based signature scheme
specified by the
[NIST FIPS 206 initial public draft](https://csrc.nist.gov/pubs/fips/206/ipd),
as the post-quantum signature scheme for Matrix. FN-DSA (Falcon) was selected by
NIST for small signatures and fast verification — both critical for
high-throughput federation.

### Algorithm Parameters

This MSC proposes a single, unified signature scheme. Allowing joint-Dilithium
schemes defeats the purpose of Falcon based schemes: small signatures and keys.

| Algorithm    | NIST Level  | Public Key | Signature  | Verification | Use Case                     |
| ------------ | ----------- | ---------- | ---------- | ------------ | ---------------------------- |
| `fn-dsa-512` | I (128-bit) | 897 bytes  | ~666 bytes | ~0.1 ms      | HTTP transport. PDU signing. |

Homeservers that support this MSC MUST support `fn-dsa-512` for server signing
key publication, self-signing, and federation transport authentication.

> **Note:** For readability, this proposal uses the intended stable identifier
> `fn-dsa-512` throughout the main text and examples. Until this MSC is accepted
> and merged into the Matrix specification, implementations MUST use the
> unstable identifier `tk.nutra.msc45xx.fn-dsa-512` in all protocol fields (key
> IDs, signature entries, and algorithm names). See
> [Unstable Prefix](#unstable-prefix) for the full mapping.

### Key Identifier Format

Matrix currently identifies keys using the format `algorithm:key_id` (e.g.,
`ed25519:abc123`). This MSC extends the set of recognized algorithm identifiers:

| Key Algorithm | Description                  | Key ID Format         |
| ------------- | ---------------------------- | --------------------- |
| `ed25519`     | Existing Ed25519 (unchanged) | `ed25519:<key_id>`    |
| `fn-dsa-512`  | FN-DSA at NIST Level I       | `fn-dsa-512:<key_id>` |

Key IDs MUST be unique within each algorithm namespace on a given server.

### FN-DSA Encoding and Signing Operation

This MSC targets FN-DSA-512 (n=512, q=12289) as specified by the FIPS 206
initial public draft. Implementations MUST track the exact FIPS 206 revision
named by this MSC; if the final standard changes encodings or parameters, a
follow-up MSC will reconcile.

**Public key encoding.** The public key is the raw FN-DSA-512 public key byte
string as defined by FIPS 206. It is encoded as unpadded base64 using the
standard RFC 4648 alphabet.

**Signature encoding.** The signature is the raw FN-DSA-512 signature byte
string as defined by FIPS 206. Unlike the original Falcon submission which used
variable-length signatures, FIPS 206 mandates a fixed-length encoding (padded
with zeros). For FN-DSA-512, the signature is exactly 666 bytes. Implementations
MUST reject signatures of any other length. Signatures are encoded as unpadded
base64 using the standard RFC 4648 alphabet.

**Signing operation.** The message signed is the UTF-8 byte sequence of the
Matrix Canonical JSON representation of the object after removing `signatures`
and `unsigned`, consistent with existing Matrix signing conventions. FN-DSA is
invoked in pure (non-prehash) mode with an empty context string. Implementations
MUST reject non-canonical public key and signature encodings.

These encoding and signing rules are the normative definition of `fn-dsa-512`
for the entire Matrix protocol; MSC 45YY (PDU signing) and MSC 0F00 (E2EE) build
on them by reference.

### Server Signing Keys

The `GET /_matrix/key/v2/server` response includes both key types:

```json
{
    "server_name": "example.com",
    "verify_keys": {
        "ed25519:auto": {
            "key": "<unpadded-base64-ed25519-pubkey>"
        },
        "fn-dsa-512:pqc0": {
            "key": "<unpadded-base64-fn-dsa-512-pubkey>"
        }
    },
    "old_verify_keys": {
        "fn-dsa-512:pqc_retired": {
            "key": "<unpadded-base64-fn-dsa-512-pubkey>",
            "expired_ts": 1798761600000
        }
    },
    "signatures": {
        "example.com": {
            "ed25519:auto": "<base64-ed25519-signature>",
            "fn-dsa-512:pqc0": "<base64-fn-dsa-512-signature>"
        }
    },
    "valid_until_ts": 1798848000000
}
```

FN-DSA public keys are encoded as unpadded base64. Servers SHOULD begin
publishing FN-DSA keys immediately, to pre-distribute public keys across the
federation ahead of any downstream use (transport authentication in this MSC;
PDU signing in MSC 45YY). Pre-distribution matters: every server whose FN-DSA
key is observed, verified, and cached _before_ a quantum adversary exists gains
post-quantum identity continuity from that point forward (see
[Server Key Trust Model](#server-key-trust-model)).

#### Server Key Trust Model

Once a server publishes an FN-DSA signing key, the `/_matrix/key/v2/server`
response MUST include an FN-DSA self-signature in the `signatures` field
alongside the existing Ed25519 signature. Receiving servers MUST verify this
self-signature before trusting the FN-DSA key.

Initial FN-DSA key discovery is trust-on-first-use (TOFU): it is authenticated
by the existing Matrix server-key trust model (Ed25519 signatures and/or notary
attestation). First-use discovery is not post-quantum secure against an attacker
who has already compromised or quantum-derived the server's Ed25519 signing key
before the FN-DSA key was observed. Post-quantum protection for server identity
applies once an FN-DSA key has been successfully verified and cached by the
receiving server.

Once a receiving server has successfully verified and cached a valid FN-DSA
signing key for a remote server, subsequent FN-DSA key changes MUST be
authenticated: the replacement key response MUST be signed by a previously
trusted FN-DSA key (which MAY have been retired to `old_verify_keys` with a
non-expired `expired_ts`). A new FN-DSA key MUST NOT be accepted solely on the
basis of Ed25519 authentication if the receiving server has previously observed
a valid FN-DSA key for that server.

Servers SHOULD pin observed FN-DSA keys and treat unexpected key changes —
particularly the disappearance of a previously-observed FN-DSA key or the
appearance of an unattested replacement — as potential compromise indicators
worthy of operator alerts. If a receiving server has previously pinned a valid
FN-DSA key and later encounters a replacement that is not authenticated by a
previously trusted non-expired FN-DSA key, it MUST treat the replacement as
untrusted by default. Implementations MAY provide an explicit administrative
recovery mechanism for legitimate key-loss scenarios; specification of an
authenticated PQ key-reset protocol is deferred to future work.

Key notaries (`/_matrix/key/v2/query`) MUST include FN-DSA keys and their
corresponding signatures in responses when present on the queried server.
Notaries MUST validate the remote server's FN-DSA self-signature before
attesting to the key; if the self-signature is invalid, the notary MUST NOT
include that FN-DSA key in its response. Notary responses are themselves signed
objects; notaries that support this MSC MUST include FN-DSA signatures on their
responses.

FN-DSA keys follow identical validity semantics to Ed25519 keys: a signature
made by `fn-dsa-512:<key_id>` is valid if the key was valid at the time of the
signed operation. Retired FN-DSA keys appear in `old_verify_keys` with an
`expired_ts`. The `valid_until_ts` field governs cache lifetime for the entire
key response, identically to existing behavior.

### Federation HTTP Authentication

Sending servers that support this MSC MUST include the `X-Matrix-PQC` header on
all outgoing federation requests. Unknown HTTP headers are safely ignored per
RFC 9110, so no capability discovery is needed for legacy servers.

```http
Authorization: X-Matrix origin="example.com",destination="matrix.org",key="ed25519:auto",sig="<base64-ed25519-signature>"
X-Matrix-PQC: origin="example.com",destination="matrix.org",key="fn-dsa-512:pqc0",sig="<base64-fn-dsa-signature>"
```

The FN-DSA signature MUST be computed over the same JSON signing object used for
existing Matrix federation request authentication (containing `method`, `uri`,
`origin`, `destination`, and `content` when present).

#### Header Syntax

`X-Matrix-PQC` uses the same parameter syntax and parsing rules as the existing
`Authorization: X-Matrix` header. Required parameters are `origin`,
`destination`, `key`, and `sig`. Unknown parameters MUST be ignored. Duplicate
parameters or multiple `X-Matrix-PQC` headers render the request authentication
invalid. The `sig` parameter value is the unpadded base64-encoded FN-DSA
signature. Malformed headers (invalid base64, missing required parameters,
unparsable syntax) MUST be treated as absent for enforcement purposes and SHOULD
be logged.

#### Verification and Enforcement

This MSC introduces the header with **advisory-but-verified** semantics, so that
it can be deployed federation-wide without any flag day:

- Receiving servers that support this MSC MUST verify the `X-Matrix-PQC` header
  whenever it is present and the sending server has a published FN-DSA key.
- A verification failure SHOULD be logged as a warning but MUST NOT cause
  request rejection, provided the Ed25519 `Authorization` header is valid.
- If a receiving server has pinned an FN-DSA key for the sending server (see
  [Server Key Trust Model](#server-key-trust-model)), the _absence_ of the
  `X-Matrix-PQC` header on requests from that server SHOULD be logged as a
  potential downgrade indicator. Implementations MAY offer an operator-level
  strict mode that rejects unauthenticated requests from peers with pinned
  FN-DSA keys, but this is not required by this MSC.
- Legacy servers that do not support this MSC ignore the `X-Matrix-PQC` header
  entirely.

Mandatory enforcement is intentionally out of scope here: it is defined by MSC
45YY, which requires a valid `X-Matrix-PQC` header for federation traffic scoped
to PQC-required rooms. Splitting the mechanism (this MSC) from the enforcement
trigger (MSC 45YY) means the header can reach wide deployment — and FN-DSA keys
can be pinned across the federation — before anything depends on it.

The Ed25519 `Authorization` header remains required on all federation requests
as long as any legacy room version exists in the federation.

### Upgraded Connections: PQ Session Negotiation (Optional Extension)

The per-request `X-Matrix-PQC` header adds ~888 bytes (base64) of bandwidth
overhead to every federation request. This section defines an OPTIONAL mechanism
for a pair of servers to negotiate a symmetric session key via a post-quantum
KEM (ML-KEM-768, [NIST FIPS 203](https://csrc.nist.gov/pubs/fips/203/final)) and
amortize that cost by replacing per-request asymmetric signatures with
session-based HMAC authentication — an "upgraded" HTTP connection between two
PQC-capable servers.

Per-request `X-Matrix-PQC` remains the baseline; servers MUST NOT assume peer
support for session negotiation, and MUST fall back to per-request headers when
negotiation is unavailable or a session is rejected.

#### Endpoint

```
POST /_matrix/federation/unstable/tk.nutra.msc45xx/key_exchange
```

**Authentication.** The negotiation request MUST carry a valid Ed25519
`Authorization: X-Matrix` header AND a valid `X-Matrix-PQC` header. Both MUST
verify; otherwise the receiving server MUST reject the request with
`401 M_FORBIDDEN`. Authenticating the initialization request with existing
server signing keys prevents origin spoofing of the KEM handshake.

**Rate limiting.** This endpoint SHOULD be rate-limited. Servers MUST return
`429 M_LIMIT_EXCEEDED` when rate limits are exceeded. Guest access is not
applicable (server-to-server API).

**Request body:**

```json
{
    "algorithm": "ml-kem-768",
    "encapsulation_key": "<unpadded-base64-ml-kem-768-ek>"
}
```

The `encapsulation_key` is an ephemeral ML-KEM-768 encapsulation key generated
by the initiating server for this session only. Ephemeral keys MUST NOT be
reused across sessions.

**Response body (`200 OK`):**

```json
{
    "session_id": "<opaque-string>",
    "ciphertext": "<unpadded-base64-ml-kem-768-ciphertext>",
    "expires_ts": 1798848000000
}
```

The responder encapsulates against the initiator's ephemeral key, producing the
ciphertext and a 32-byte shared secret `ss`. Both sides derive the session key:

```
session_key = HKDF-SHA-256(ikm = ss,
                           salt = "",
                           info = "tk.nutra.msc45xx.session.v1|" + origin + "|" + destination + "|" + session_id,
                           length = 32)
```

**Error responses:**

- `404 M_UNRECOGNIZED` — the receiving server does not support this endpoint.
  The initiator falls back to per-request `X-Matrix-PQC` headers.
- `400 M_INVALID_PARAM` — unsupported `algorithm` or malformed
  `encapsulation_key`.
- `401 M_FORBIDDEN` — transport authentication failed.
- `429 M_LIMIT_EXCEEDED` — rate limited.

#### Session-Authenticated Requests

While a session is live, the initiating server MAY replace the `X-Matrix-PQC`
header on requests to the responder with:

```http
X-Matrix-PQC-Session: origin="example.com",destination="matrix.org",session="<session_id>",mac="<unpadded-base64-hmac>"
```

where `mac` is `HMAC-SHA-256(session_key, canonical_json(signing_object))` over
the same JSON signing object used for `X-Matrix-PQC`. The Ed25519
`Authorization` header remains required as usual.

Sessions are soft state. Either side MAY discard a session at any time (e.g. on
restart, cache pressure, or expiry). If the receiving server does not recognize
or no longer holds the `session_id`, it MUST treat the header as absent for
enforcement purposes; the sender then falls back to per-request `X-Matrix-PQC`
and MAY renegotiate. A valid `X-Matrix-PQC-Session` MAC is equivalent to a valid
`X-Matrix-PQC` signature for all verification and (future) enforcement purposes.

Note that this mechanism provides _authentication amortization only_ — it does
not provide confidentiality (TLS continues to provide transport encryption) and
symmetric MACs do not provide non-repudiation, which transport authentication
does not require. Anti-replay properties are inherited from TLS, identically to
the existing `X-Matrix` scheme.

### Migration

**Phase 1 — Transport & Key Distribution (Immediate, this MSC)** Servers begin
publishing FN-DSA keys via `/_matrix/key/v2/server` and transmitting the
`X-Matrix-PQC` header for server-to-server HTTP authentication, optionally
negotiating upgraded session-authenticated connections. PDUs continue to be
signed exclusively with Ed25519 according to legacy room versions. No room,
event, or client behavior changes.

**Phase 2 — PQC Room Version (MSC 45YY)** A new room version makes `fn-dsa-512`
the sole, authoritative PDU signature scheme and turns `X-Matrix-PQC` transport
verification into a hard requirement for traffic scoped to PQC rooms.

## Potential Issues

- **FIPS 206 not yet finalized.** FIPS 206 is in final stages but unpublished as
  of May 2026. Unstable prefixes allow parameter updates without breaking stable
  identifiers. FIPS 203/204/205 were finalized in August 2024; FIPS 206 is
  expected to follow.

- **Falcon's implementation complexity.** FN-DSA key generation and signing
  require side-channel-resistant discrete Gaussian sampling — non-constant-time
  implementations can leak private keys through timing, cache, or power side
  channels. Implementations MUST use an audited FN-DSA library that provides
  constant-time Gaussian sampling and signing. The libraries listed under
  [Implementation Guidance](#implementation-guidance) are non-normative
  examples.

- **Key rotation complexity.** Servers must now manage and rotate two
  independent key types. However, Matrix already supports key rotation via
  `old_verify_keys`, and the mechanics are identical for FN-DSA keys.

- **Public key size.** FN-DSA-512 public keys are 897 bytes (vs 32 for Ed25519),
  adding ~1.2 KB per key to `/_matrix/key/v2/server` responses. A modest
  increase in response size.

- **Per-request header overhead.** The `X-Matrix-PQC` header adds ~888 bytes of
  bandwidth per federation request. The optional
  [session negotiation extension](#upgraded-connections-pq-session-negotiation-optional-extension)
  amortizes this to a 32-byte-key HMAC per request between supporting peers.

- **Advisory enforcement window.** Until MSC 45YY (or an operator strict mode)
  makes verification mandatory, `X-Matrix-PQC` failures only produce warnings.
  This is deliberate — see [Security Considerations](#security-considerations)
  on downgrade.

## Alternatives

- **ML-DSA (FIPS 204 / Dilithium).** Integer-only arithmetic eliminates FN-DSA's
  side-channel concerns, but ML-DSA-44 signatures exceed 2.4 KB vs FN-DSA's ~666
  bytes. The bandwidth cost materially increases per-request federation
  overhead, and downstream per-event costs in MSC 45YY.

- **SLH-DSA (FIPS 205 / SPHINCS+).** Most conservative (hash-based, no lattice
  assumptions), but 17,088-byte signatures are impractical for per-request
  transport authentication. Potentially useful for long-lived trust anchors in a
  future MSC.

- **Waiting for FIPS 206 finalization.** Delaying extends the vulnerability
  window. Unstable prefixes allow early adoption without committing to final
  identifiers — and every year of pre-distributed, pinned FN-DSA keys shrinks
  the TOFU exposure window.

- **PQC TLS instead of application-layer auth.** Post-quantum TLS (X25519 +
  ML-KEM hybrid key exchange) is being deployed by CDNs and browsers and
  protects channel confidentiality, but Matrix federation identity is
  authenticated at the application layer via server signing keys, not client TLS
  certificates. Relying on TLS alone would leave server identity —
  `/_matrix/key/v2/server` responses, notary attestations, request origin —
  authenticated only by Ed25519. Application-layer FN-DSA is required
  regardless; PQC TLS is complementary and encouraged.

- **Distributed Symmetric Key Establishment (DSKE).** Information-theoretically
  secure protocols based on pre-shared random data (PSRD) and one-time pads
  eliminate asymmetric cryptography entirely. However, DSKE is catastrophically
  unsuitable for open federation: (1) PSRD is _consumable_ — each authentication
  operation permanently erases key material, requiring gigabytes of pre-shared
  state per server pair vs. FN-DSA's static ~1 KB public key that authenticates
  unlimited requests; (2) symmetric cryptography cannot provide
  _non-repudiation_ — required downstream for PDU signing, where any server must
  independently verify any historical event's authorship; (3) PSRD replenishment
  requires physical delivery (armored USB drives or dedicated QKD fiber), which
  is incompatible with permissionless internet-scale federation. DSKE is
  designed for closed, classified networks; FN-DSA provides the ultimate
  compression — ~1 KB of static text proves a server's identity to the entire
  world with zero consumable state.

## Implementation Guidance

FN-DSA libraries (status as of May 2026; FIPS 206 draft submitted August 2025,
final standard expected late 2026–2027):

| Library                                                                                                                        | Language        | FFI                                | FN-DSA Status                                                                                              | Maturity                                                    | Notes                                                                                          |
| ------------------------------------------------------------------------------------------------------------------------------ | --------------- | ---------------------------------- | ---------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| [liboqs](https://github.com/open-quantum-safe/liboqs)                                                                          | C               | Yes (Python, Rust, Go, Java, .NET) | Round 3 Falcon; FIPS 206 update tracked ([#2271](https://github.com/open-quantum-safe/liboqs/issues/2271)) | Mature (OQS reference); targets draft, not final FIPS 206   | Reference PQC library. Compiles to WASM via Emscripten. Most complete algorithm coverage.      |
| [oqs](https://crates.io/crates/oqs) / [liboqs-rust](https://github.com/open-quantum-safe/liboqs-rust)                          | Rust (FFI to C) | Yes (wraps liboqs)                 | Tracks liboqs; v0.11.0 (May 2025)                                                                          | Stable; follows liboqs releases                             | Rust bindings for liboqs. Suitable for conduwuit, Synapse-via-PyO3.                            |
| [pqcrypto-falcon](https://crates.io/crates/pqcrypto-falcon)                                                                    | Rust            | No (wraps PQClean C)               | Round 3 Falcon (PQClean); v0.4.1 (Aug 2025)                                                                | Usable today; FIPS 206 update pending PQClean upstream      | Pure-Rust build wrapper. No system C dependency — simplifies cross-compilation.                |
| [oqs-provider](https://github.com/open-quantum-safe/oqs-provider)                                                              | C (OpenSSL 3.x) | N/A                                | Experimental Falcon via liboqs; not natively in OpenSSL 3.5+                                               | Research/testing; not audited for production signing        | OpenSSL provider. Useful for TLS experimentation, not directly for Matrix JSON signing.        |
| [falcon-crypto](https://www.npmjs.com/package/falcon-crypto) / [@btq-js/falcon-wasm](https://github.com/nickthecook/falcon-js) | JavaScript/WASM | No                                 | Round 3 Falcon (Emscripten of reference C)                                                                 | Community; requires audit for constant-time WASM guarantees | Browser/Node.js. Must verify FIPS 206 alignment and side-channel resistance before production. |

All implementations MUST use constant-time Gaussian sampling and signing
operations. Server-side deployments SHOULD prefer native (C/Rust)
implementations. ML-KEM-768 (for the optional session extension) is available in
liboqs and, increasingly, in mainstream TLS libraries following FIPS 203
finalization.

## Security Considerations

- **Real-time impersonation.** The primary real-time quantum threat is an
  attacker deriving a server's Ed25519 private key to spoof federation traffic
  and server-key responses. This MSC mitigates the transport half of that
  vector: once FN-DSA keys are distributed and pinned, `X-Matrix-PQC` provides
  quantum-resistant request authentication, and the key trust model prevents a
  quantum-equipped attacker from silently replacing a pinned FN-DSA key using
  only a broken Ed25519 key. Forged _events_ are addressed by MSC 45YY.

- **TOFU bootstrap window.** Initial FN-DSA key discovery is authenticated by
  Ed25519 and is therefore not post-quantum secure. This is an argument for
  deploying this MSC as early and widely as possible: keys pinned before
  cryptographically relevant quantum computers exist are protected thereafter.

- **Downgrade attacks.** During the advisory period, an attacker who can strip
  HTTP headers (i.e. who controls TLS termination or a private key) could
  suppress `X-Matrix-PQC` without causing rejection. Pinning plus
  absence-logging (and the optional strict mode) narrows this; room-scoped
  mandatory enforcement arrives with MSC 45YY. Ed25519 `Authorization` remains
  the floor, so this MSC never _weakens_ existing authentication.

- **Timing side-channels.** FN-DSA's discrete Gaussian sampler leaks private
  keys via timing analysis if implemented incorrectly. All implementations MUST
  use audited, constant-time libraries (see Implementation Guidance).

- **Session key hygiene (optional extension).** Session keys are derived from
  ephemeral ML-KEM keys and MUST NOT outlive `expires_ts`. Compromise of a
  session key permits transport-level impersonation toward one peer until expiry
  — bounded by short session lifetimes and by the requirement that negotiation
  itself is FN-DSA-authenticated.

- **Algorithm agility.** The `algorithm:key_id` format provides syntactic
  extensibility for future PQC standards. Deploying a new algorithm still
  requires specification of its identifier, encodings, and verification rules,
  but does not require structural changes to the key formats. If FN-DSA is
  compromised, the unstable prefix can be deprecated and a replacement
  introduced via a follow-up MSC.

- **Key compromise recovery.** Identical to Ed25519: rotate the key, publish the
  old key in `old_verify_keys` with `expired_ts`. Note the trust-model
  constraint: a replacement FN-DSA key must be authenticated by a previously
  trusted FN-DSA key, so servers SHOULD keep an offline backup of at least one
  FN-DSA key capable of signing a rotation.

## Unstable Prefix

While this MSC is in development, the following unstable prefixes are used:

| Stable Identifier                                | Unstable Identifier                                          |
| ------------------------------------------------ | ------------------------------------------------------------ |
| `fn-dsa-512` (key algorithm)                     | `tk.nutra.msc45xx.fn-dsa-512`                                |
| `X-Matrix-PQC` (HTTP header)                     | `X-Matrix-PQC` (no prefix needed, custom header)             |
| `X-Matrix-PQC-Session` (HTTP header)             | `X-Matrix-PQC-Session` (no prefix needed, custom header)     |
| `/_matrix/federation/v1/key_exchange` (endpoint) | `/_matrix/federation/unstable/tk.nutra.msc45xx/key_exchange` |

The unstable algorithm prefix is used in `verify_keys` key IDs, `signatures`
entries, and `X-Matrix-PQC` header `key` parameters. For example, the
`/_matrix/key/v2/server` response would use the unstable algorithm identifier in
key IDs:

```json
{
    "verify_keys": {
        "tk.nutra.msc45xx.fn-dsa-512:pqc0": {
            "key": "<base64-fn-dsa-512-pubkey>"
        }
    }
}
```

Once this MSC is accepted but not yet merged into a released spec version,
implementations SHOULD support both the unstable prefix and the stable
identifier, accepting either.

### Pre-Finalization Deployment Guidance

FIPS 206 has not been finalized as of May 2026. Implementations deploying FN-DSA
before finalization MUST observe the following constraints:

- **All keys are temporary.** FN-DSA keys published under unstable identifiers
  (`tk.nutra.msc45xx.fn-dsa-512`) MUST be treated as provisional. Operators
  SHOULD expect mandatory key rotation if FIPS 206 final changes encodings,
  parameters, or the signing algorithm relative to the draft revision used.
- **Pin a specific draft revision.** Implementations MUST document which FIPS
  206 draft revision they target. Interoperability between implementations
  targeting different draft revisions is not guaranteed.
- **Use unstable identifiers everywhere.** During the draft period,
  `/_matrix/key/v2/server` key entries and `X-Matrix-PQC` header `key`
  parameters MUST use the unstable algorithm identifier
  (`tk.nutra.msc45xx.fn-dsa-512`), not the stable identifier. This ensures that
  draft-era signatures are distinguishable from signatures produced under the
  finalized standard, and that receiving servers can unambiguously determine
  which encoding rules apply.
- **Rotation on parameter change.** If a subsequent FIPS 206 draft or the final
  standard changes the public key encoding, signature encoding, or algorithm
  semantics, all previously published unstable FN-DSA keys MUST be retired to
  `old_verify_keys` and replaced with keys conforming to the updated
  specification.
- **No production trust assumptions.** During the unstable period, FN-DSA
  signatures provide defence-in-depth but MUST NOT be the sole basis for
  production security decisions. Ed25519 signatures and transport authentication
  remain the authoritative trust anchors until FIPS 206 is finalized and stable
  identifiers are adopted.

## Dependencies

- **NIST FIPS 206 (FN-DSA):** This MSC targets the FIPS 206 initial public
  draft. Unstable prefixes and the deployment guidance above buffer against
  pre-finalization changes. Once FIPS 206 is finalized, this MSC will be updated
  to reference the final standard, and stable identifiers (`fn-dsa-512`) will
  replace unstable prefixes.
- **NIST FIPS 203 (ML-KEM):** Required only by the optional session negotiation
  extension. FIPS 203 was finalized in August 2024.

This MSC has no dependency on MSC 45YY or MSC 0F00; they depend on it.

## Backwards Compatibility

This proposal is fully backwards-compatible:

- **Key distribution** is additive — new entries in `verify_keys`,
  `old_verify_keys`, and `signatures`. Legacy servers ignore unknown key
  algorithms.
- **Transport auth** is additive — `X-Matrix-PQC` and `X-Matrix-PQC-Session` are
  ignored by legacy servers per RFC 9110, and the Ed25519 `Authorization` header
  remains present and authoritative.
- **One new endpoint**, which is OPTIONAL, discoverable by its `404`, and has a
  mandatory fallback path.
- **Zero impact on events, PDUs, room versions, or clients.**

## References

[^1]:
    **FIPS 206 Status Update**
    https://groups.google.com/a/list.nist.gov/g/pqc-forum/c/1HXzjlMUU6Y
