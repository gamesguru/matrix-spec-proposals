# MSC 00FD: Signing Key Caching and Key ID Uniqueness

Matrix federation relies on server signing keys to authenticate PDUs and federation HTTP requests. The current specification defines the `/_matrix/key/v2/server` endpoint for key distribution and `/_matrix/key/v2/query` for notary-based key lookup, but provides insufficient guidance on how receiving servers should cache keys, handle key rotation, and respond to Key ID collisions.

In practice, this has led to:

1. **Excessive key fetching.** Servers that re-fetch signing keys from remote servers or the `matrix.org` notary for every PDU verification, causing federation latency spikes and split-brain room graphs during transient network failures.
2. **Ambiguous behavior on Key ID reuse.** When a server administrator wipes their database and regenerates a signing key under the same Key ID (e.g., `ed25519:auto` or `ed25519:1`), receiving servers have no clear guidance on how to handle the resulting collision. Some implementations have considered "trial verification" — caching multiple key bodies for the same ID and trying each one — which introduces severe security vulnerabilities.

This MSC formalizes signing key caching requirements, Key ID uniqueness semantics, and admin guardrails as **server behavior standardization**. It applies exclusively to server-to-server federation key management and does not modify PDU authorization rules, state resolution, or any room-version-scoped logic.

## Proposal

### Key Caching Requirements

