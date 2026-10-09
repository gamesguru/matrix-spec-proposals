# MSC00A1: Deprecate Non-Standard Signing Algorithms for Federation

Matrix federation currently relies on `ed25519` for PDU signing, server key
publication, and federation transport authentication. The protocol specification
defines `ed25519` as the only recognized signing algorithm, yet the
`algorithm:key_id` format in `verify_keys`, `signatures`, and related structures
is syntactically open — any string is accepted as an algorithm identifier. In
practice, this has allowed homeserver implementations to ship experimental,
vendor-specific, or non-standard cryptographic primitives without formal
specification, creating interoperability failures, a bloated attack surface, and
a significant barrier to alternative homeserver implementations achieving full
federation compatibility.

This MSC formally deprecates all signing algorithms that are not explicitly
defined by the Matrix specification, restricting federation to a closed set of
audited, standardized primitives. Changes are split into two phases: immediate
server-behavior improvements that do not affect consensus, and
room-version-gated validation rules that enforce strict rejection.

**Scope.** This MSC applies exclusively to **server-to-server federation
signatures** — PDU `signatures` dictionaries, `/_matrix/key/v2/server` key
responses, and federation HTTP request authentication. It does not apply to
client-server device keys, user cross-signing keys, or End-to-End Encryption
(E2EE).

## Proposal

### Event-Handling Taxonomy

For clarity, this MSC uses the following processing categories:

- **Accepted event.** Valid format, valid required signatures, and passes the
  room-version auth rules. It can affect the room DAG, room state, and client
  history.
- **Client-filtered event.** Independent of state resolution or auth checks — a
  delivery-time filter that can apply to any already-persisted event (Accepted,
  Soft-failed, or Outlier — see below), withholding it from a specific recipient
  at read time via local, per-recipient policy — `history_visibility`,
  redaction, ignored-user lists, relation/edit aggregation. Does not change the
  event's stored status, DAG placement, or auth outcome.
- **Server-configured omission.** Independent of state resolution or auth checks
  — a unilateral, operator-controlled policy (server ACLs, peer blocklists,
  room/server denylists, or manual overrides) governing whether a given
  deployment chooses to fetch, accept, or retain events from a particular peer
  or room. It is not part of the room-version auth algorithm and MUST NOT be
  conflated with auth-rejection. Servers with different ACL/blocklist
  configurations or manual overrides do not disagree about what happened in the
  room — they disagree only about what they locally store or show.
- **Outlier event.** Valid format and valid required signatures, but the
  receiving server does not yet have enough surrounding room data to fully auth
  or place it as a normal timeline event. It may be persisted as an outlier
  candidate and later de-outliered if the missing context arrives.
- **Soft-failed event.** Valid format, valid required signatures, and valid
  auth-at-event, but it fails checks against the receiver's current forward
  extremity view. It may be persisted and participate in state resolution or
  federation bookkeeping, but is normally hidden from clients and not used as a
  forward extremity by the receiving server.
- **Auth-rejected event.** Valid format and valid required signatures, but it
  fails the room-version authorization rules against its auth events or the
  resolved state at the event. It may be persisted as a DAG artifact, but is not
  relayed to clients and is not used as a new `prev_event` by the rejecting
  server.
- **Hard-invalid candidate PDU.** Parseable enough to identify as a candidate
  event, but it fails required non-auth validation such as a missing or
  mathematically invalid required signature, malformed required signing key
  material, unsupported required signing algorithm, malformed hashes, or other
  room-version-independent event validity checks. It MUST NOT be treated as a
  Matrix room event. Implementations may retain only its event ID, reference
  hash, or raw body for retry suppression, diagnostics, or abuse handling.
- **Unpersistably invalid input.** Input that is not safely representable even
  as a candidate PDU: invalid JSON, no usable event ID or reference hash,
  missing room ID where one is required, structurally nonsensical data, or
  resource-exhaustion payloads. It should be dropped at the boundary, optionally
  logged or rate-limited, but not stored as an event or event-like object.

This MSC only changes how unsupported signing algorithms are classified within
that taxonomy. It does not redefine soft-fail, outlier, or auth-rejection.

### Recognized Signing Algorithms

The following signing algorithms are recognized for Matrix federation:

<!-- markdownlint-disable MD013 -->

| Algorithm                                | Status      | Specification                                        |
| ---------------------------------------- | ----------- | ---------------------------------------------------- |
| `ed25519`                                | **Active**  | Matrix spec                                          |
| `fndsa512` (`tk.nutra.msc45xx.fndsa512`) | **Pending** | [MSC 00E4](00E4-quantum-sigs-minting-server-keys.md) |

