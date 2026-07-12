# MSC45XX PR-readiness review and directions

Review of `00E1-quantum-sigs-federation-key-exchange.md` plus
`current-00E1-staged.patch`, responding to `ADVISORY_REQUEST.md`. Everything
below was checked against the actual committed text, the staged patch, MSC4499,
and — where claims were checkable — recomputed independently. Part I lists
defects that must be fixed before a PR is defensible, each with a concrete fix
design. Part II answers the seven questions. Parts III–VII cover the remaining
review areas. Part VIII is the ordered punch list.

---

## Part I — Blocking defects

### B1. The mandatory base publication PoW is unimplementable as specified

The spec now says every FN-DSA key publication — first publication and every
rotation — MUST be accompanied by a valid proof, that receivers and notaries
MUST reject publications lacking one, _and_ that "the challenge MUST be rejected
if … the `challenge` value was not issued by the verifier." Those three
statements cannot coexist with Matrix's pull-based key distribution:

1. The verifier of a key publication is _every server that ever fetches the
   key_. A verifier-issued fresh challenge implies the origin solves a
   10–15-second puzzle per verifier, on demand, forever — the exact
   CPU-amplification failure the design correctly forbids for notaries, now
   pointed at origins.
2. The proof has no specified carriage. Nothing in the document says where the
   base publication proof travels. The key package must stay canonical and
   notary-immutable, the notary-challenge text only covers the notary flow, and
   `grep` confirms no other transport is defined. As committed, a receiver is
   required to reject a key for lacking a proof it has no defined way to
   receive.

**Fix design: redefine the base publication PoW as a non-interactive stamp.**
The publication gate's semantics are "minting this key cost work," which is a
Hashcash-style stamp, not a freshness challenge. Freshness is what the
_notary-scoped_ challenge exists for. Concretely:

- The stamp travels inside the FN-DSA key object itself:

    ```json
    "verify_keys": {
        "fn-dsa-512:<short_id>": {
            "key": "<unpadded-base64-fn-dsa-512-pubkey>",
            "pow": {
                "algorithm": "tk.nutra.msc45xx.pow.cuckoo-cycle-42-29-sha256",
                "nonce": 8137226,
                "solution": [123, 456, "..."]
            }
        }
    }
    ```

    This placement solves carriage completely: the stamp is inside the signed
    object, so it is covered by both self-signatures (tamper-evident), included
    in `server_key_package_sha256` deterministically (it never changes for a
    given key, so package stability is unaffected), and preserved verbatim by
    notary redistribution with no new rules.

- The verifier _reconstructs_ the seed input rather than receiving a challenge:

    ```text
    stamp_object = {
        "algorithm": "tk.nutra.msc45xx.pow.cuckoo-cycle-42-29-sha256",
        "resource": {
            "action": "fn-dsa-key-publication",
            "server_name": <server_name of the enclosing response>,
            "key_id_sha256": <recomputed from the advertised key body>
        }
    }
    graph_seed(nonce) = SHA-256(canonical_json(stamp_object) || uint64_le(nonce))
    ```

    No `challenge`, no `expires_ts`, no issuer. The "issued by the verifier" and
    expiry rules move to the notary-scoped challenge section, which is the only
    interactive flow. Drop `key_metadata_sha256` from the stamp binding:
    metadata can legitimately change across refreshes without re-minting the
    key, and it is already covered by both self-signatures.

- Cost profile becomes sane: ~10–15 s once per key at generation time, zero per
  rotation of _other_ material, zero per `valid_until_ts` refresh, zero per
  verifier. Verification stays at microseconds. Update the "Mandatory
  proof-of-work latency" Potential Issue and the rotation-scripting guidance
  accordingly (the cost sits at keygen, not in the refresh loop).

- Receivers verify the stamp on first observation of a key body and cache the
  result by `key_id_sha256`.

One honest paragraph must accompany the unconditional MUST wherever it stays: a
receiver-side hard-reject means a Cuckoo-verifier bug in any homeserver bricks
FN-DSA acceptance on that implementation. The mitigations are exactly the ones
you've started building — normative vectors (see B2/B3), the reduced-work test
profile, and the upgrade-class taxonomy for shipping verifier fixes — so wire
those three things together explicitly in the text.

### B2. The normative PoW challenge-object vector is non-canonical — and the generator is at fault

