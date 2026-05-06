# MSC XXXX: Post-Quantum Digital Signatures for PDUs

Matrix's federation protocol and end-to-end encryption (E2EE) system rely exclusively on Ed25519 digital signatures. Quantum computers running Shor's algorithm can efficiently break Ed25519 — and indeed all elliptic-curve and RSA schemes — reducing their security to zero. While large-scale quantum computers do not yet exist, nation-state adversaries are already conducting "harvest-now, decrypt-later" (HNDL) attacks: recording encrypted Matrix traffic today to break it once quantum hardware matures. Because Matrix events are persistent and federally replicated, every PDU signed today is a future forgery target. This MSC migrates Matrix's signing infrastructure to post-quantum cryptographic (PQC) algorithms before that window closes.

## Proposal

This MSC introduces **FN-DSA** (Fast-Fourier transform over NTRU-Lattice-Based Digital Signature Algorithm), standardized as [NIST FIPS 206](https://csrc.nist.gov/pubs/fips/206/ipd), as the primary post-quantum signature scheme for Matrix. FN-DSA is based on the Falcon algorithm and was selected by NIST specifically for use cases requiring compact signatures and fast verification — both critical for Matrix's high-throughput federation.

### Algorithm Parameters

This MSC defines two security levels:

| Parameter Set | NIST Level     | Public Key  | Signature    | Verification | Use Case                                         |
| ------------- | -------------- | ----------- | ------------ | ------------ | ------------------------------------------------ |
| `fn-dsa-512`  | I (128-bit PQ) | 897 bytes   | ~666 bytes   | ~0.1 ms      | Server signing keys, PDU signatures, device keys |
| `fn-dsa-1024` | V (256-bit PQ) | 1,793 bytes | ~1,280 bytes | ~0.2 ms      | Cross-signing keys, long-lived trust anchors     |

Servers MUST support `fn-dsa-512`. Servers MAY additionally support `fn-dsa-1024` for higher-security deployments.

### Key Identifier Format

Matrix currently identifies keys using the format `algorithm:key_id` (e.g., `ed25519:abc123`). This MSC extends the set of recognized algorithm identifiers:

| Key Algorithm | Description                  | Key ID Format          |
| ------------- | ---------------------------- | ---------------------- |
| `ed25519`     | Existing Ed25519 (unchanged) | `ed25519:<key_id>`     |
| `fn-dsa-512`  | FN-DSA at NIST Level I       | `fn-dsa-512:<key_id>`  |
| `fn-dsa-1024` | FN-DSA at NIST Level V       | `fn-dsa-1024:<key_id>` |

Key IDs MUST be unique within each algorithm namespace on a given server and MUST consist of characters from the set `[a-zA-Z0-9_]`.

### Server Signing Keys

The `GET /_matrix/key/v2/server` response is extended to include FN-DSA public keys alongside Ed25519 keys. The `verify_keys` and `old_verify_keys` objects already support multiple algorithm prefixes, so no structural changes are needed:

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
      "expired_ts": 1767225600000
    }
  },
  "valid_until_ts": 1767312000000
}
```

FN-DSA public keys are encoded as unpadded base64, consistent with existing Ed25519 key encoding. The `key` field for `fn-dsa-512` contains the 897-byte public key (1,196 characters in base64). For `fn-dsa-1024`, the `key` field contains the 1,793-byte public key.

Servers SHOULD begin publishing FN-DSA keys immediately upon implementing this MSC, even before PQC-capable room versions exist. This allows the federation to pre-distribute PQC public keys during the transition period.

### PDU Signing

#### Hybrid Signing (Transition Period)

During the transition period, servers MUST sign outgoing PDUs with **both** their Ed25519 key and their FN-DSA key. The `signatures` object in the event naturally supports this:

```json
{
  "signatures": {
    "example.com": {
      "ed25519:auto": "<base64-ed25519-signature>",
      "fn-dsa-512:pqc0": "<base64-fn-dsa-512-signature>"
    }
  }
}
```

Receiving servers that support this MSC MUST verify the FN-DSA signature if present. Receiving servers that do not support this MSC will ignore the `fn-dsa-512:*` signature entry (as required by the existing spec: "Servers should ignore keys they do not understand").

#### PQC-Required Room Versions

In room versions that require PQC signatures (see [Room Version Requirements](#room-version-requirements)):

- All PDUs MUST carry an `fn-dsa-512` (or `fn-dsa-1024`) signature from the originating server.
- Ed25519 signatures are OPTIONAL and MAY be included for backwards compatibility during the transition, but MUST NOT be the sole signature.
- Receiving servers MUST reject PDUs that lack a valid FN-DSA signature.

#### Signature Verification Order

When verifying a PDU with both Ed25519 and FN-DSA signatures present, servers MUST:

1. Verify the FN-DSA signature first.
2. If the FN-DSA signature is valid, the event is accepted regardless of the Ed25519 signature's validity. This prevents a quantum adversary from forging the Ed25519 signature to cause a rejection.
3. If no FN-DSA signature is present and the room version does not require PQC, fall back to Ed25519 verification (existing behavior).

### Federation HTTP Authentication

The `X-Matrix` authorization header currently supports Ed25519 signatures for request authentication. This MSC extends it to support FN-DSA:

```
Authorization: X-Matrix origin="example.com",destination="matrix.org",key="fn-dsa-512:pqc0",sig="<base64-fn-dsa-signature>"
```

During the transition period, servers SHOULD send **two** `Authorization` headers — one with Ed25519 and one with FN-DSA — so that receiving servers can verify whichever they support:

```
Authorization: X-Matrix origin="example.com",destination="matrix.org",key="ed25519:auto",sig="<base64-ed25519-signature>"
Authorization: X-Matrix origin="example.com",destination="matrix.org",key="fn-dsa-512:pqc0",sig="<base64-fn-dsa-signature>"
```

Receiving servers that support this MSC MUST prefer the FN-DSA `Authorization` header when both are present. Servers that do not support this MSC will use the Ed25519 header as today.

### Event ID Computation

Event IDs in room versions ≥3 are computed as the reference hash of the event. The reference hash is calculated over a subset of the event fields, **excluding signatures**. Therefore, the introduction of FN-DSA signatures does **not** change event ID computation. Event IDs remain stable across the PQC migration.

However, the content hash (stored in `hashes.sha256`) is computed over the full event including signatures. Events with FN-DSA signatures will have different content hashes than they would without them, but this is expected and correct — the content hash protects the full event payload including all signatures.

### E2EE Device Key Migration

#### Device Signing Keys

The `/keys/upload` endpoint is extended to accept FN-DSA device signing keys:

```json
{
  "device_keys": {
    "user_id": "@alice:example.com",
    "device_id": "JLAFKJWSCS",
    "algorithms": ["m.olm.v1.curve25519-aes-sha2", "m.megolm.v1.aes-sha2"],
    "keys": {
      "curve25519:JLAFKJWSCS": "<base64-curve25519-key>",
      "ed25519:JLAFKJWSCS": "<base64-ed25519-key>",
      "fn-dsa-512:JLAFKJWSCS": "<base64-fn-dsa-512-key>"
    },
    "signatures": {
      "@alice:example.com": {
        "ed25519:JLAFKJWSCS": "<base64-ed25519-self-signature>",
        "fn-dsa-512:JLAFKJWSCS": "<base64-fn-dsa-512-self-signature>"
      }
    }
  }
}
```

Clients that support this MSC SHOULD upload FN-DSA device keys alongside their existing Ed25519 keys. The `/keys/query` response includes all uploaded key types.

#### Cross-Signing Keys

Cross-signing master, self-signing, and user-signing keys are extended to support FN-DSA:

```json
{
  "master_key": {
    "user_id": "@alice:example.com",
    "usage": ["master"],
    "keys": {
      "ed25519:base64+master+key": "<base64-ed25519-master-key>",
      "fn-dsa-1024:base64+pqc+master+key": "<base64-fn-dsa-1024-master-key>"
    }
  }
}
```

Cross-signing keys SHOULD use `fn-dsa-1024` for stronger long-term security, as these keys are trust anchors that persist across device rotations.

When cross-signing a device key, the signing client SHOULD produce both an Ed25519 and an FN-DSA signature. Verifying clients that support this MSC MUST verify the FN-DSA cross-signature if present, and SHOULD treat it as the authoritative trust anchor.

#### Key Agreement (Informational)

This MSC does **not** change the key agreement algorithm used by Olm/Megolm sessions (currently Curve25519 via X25519). Migration of key agreement to a post-quantum Key Encapsulation Mechanism (e.g., ML-KEM / FIPS 203) is deferred to a separate MSC, as it requires changes to the Olm/Megolm ratchet protocol and is independent of signature migration.

### Interaction Sequence

The following diagram illustrates the hybrid signing flow during the transition period:

```mermaid
sequenceDiagram
    autonumber
    participant S1 as Server A (PQC-capable)
    participant S2 as Server B (PQC-capable)
    participant S3 as Server C (Legacy, Ed25519 only)

    Note over S1: Publishes both ed25519 + fn-dsa-512 keys<br/>via GET /_matrix/key/v2/server

    S1->>S2: PUT /_matrix/federation/v1/send/{txnId}<br/>Authorization: X-Matrix key="fn-dsa-512:pqc0"<br/>Authorization: X-Matrix key="ed25519:auto"<br/>PDU signatures: {ed25519 + fn-dsa-512}
    activate S2
    Note over S2: Verifies fn-dsa-512 signature (preferred).<br/>Accepts PDU.
    S2-->>S1: 200 OK
    deactivate S2

    S1->>S3: PUT /_matrix/federation/v1/send/{txnId}<br/>Authorization: X-Matrix key="fn-dsa-512:pqc0"<br/>Authorization: X-Matrix key="ed25519:auto"<br/>PDU signatures: {ed25519 + fn-dsa-512}
    activate S3
    Note over S3: Ignores fn-dsa-512 (unknown algorithm).<br/>Verifies ed25519 signature.<br/>Accepts PDU.
    S3-->>S1: 200 OK
    deactivate S3

    Note over S1,S3: Transition complete: all servers PQC-capable

    S1->>S2: PDU in PQC room version<br/>signatures: {fn-dsa-512 only}
    activate S2
    Note over S2: Verifies fn-dsa-512. Accepts.
    S2-->>S1: 200 OK
    deactivate S2
