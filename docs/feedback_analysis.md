# Unpacking the Detailed Feedback on MSC4499 Test Suite

This analysis walks through every claim in
[detailed-feedback-001.md](https://github.com/matrix-org/complement/blob/main/tests/msc4499/detailed-feedback-001.md),
cross-references it against the actual
[MSC4499 text](https://github.com/matrix-org/complement/blob/main/tests/msc4499/4499-key-caching.md)
and the
[test code](https://github.com/matrix-org/complement/blob/main/tests/msc4499/msc4499_key_uniqueness_test.go),
and gives a verdict on each point.

---

## Section 1: "Most of these are genuine gaps only relative to the MSC"

### Claim: First Seen Wins is new MSC behavior, not existing spec

> **Verdict: Correct.** The MSC explicitly states this is a new requirement. The
> current spec has no cache-pinning mandate. Both Synapse and Dendrite "failing"
> FSW is the expected pre-implementation state — the tests are testing _draft
> proposal_ behavior, not current-spec behavior.

The MSC itself acknowledges this in its backwards compatibility section
([4499-key-caching.md:L404-L415](https://github.com/matrix-org/complement/blob/main/tests/msc4499/4499-key-caching.md#L404-L415)):

> _"This proposal is fully backwards-compatible ... No protocol wire changes ...
> Misconfigured servers experience a clarified failure mode."_

The phrase "backwards-compatible" means **wire-compatible** — existing servers
keep federating. It does NOT mean "existing servers already pass these tests."
The feedback is right to flag this distinction.

### Claim: Negative caching is not a current-spec requirement

> **Verdict: Correct.** The MSC introduces this at
> [4499-key-caching.md:L49-L58](https://github.com/matrix-org/complement/blob/main/tests/msc4499/4499-key-caching.md#L49-L58)
> with explicit MUST/SHOULD language. No prior spec mandates exponential backoff
> for key fetch failures.

### Claim: Coalescing is a quality-of-implementation property the MSC wants to make normative

> **Verdict: Correct.** The MSC uses SHOULD language at
> [L55-L57](https://github.com/matrix-org/complement/blob/main/tests/msc4499/4499-key-caching.md#L55-L57):
> _"Implementations SHOULD coalesce concurrent outgoing key fetch
> requests..."_  
> However, note it's a **SHOULD**, not a MUST. The test
> [TestKeyFetchCoalescing](https://github.com/matrix-org/complement/blob/main/tests/msc4499/msc4499_key_uniqueness_test.go#L369-L429)
> asserts `reqCount > 2` as a failure, which is a reasonable threshold for a
> SHOULD, but the feedback is correct that this isn't even a hard MUST in the
> proposal.

### Claim: Dendrite's `TestHistoricalEventVerification` failure is a real bug against today's spec

> **Verdict: Partially correct, but needs nuance.** The MSC does codify
> timestamp-aware key validity at
> [L254-L263](https://github.com/matrix-org/complement/blob/main/tests/msc4499/4499-key-caching.md#L254-L263).
> However, the _existing_ spec already says that `expired_ts` indicates when a
> key stopped being valid. Whether Dendrite's failure here is a current-spec bug
> or an MSC4499-only requirement depends on how you interpret the existing
> spec's language around `old_verify_keys` — but `gomatrixserverlib`'s
> verification API is indeed timestamp-aware, so if Dendrite isn't using it
> correctly, that's arguable as a current bug.
>
> [!IMPORTANT] **Key takeaway from Section 1:** The framing "Synapse fails
> compliance" is misleading. The accurate statement is "Synapse hasn't
> implemented a draft proposal." Only Dendrite's historical-event verification
> failure is arguably a current-spec bug.

---

## Section 2: Test Soundness Problems

This is the most technically substantive section and contains the sharpest
actionable findings.

### 2a: `TestIntraPayloadRejection` — Cache Contamination from Control Case

> **Claim:** The control case (L343-L350) queries with
> `minimum_valid_until_ts: 0`, caching a good key valid for 24 hours. The
> collision phase then uses the _same key ID_ and the _same_
> `minimum_valid_until_ts: 0`. The cached key fully satisfies the second query,
> so a well-behaved homeserver answers from cache without ever contacting the
> mock.

**Let's verify against the code:**

```go
// Line 310: Same key ID for both phases
keyID := gomatrixserverlib.KeyID("ed25519:msc4499_key")

// Line 322: Control case starts with shouldCollide = false
shouldCollide: false,

// Lines 331-339: Control query uses minimum_valid_until_ts: 0
"minimum_valid_until_ts": 0,

// Lines 353-355: Then flips to shouldCollide = true
mockKeyServer.shouldCollide = true

// Lines 357-358: Collision query uses the SAME request body (same bytes object!)
resp, err := fedClient.Post("https://hs1/_matrix/key/v2/query", "application/json", bytes.NewReader(bodyBytes))
```

> **Verdict: This is a valid and serious bug in the test.** The collision query
> reuses `bodyBytes` which has `minimum_valid_until_ts: 0`. Since the control
> case already cached a key valid for 24 hours, the homeserver has zero reason
> to re-fetch. The mock's `shouldCollide = true` state is never consulted. The
>
> test is proving that "Synapse has a cache" — not that "Synapse accepts
> colliding payloads."
>
> [!CAUTION] The Synapse intra-payload rejection result should be considered
> **unproven** until this test is fixed.

**Fix options:**

1. Use a **different key ID** for the collision phase (cleanest)
2. Set `minimum_valid_until_ts` past the cached `valid_until_ts` to force a
   re-fetch
3. Assert on `requestCount` to prove the mock was actually hit

### 2b: `TestIntraPayloadRejection` — Wrong Status Code Assertion

> **Claim:** The test asserts non-200 status (L363-L365), but the notary API
> contract returns `200` with keys _omitted_ from the response when they can't
> be fetched/validated.

**Cross-referencing the MSC:**

The MSC at
[L141-L147](https://github.com/matrix-org/complement/blob/main/tests/msc4499/4499-key-caching.md#L141-L147)
says:

> _"If a receiving server detects a key ID collision within a single HTTP
> response, the entire response MUST be rejected as malformed."_

The MSC says "the entire response MUST be rejected" — but it does **not**
specify how this rejection is communicated via the notary endpoint. The
feedback's point is:

- Under the **current** spec, `POST /_matrix/key/v2/query` returns `200` with
  whatever keys could be validated; unresolvable servers are simply omitted from
  `server_keys`.
- A "rejection" of the upstream payload results in `200 {"server_keys": []}` —
  the key is absent, not a 5xx error.
- The in-repo MSC at
  [proposals/4499-key-caching.md](../proposals/4499-key-caching.md#L315-L320)
  explicitly specifies this notary behavior: "If a notary rejects an upstream
  key response as malformed, it MUST still return HTTP 200 for the enclosing
  `/_matrix/key/v2/query` response, omit that response from the `server_keys`
  array, and MAY continue serving other valid entries in the batch."

> **Verdict: Valid.** The correct assertion should be content-level: `200`
> status, and the colliding key **absent** from `server_keys`. The test's
> non-200 assertion would cause a fully compliant implementation to fail.
>
> [!NOTE]  
> The canonical MSC specification in `proposals/4499-key-caching.md` (lines
> 315–320) defines this observable notary rejection requirement.

### 2c: `TestIntraPayloadRejection` — Mischaracterized Threat Model

> **Claim:** The mock puts the same key ID in `verify_keys` AND
> `old_verify_keys` with _different_ key material. This is syntactically valid
> JSON. It's not a "duplicate JSON key" attack.

**Verifying against the mock code** at
[L73-L91](https://github.com/matrix-org/complement/blob/main/tests/msc4499/msc4499_key_uniqueness_test.go#L73-L91):

```go
rawJSON := fmt.Sprintf(`{
    "server_name": "%s",
    "valid_until_ts": %d,
    "verify_keys": {
        "%s": {
            "key": "%s"
        }
    },
    "old_verify_keys": {
        "%s": {
            "key": "%s",
            "expired_ts": %d
        }
    }
}`, m.serverName, ..., m.keyID,
    base64.RawStdEncoding.EncodeToString(m.pubKey),  // real key in verify_keys
    m.keyID,
    base64.RawStdEncoding.EncodeToString(colPub),    // DIFFERENT key in old_verify_keys
    ...)
```

> **Verdict: Correct.** The same _key ID_ appears in two _different_ JSON
> objects (`verify_keys` and `old_verify_keys`), not as duplicate keys in one
> object. Every JSON parser handles this identically. The feedback correctly
> identifies **three distinct threat cases** the MSC needs to distinguish:

| Case  | Description                                                                             | Tested?                                                            | MSC Coverage                                                                                                                                                                                                                                                                          |
| ----- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **A** | Literal duplicate keys within a single JSON object (raw-bytes parser-divergence attack) | ❌ Not tested (Go maps can't represent it; need hand-crafted JSON) | [L141-L147](https://github.com/matrix-org/complement/blob/main/tests/msc4499/4499-key-caching.md#L141-L147) — partially                                                                                                                                                               |
| **B** | Same key ID in `verify_keys` and `old_verify_keys` with **different** material          | ✅ Currently tested                                                | [L141-L147](https://github.com/matrix-org/complement/blob/main/tests/msc4499/4499-key-caching.md#L141-L147) — covered: _"A single key response payload MUST NOT contain multiple different public key bodies for the same key ID (e.g., across `verify_keys` and `old_verify_keys`)"_ |
| **C** | Same key ID in both maps with **identical** material                                    | ❌ Not tested                                                      | [L144-L145](https://github.com/matrix-org/complement/blob/main/tests/msc4499/4499-key-caching.md#L144-L145) — explicitly legal: _"The same key body appearing under one key ID in both `verify_keys` and `old_verify_keys` is legal."_                                                |

> [!NOTE] The MSC **does** explicitly distinguish cases B and C. The feedback
> says it needs to — but actually reading the MSC text shows it already does at
> L144-L145. Score one for the MSC's drafting. However, case A (literal
> duplicate JSON object keys) is indeed not tested and would require
> hand-crafted JSON.

### 2d: `TestKeyIDFirstSeenWinsDirect` — Over-specified Assertion

> **Claim:** The test asserts the notary returns pinned key A. But a compliant
> FSW implementation could also reject the poisoned response AND decline to
> return key A (since A doesn't satisfy the new `minimum_valid_until_ts`),
> returning `200` with the server omitted.

**Checking the test code** at
[L218-L221](https://github.com/matrix-org/complement/blob/main/tests/msc4499/msc4499_key_uniqueness_test.go#L218-L221):

```go
// Force re-fetch with minimum_valid_until_ts > cached valid_until_ts
minValidUntil := mockKeyServer.validUntil.Add(time.Hour).UnixMilli()
// Asserts key A is returned
queryNotary(t, fedClient, "https://hs1", string(originName), string(keyID), minValidUntil,
    base64.RawStdEncoding.EncodeToString(pubKeyA))
```

And
[queryNotary](https://github.com/matrix-org/complement/blob/main/tests/msc4499/msc4499_key_uniqueness_test.go#L133-L166)
hard-asserts `foundKey == expectedKeyBase64`.

> **Verdict: Valid.** The test is over-specified. The MSC's actual invariant is
> "key B is NEVER returned and NEVER cached." Two compliant behaviors exist:
>
> 1. Return pinned key A (even though it doesn't satisfy
>    `minimum_valid_until_ts` — debatable)
> 2. Return `200 {"server_keys": []}` — key A can't satisfy the request, and key
>    B is rejected
>
> The test only accepts behavior 1. The feedback's suggested fix is sound:
> assert `foundKey != keyB` (empty or A both acceptable), then follow up with a
> `minimum_valid_until_ts: 0` query to prove A is still cached (cache wasn't
> poisoned).
>
> [!TIP] The feedback's additional suggestion — sending an event signed by key B
> and asserting rejection — is excellent. It tests the _actual security
> property_ (event verification) rather than just the proxy (notary endpoint
> behavior).

---

## Section 3: MSC Wording Issues

### 3a: Observable rejection semantics per surface

> **Verdict: Addressed in canonical MSC.** While earlier drafts stated "MUST be
> rejected as malformed" without detailing notary response format, the canonical
> MSC text at
> [proposals/4499-key-caching.md](../proposals/4499-key-caching.md#L315-L320)
> explicitly defines observable notary rejection: return HTTP 200 and omit the
> malformed response from the `server_keys` array.

### 3b: Collision definitions need precision across three cases

> **Verdict: Addressed in canonical MSC.** The MSC distinguishes case B
> (different material, cross-map → MUST reject) from case C (identical material
> → legal). Case A (literal duplicate JSON keys) is explicitly addressed at
> [proposals/4499-key-caching.md](../proposals/4499-key-caching.md#L328-L335):
> _"Furthermore, implementations MUST reject key response payloads containing
> duplicate keys within a single JSON object, at any depth, anywhere in the
> response document... This rejection applies to the raw received bytes before
> any canonicalization."_

### 3c: Negative caching as SHOULD with test-observable bounds

> **Verdict: Reasonable suggestion.** The MSC uses MUST at
> [L49-L51](https://github.com/matrix-org/complement/blob/main/tests/msc4499/4499-key-caching.md#L49-L51)
> for the general requirement and specifies backoff parameters ("starting at 1
> minute, capping at 1 hour"). The suggestion to express test-observable bounds
> is practical for implementors.

### 3d: FSW recovery story

> **Verdict: Already addressed.** The MSC includes a dedicated "Recovery from
> key loss" section at
> [L220-L231](https://github.com/matrix-org/complement/blob/main/tests/msc4499/4499-key-caching.md#L220-L231)
> and manual cache eviction at
> [L239-L251](https://github.com/matrix-org/complement/blob/main/tests/msc4499/4499-key-caching.md#L239-L251).
> The feedback may not have seen the full MSC text. The HPKP-style objection is
> explicitly handled.

### 3e: Experimental flag gating

> **Verdict: Contradicted by MSC.** The MSC's unstable prefix section at
> [L391-L396](https://github.com/matrix-org/complement/blob/main/tests/msc4499/4499-key-caching.md#L391-L396)
> explicitly states: _"This MSC does not introduce new protocol identifiers and
> does not require an unstable prefix."_ The rationale is that these are
> cache-policy changes that don't alter wire format. The feedback's suggestion
> to gate behind `org.matrix.msc4499` is a reasonable deployment strategy but
> goes against the MSC's stated design.

---

## Section 4: Suggested Plan Forward — Assessment

| Step       | Suggestion                                                                | Assessment                                                                                                  |
| ---------- | ------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| **Step 1** | Build requirements traceability matrix (assertion → MUST/SHOULD sentence) | ✅ Excellent idea. Would immediately clarify which tests are testing current spec vs. MSC requirements      |
| **Step 2** | Fix the two unsound tests, re-run                                         | ✅ **Priority 1.** The `TestIntraPayloadRejection` cache contamination bug is the most urgent fix           |
| **Step 3** | File upstream issues with correct framing                                 | ✅ Good framing advice. Dendrite `expired_ts` = current-spec bug. Everything else = MSC implementation work |
| **Step 4** | Three-column reporting (current-spec? / MSC? / behavior)                  | ✅ Would prevent the misleading "Synapse fails compliance" narrative                                        |

---

## Summary: What's Right, What's Wrong, What's Missing

### Feedback Gets Right ✅

1. **FSW/negative-caching/coalescing failures are expected MSC gaps**, not
   "bugs"
2. **`TestIntraPayloadRejection` has a cache contamination bug** that
   invalidates the Synapse result
3. **The non-200 status assertion is wrong** — should be content-level (key
   absent from `server_keys`)
4. **`TestKeyIDFirstSeenWinsDirect` is over-specified** — should accept both
   "return pinned A" and "omit server"
5. **Three-column reporting** would fix the misleading compliance framing
6. **The backwards-compatibility misunderstanding** (wire-compatible ≠ servers
   already pass)

### Feedback Gets Wrong / Partially Wrong ❌

1. **Claims MSC doesn't distinguish collision cases B and C** — it does,
   explicitly at
   [L144-L145](https://github.com/matrix-org/complement/blob/main/tests/msc4499/4499-key-caching.md#L144-L145)
2. **Claims MSC doesn't address recovery** — it has dedicated sections on key
   loss recovery and manual cache eviction
3. **Acknowledges not reading the MSC** — _"I tried to pull the actual MSC4499
   text and couldn't find it"_ — which explains the above misses

### Action Items

> [!IMPORTANT] **Immediate fixes needed in the test suite:**

1. **Fix `TestIntraPayloadRejection` cache contamination:** Use a fresh key ID
   for the collision phase, or set `minimum_valid_until_ts` past the cached
   validity, AND assert `requestCount` to prove the mock was hit
2. **Fix `TestIntraPayloadRejection` status assertion:** Assert `200` with key
   absent from `server_keys`, not non-200
3. **Relax `TestKeyIDFirstSeenWinsDirect` assertion:** Assert `foundKey != keyB`
   instead of `foundKey == keyA`, then add a follow-up query to prove cache
   integrity
4. **Add a Case A test** for literal duplicate JSON object keys (requires
   hand-crafted JSON, not Go maps)
5. **Add event-path verification** to FSW test (send event signed by key B,
   assert rejection)

> [!NOTE]  
> **MSC clarifications to consider:**
>
> - Ensure notary rejection semantics (HTTP 200 + omission from `server_keys`)
>   are consistently reflected across all test suites
> - Address literal duplicate JSON keys (case A) more precisely
> - Consider whether negative-caching backoff needs test-observable bounds