Servers MUST cache remote server signing keys obtained from `/_matrix/key/v2/server` responses and `/_matrix/key/v2/query` notary responses. The following requirements apply to all signing algorithm types (`ed25519`, and `fn-dsa-512` once [MSC 00FF](https://github.com/matrix-org/matrix-spec-proposals/pull/00FF) is accepted).

**Cache refresh lifetime.** Servers MUST cache key responses and SHOULD proactively refresh cached keys before the `valid_until_ts` expiry to avoid verification failures during key rotation windows. Servers MUST NOT fall back to fetching keys from remote servers or notary servers for every individual PDU or HTTP request verification.

**Cache persistence.** Key caches SHOULD be persisted to durable storage (e.g., database) rather than held only in memory. A server restart should not require re-fetching every remote server's keys from the network.

**Notary fallback.** When a required signing key is not present in the local cache and the remote server is unreachable, servers SHOULD query a configured notary server (`/_matrix/key/v2/query`). Servers MUST NOT treat notary unavailability as a verification success.

### Key ID Uniqueness Invariant

A Key ID (`algorithm:key_id`) MUST map to exactly one public key body for a given remote server. This is a strict, permanent 1:1 binding. The purpose of a Key ID is to provide an unambiguous reference from a signature entry to a specific cryptographic key; allowing multiple key bodies under the same ID defeats this purpose.

**Permanent binding.** The cryptographic binding between a Key ID and its public key body is a **permanent record**, not a cache entry. While `valid_until_ts` dictates when a server should refresh the `/_matrix/key/v2/server` endpoint, the observed association between a Key ID and its key body MUST NOT be purged from the server's key database when `valid_until_ts` expires. Purging this binding would cause "collision amnesia" — the server would lose track of the original key body and blindly accept a colliding key body on the next fetch.

**Collision detection.** If a server observes a key response (whether fetched directly via `/_matrix/key/v2/server` or via a `/_matrix/key/v2/query` notary) from a remote server where a Key ID that was previously associated with public key `A` is now associated with a different public key `B`, the receiving server MUST:

1. **Retain the previously observed key.** The original key body remains authoritative for that Key ID. The conflicting key response MUST NOT replace it.
2. **Log the collision.** The server SHOULD log the Key ID collision at warning level, including the remote server name, the Key ID, and the SHA-256 fingerprints of both the cached and conflicting public keys. This alerts the operator to a potential misconfiguration or compromise on the remote server.
3. **Never perform trial verification.** The server MUST NOT cache multiple key bodies for the same Key ID and attempt signature verification against each one. See [Security Considerations](#security-considerations) for the vulnerabilities this would introduce.

**Intra-payload rejection.** A single key response payload MUST NOT contain multiple different public key bodies for the same Key ID (e.g., across `verify_keys` and `old_verify_keys`, or duplicated within the same dictionary). If a receiving server detects a Key ID collision within a single HTTP response, the entire response MUST be rejected as malformed.

**First Seen Wins.** The collision detection rule follows a strict **First Seen Wins** policy. The first public key body observed for a given `(server_name, algorithm, key_id)` tuple is the permanent binding. This is a direct consequence of Matrix's Trust-On-First-Use (TOFU) model for server key discovery.

**Localized impact acknowledgement.** The First Seen Wins rule will cause a **localized DAG divergence** for the misconfigured server: peers that cached the original key will reject new events from the server (signature verification fails against the wrong key body), while peers that never cached the original key will accept them. This is an unavoidable consequence of out-of-band key resolution — different servers observe different key states at different times. This MSC does not and _cannot_ eliminate this divergence, because key fetching is not part of the room DAG consensus. What this MSC does is make the divergence **deterministic, documented, and intentional**: it is the correct punishment for a protocol violation (Key ID reuse), and it creates immediate, visible failure that forces the administrator to fix their configuration rather than silently corrupting historical verification.

### Key Rotation Procedure

When a server rotates its signing key, the administrator MUST:

1. **Generate a new key with a new, unique Key ID.** For example, rotating from `ed25519:1` to `ed25519:2`, or from `fn-dsa-512:pqc0` to `fn-dsa-512:pqc1`.
2. **Retire the old key.** The old key MUST appear in the `old_verify_keys` section of the `/_matrix/key/v2/server` response with an appropriate `expired_ts` timestamp.
3. **Publish the new key.** The new key appears in `verify_keys` with the new Key ID.

Reusing a Key ID with a different key body is a **protocol violation**. This most commonly occurs when an administrator wipes a server's database, regenerates signing keys, but leaves the server configuration set to the same Key ID (e.g., the default `ed25519:auto`).

### Admin Startup Guardrails

Homeserver implementations SHOULD detect Key ID reuse at startup. If the server's configured signing key has a different key body than what was previously persisted for that Key ID, the server SHOULD refuse to start and emit a clear error message instructing the administrator to either restore the original key or assign a new Key ID. This prevents the misconfiguration from propagating to the federation in the first place.

Because local startup guardrails cannot detect collisions if the server's database has been entirely wiped (the most common cause of Key ID reuse), homeserver implementations SHOULD ensure that default Key ID generation incorporates a timestamp or high-entropy component (e.g., `ed25519:a7B_93k` rather than the default `ed25519:auto`). This ensures that if an administrator regenerates keys after a total state loss, a novel Key ID is structurally guaranteed.

This is the most effective mitigation because it eliminates the root cause: it stops the bad key from ever being published, avoiding the federation-wide collision detection and localized divergence entirely.

### Recovery from Key Loss

If a remote server has irrecoverably lost its private signing key (e.g., unrecoverable database failure without backup):

1. **The administrator MUST generate a new key with a new Key ID.**
2. **If the public key material is still known** (e.g., from backups, logs, or cached by peers), the lost key SHOULD be published in `old_verify_keys` with `expired_ts` set to the approximate time of loss.
3. **If the public key material is also lost**, the administrator must accept that historical events signed by the lost key may fail verification on servers that never cached it. There is no protocol-level recovery for this scenario — by design.

The protocol does not provide an automated recovery mechanism for Key ID collisions. It is safer for the federation to surface the misconfiguration as visible failure — forcing the administrator to discover and fix the error — than to bake dangerous trial verification logic into every homeserver to silently accommodate administrative mistakes.

**Manual cache eviction.** Because the First Seen Wins policy permanently binds a Key ID, a successful TOFU poisoning attack (or a catastrophic remote misconfiguration with no recovery path) will result in permanent federation failure with that server. To allow recovery, homeserver implementations SHOULD provide an administrative mechanism (e.g., an Admin API or CLI tool) to manually evict the cached key-body bindings for a specific remote server name, allowing a human operator to break the binding and re-initiate TOFU. This is an intentionally manual, operator-gated escape hatch — it must not be automatable or triggerable via federation traffic.

### Historical Event Verification

Cached keys, including keys retired to `old_verify_keys`, MUST be retained for historical PDU verification. An event signed by `algorithm:key_id` at time `T` is valid if the key identified by `algorithm:key_id` was active at time `T` — that is, the key's publication preceded `T` and `T` < `expired_ts` (or the key has no `expired_ts`, indicating it was active until replaced).

The strict Key ID uniqueness invariant ensures that this lookup is always unambiguous: for any `(server_name, algorithm, key_id)` tuple, there is at most one public key body, and its validity window is well-defined.

## Why This MSC Does Not Propose Room Version Changes

Key ID collision detection is a **local server observation** — it depends on out-of-band HTTP key fetching, not on the immutable event JSON that room version auth rules evaluate. Room version authorization rules must be **pure mathematical functions** that produce the same result on every server given the same event and room state. Because different servers fetch keys at different times and may have different cache histories, a collision-based auth rule would guarantee the exact split-brain it tries to prevent:

1. Server A (online for years) has the old key cached, detects a collision, and rejects new events.
2. Server B (booted up yesterday) only knows the new key, sees no collision, and accepts the events.
3. The room permanently forks.

Additionally, under Matrix's TOFU model, a `/_matrix/key/v2/server` response is self-signed by the private key _in the payload_. An attacker who briefly hijacks a server's IP (DNS spoofing, BGP hijacking) can generate a new keypair, label it with the target's Key ID, and produce a mathematically valid self-signature. If collision detection were an auth rule, the attacker would trivially weaponize it — injecting a collision that permanently blacklists the legitimate server's Key ID from all Room Version N rooms, without ever needing the real private key.

This MSC therefore operates exclusively at the **Federation API / server behavior layer**. It standardizes how servers cache, detect, and react to key anomalies, but explicitly does not touch room version consensus rules.

## Potential Issues

- **Misconfigured servers will experience localized isolation.** An administrator who wipes their database and regenerates keys under the same Key ID will find their server unable to federate with peers that cached the original key. This is intentional — the protocol prioritizes cryptographic correctness over convenience. The fix is straightforward: change the Key ID in the server configuration.

- **No automated Key ID collision recovery.** Unlike some protocols that provide key-reset ceremonies or trusted-third-party recovery, Matrix intentionally provides no automated mechanism. Automated recovery introduces trust assumptions that conflict with Matrix's zero-trust federation model.

- **Permanent key-body storage.** The permanent binding requirement means servers must retain key-body records indefinitely, proportional to the number of remote servers encountered. For a typical homeserver federating with a few thousand servers, this is negligible (a few megabytes of public key material).

- **Localized DAG divergence is unavoidable.** The First Seen Wins rule means that peers with different cache histories may disagree on events from a misconfigured server. This is an inherent property of out-of-band key resolution and cannot be solved at the protocol level. This MSC makes the behavior deterministic rather than implementation-dependent, which is an improvement over the status quo.

## Alternatives

- **Trial verification (try all cached keys for a Key ID).** Explicitly rejected. Trial verification introduces a CPU-exhaustion DoS vector (an attacker can spam garbage-signed events, forcing `N` expensive signature verifications per event), breaks historical DAG verification (which key was active when?), and violates the cryptographic identity contract of the Key ID.

- **Room-version-gated strict rejection.** Rejected. Key collision detection is out-of-band local state, not derivable from event JSON. A collision-based auth rule would guarantee split-brain (see [Why This MSC Does Not Propose Room Version Changes](#why-this-msc-does-not-propose-room-version-changes)). Worse, it would weaponize TOFU: an attacker who briefly hijacks a server's IP could inject a collision that permanently blacklists the victim's Key ID from Room Version N rooms.

- **Soft failure on Key ID collision (warn but accept the new key).** This silently breaks historical verification — events signed under the old key body would fail verification using the new key, corrupting state resolution for rooms involving the affected server. Rejected.

- **Key ID collision resolution via notary consensus.** Peers could query multiple notary servers and accept the key body attested by a majority. This introduces a trusted-third-party assumption that Matrix's federation model explicitly avoids, and notary servers may themselves have stale caches. Rejected.

- **Automatic Key ID bumping by the server.** Homeserver implementations could auto-increment the Key ID on every key generation, preventing collisions entirely. This is a reasonable implementation best practice and is RECOMMENDED by this MSC (see Admin Startup Guardrails), but cannot be mandated at the protocol level because Key ID assignment is a server-local configuration decision.

## Security Considerations

- **CPU-exhaustion DoS prevention.** The strict 1:1 Key ID → key body mapping eliminates the trial verification attack vector. Signature verification is performed against exactly one key per Key ID, bounding the computational cost per event to `O(number of signing servers)` rather than `O(number of signing servers × cached keys per ID)`.

- **TOFU cache poisoning.** Under Matrix's Trust-On-First-Use model, a `/_matrix/key/v2/server` response is self-signed by the private key associated with the payload. An attacker who briefly hijacks a server's IP (DNS spoofing, BGP hijacking) can generate a new keypair, label it with the target's Key ID, and produce a mathematically valid self-signature. The First Seen Wins policy protects against this: if the legitimate key was cached first, the attacker's key is rejected as a collision. If the attacker's key is cached first (the server was never contacted before), TOFU provides no protection regardless of this MSC — this is an inherent limitation of TOFU, not a flaw in this proposal.

- **DAG integrity.** The Key ID uniqueness invariant guarantees that historical signature verification is deterministic. For any event at any point in time, the key that signed it is unambiguously identified by the `(server_name, algorithm, key_id)` tuple in the `signatures` dictionary.

- **Compromise detection.** Key ID collisions are a potential indicator of server compromise (an attacker generating a new key and attempting to publish it under an existing ID). Hard rejection with operator alerting provides an early warning mechanism.

- **Cache expiration ≠ binding expiration.** The `valid_until_ts` field governs when to _refresh_ the key endpoint, not when to _forget_ the key body. Servers that purge key-body bindings on `valid_until_ts` expiry create a window where collision detection is blind. This MSC explicitly requires permanent retention of key-body bindings to close this gap.

- **Storage exhaustion DoS.** Mandating permanent storage of key-body bindings introduces a
  theoretical storage exhaustion vector if an attacker forces a server to fetch and permanently
  store millions of unique Key IDs. Homeserver implementations SHOULD mitigate this by enforcing a
  reasonable maximum limit on the number of cached Key IDs per remote server name (e.g., 1,000 keys).
  If a remote server reaches this quota, receiving servers MUST ignore new Key IDs for that domain.
  As with TOFU poisoning, recovering from an exhausted quota requires the administrator to use the
  manual cache eviction escape hatch. Implementations MUST rely on existing federation rate-limiting
  to discard junk traffic before allocating database records. In practice, legitimate servers
  publish single-digit numbers of active keys at any given time; a server claiming thousands of Key
  IDs is unambiguously hostile. To optimize database performance and minimize index footprint on
  high-volume production deployments, homeserver implementations SHOULD utilize partial index
  constraints (e.g., `WHERE is_compromised = FALSE` in PostgreSQL) when indexing the cached
  signing keys.

## Unstable Prefix

This MSC does not introduce new protocol identifiers and does not require an unstable prefix. The behavioral changes (mandatory caching, permanent key-body binding, collision detection, trial verification prohibition) are implementation requirements that can be adopted immediately.

## Dependencies

- None. This MSC is independent of other proposals. It applies to `ed25519` keys today and will apply equally to `fn-dsa-512` keys if [MSC 00FF](https://github.com/matrix-org/matrix-spec-proposals/pull/00FF) is accepted.

## Backwards Compatibility

This proposal is fully backwards-compatible:

- **No protocol wire changes.** No new fields, endpoints, or response formats are introduced.
- **No room version changes.** No PDU authorization or state resolution rules are modified.
- **Existing well-configured servers are unaffected.** Servers that already use unique Key IDs on rotation (the expected behavior) experience no change.
- **Misconfigured servers experience a clarified failure mode.** Servers that reuse Key IDs with different key bodies will be rejected by peers implementing this MSC. This failure already occurs unpredictably today (depending on cache state); this MSC makes the behavior deterministic and well-documented.

---

## MSC Checklist

- [ ] Are [appropriate implementation(s)](https://spec.matrix.org/proposals/#implementing-a-proposal) specified in the MSC's PR description?
- [x] Are all MSCs that this MSC depends on already accepted?
- [ ] For each endpoint that is introduced or modified:
  - [x] N/A — no endpoints are introduced or modified
- [x] Will the MSC require a new room version, and if so, has that been made clear?
  - [x] No new room version required. This MSC operates at the Federation API layer only.
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
- [ ] Changes have applicable [Sign Off](https://github.com/matrix-org/matrix-spec-proposals/blob/main/CONTRIBUTING.md#sign-off) from all authors/editors/contributors
