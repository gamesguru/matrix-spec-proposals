# MSC4499: Strict server signing key caching and key ID uniqueness

<!--
Standardizes server signing key caching and introduces a First Seen Wins rule for key ID uniqueness.

[Rendered](https://github.com/gamesguru/matrix-spec-proposals/blob/guru/4499-strict-key-caching/proposals/4499-key-caching.md)

### Implementations

- [Complement: formal server key caching and notary fall-back rules](https://github.com/gamesguru/complement/pull/6)
- [feat: add server key notary endpoints and MSC4499 compliance (gamesguru/continuwuity)](https://github.com/gamesguru/continuwuity/pull/75)

#### Current (baseline Synapse) Complement results: (9 pass / 1 skip / 7 fail)

<img width="482" height="443" alt="image" src="https://github.com/user-attachments/assets/d49a4446-5e10-44ef-8ce8-386a2e505ecd" />

#### Current (continuwuity impl) Complement results: (10 pass / 0 skip / 4 fail)

<img width="468" height="120" alt="image" src="https://github.com/user-attachments/assets/8cd14c10-0a90-4685-9695-b5ae8d34ce9c" />
-->

## Introduction

Matrix federation relies on cryptographic signatures to prove that events and
messages genuinely originated from the claimed server. To verify these
signatures, servers must continuously fetch and cache each other's public
signing keys.

Because the specification lacks a strict caching contract for this process, new
homeserver implementations often attempt to be "helpful." Without explicit
guidance, developers may design flexible caches that store multiple key bodies
for a single key ID and perform verification either with the most recently
observed key or the first one which works (trial verification).

While existing implementations such as Synapse effectively enforce a unique
`(server_name, key_id)` constraint at the storage layer, the protocol itself
does not mandate or enforce this behavior. This ambiguity leaves a loophole
where key collisions in the wild can cause room state DAG divergence (divergent
event acceptance/rejection across peers), and introduces a potential
CPU-exhaustion DoS vector for any implementation that attempts to gracefully
handle them.

Matrix's out-of-band key fetching model is inherently fragile when subjected to
domain re-registrations or catastrophic server misconfigurations. Rather than
masking this fragility with unpredictable trial verification, the requirements
proposed here codify existing best practice to make failures deterministic and
loud. Their feasibility has been demonstrated through a Complement conformance
suite and a qualifying `continuwuity` implementation developed as part of this
MSC's validation process. This proposal standardizes these practices to ensure
consistent security guarantees across the ecosystem.

## Proposal

### Relationship to existing specification

This MSC strengthens and supersedes the existing key caching and verification
rules defined in the Matrix specification (specifically the
[Server-Server API § Retrieving server keys](https://spec.matrix.org/v1.18/server-server-api/#retrieving-server-keys)
and the notary query endpoint).

Specifically, this proposal formalizes the following behaviors:

1. **First Seen Wins (FSW):** Replaces implicit "trial verification" logic with
   a strict, permanent 1:1 key ID uniqueness requirement.
2. **Negative Caching:** Upgrades the existing `SHOULD` caching guidance to a
   `MUST`, and introduces formal exponential backoff constraints for failures.
3. **Payload Sanitization:** Mandates the rejection of payloads containing
   identical key IDs with differing key material, and requires duplicate key
   detection within a single JSON dictionary.
4. **Historical Validation:** Formalizes timestamp-aware key validity for
   historical events, clarifying the role of `expired_ts`.
5. **Two-tier bindings (notary fallback):** Establishes that notary-learned
   bindings are provisional and can be overridden by direct, TLS-authenticated
   origin fetches, preventing malicious or compromised notaries from permanently
   poisoning key state.

This proposal also formalizes the `valid_until_ts` 7-day validity clamp as a
normative cache constraint.

### Key caching requirements

Servers MUST cache remote server signing keys obtained from
`/_matrix/key/v2/server` responses and `/_matrix/key/v2/query` notary responses.
The following requirements apply to all signing algorithm types (`ed25519`, and
any future signing algorithms, such as post-quantum algorithms).

**Cache refresh lifetime.** Servers MUST cache key responses and SHOULD
proactively refresh cached keys before their clamped `valid_until_ts` expiry
(i.e., restricted to at most 7 days from fetch) to avoid verification failures
during key rotation windows. When a server re-fetches a key and receives the
exact same key body it already has, this is a normal refresh; the server MUST
simply update its cached `valid_until_ts` and `expired_ts` timestamps.
Furthermore, servers MUST rely on their cache. They MUST NOT fall back to
fetching keys from remote servers or notary servers for every individual PDU or
HTTP request verification if a valid key is already cached locally.

**Negative caching and backoff.** Servers MUST cache fetch failures. A dead or
unreachable remote server can cause fetch storms if every inbound event or
reference triggers a fresh network request. Servers MUST implement exponential
backoff per remote server for failed key fetches. Servers MUST NOT re-fetch a
failed server's keys within 60 seconds of the last failure (the mandatory
floor), up to a recommended cap of 1 hour.

An inbound federation HTTP request whose immediate authentication _requires_ a
key fetch for the backoff-listed server (specifically, verifying the `X-Matrix`
request signature of the calling server) SHOULD permit at most one immediate,
rate-limited fetch attempt per backoff interval. This exemption MUST NOT be
applied to PDU signature verification (which MUST instead employ a
parked-PDU-and-retry pattern, allowing the transaction or PDU processing to wait
out the backoff interval without triggering a fetch). This ensures that a deluge
of new incoming events cannot bypass the 60-second negative cache floor.

Implementations SHOULD coalesce concurrent outgoing key fetch requests for the
same remote domain into a single active HTTP request to prevent network
saturation. If that fetch succeeds and the request authenticates, servers SHOULD
clear the backoff state.

To facilitate automated conformance testing in continuous integration
environments where a 60-second wait is impractical, implementations SHOULD
support configuring the minimum negative caching backoff floor (e.g., via a
test-only configuration option `org.matrix.msc4499_backoff_secs` or a
corresponding environment variable).

**Cache persistence.** Key caches SHOULD be persisted to durable storage (e.g.,
database) rather than held only in memory. A server restart should not require
re-fetching every remote server's keys from the network.

**Notary internal indexing.** Notary servers act as massive aggregation points
for federation keys. To prevent them from becoming distribution vectors for
collisions, notaries MUST also enforce the First Seen Wins rule internally.
However, to preserve a forensic trail of misconfigurations and anomalous event
rejections without violating strict database constraints (which usually enforce
a unique `(server_name, key_id)` tuple), notary implementations SHOULD
internally index observed key bodies by their full SHA-256 digest. For PQC
algorithms that use hash-derived key IDs, notaries SHOULD also index by the
derived `key_id` for lookup, but the full SHA-256 digest remains the canonical
fingerprint for the key body itself. This allows the notary to safely store
historical collisions, even if it only serves the "first seen" key via the
active API.

**Notary fallback (two-tier binding).** When a required signing key is not
present in the local cache, servers typically query a configured notary server
(`/_matrix/key/v2/query`). Because a notary is a relay, a direct fetch over
validated TLS to the actual server name (`/_matrix/key/v2/server`) provides
strictly stronger cryptographic proof of ownership.

To prevent a malicious or compromised notary from permanently ossifying a
poisoned key binding, bindings first observed via a notary are **provisional**.
They are used normally for verification, but if a subsequent _direct_ fetch from
the origin server yields a key body that conflicts with the provisional binding,
the direct fetch MUST override the provisional one, subject to the freeze
exception below. The server updates its cache to the direct-observed key body
and MUST log the collision loudly. The server SHOULD log which events (or at
minimum which rooms/time window) were verified under the displaced binding, and
MAY re-verify recent events. Bindings observed directly from the origin server
are **permanent** (see below). Servers MUST NOT treat notary unavailability as a
verification success.

**Provisional override freeze (expiration/retirement guard).** To prevent domain
re-registration hijackers from overriding historical keys, a provisional binding
MUST NOT be overridden if it has already expired or been retired. Specifically,
if the provisional key's cached `valid_until_ts` has passed, or if the key was
originally learned from `old_verify_keys` with an `expired_ts` in the past, the
binding is **frozen**. In this scenario, any conflicting key body returned by a
direct fetch for that key ID MUST be rejected as a collision. A provisional
override is only permitted if the provisional binding's key body remains within
its active validity window (`valid_until_ts` in the future). This prevents a
hijacker from reclaiming a retired or expired key ID with a new key body, while
preserving the recovery path against a compromised notary for active keys.

**Binding promotion.** A provisional (notary-observed) binding becomes permanent
the first time a direct fetch from the origin confirms the same key body.
Servers SHOULD attempt a prompt direct fetch after learning any binding via a
notary, to promote the binding and close the provisional window. Once permanent,
the binding is subject to the standard First Seen Wins rule: a later direct
fetch presenting a different key body for the same key ID is a collision and
MUST be rejected and logged. Direct-versus-direct conflicts are always resolved
by First Seen Wins; the two-tier rule applies only to the notary-versus-direct
case. Notary-versus-notary conflicts (or the same notary at two different times)
are also resolved by First Seen Wins among provisional observations.

### Key ID uniqueness invariant

A key ID (`algorithm:key_id`) MUST map to exactly one public key body for a
given remote server. This is a strict, permanent 1:1 binding. The purpose of a
key ID is to provide an unambiguous reference from a signature entry to a
specific cryptographic key; allowing multiple key bodies under the same ID
defeats this purpose.

**Permanent binding.** The cryptographic binding between a key ID and its public
key body is a **permanent record**, not a cache entry. This permanence governs
_key-body identity_ only; it does not alter the validity-window semantics (e.g.,
event signatures are still verified against the key's validity at the event's
`origin_server_ts`, and federation requests still require a currently valid
key). While `valid_until_ts` dictates when a server should refresh the
`/_matrix/key/v2/server` endpoint, the observed association between a key ID and
its key body MUST NOT be purged from the server's key database when
`valid_until_ts` expires. Purging this binding would cause "collision amnesia" —
the server would lose track of the original key body and blindly accept a
colliding key body on the next fetch.

**Collision detection.** If a server observes a key response (whether fetched
directly via `/_matrix/key/v2/server` or via a `/_matrix/key/v2/query` notary)
from a remote server where a key ID that was previously associated with public
key `A` is now associated with a different public key `B`, the receiving server:

1. **MUST retain the previously observed key.** The original key body remains
   authoritative for that key ID, unless the existing binding is provisional and
   the new observation is a direct fetch, in which case the two-tier override
   rule applies (see Notary fallback). In all other cases, the conflicting
   response MUST NOT replace it.
2. **SHOULD log the collision.** It helps forensically to log the key ID
   collision at warning level, including the remote server name, the key ID, and
   the SHA-256 fingerprints of both the cached and conflicting public keys. This
   alerts the operator to a potential misconfiguration or compromise on the
   remote server and may aid in community forensic or reconciliation efforts.
3. **MUST NOT perform trial verification.** The server SHOULD NOT cache multiple
   key bodies under the same key ID (the notary forensic index above excepted)
   and MUST NOT attempt signature verification against anything other than the
   single bound key. See [Security considerations](#security-considerations) for
   the vulnerabilities this would introduce.

**Intra-payload rejection.** A single key response payload MUST NOT contain
multiple different public key bodies for the same key ID (e.g., across
`verify_keys` and `old_verify_keys`, or duplicated within the same dictionary).
The same key body appearing under one key ID in both `verify_keys` and
`old_verify_keys` is legal. If a receiving server detects a key ID collision
within a single HTTP response, the entire response MUST be rejected as
malformed. No new Matrix `errcode` is introduced for this rejection; standard
HTTP fetch failures apply. When a notary server rejects an upstream key response
as malformed under this rule, the malformed response MUST NOT be included in the
`server_keys` array in the notary's response; the notary MAY continue serving
previously-cached valid entries for that server in the same response if
applicable (HTTP 200 with the malformed key absent). The notary MUST NOT convert
an upstream payload rejection into a non-200 status code, as this would break
batch queries where only a subset of queried servers returned malformed
payloads. When a direct fetch (`/_matrix/key/v2/server`) is rejected as
malformed, the server MUST treat it as a fetch failure for purposes of negative
caching and backoff.

Implementations SHOULD employ a JSON parser or pre-processing step capable of
detecting duplicate keys within a single JSON object for key response payloads
(`/_matrix/key/v2/server` and `/_matrix/key/v2/query` responses), as standard
JSON parsers in most languages silently deduplicate them. This requirement is
specific to key response payloads due to their cryptographic sensitivity; it
does not impose a general duplicate-key detection mandate on all Matrix JSON
parsing. The cross-map collision check (same key ID appearing in both
`verify_keys` and `old_verify_keys` with different key material) MUST be
implemented regardless.

**First Seen Wins.** The collision detection rule follows a strict **First Seen
Wins** policy. The first public key body observed for a given
`(server_name, algorithm, key_id)` tuple (whether found in `verify_keys` or
`old_verify_keys`) is the permanent binding. This is a direct consequence of
Matrix's Trust-On-First-Use (TOFU) model for server key discovery.

**Localized impact acknowledgement.** The First Seen Wins rule will cause a
**localized DAG divergence** for the misconfigured server: peers that cached the
original key will reject new events from the server (signature verification
fails against the wrong key body), while peers that never cached the original
key will accept them. This is an unavoidable consequence of out-of-band key
resolution — different servers observe different key states at different times.
This MSC does not and _cannot_ eliminate this divergence, because key fetching
is not part of the room DAG mainline. What this MSC does is make the divergence
**deterministic, documented, and intentional**: it prioritizes strict
cryptographic integrity over silently corrupting historical verification. While
this unavoidably leaves affected peers with a split-brain view of the room
(requiring manual cache eviction or state resets to recover) if the origin
server is not fixed, it creates an immediate, visible failure that forces the
misconfigured administrator to correct their setup. Eliminating this collateral
damage entirely requires a new room version mandating content-addressed key IDs,
which is deferred to a future MSC (see
[Future considerations](#future-considerations)).

### Key rotation procedure

When a server rotates its signing key, the administrator MUST:

1. **Generate a new key with a new, unique key ID.** For example, rotating from
   `ed25519:1` to `ed25519:2`, or from `pqc_algo:0` to `pqc_algo:1`.
2. **Retire the old key.** The old key MUST appear in the `old_verify_keys`
   section of the `/_matrix/key/v2/server` response with an appropriate
   `expired_ts` timestamp.
3. **Publish the new key.** The new key appears in `verify_keys` with the new
   key ID.

Reusing a key ID with a different key body is a **protocol violation**. This
most commonly occurs when an administrator wipes a server's database,
regenerates signing keys, but leaves the server configuration set to the same
key ID (e.g., the default `ed25519:auto`).

If this happens, administrators must rotate to a fresh key ID immediately. They
should further take efforts to correct membership or state drifts that occurred
during the period when an invalid, duplicated key was used to sign PDUs.

### Admin startup guardrails

Homeserver implementations SHOULD detect key ID reuse at startup. If the
server's configured signing key has a different key body than what was
previously persisted for that key ID, the server MUST refuse to start and emit a
clear error message instructing the administrator to either restore the original
key or assign a new key ID. This prevents the misconfiguration from propagating
to the federation in the first place.

Because local startup guardrails cannot detect collisions if the server's
database has been entirely wiped (the most common cause of key ID reuse),
homeserver implementations SHOULD ensure that default key ID generation
incorporates a timestamp or high-entropy component (e.g., `ed25519:a7B_93k`
rather than the default `ed25519:auto` or `ed25519:1`). This ensures that if an
administrator regenerates keys after a total state loss, a novel key ID is
structurally guaranteed. It also protects against a new server owner unwittingly
re-registering under a domain which formerly ran a Conduit server.

This is the most effective mitigation because it eliminates the root cause: it
stops the bad key from ever being published, avoiding the federation-wide
collision detection and localized divergence entirely.

### Recovery from key loss

If a remote server has irrecoverably lost its private signing key (e.g.,
unrecoverable database failure without backup):

1. **The administrator MUST generate a new key with a new key ID.**
2. **If the public key material is still known** (e.g., from backups, logs, or
   cached by peers), the lost key SHOULD be published in `old_verify_keys` with
   `expired_ts` set to the approximate time of loss. If it can be corroborated
   from an established notary, it should also be self-published under old keys.
3. **If the public key material is also lost**, the administrator must accept
   that historical events signed by the lost key may fail verification on
   servers that never cached it. There is no protocol-level recovery for this
   scenario — by design.

The protocol does not provide an automated recovery mechanism for key ID
collisions. It is safer for the federation to surface the misconfiguration as a
visible failure — forcing the administrator to discover and fix the error — than
to bake dangerous trial verification logic into every homeserver to silently
accommodate administrative mistakes.

**Manual cache eviction.** Because the First Seen Wins policy permanently binds
a key ID, a successful TOFU poisoning attack (or a catastrophic remote
misconfiguration with no recovery path) will result in permanent federation
failure with that server. To allow recovery, homeserver implementations MUST
provide an administrative mechanism (e.g., an Admin API or CLI tool) to manually
evict the cached key-body bindings for a specific remote server name, allowing a
human operator to break the binding and re-initiate TOFU.

This manual eviction MUST be logged loudly by the homeserver, including both the
server name and the fingerprints of the evicted keys. This is an intentionally
manual, operator-gated escape hatch — it must not be automatable or triggerable
via federation traffic (e.g., room ACLs and other federation-visible mechanisms
MUST NOT be able to trigger eviction).

### Historical event verification

Cached keys, including keys retired to `old_verify_keys`, MUST be retained for
historical PDU verification. An event signed by `algorithm:key_id` at time `T`
(where `T` is the event's `origin_server_ts`) is valid if and only if: (1) `T`
falls within the key's validity window, evaluated against the receiver's cached
observation at the time of verification (if the key has an `expired_ts`, i.e. it
appears in `old_verify_keys`, `T` must be less than `expired_ts`; otherwise, `T`
must be less than the clamped `valid_until_ts`), and (2) the event signature
cryptographically validates. This means a key retired at the origin remains
honored by peers until their local caches refresh. The 7-day cache validity
clamp restricts the window in which the key is authorized to sign new events,
but does not invalidate historically signed events when verifying them years
later.

Servers MUST sanity-check `expired_ts` values in `old_verify_keys`. A future
`expired_ts` (beyond a 5-minute clock-skew allowance) MUST be treated as
malformed for that specific key entry, but MUST NOT poison the rest of the
response payload.

The strict key ID uniqueness invariant ensures that this lookup is always
unambiguous: for any `(server_name, algorithm, key_id)` tuple, there is at most
one public key body, and its validity window is well-defined. This permanent
binding also acts as a forensic asset post-compromise: you can definitively
prove which specific key body signed what event, and when.

## Why this MSC does not propose room version changes

Key ID collision detection is a **local server observation** — it depends on
out-of-band HTTP key fetching, not on the immutable event JSON that room version
auth rules evaluate. Room version authorization rules must be **pure
mathematical functions** that produce the same result on every server given the
same event and room state. Because different servers fetch keys at different
times and may have different cache histories, a collision-based auth rule would
guarantee the exact split-brain it tries to prevent:

1. Server A (online for years) has the old key cached, detects a collision, and
   rejects new events.
2. Server B (booted up yesterday) only knows the new key, sees no collision, and
   accepts the events.
3. The room permanently forks.

Additionally, under Matrix's TOFU model, a `/_matrix/key/v2/server` response is
self-signed by the private key _in the payload_. An attacker who briefly hijacks
a server's IP (DNS spoofing, BGP hijacking) can generate a new keypair, label it
with the target's key ID, and produce a mathematically valid self-signature. If
collision detection were an auth rule, the attacker would trivially weaponize it
— injecting a collision that permanently blacklists the legitimate server's Key
ID from all Room Version N rooms, without ever needing the real private key.

This MSC therefore operates exclusively at the **Federation API / server
behavior layer**. It standardizes how servers cache, detect, and react to key
anomalies, but explicitly does not touch room version consensus rules.

## Potential issues

- **Misconfigured servers will experience localized isolation.** An
  administrator who wipes their database and regenerates keys under the same Key
  ID will find their server unable to federate with peers that cached the
  original key. This is intentional — the protocol prioritizes cryptographic
  correctness over convenience. The fix is straightforward: change the key ID in
  the server configuration and remediate any membership or state divergences.

- **No automated key ID collision recovery.** Unlike some protocols that provide
  key-reset ceremonies or trusted-third-party recovery, Matrix intentionally
  provides no automated mechanism. Automated recovery introduces trust
  assumptions that conflict with Matrix's zero-trust federation model.

- **Permanent key-body storage.** The permanent binding requirement means
  servers must retain key-body records indefinitely, proportional to the number
  of remote servers encountered. For a typical homeserver federating with a few
  thousand servers, this is negligible (a few megabytes of public key material).

- **Two-tier binding and the TOFU window.** Allowing a direct fetch to override
  a provisional notary binding means an attacker who can serve a direct
  `/_matrix/key/v2/server` response (IP hijack, DNS spoofing) can displace a
  notary-learned key. While this extends the window of vulnerability beyond the
  initial TOFU race, requiring servers to attempt a prompt direct fetch upon
  learning a notary binding bounds this window (see Binding promotion). The
  override primarily removes the ability of a compromised _notary_ to
  permanently ossify a poisoned binding. See also the **Direct-override
  spoofing** entry under Security considerations.

- **Localized DAG divergence is unavoidable.** The First Seen Wins rule means
  that peers with different cache histories may disagree on events from a
  misconfigured server. This is an inherent property of out-of-band key
  resolution and cannot be solved at the protocol level. This MSC makes the
  behavior deterministic rather than implementation-dependent, which is an
  improvement over the status quo. A full solution is deferred to
  content-addressed key IDs or Member Keys; see
  [Future considerations](#future-considerations).

## Alternatives

- **Trial verification (try all cached keys for a key ID).** Explicitly
  rejected. Trial verification introduces a CPU-exhaustion DoS vector (an
  attacker can spam garbage-signed events, forcing `N` expensive signature
  verifications per event), breaks historical DAG verification (which key was
  active when?), and violates the cryptographic identity contract of the key ID.

- **Room-version-gated strict rejection.** Rejected. Key collision detection is
  out-of-band local state, not derivable from event JSON. A collision-based auth
  rule would guarantee split-brain (see
  [Why this MSC does not propose room version changes](#why-this-msc-does-not-propose-room-version-changes)).
  Worse, it would weaponize TOFU: an attacker who briefly hijacks a server's IP
  could inject a collision that permanently blacklists the victim's key ID from
  Room Version N rooms.

- **Soft failure on key ID collision (warn but accept the new key).** This
  silently breaks historical verification — events signed under the old key body
  would fail verification using the new key, corrupting state resolution for
  rooms involving the affected server. Rejected.

- **Key ID collision resolution via notary consensus.** Peers could query
  multiple notary servers and accept the key body attested by a majority. This
  introduces a trusted-third-party assumption that Matrix's federation model
  explicitly avoids, and notary servers may themselves have stale caches.
  Rejected.

- **Automatic key ID bumping by the server.** Homeserver implementations could
  auto-increment the key ID on every key generation, preventing collisions
  entirely. This is a reasonable implementation best practice and is RECOMMENDED
  by this MSC (see Admin startup guardrails), but cannot be mandated at the
  protocol level because key ID assignment is a server-local configuration
  decision.

## Security considerations

- **CPU-exhaustion DoS prevention.** The strict 1:1 key ID → key body mapping
  eliminates the trial verification attack vector. Signature verification is
  performed against exactly one key per key ID, bounding the computational cost
  per event to `O(number of signing servers)` rather than
  `O(number of signing servers × cached keys per ID)`.

- **TOFU cache poisoning.** Under Matrix's Trust-On-First-Use model, a
  `/_matrix/key/v2/server` response is self-signed by the private key associated
  with the payload. An attacker who briefly hijacks a server's IP (DNS spoofing,
  BGP hijacking) can generate a new keypair, label it with the target's key ID,
  and produce a mathematically valid self-signature. The First Seen Wins policy
  protects against this: if the legitimate key was cached first, the attacker's
  key is rejected as a collision. If the attacker's key is cached first (the
  server was never contacted before), TOFU provides no protection regardless of
  this MSC — this is an inherent limitation of TOFU, not a flaw in this
  proposal.

- **Direct-override spoofing.** While allowing direct fetches to override
  provisional notary-learned keys prevents notary-enforced lock-in, it
  temporarily exposes the server to DNS/BGP spoofing on direct connections. This
  is an acceptable TOFU trade-off because (1) direct connections use WebPKI TLS
  certificate validation (bringing in standard internet-grade security), (2) the
  window of vulnerability is bounded to the brief provisional period before the
  server performs a confirming direct fetch, and (3) future MSCs such as a
  Global Settings Lock would effectively mitigate this concern.

- **DAG integrity.** The key ID uniqueness invariant guarantees that historical
  signature verification is deterministic. For any event at any point in time,
  the key that signed it is unambiguously identified by the
  `(server_name, algorithm, key_id)` tuple in the `signatures` dictionary.

- **Compromise detection.** Key ID collisions are a potential indicator of
  server compromise (an attacker generating a new key and attempting to publish
  it under an existing ID). Hard rejection with operator alerting provides an
  early warning mechanism.

- **Stolen retired keys and backdated forgeries.** The strict enforcement of
  `expired_ts` prevents an attacker holding a compromised retired key from
  signing fresh, current events. However, it cannot stop them from backdating
  the `origin_server_ts` to a time before the `expired_ts` to forge plausible
  historical events. While limiting stolen keys to backdated forgeries is still
  a major reduction in power (and backdated events have limited utility due to
  `prev_events` and depth constraints), `expired_ts` does not provide total
  forward secrecy for the room history.

- **Domain expiration and re-registration (Provisional overriding).** Under the
  two-tier binding rules, a notary-learned key is provisional and can be
  overridden by a direct fetch from the origin server over TLS. If a server goes
  offline, its domain expires, and years later a different entity re-registers
  the domain, that new owner can establish a new Matrix server and publish a
  different key under the same key ID. For any peer that only cached the old key
  _provisionally_ (and never promoted it via direct contact during the original
  server's lifetime), a direct fetch would normally risk overriding the notary's
  cached key body.

    To completely mitigate this threat for historical messages, the
    **Provisional override freeze** rule prevents a direct fetch from overriding
    a provisional notary-learned binding if the provisional key is already
    expired or retired (i.e., its cached `valid_until_ts` has passed or it was
    originally retrieved from `old_verify_keys` with an `expired_ts`). This
    permanently freezes the historical key state, preventing the new domain
    owner from reviving or reclaiming the retired/expired key ID with a new key
    body. However, if the old domain owner went offline abruptly _before_ their
    key's active validity window expired, a brief window of vulnerability exists
    until the provisional key's cached `valid_until_ts` passes (typically up to
    7 days). To limit this residual window, peers SHOULD aggressively attempt to
    promote provisional notary bindings to permanent status by conducting direct
    fetches while the original server is active, and notary servers MUST
    permanently enforce First Seen Wins internally to preserve historical key
    materials in the wider ecosystem.

- **Cache expiration ≠ binding expiration.** The `valid_until_ts` field governs
  when to _refresh_ the key endpoint, not when to _forget_ the key body. Servers
  that purge key-body bindings on `valid_until_ts` expiry create a window where
  collision detection is blind. This MSC explicitly requires permanent retention
  of key-body bindings to close this gap.

- **Storage exhaustion DoS.** Mandating permanent storage of key-body bindings
  introduces a theoretical storage exhaustion vector if an attacker forces a
  server to fetch and permanently store millions of unique key IDs. Homeserver
  implementations SHOULD mitigate this by enforcing a reasonable maximum limit
  on the number of cached key IDs per remote server name (e.g., 3,000 keys). If
  a remote server reaches this quota, receiving servers MUST NOT ignore new Key
  IDs permanently. Instead, they MUST evict the oldest or least-recently-used
  expired keys (keys in `old_verify_keys` with the oldest `expired_ts`). This
  inevitably reopens collision blindness for the evicted key IDs, representing
  an unavoidable trade-off between bounded storage and perfect permanent
  pinning. Keys currently published in the `verify_keys` section of a direct
  fetch MUST always be prioritized and exempt from eviction.

    To resolve the contradiction that arises if a remote server's `verify_keys`
    dictionary alone exceeds the storage quota (as active keys are exempt from
    eviction, making eviction unsatisfiable), a strict ceiling on the number of
    active keys MUST be enforced. If a single key response payload contains more
    than 50 keys in its `verify_keys` dictionary, receiving servers MUST treat
    the entire response payload as malformed/hostile and reject it. This
    prevents hostile or broken servers from hollowing out the storage limit with
    un-evictable active keys.

    Notary servers SHOULD proactively move older keys from an upstream origin's
    `verify_keys` into the notary's `old_verify_keys` response if the total
    number of active keys for that origin would otherwise exceed the 50-key
    ceiling. This ensures that the notary's response remains acceptable to
    downstream servers while still facilitating historical verification.

    Implementations MUST rely on existing federation rate-limiting to discard
    junk traffic before allocating database records. In practice, legitimate
    servers publish single-digit numbers of active keys at any given time; a
    server claiming thousands of key IDs is unambiguously hostile. A future
    Proof-of-Work gated proposal may mitigate the spurious bulk generation of
    keys behind Equihash or Cuckoo Cycle.

## Unstable prefix

This MSC does not introduce new protocol identifiers, API endpoints, or error
codes, and does not require an unstable prefix. Existing authentication and
rate-limiting requirements for key fetching remain strictly intact.

However, because strict collision rejection (First Seen Wins) may break
federation with misconfigured servers in the wild, implementations SHOULD
provide an initial **collision-observation phase**. During this phase, servers
SHOULD log detected collisions (same key ID, different material) as warnings
without rejecting the new key, allowing operators to gather real-world data on
ecosystem breakage before switching to strict enforcement. Implementations are
encouraged to gate strict enforcement behind a configuration flag (e.g.,
`org.matrix.msc4499_strict_caching`) during the rollout period.

## Dependencies

- None. This MSC is independent of other proposals. It applies to `ed25519` keys
  today and will apply equally to future post-quantum algorithms if accepted
  into the spec.

## Open questions

- **Role of community ban lists / Draunir / ACLs:**
    - With First Seen Wins locally isolating a misconfigured server, how do
      external moderation tools interact with this? If a server cannot federate
      due to a key mismatch, do tools like Draunir see this as a temporary
      outage or something that triggers administrative alerts? Furthermore,
      could room ACLs be used maliciously to force a cache eviction or bypass
      the First Seen Wins rule?
    - Should community ban lists play a role in banning servers where reasonable
      grounds for suspecting bulk or spam generation of keys is known?

## Backwards compatibility

This proposal is fully backwards-compatible:

- **No protocol wire changes.** No new fields, endpoints, error codes, or
  response formats are introduced.
- **No room version changes.** No PDU authorization or state resolution rules
  are modified.
- **Existing well-configured servers are unaffected.** Servers that already use
  unique key IDs on rotation (the expected behavior) experience no change.
- **Misconfigured servers experience a clarified failure mode.** Servers that
  reuse key IDs with different key bodies will be rejected by peers implementing
  this MSC. This failure already occurs unpredictably today (depending on cache
  state); this MSC makes the behavior deterministic and well-documented.
- **Incremental adoption.** Implementations with established, convergent key
  verification behavior SHOULD adopt the observation phase (see Unstable prefix)
  before enabling enforcement. Changes to signature verification paths can
  affect state resolution agreement between peers; operators SHOULD monitor for
  unexpected federation failures before enabling strict enforcement.

## Future considerations

### Content-addressed key IDs (stricter protocol requirements)

The root cause of key ID collisions is that the `key_id` is currently an
arbitrary, administrator-defined string (e.g., `ed25519:auto`). A future room
version could eliminate this entire class of vulnerabilities by mandating that
the `key_id` must be deterministically derived from the public key body
itself—for example, `ed25519:<base64(SHA256(KeyBody))[:16]>`.

Under this paradigm, a key ID collision becomes exceedingly difficult. If an
administrator regenerates their keys, the new key body structurally enforces a
novel key ID. This entirely mitigates the TOFU poisoning vulnerability (an
attacker cannot assert a new key under an old ID without conducting a
computationally intractable search) and eliminates the need for out-of-band
collision detection heuristics, allowing us to enforce strict key uniqueness
directly within room version auth rules.

Because this fundamentally requires changing how signatures are validated within
the room DAG and invalidates legacy key formats in the wild, it requires a new
room version and is deferred to a future MSC. Until then, protection must remain
strictly at the local server caching layer as outlined in this proposal.

### Member Keys [MSC4430]

The Member Keys proposal would move key bodies in-band under a future room
version, sidestepping the complications inherent in today's out-of-band notary
model. If adopted, the caching rules in this MSC remain relevant for existing
room versions and for server-level signing keys.