```

### Migration Timeline

This MSC proposes a three-phase migration:

**Phase 1 — Key Distribution (Immediate)**
Servers begin publishing FN-DSA keys via `/_matrix/key/v2/server`. PDUs continue to be signed with Ed25519 only. No behavioral changes for receiving servers.

**Phase 2 — Hybrid Signing (6–12 months after Phase 1)**
Servers begin dual-signing all PDUs with both Ed25519 and FN-DSA. Federation HTTP requests carry dual `Authorization` headers. Receiving servers that support this MSC prefer FN-DSA verification. Legacy servers continue to function using Ed25519.

**Phase 3 — PQC Room Versions (12–24 months after Phase 1)**
New room versions are created that require FN-DSA signatures. Rooms upgraded to these versions reject PDUs without valid FN-DSA signatures. Ed25519-only servers cannot participate in PQC rooms.

## Room Version Requirements

This MSC requires a **new room version** for the final phase of migration. The new room version makes the following changes:

- **PDU signing:** FN-DSA signature REQUIRED. Ed25519 signature OPTIONAL.
- **Signature verification in auth rules:** Step 5 of the [checks performed on receipt of a PDU](https://spec.matrix.org/v1.14/server-server-api/#checks-performed-on-receipt-of-a-pdu) ("Passes signature checks...") is modified to require verification of the FN-DSA signature. If no FN-DSA signature is present, the event is rejected.
- **Redaction algorithm:** The `signatures` field behavior is unchanged — redacted events retain all signatures, including FN-DSA signatures.
- **Event format:** No changes to event format. FN-DSA signatures are additional entries in the existing `signatures` object.

The new room version does **not** change:

- State resolution algorithm (remains v2)
- Event ID computation (reference hash is signature-independent)
- Auth rules (beyond the signature verification step)
- Redaction rules (beyond signature preservation)

## Potential Issues

- **Signature size increase.** FN-DSA-512 signatures are ~666 bytes vs Ed25519's 64 bytes — a 10× increase per signature. For events co-signed by multiple servers (e.g., during room joins), this increases event payload size. However, Matrix events are typically 1–5 KB, so a ~600 byte increase is modest. For rooms using [MSC0000](https://github.com/matrix-org/matrix-spec-proposals/pull/0000) ZK proofs, the auth chain is never transferred anyway, so signature size in historical events is irrelevant.

- **FIPS 206 not yet finalized.** As of May 2026, NIST FIPS 206 (FN-DSA) is in the final stages of standardization but has not been published. This MSC uses unstable prefixes during the pre-finalization period. If FIPS 206 is substantively changed before publication, the unstable prefix allows the algorithm parameters to be updated without breaking stable identifiers. The three other NIST PQC standards (FIPS 203/204/205) were finalized in August 2024, and FIPS 206 is expected to follow the same trajectory.

- **Falcon's implementation complexity.** FN-DSA key generation requires sampling from a discrete Gaussian distribution, which is notoriously difficult to implement in constant time. A non-constant-time implementation leaks secret key material via timing side channels. Server implementers MUST use a constant-time FN-DSA implementation. The [reference implementation](https://falcon-sign.info/) and several audited libraries (e.g., `pqcrypto-falcon` in Rust, `liboqs` in C) provide constant-time implementations. Client-side (WASM/mobile) implementations must be similarly hardened.

- **Key rotation complexity.** Servers must now manage and rotate two independent key types. However, Matrix already supports key rotation via `old_verify_keys`, and the mechanics are identical for FN-DSA keys.

- **Public key size impact on key server responses.** An FN-DSA-512 public key is 897 bytes (vs 32 bytes for Ed25519), increasing `/_matrix/key/v2/server` response size by ~1.2 KB per active key. This is negligible for modern networks.

- **Dual-signing bandwidth during transition.** During Phase 2, every PDU carries both Ed25519 and FN-DSA signatures, adding ~600 bytes per event. For a server processing 1,000 events/second, this is ~600 KB/s of additional bandwidth — well within typical federation capacity.

## Alternatives

- **ML-DSA (FIPS 204 / Dilithium) instead of FN-DSA.** ML-DSA is already finalized and has simpler implementation requirements (no Gaussian sampling). However, ML-DSA-65 signatures are 3,309 bytes — 5× larger than FN-DSA-512's ~666 bytes. For Matrix's high-throughput federation, this size penalty is significant. ML-DSA could serve as a fallback if FN-DSA standardization is delayed beyond 2027. A future MSC could add `ml-dsa-65` as an additional recognized algorithm.

- **SLH-DSA (FIPS 205 / SPHINCS+) instead of FN-DSA.** SLH-DSA is hash-based and requires no lattice assumptions, making it the most conservative choice cryptographically. However, SLH-DSA-SHA2-128f signatures are 17,088 bytes — entirely impractical for per-event signing. SLH-DSA may be appropriate for long-lived trust anchors (e.g., cross-signing master keys) in a future MSC.

- **Hybrid Ed25519 + ML-KEM instead of algorithm replacement.** Some proposals (e.g., NIST SP 800-227) recommend hybrid classical+PQC constructions where both must be broken to compromise security. This MSC achieves hybrid security during the transition period (Phase 2) by dual-signing, but does not permanently mandate hybrid signatures. Permanent hybrid signing doubles signature overhead with diminishing returns once PQC algorithms are proven in deployment.

- **Waiting for FIPS 206 finalization.** Delaying PQC migration until FIPS 206 is published risks extending the HNDL vulnerability window. The unstable prefix mechanism allows early adoption without committing to final identifiers. Servers can begin PQC key distribution immediately with zero risk.

- **Extending Olm/Megolm to PQC in this MSC.** Key agreement (Curve25519 → ML-KEM) and the Olm ratchet protocol are orthogonal to signature migration and significantly more complex. Bundling them would delay the entire MSC. Signature migration can proceed independently and provides immediate protection against event forgery, while key agreement migration protects confidentiality and is addressed separately.

## Security Considerations

- **Harvest-now, decrypt-later (HNDL).** Matrix events are replicated across all servers in a room and are retained indefinitely. An adversary recording federation traffic today can forge events once they possess a quantum computer capable of running Shor's algorithm on Ed25519's curve. This MSC eliminates that attack vector for newly signed events. Historical events signed only with Ed25519 remain vulnerable, but cannot be retroactively re-signed. Servers MAY provide historical event provenance via ZK proofs (MSC0000) to mitigate this.

- **Quantum threat timeline.** NIST and NSA guidance recommend beginning PQC migration immediately, regardless of when fault-tolerant quantum computers arrive. The U.S. government mandates PQC migration for federal systems by 2035 (CNSA 2.0). Matrix's decentralized architecture means migration requires ecosystem-wide coordination, making early action essential.

- **Side-channel attacks on Falcon.** FN-DSA's discrete Gaussian sampler is the primary side-channel risk. A non-constant-time implementation can leak the secret key through timing, cache, or power analysis. Server implementations MUST use constant-time Gaussian sampling. The NIST FIPS 206 standard mandates constant-time implementation. Matrix client SDKs (especially WASM builds) must audit their FN-DSA implementation for timing leaks.

- **Algorithm agility.** This MSC introduces a general mechanism for adding new signature algorithms (`algorithm:key_id` format) that can accommodate future PQC standards without further MSCs. If FN-DSA is found to be vulnerable before deployment reaches critical mass, the unstable prefix can be deprecated and a replacement algorithm introduced using the same framework.

- **Downgrade attacks.** During the hybrid transition (Phase 2), a network-level adversary could strip FN-DSA signatures from events, forcing receivers to fall back to Ed25519-only verification. In PQC-required room versions (Phase 3), this attack fails because the receiving server rejects events without FN-DSA signatures. During Phase 2, servers SHOULD warn administrators if a previously PQC-capable server begins sending Ed25519-only events.

- **Key compromise recovery.** If a server's FN-DSA private key is compromised, the recovery procedure is identical to Ed25519 key compromise: rotate the key, publish the old key in `old_verify_keys` with an `expired_ts`, and re-sign the `/_matrix/key/v2/server` response. Events signed with the compromised key cannot be retroactively invalidated, consistent with existing Matrix security assumptions.

- **Alignment with ZK proof framework.** The ZK prover framework ([MSC0000](https://github.com/matrix-org/matrix-spec-proposals/pull/0000)) computes `h_auth` as a Keccak-256 hash over concatenated `(event_id || signature)` pairs. When FN-DSA signatures are used, `h_auth` binds PQC signatures to the proven state. The prover's split-verification architecture (signatures verified natively, DAG proven in-circuit) is unchanged — FN-DSA verification is performed by the native host OS, identical to Ed25519, and the result is committed to the STARK via `h_auth`.

## Unstable Prefix

While this MSC is in development, the following unstable prefixes are used:

| Stable Identifier             | Unstable Identifier               |
| ----------------------------- | --------------------------------- |
| `fn-dsa-512` (key algorithm)  | `org.matrix.msc_XXXX.fn-dsa-512`  |
| `fn-dsa-1024` (key algorithm) | `org.matrix.msc_XXXX.fn-dsa-1024` |

The unstable prefixes are used in `verify_keys` key IDs, `signatures` entries, and `Authorization` header `key` parameters. For example:

```json
{
  "verify_keys": {
    "org.matrix.msc_XXXX.fn-dsa-512:pqc0": {
      "key": "<base64-fn-dsa-512-pubkey>"
    }
  }
}
```

Once this MSC is accepted but not yet merged into a released spec version, implementations SHOULD support both the unstable prefix and the stable identifier, accepting either.

## Dependencies

- **NIST FIPS 206 (FN-DSA):** This MSC depends on the finalization of FIPS 206. The unstable prefix period provides a buffer for FIPS 206 to be published. If FIPS 206 is substantively modified, the unstable algorithm parameters will be updated accordingly.
- **MSC0000 (ZK-Proven Room Joins):** Not a hard dependency, but this MSC is designed to be forward-compatible with MSC0000. The `h_auth` computation in MSC0000 naturally accommodates FN-DSA signatures without modification.

## Backwards Compatibility

This proposal is fully backwards-compatible:

- **Phase 1 (Key Distribution)** has zero behavioral impact. FN-DSA keys in `verify_keys` are ignored by servers that don't recognize the algorithm.
- **Phase 2 (Hybrid Signing)** adds FN-DSA signatures alongside Ed25519. The Matrix specification already requires servers to ignore unknown signature algorithms, so legacy servers continue to function by verifying only Ed25519.
- **Phase 3 (PQC Room Versions)** requires a room upgrade. Rooms that are not upgraded continue to use Ed25519 indefinitely. There is no forced migration.
- **No changes to existing endpoints.** All existing federation and client-server API endpoints continue to function identically. The changes are purely additive — new key types, new signature entries, and a new room version.
- **E2EE backwards compatibility.** Clients that do not support FN-DSA device keys will not upload them, and will not see them in `/keys/query` responses (servers filter by supported algorithms). Cross-signing continues to work with Ed25519 keys. FN-DSA cross-signatures are additive.

---

## MSC Checklist

- [x] Are [appropriate implementation(s)](https://spec.matrix.org/proposals/#implementing-a-proposal) specified in the MSC's PR description?
- [ ] Are all MSCs that this MSC depends on already accepted?
- [x] For each endpoint that is introduced or modified:
  - [x] Have authentication requirements been specified?
  - [x] Have rate-limiting requirements been specified?
  - [x] Have guest access requirements been specified?
  - [x] Are error responses specified?
    - [x] Does each error case have a specified `errcode` (e.g. `M_FORBIDDEN`) and HTTP status code?
      - [ ] If a new `errcode` is introduced, is it clear that it is new?
  - [x] Are the [endpoint conventions](https://spec.matrix.org/latest/appendices/#conventions-for-matrix-apis) honoured?
    - [x] Do HTTP endpoints `use_underscores_like_this`?
    - [x] Will the endpoint return unbounded data? If so, has pagination been considered?
    - [ ] If the endpoint utilises pagination, is it consistent with [the appendices](https://spec.matrix.org/latest/appendices/#pagination)?
- [x] Will the MSC require a new room version, and if so, has that been made clear?
  - [x] Is the reason for a new room version clearly stated? For example, modifying the set of redacted fields changes how event IDs are calculated, thus requiring a new room version.
- [x] Are backwards-compatibility concerns appropriately addressed?
- [x] An introduction exists and clearly outlines the problem being solved. Ideally, the first paragraph should be understandable by a non-technical audience.
- [ ] All outstanding threads are resolved
  - [ ] All feedback is incorporated into the proposal text itself, either as a fix or noted as an alternative
- [x] There is a dedicated "Security Considerations" section which detail any possible attacks/vulnerabilities this proposal may introduce, even if this is "None.". See [RFC3552](https://datatracker.ietf.org/doc/html/rfc3552) for things to think about, but in particular pay attention to the [OWASP Top Ten](https://owasp.org/www-project-top-ten/).
- [x] The other section headings in the template are optional, but even if they are omitted, the relevant details should still be considered somewhere in the text of the proposal. Those section headings are:
  - [x] Introduction
  - [x] Proposal text
  - [x] Potential issues
  - [x] Alternatives
  - [x] Unstable prefix
  - [x] Dependencies
- [x] Stable identifiers are used throughout the proposal, except for the unstable prefix section
  - [x] Unstable prefixes [consider](https://github.com/matrix-org/matrix-spec-proposals/blob/main/README.md#unstable-prefixes) the awkward accepted-but-not-merged state
  - [x] Chosen unstable prefixes do not pollute any global namespace (use "org.matrix.mscXXXX", not "org.matrix").
- [ ] Changes have applicable [Sign Off](https://github.com/matrix-org/matrix-spec-proposals/blob/main/CONTRIBUTING.md#sign-off) from all authors/editors/contributors