<!-- markdownlint-enable MD013 -->

For transition-period membership checks, implementations MUST treat
`tk.nutra.msc45xx.fndsa512` as equivalent to `fndsa512`, or normalize that
unstable identifier to `fndsa512` before checking the allowlist.

All other algorithm identifiers — including but not limited to custom elliptic
curves, RSA-based schemes, vendor-specific key types, and any algorithm not
explicitly defined by an accepted MSC or the Matrix specification — are
deprecated.

### Phase 1: Immediate Server Behavior (No Room Version Required)

The following changes affect only local server behavior and do not alter the
deterministic validation outcome of any incoming event within the room DAG. They
can be deployed immediately without risking consensus divergence.

#### Generation and Publication

Homeserver implementations MUST NOT:

- **Publish** signing keys using non-standard algorithm identifiers in
  `/_matrix/key/v2/server` responses.
- **Sign** PDUs, federation HTTP requests, or key server responses using
  non-standard algorithms.
- **Provide configuration options** to enable bespoke or experimental
  cryptographic primitives for federation signing, except through the unstable
  prefix mechanism defined by the MSC process.

#### Reception and Tolerance

When receiving remote key material (from either `/_matrix/key/v2/server` or
`/_matrix/key/v2/query`) or PDU signatures:

Homeserver implementations MUST:

- **Ignore** unrecognized algorithm entries in the `signatures` dictionary of
  PDUs and in remote key responses (from either `/_matrix/key/v2/server` or
  `/_matrix/key/v2/query`). The presence of an unrecognized algorithm entry MUST
  NOT cause event rejection or key response rejection, provided at least one
  recognized algorithm entry is present and valid. If a recognized algorithm
  signature (e.g., `ed25519`) is present but mathematically invalid, the event
  is a **hard-invalid candidate PDU** and MUST NOT proceed to room-version auth
  evaluation. The server MUST NOT fall back to attempting verification against
  an unrecognized algorithm entry.
- **Accept but quarantine legacy keys.** If a key response (from either the
  remote server's `/_matrix/key/v2/server` endpoint or a `/_matrix/key/v2/query`
  notary) contains **only** unrecognized algorithm keys (and no valid `ed25519`
  or `fndsa512` entry), servers MUST NOT reject the HTTP response outright.
  Because key fetching often occurs in background tasks without room-version
  context (e.g., proactive cache refresh before `valid_until_ts` expiry),
  rejecting the response at the HTTP layer would break historical verification.
  Instead, the server SHOULD cache the key material and log a warning. The
  absence of a recognized algorithm simply means this server cannot participate
  in modern (Room Version N) federation, but its key response remains
  technically valid for legacy verification.

When evaluating a PDU belonging to a **pre-N room version** with **only**
unrecognized algorithm signatures and **no** recognized algorithm entry (e.g.,
no `ed25519` or `fndsa512` signature), the server MUST fall back to existing
legacy signature verification behavior. Standard servers that lack the
cryptographic libraries to verify the unrecognized algorithm will naturally fail
verification; for standard servers without support for that algorithm, this
naturally results in the event being treated as a **hard-invalid candidate PDU**
— but this is the existing behavior, not a new room-version-independent protocol
rule introduced by this MSC. Phase 2 formalizes the recognized-algorithm
requirement for Room Version N and above.

Homeserver implementations SHOULD:

- **Log a warning** when unrecognized algorithm entries are encountered in
  remote key responses or PDU signatures, including the remote server name and
  the unrecognized algorithm identifier, to aid operator diagnostics.

**Rationale.** These Phase 1 rules are consensus-safe because they do not create
new auth failures, soft-fail paths, or outlier behavior. An event that was valid
under prior rules remains valid if it still has a valid recognized signature. An
event that lacked a verifiable required signature remains hard-invalid. The only
behavioral changes are: (1) this server stops _generating_ non-standard
material, (2) this server ignores extra non-standard signatures when a required
recognized signature is present, and (3) this server logs warnings when it
encounters non-standard material from others — none of which changes the room
DAG.

### Phase 2: Strict Enforcement (Room Version Bump Required)

The following rules MUST NOT be enforced in existing room versions. They are
gated to a future room version (hereafter "Room Version N") to prevent
split-brain consensus divergence.

In Room Version N:

- Events whose `signatures` dictionary contains **only** unrecognized algorithm
  entries and no valid `ed25519` or `fndsa512` signature from the expected
  origin server are **hard-invalid candidate PDUs** and MUST NOT proceed to
  room-version auth evaluation.