Independently recomputed: the published `canonical_json_utf8` orders the
`resource` keys `action`, `server_name`, `key_id_sha256`. Matrix Canonical JSON
requires lexicographic ordering: `action`, `key_id_sha256`, `server_name`.
Worse, the published `graph_seed_hex` (`284e0a66…`) reproduces exactly from the
**unsorted** serialization (SHA-256 over the published string plus
`uint64_le(8137226)`), while the correctly canonicalized object yields
`8750291f…`. Conclusion: this is not a transcription slip — the vector generator
in `tools/msc45xx-vectors/`, and therefore `gomatrixlib`'s canonicalization, is
not sorting object keys. Any implementation that canonicalizes correctly will
fail this "normative" vector, and any implementation built to pass it inherits
the bug.

Directions: fix the canonicalizer; add property tests against a second,
independent canonical-JSON implementation (e.g. matrix-org's `canonicaljson`
Python package) covering key sorting at every nesting depth, integer range
enforcement, and UTF-8 handling; regenerate **every** vector in the Test vectors
section from the fixed generator; and add a CI job that recomputes each
published vector from the prose-specified algorithm with a non-Go
implementation, so the vectors are cross-checked by construction rather than
emitted by the same code they're meant to validate.

### B3. The reduced-work Cuckoo vector contradicts the profile's own bounds

The reduced profile declares `edge_bits = 8`, but the published proof contains
edge indices 289 and 3503, both ≥ 2^8 = 256. The production profile defines "The
bipartite graph has `2^29` edges … Edge `i` (for `0 ≤ i < 2^29`)" and "Each edge
index MUST be less than `2^29`" — under the same convention, the reduced
profile's edge indices must be < 256. Either the generator uses a different
(undocumented) convention where node space is `2^edge_bits` but edge indices
range wider, or it shares the B2 bug family. Directions: define the reduced test
profile explicitly and completely (edge index domain, node modulus,
`proof_size`, and its own algorithm identifier such as
`…pow.cuckoo-cycle-4-8-sha256.testonly`), regenerate the vector, and assert in
text that test profiles MUST NOT be accepted on the wire — the current "not the
production profile" note is right but the profile itself is unspecified, which
defeats the vector's purpose.

### B4. The `short_id` trial-verification rule contradicts MSC4499, which this MSC claims to incorporate

The intro says this MSC "incorporates cleanup from MSC4499." MSC4499's key-ID
uniqueness invariant states: a key ID MUST map to exactly one key body per
server, receiving servers "MUST NOT perform trial verification," and
first-seen-wins with intra-payload rejection governs collisions. 00E1's
collision rule mandates the opposite: retain up to four colliding bodies per
`short_id` and trial-verify against each with an exactly-one-verifies rule.
These cannot both hold, and 4499's position is the right one — its security
rationale (trial verification creates signature-ambiguity machinery in every
verifier to accommodate a pathological publisher) applies with full force here.

The hash-derived ID makes the 4499-conformant fix nearly free, because
collisions are exclusively self-inflicted and preventable at the source:

- **Origin-side prevention (new normative rule):** at key generation, if the new
  key's `short_id` collides with any FN-DSA key the server has ever published
  (`verify_keys`, `old_verify_keys`, or historical), the server MUST discard the
  candidate and generate a fresh keypair. Expected cost: nothing — a
  within-server collision needs ~2^48 generated keys to occur by chance, and
  deliberate collision-grinding is now a wasted 2^48 effort because the second
  key is unpublishable.
- **Receiver-side handling (adopt 4499 verbatim):** two distinct bodies under
  one `short_id` within a single response → reject the whole response as
  malformed (4499 intra-payload rule). A fetch that rebinds a previously
  observed `short_id` to a different body → retain the original binding, log
  loudly, never trial-verify (4499 collision detection). Recovery from a
  genuinely wedged binding is 4499's operator-gated manual eviction, which you
  can cite rather than respecify.
- **Delete:** the multi-candidate retention rule, the exactly-one-verifies rule,
  the candidate cap of 4, and the corresponding half of the "Hash-derived short
  ID collisions" security bullet (keep the 2^48/2^96 cost analysis; replace the
  "bounded ambiguity" conclusion with "unpublishable by rule, self-inflicted
  protocol violation per MSC4499 semantics if attempted").

