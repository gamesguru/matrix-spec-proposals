# Rationale: Duplicate JSON key detection for key responses

A SHOULD is likely too weak for duplicate JSON key detection in key response payloads, for the following reasons:
**The MSC already says MUST at [L141-L147](https://github.com/matrix-org/complement/blob/main/tests/msc4499/4499-key-caching.md#L141-L147):**

> *"A single key response payload MUST NOT contain multiple different public key bodies for the same key ID (e.g., across `verify_keys` and `old_verify_keys`, **or duplicated within the same dictionary**) ... the entire response MUST be rejected as malformed."*

So the normative requirement is already MUST reject. The only question is whether *detecting* it should be SHOULD or MUST. I'd argue: keep the MUST reject, and upgrade the detection guidance too. Here's the case:

1. **This is a known attack class.** Parser divergence attacks exploit exactly this — two implementations see different values for the same key. That's the *precise threat model* MSC4499 exists to address.
2. **The payload is a cryptographic identity document.** This isn't a chat message where leniency is tolerable. It's server signing keys. The bar should be high.
3. **Detection is cheap.** Every major language has a path:
   - Python: `json.loads(data, object_pairs_hook=check_dupes)` — trivial one-liner
   - Go: `json.Decoder` token-by-token scan, parser hooks, custom visitors, or
     equivalent parser configuration that observes every object member before deserialization
   - Rust: `serde_json` with a custom `Visitor`, tokenizer hooks, or equivalent
     parser configuration that observes every object member before deserialization
4. **A SHOULD here means "you can skip it."** And if implementors skip it, you get exactly the parser-divergence attack the MSC is trying to prevent — just at a different layer.

**What I'd recommend for the MSC text** (stronger than what was proposed):

> *Implementations MUST reject key response payloads containing duplicate keys within any single JSON object. Because standard JSON parsers in most languages silently deduplicate object keys before application logic processes them, implementations MUST employ a pre-processing step, streaming tokenizer, or parser configuration capable of detecting duplicate keys prior to deserialization of `/_matrix/key/v2/server` and `/_matrix/key/v2/query` response payloads. This requirement is specific to key response payloads due to their cryptographic sensitivity; it does not impose a general duplicate-key detection mandate on all Matrix JSON parsing.*

That last sentence is important — it scopes the mandate to key payloads only, so you're not accidentally requiring every homeserver to rewrite its entire JSON pipeline. The cost is one specialized parser/scanner for a single, well-defined, low-volume endpoint.

The three changes the MSC still needs are:

1. **Observable rejection semantics** (notary returns 200 with key absent, not 5xx) — not yet applied
2. **Duplicate JSON key detection** — upgrade from SHOULD to MUST, scoped to key payloads — not yet applied  
3. **Negative caching bounds** — ✅ already applied

Want me to make both remaining edits to [4499-key-caching.md](https://github.com/matrix-org/complement/blob/main/tests/msc4499/4499-key-caching.md)?

---

Honestly? Yes, for the duplicate key detection specifically, the SHOULD is a compromise.

The observable rejection semantics and negative caching parts of the patch are solid — no issues there.

But on duplicate JSON keys: the patch says *"SHOULD employ a JSON parser or pre-processing step"* and then immediately hedges with *"because reliable detection is parser-dependent, the cross-map check remains the primary defense."* That's essentially telling implementors "you should try, but it's okay if you don't, because the other check covers you." And it doesn't — Case A (within-object duplicates) and Case B (cross-map duplicates) are *different attack surfaces*. Case A exploits parser divergence to smuggle a different key value past one implementation that another implementation rejects. The cross-map check can't catch that because the parser already resolved the conflict before your code runs.

The practical argument for SHOULD is "you can't mandate what parsers do." But you're not mandating parser behavior — you're mandating that implementations use a parser (or pre-scan) *capable* of detecting this, for two specific low-volume endpoints. That's a scoped, defensible MUST.

If you want to hold the line, I'd change just the last paragraph of the patch to:

```
Implementations MUST employ a JSON parser or pre-processing step capable of
detecting duplicate keys within a single JSON object for key response payloads
(/_matrix/key/v2/server and /_matrix/key/v2/query responses), as standard JSON
parsers in most languages silently deduplicate them. This requirement is specific
to key response payloads due to their cryptographic sensitivity; it does not
impose a general duplicate-key detection mandate on all Matrix JSON parsing.
The cross-map collision check (same key ID appearing in both verify_keys and
old_verify_keys with different key material) MUST be implemented regardless.
```

That's not really more text — it's the same volume but with MUST and a scope limiter. The scope limiter ("specific to key response payloads") is what makes it defensible in review.

But if you'd rather ship the SHOULD to avoid a fight and upgrade it later — the patch as-is is still a genuine improvement over the current text which says nothing at all about detection.