- The set of recognized algorithms for Room Version N is explicitly: `ed25519`
  and `fndsa512` (if MSC 00E4 is accepted by the time Room Version N is
  specified).
- Servers MUST NOT fall back to non-standard algorithms when verification with a
  recognized algorithm fails.

In particular, unsupported or malformed required signature algorithms are not a
soft-fail condition and not an auth-rejection condition. They fail earlier, at
the required-signature validation stage, before the PDU can be treated as a
Matrix room event for DAG or auth purposes. Implementations MAY retain failed
candidate metadata for retry suppression, diagnostics, or abuse handling.

**Historical code caveat.** Because Matrix rooms are immutable DAGs, homeserver
implementations cannot delete support for legacy algorithms entirely. Events in
historical room versions (Room Version 1 through the version preceding N) must
remain verifiable using whatever algorithm was valid at the time they were
created. "Deprecation" in this context means strictly quarantining legacy
verification code so it is _only_ invoked when processing historical room
versions, while actively rejecting non-standard algorithms for Room Version N
and above.

### Unstable Prefix Extension Point

This MSC does not preclude future algorithm additions. New signing algorithms
MUST be introduced via the standard MSC process with an unstable prefix (e.g.,
`mscXXXX_new_algorithm`). Implementations that encounter an unstable-prefixed
algorithm identifier SHOULD treat it as an unrecognized algorithm (ignore for
verification, do not reject events) unless they explicitly implement the
corresponding MSC.

## Potential Issues

- **Private deployments with custom algorithms.** Organizations running private
  Matrix federations that have implemented bespoke signing algorithms will need
  to migrate to `ed25519`. This is considered acceptable: non-standard
  algorithms already prevent correct federation with mainstream homeservers, and
  private deployments can coordinate migration timelines internally.

- **Existing non-standard keys in the wild.** Some servers may currently publish
  non-standard algorithm entries in their `/_matrix/key/v2/server` responses.
  Phase 1 requires ignoring (not rejecting) unrecognized entries when a
  recognized entry is also present, preserving backwards compatibility. Phase 2
  enforcement is gated to a future room version, giving operators time to
  migrate.

- **Legacy code cannot be removed.** Even after Phase 2, homeservers must retain
  the ability to verify historical events in pre-N room versions using whatever
  algorithms were valid at creation time. This is an inherent constraint of
  Matrix's immutable DAG model, not a deficiency of this MSC.

## Alternatives

- **Enforce globally without a room version bump.** Tightening signature
  _validation_ rules mid-room-version would cause immediate split-brain: updated
  servers would reject events that legacy servers accept, forking the room DAG.
  This was rejected because it violates Matrix's core consensus invariant.

- **Explicit algorithm allowlist per room version.** Instead of a blanket
  deprecation, each room version could define its own set of allowed algorithms.
  This adds unnecessary complexity — the recognized algorithm set should be a
  protocol-wide constant that room versions inherit, not a per-version
  configuration.

- **Soft deprecation (warn only, never reject).** A weaker approach would log
  warnings but never enforce. Phase 1 of this MSC is effectively a soft
  deprecation; Phase 2 provides the hard enforcement that actually eliminates
  the attack surface. Both phases are necessary.

- **Do nothing.** The status quo is tolerable because no mainstream homeserver
  ships non-standard algorithms. However, formalizing the restriction eliminates
  ambiguity, reduces the implementation burden for new homeserver projects, and
  prevents future regression.

## Security Considerations

- **Reduced attack surface.** Cryptographic agility — supporting a broad menu of
  algorithms — is increasingly recognized as a security liability. Each
  additional algorithm is a potential vector for side-channel attacks, downgrade
  attacks, or implementation bugs in rarely-exercised code paths. Restricting to
  a closed set of audited primitives reduces the federation's collective attack
  surface.

- **Downgrade attack prevention.** Without this MSC, a sophisticated attacker
  could theoretically introduce a weak algorithm into a homeserver's key
  response and attempt to trick poorly-implemented peers into verifying against
  the weak key. Phase 1 eliminates this by requiring implementations to ignore
  unrecognized algorithms. Phase 2 further hardens this by rejecting events that
  lack a recognized signature entirely.

- **Interoperability.** Enforcing a common algorithm set ensures that all
  homeserver implementations (Synapse, Dendrite, Conduit, Conduwuit,
  Continuwuity, etc.) can verify each other's signatures without maintaining
  compatibility shims for unknown primitives.

- **Correct failure classification.** Unsupported or malformed required signing
  algorithms must not be misclassified as soft-failed or auth-rejected events.
  Those categories are for events that are already valid Matrix room events with
  valid required signatures. Treating signature-invalid PDUs as if they had
  entered the room DAG would blur implementation boundaries and risk
  inconsistent persistence or relay behavior across homeservers.