The transfer-detection fingerprint (cross-_server_ reuse) is unaffected and
stays as-is — its current formulation (refuse attestation, alert, never evict
the original) is correct.

### B5. The real-time impersonation claim is false during the advisory period

Security Considerations now says an attacker holding only a server's Ed25519 key
"cannot forge live federation traffic." Under this MSC's own enforcement rules,
they can: `X-Matrix-PQC` is advisory, receivers MUST NOT reject a request whose
Ed25519 `Authorization` is valid, so the attacker simply omits the PQC header
and their forged request is accepted with a logged warning. The claim only
becomes true where PQC transport is _enforced_ — MSC 45YY room-scoped traffic,
or an operator strict mode. Compounding this, the strict mode is referenced once
("Until MSC 45YY (or an operator strict mode) makes verification mandatory") but
is no longer defined anywhere — earlier drafts had a MAY in the enforcement
rules that has since been dropped.

Directions: (1) reinstate the strict-mode MAY in "Verification and enforcement
rules" ("Implementations MAY offer an operator-level strict mode that rejects
requests lacking valid PQC transport authentication from peers with cached
FN-DSA keys"), so the claim has a present-tense path; (2) scope the
impersonation bullet explicitly: passive/recorded-traffic forgery and
key-response substitution are prevented now; live request forgery is prevented
only under enforcement (45YY scope or strict mode). The rest of that bullet —
the "same trust model as classical, upgraded algorithm" framing and the
fetch-path-position analysis — is honest, internally consistent with the
replacement-model decision, and should stay. It is a defensible position
provided the document never again claims continuity it doesn't provide; the
current draft, to its credit, no longer does.

### B6. The notary challenge endpoint fails the MSC checklist it claims to pass

`POST /_matrix/key/v2/fn_dsa_publication_challenge` as staged: squats the stable
`v2` namespace before acceptance (must be
`/_matrix/key/unstable/tk.nutra.msc45xx/publication_challenge` during the draft
period, with the stable target listed in the Unstable Prefix table); specifies
no authentication, no rate limiting, no error responses, no replay or single-use
semantics, and no completion path beyond "for example … a notary-specific
challenge-completion endpoint." Meanwhile the MSC Checklist's endpoint items
remain ticked with answers written for the session endpoint. Part II Q2/Q3/Q5
below give the full endpoint design; the checklist must then be re-answered per
endpoint, not globally.

---

## Part II — The seven questions

### Q1. Package hash: with or without `signatures`?

**Without — the current choice is correct, and there is a decisive reason the
text should state.** Under the existing server-server spec, a notary answering
`/_matrix/key/v2/query` signs the key objects it returns: the redistributed
object carries the origin's signatures _plus the notary's_. A
signature-inclusive package hash therefore can never match between the
direct-fetch path and the notary path — the notary's spec-mandated co-signature
mutates the `signatures` dictionary. The signing-object hash (remove
`signatures` and `unsigned`) is the unique identity that is invariant across
every legitimate distribution path, and it has a second virtue: it is exactly
the byte string both self-signatures sign, so verifying either signature already
authenticates precisely the content the hash names.

Two consequential edits. First, the staged wording "remain byte-for-byte
equivalent after normal JSON parsing and Canonical JSON serialization" is
**false** for the notary path (extra signature entry) and must be corrected to:
"identical after removing `signatures` and `unsigned`, and therefore equal in
`server_key_package_sha256`; the only spec-sanctioned mutation a notary may
perform is adding entries under `signatures`." Second, if full received- bytes
fidelity auditing is wanted, add an _optional_, explicitly path-specific
`observed_response_sha256` (digest of the exact HTTP body bytes as received) to
notary observations — clearly labeled as non-comparable across paths. Do not try
to make one hash serve both purposes.

Also note: `valid_until_ts` sits inside the signing object, so the package hash
identifies a _response snapshot_, not a stable key identity — it rotates on
every periodic re-issue. That is correct for observation binding (the point is
to pin what was observed) but has a race consequence handled in Q5.

### Q2. Endpoint placement

Key namespace, unstable path now. The challenge is key-infrastructure machinery
consumed by notaries that already live under `/_matrix/key/`; putting it under
`/_matrix/federation/` would strand it in the wrong API family. During the draft
period:

```http
POST /_matrix/key/unstable/tk.nutra.msc45xx/publication_challenge
POST /_matrix/key/unstable/tk.nutra.msc45xx/publication_challenge/complete
```

with stable targets `/_matrix/key/v2/publication_challenge[/complete]` recorded
in the Unstable Prefix table (adding endpoints to the `v2` tree post-acceptance
is normal; squatting it pre-acceptance is not). Authentication: the request MUST
carry a valid `Authorization: X-Matrix` header, and the notary MUST reject
(`403 M_FORBIDDEN`) a request whose authenticated origin does not equal the
`server_name` in the body — otherwise third parties can mint challenges (and
burn notary signing capacity) on behalf of servers they don't control.
`X-Matrix-PQC` SHOULD accompany when the origin already has a published FN-DSA
key; for a first publication it cannot exist yet, so it cannot be required. Rate
limiting: MUST, with `429 M_LIMIT_EXCEEDED`. Errors: `400 M_INVALID_PARAM`
(malformed digests), `403 M_FORBIDDEN` (origin mismatch / auth failure),
`404 M_UNRECOGNIZED` (unsupported). Statelessness: the notary already signs the
challenge object, so it need not store issued challenges — verification of its
own signature plus a bounded single-use cache of completed `challenge` values
within `expires_ts` (Q5) is the entire state requirement.

### Q3. Explicit completion endpoint — yes

Piggybacking completion on the key-query workflow conflates roles: queriers are
arbitrary third parties, the completer is the origin, and the completion needs
origin authentication that query flows don't carry. Specify
`…/publication_challenge/complete` taking the exact signed challenge object plus
the proof:

```json
{
    "challenge_object": { "...": "the notary-signed challenge, verbatim" },
    "proof": { "algorithm": "…", "nonce": 8137226, "solution": [123, "..."] }
}
```