- **No monoculture risk.** This MSC restricts the _current_ set but explicitly
  preserves the MSC process as the extension mechanism. If `ed25519` is
  compromised, new algorithms can be introduced through the standard proposal
  process. The restriction is against _unspecified_ algorithms, not against
  _future_ algorithms.

## Unstable Prefix

This MSC does not introduce new identifiers and does not require an unstable
prefix. Phase 1 behavioral changes can be implemented immediately. Phase 2
enforcement is gated to a future room version that will be specified by a
separate Room Version MSC.

## Dependencies

- None. This MSC is independent of
  [MSC 00E4](00E4-quantum-sigs-minting-server-keys.md) (Post-Quantum Digital
  Signatures for Federation), although it is complementary. If MSC 00E4 is
  accepted before Room Version N is finalized, `fndsa512` is included in the
  recognized algorithm set.
- The rest of the quantum-signature series —
  [MSC 00E2](00E2-quantum-sigs-federation-room-pdu.md) (PDU signing),
  [MSC 00E5](00E5-quantum-sigs-federation-session-negotiation.md) (session
  negotiation), and [MSC 00EA](00EA-quantum-sigs-e2ee.md) (E2EE) — build on the
  `fndsa512` key material minted by MSC 00E4 and do not themselves add new
  entries to the Recognized Signing Algorithms table above.
- [MSC 00DA](00DA-bls-signatures-non-interactive-aggregation.md) (BLS signatures
  and non-interactive aggregation) proposes a second new signing primitive for
  federation. If accepted, its BLS scheme would need its own row in the
  Recognized Signing Algorithms table, alongside `fndsa512`.
- [MSC4499](4499-key-caching.md) (strict server signing key caching and key ID
  uniqueness) is complementary rather than overlapping: this MSC restricts
  _which algorithms_ may appear in `verify_keys`/`signatures`, while MSC4499
  governs how a given key ID's key body is cached and bound once published. Both
  apply to the same `algorithm:key_id` material.

## Backwards Compatibility

This proposal is backwards-compatible:

- **Phase 1 is fully backwards-compatible.** All servers currently using
  `ed25519` continue to work identically. Non-standard entries are ignored, not
  rejected. No consensus impact.
- **Phase 2 is scoped to a new room version.** Existing rooms are unaffected.
  Only rooms created in or upgraded to Room Version N enforce strict algorithm
  validation.
- **Historical verification is preserved.** Legacy room versions retain their
  original validation semantics. Servers must keep legacy algorithm support for
  historical event verification, quarantined to pre-N room version code paths.
- **No new endpoints or protocol changes.** This MSC tightens implementation
  behavior and scopes enforcement to a future room version.

---

## MSC Checklist

- [ ] Are
      [appropriate implementation(s)](https://spec.matrix.org/proposals/#implementing-a-proposal)
      specified in the MSC's PR description?
- [x] Are all MSCs that this MSC depends on already accepted?
- [ ] For each endpoint that is introduced or modified:
  - [x] N/A — no endpoints are introduced or modified
- [x] Will the MSC require a new room version, and if so, has that been made
      clear?
  - [x] Phase 2 requires a new room version. Phase 1 does not.
- [x] Are backwards-compatibility concerns appropriately addressed?
- [x] An introduction exists and clearly outlines the problem being solved.
      Ideally, the first paragraph should be understandable by a non-technical
      audience.
- [ ] All outstanding threads are resolved
  - [ ] All feedback is incorporated into the proposal text itself, either as a
        fix or noted as an alternative
- [x] There is a dedicated "Security Considerations" section which detail any
      possible attacks/vulnerabilities this proposal may introduce, even if this
      is "None.". See [RFC3552](https://datatracker.ietf.org/doc/html/rfc3552)
      for things to think about, but in particular pay attention to the
      [OWASP Top Ten](https://owasp.org/www-project-top-ten/).
- [x] The other section headings in the template are optional, but even if they
      are omitted, the relevant details should still be considered somewhere in
      the text of the proposal. Those section headings are:
  - [x] Introduction
  - [x] Proposal text
  - [x] Potential issues
  - [x] Alternatives
  - [x] Unstable prefix
  - [x] Dependencies
- [x] Stable identifiers are used throughout the proposal, except for the
      unstable prefix section
- [ ] Changes have applicable
      [Sign Off](https://github.com/matrix-org/matrix-spec-proposals/blob/main/CONTRIBUTING.md#sign-off)
      from all authors/editors/contributors