The completion request MUST carry `Authorization: X-Matrix` from the origin and
MUST carry `X-Matrix-PQC` signed by the very FN-DSA key whose `key_id_sha256`
the challenge binds (notary checks the header's `short_id` against the bound
digest's prefix and verifies against the fetched key body). This is the piece
that makes the provenance claim real: without keyholder-bound completion, anyone
can request and solve a challenge over `example.com`'s public data, and the
observation's "the origin performed work bound to this notary" becomes "someone
with a CPU did." With it, the completion proves possession-plus-work in one
step. On success the notary returns the signed observation record (or the
`notary_challenge` fragment it will embed); on failure: `400 M_INVALID_PARAM`
(proof invalid), `403 M_FORBIDDEN` (auth/keyholder mismatch), `410` or `400`
with `M_INVALID_PARAM` for expired challenges, `403 M_FORBIDDEN` for reused ones
(single-use), `429 M_LIMIT_EXCEEDED`.

### Q4. Is the base PoW still needed alongside notary challenges?

After the B1 redesign the two mechanisms stop being duplicates and become a
clean division of labor, which the text should state as the rationale: the
**stamp** is non-interactive, minted once per key, verifiable offline by every
receiver forever — it is the only PoW any non-notary verifier can check. The
**notary challenge** is interactive, per-notary, freshness-bearing, and binds a
package snapshot and a timeline — properties a non-interactive stamp cannot
have. Keep both: stamp mandatory (as the team has decided), notary challenge
optional local policy (as drafted). For the record, the pure-security case for
the mandatory stamp remains thin — self-signing already prevents third-party
floods, and rate limiting covers the rest — so the honest framing in Security
Considerations ("uniform minting cost, imposed precisely because the protocol
does not otherwise distinguish publishers") is the right defense and should be
kept word-for-word. If review pressure forces a concession, the fallback that
loses least is: stamp mandatory-to-mint and mandatory-for-notary-attestation,
receiver-side reject-on-invalid but warn-on-absent during the unstable period —
but treat that as the concession position, not the proposal.

### Q5. Are the challenge fields sufficient against replay/rebinding?

Nearly. The resource binds `action`, `server_name`, `key_id_sha256`,
`key_metadata_sha256`, `server_key_package_sha256`, and `issuer`, and the seed
covers the whole challenge object including `algorithm` and `expires_ts` — so
cross-key, cross-server, cross-package, and cross-algorithm rebinding are all
closed. Four additions are required to finish the job:

1. **Issuer self-check:** a notary verifying a completion MUST check
   `resource.issuer` equals its own server name. Without this, a challenge
   issued by notary A can be replayed to notary B (both would find their binding
   fields plausible). The field exists; the check is unstated.
2. **Single-use:** a notary MUST NOT accept the same `challenge` value twice;
   define `challenge_id` (if kept distinct from `challenge`) as base64url, 1–128
   chars, ≥128 bits entropy, and state which one keys the replay cache.
3. **Keyholder-bound completion** (Q3) closes third-party completion, which is a
   rebinding of the _prover_, not the resource.
4. **The `valid_until_ts` race:** because the package hash pins a snapshot and
   origins periodically re-issue responses with fresh `valid_until_ts`, a
   challenge can be invalidated mid-flight by the origin's own refresh timer.
   Specify: challenge `expires_ts` SHOULD be short (≤ 15 minutes is
   RECOMMENDED), and the origin MUST continue serving the exact response whose
   hash it committed to until completion or expiry. State the failure mode (hash
   mismatch at the notary's verification fetch → challenge void, re-request) so
   implementers don't invent divergent recovery.

### Q6. Should observations expose the challenge/proof digests?

Yes — add `challenge_sha256` and `proof_sha256` (digests of the canonical JSON
of the signed challenge object and of the proof response) inside
`notary_challenge`. Timestamps alone are assertions; with the digests, a
third-party auditor holding a retained bundle can bind the asserted timeline to
specific artifacts, which is the difference between "notary says it happened at
T" and "notary is committed to _this challenge_ having happened at T." On the
framing question: keep the current design of hashing the `notary_challenge`
object into the signature input rather than length-framing each timestamp
individually. Canonical JSON of the subobject is already unambiguous, the object
hash automatically covers the two new digest fields, and individual framing
would only pay off if verifiers needed to check timestamps without the object —
no such use case exists. One rule to add: the verifier recomputes
`notary_challenge_sha256` from the object _as present_, so unknown future fields
are covered by the signature automatically — note this explicitly so Patch-class
additions to the object are understood to be signature-affecting (which pushes
them to Minor class in your taxonomy; cross-reference it).

### Q7. Does the text prevent notary metadata from mutating the origin package?

The intent is fully present; two gaps remain. First, the "byte-for-byte"
sentence must be corrected per Q1 — as written it _overstates_ fidelity and a
reviewer will (rightly) test it against the notary co-signature and find it
false. Second, add the one missing conformance rule with a test: "A notary MUST
NOT add, remove, reorder, or rewrite any member of the origin key object other
than adding entries under `signatures`. Conformance test:
`server_key_package_sha256` recomputed from a notary-redistributed object MUST
equal the digest recomputed from a direct origin fetch of the same snapshot."
With those two edits, the answer to Q7 is yes.

---

## Part III — TLS 1.3 compact provenance: precision items

The section's trust boundaries are unusually honest (the TLSNotary/DECO
delimitation is exactly right) and should not be weakened. To make it
academically concrete:

1. **Cite the verification construction precisely.** Verification is RFC 8446
   §4.4.3: the signed content is 64 bytes of 0x20, the exact ASCII context
   string `"TLS 1.3, server CertificateVerify"`, a single 0x00, then the
   transcript hash. Name the section and the context string in the text; "the
   TLS 1.3 CertificateVerify construction for the server context" currently
   forces implementers back to the RFC to guess which of several strings you
   mean.
2. **Pin the two undefined encodings.** `handshake_transcript_hash` and
   `server_certificate_verify_signature` are shown as base64url in the example
   but never defined in Formatting definitions. Add both (unpadded base64url),
   and note the deliberate deviation from the "Matrix signatures use standard
   base64" convention — these are TLS artifacts, not Matrix signatures — so the
   encoding audit (Part IV) closes cleanly.
3. **Constrain the enums.** `transcript_hash_algorithm` MUST equal the hash of
   the negotiated cipher suite; `certificate_verify_signature_scheme` MUST be a
   TLS `SignatureScheme` registry name (the example's `ecdsa_secp256r1_sha256`
   already follows this — make it a rule). `transport` needs its value set
   defined (`"https"` only?) and the TLS fields declared mandatory when
   `transport` is `"https"` — the signature input frames
   `leaf_spki_sha256`/`leaf_cert_sha256` unconditionally, so absent values are
   currently unrepresentable anyway; say so.
4. **Add the staleness caveat.** Nothing binds the TLS evidence to
   `observed_at`: a notary can capture one valid `CertificateVerify` and attach
   it to observations for months (until cert expiry, and even after —
   verification against an expired-but-obtained cert still succeeds
   mathematically). The compact form proves "the TLS key holder signed this
   transcript hash _at some time_," not "during this observation." One sentence
   in Trust and enforcement boundaries; the full-transcript audit bundle is the
   existing escape hatch for anyone needing more.
5. **Scope by TLS version.** The construction is 1.3-only; state that
   `tls_13_provenance` MUST be omitted for non-1.3 fetches rather than
   approximated.

---

## Part IV — Encoding audit (base64 vs base64url)

Current assignments, verified consistent: FN-DSA public keys and signatures,
Ed25519 material, `X-Matrix-PQC`/`X-Matrix-PQC-Session` `sig`/`mac` parameters,
and ML-KEM `encapsulation_key`/`ciphertext` — unpadded **standard base64**
(matching existing Matrix signature conventions). All SHA-256 digests
(`key_id_sha256`, `key_metadata_sha256`, `server_key_package_sha256`,
`leaf_spki_sha256`, `leaf_cert_sha256`, observation digests), `short_id`, PoW
`challenge` values, and `session_id` — unpadded **base64url** (URL/
identifier-safe). This split is coherent; the direction is to state it once as a
normative rule ("signatures and key material: unpadded standard base64; digests
and identifiers: unpadded base64url") rather than per-field, then fix the two
undefined fields from Part III item 2 and define
`notary_challenge.challenge_id`'s alphabet. No other inconsistencies found.

---

## Part V — Performance verdicts

**Origin-side solving:** with B1, ~10–15 s once per key lifetime is
operationally safe, including for automated deployments (it moves to keygen, out
of the refresh loop). Without B1 it is unbounded per-verifier work — which is
the strongest performance argument for B1. **Notary economics:** challenge
issuance costs the notary one Ed25519 + one FN-DSA signature (~5 ms) — cheap but
not free, hence the MUST-rate-limit and origin-match auth in Q2; completion
verification and stamp verification are microseconds and memoryless (84 SipHash
evaluations plus the cycle check), so verification DoS is closed by
construction. **`42-29` sizing:** hundreds of MB and seconds-scale solving on
commodity hardware is proportionate for a once-per-key stamp; the
stochastic-solve-time paragraph and the MUST-NOT reject-on-timing rule are
correct and should stay. **X-Matrix-PQC vs sessions:** ~888 bytes/request
against a 32-byte-keyed HMAC after one KEM round trip is well balanced; the
unidirectionality, constant-time comparison, and FIPS 203 input validation are
all in place. The remaining session-section problem is architectural, not
cryptographic — see Part VI.

---

## Part VI — Document architecture and the minimum viable protocol

At ~1,490 lines this MSC is now four proposals wearing one coat, and the
advisory request's own "too many advisory mechanisms?" worry is the correct
instinct. The layering is individually defensible but jointly heavy, and Matrix
reviewers evaluate blast radius per MSC. Recommended partition:

- **00E1 (core):** algorithm definition, key ID/`short_id`, encoding/signing,
  server signing keys, trust model, publication stamp (post-B1), `X-Matrix-PQC`
  transport, migration. This is the minimum viable protocol, and the text should
  say so in one sentence: _FN-DSA keys + self-signature + hash-derived short
  ID + advisory transport header; everything else is a detachable layer._
- **00E4 (notary provenance suite):** notary observations, TLS 1.3 compact
  provenance, notary-scoped challenges, completion endpoint. These are one
  coherent feature (signed third-party observation of key publication) with one
  consumer (audit/diagnostics), zero acceptance-semantics impact by design — the
  definition of a splittable MSC.
- **00E5 (session negotiation):** the ML-KEM section. The heading already says
  "(future MSC)" while the body is fully normative and the checklist claims its
  endpoint items — an incoherence flagged in earlier review that still stands.
  Splitting resolves it; if you keep it in-file instead, delete "(future MSC)"
  and the "a follow-up MSC will formally define" sentence and own it as an
  optional extension of this MSC.

The confidentiality point the request asks about is already stated correctly
("authentication amortization only … TLS continues to provide transport
encryption"); splitting is about cognitive load, not correctness.

---

## Part VII — Consistency and housekeeping sweep

In rough order of embarrassment-per-effort: the typo "implementation detail an
lead to network divergence" (line 95) survives a third draft — land the
one-character fix and the spellcheck CI with it. "Operator strict mode" is
referenced (line 987) but defined nowhere — B5 reinstates it. MSC4499 is cited
in the introduction but absent from Dependencies, and the checklist still
answers "No MSC dependencies"; after B4 this MSC substantively depends on 4499's
key-ID invariant, so add the dependency, describe the relationship in one
paragraph (what is inherited: uniqueness invariant, first-seen-wins, manual
eviction; what this MSC adds: hash-derived IDs make origin-side prevention
possible), and untick the box until 4499 is accepted. The `fips_206_revision`,
`claims`, and (post-B1) `pow` key-object fields, the `notary_observations` and
`server_key_package_sha256` response fields, the `notary_challenge` object, and
both new endpoints all need unstable-prefix table entries. Decide and document
the context-string question: the `matrix:…:v1` tags are baked into hashes, so
per your own taxonomy changing them at stabilization would be a Major change —
the right resolution is an explicit exemption paragraph ("context strings are
versioned in-band and stable from first publication; they are not wire
identifiers and take no unstable prefix"), which costs three lines and
forestalls a review thread. The `fn-dsa-512` parameter table still carries "PoW
verify: ~1 ms" — the paragraph under it visibly exists to apologize for the
confusion; remove the cell, keep the paragraph's first sentence. Lines 59–62
("MUST further conform to the exact confirmation scheme or verification list
defined in this proposal (the same checklist every other server will use)") is
normative language pointing at nothing identifiable — either point it at a named
conformance section or delete it. `00EA-quantum-sigs-e2ee.md` still references
MSC 00FF and `org.matrix.msc00FF` throughout. The title ("…revised federation
semantics") is vaguer than the previous one; reviewers skim titles —
"Post-quantum server keys, publication proof-of-work, and federation transport
authentication" says what's inside. Finally, re-answer the MSC checklist per
endpoint (three endpoints post-B6/Q3: session key_exchange,
publication_challenge, publication_challenge/complete), and plan to move the
checklist into the PR description at submission.

---

## Part VIII — Ordered punch list

1. **B2/B3 (do first — everything else re-derives from vectors):** fix
   `gomatrixlib` canonicalization, define the reduced test profile, regenerate
   all vectors, add second-implementation vector CI.
2. **B1:** convert the base publication PoW to the in-key non-interactive stamp;
   move `challenge`/`expires_ts`/issuer semantics into the notary-challenge
   section only; update the latency Potential Issue and the PoW security bullet.
3. **B4:** adopt MSC4499 collision semantics (origin-side keygen uniqueness,
   intra-payload rejection, first-seen-wins, cite 4499's manual eviction);
   delete trial verification, the exactly-one rule, and the candidate cap.
4. **B6 + Q2/Q3/Q5:** unstable-prefix both endpoint paths; specify auth
   (origin-match; keyholder-bound completion), rate limits, errors, single-use,
   issuer self-check, the ≤15-minute expiry and origin freeze window; re-answer
   the checklist per endpoint.
5. **B5:** reinstate strict mode; rescope the impersonation claim to enforced
   contexts.
6. **Q1/Q7:** fix the "byte-for-byte" wording; add the notary non-mutation rule
   and its conformance test; optionally add `observed_response_sha256`.
7. **Q6:** add `challenge_sha256`/`proof_sha256` to `notary_challenge`; note the
   object-hash-covers-unknown-fields rule and its taxonomy interaction.
8. **Part III:** RFC 8446 §4.4.3 citation and context string; pin the two TLS
   field encodings; enum constraints; staleness caveat; 1.3-only scoping.
9. **Part VI:** split 00E4/00E5 (or, minimally, resolve the session section's
   "(future MSC)" contradiction in place); add the minimum-viable-protocol
   sentence.
10. **Part VII sweep:** typo, unstable-prefix table entries, MSC4499 dependency,
    context-string exemption paragraph, table cell, dangling conformance
    sentence, 00EA staleness, title, per-endpoint checklist, Part IV encoding
    rule paragraph.
