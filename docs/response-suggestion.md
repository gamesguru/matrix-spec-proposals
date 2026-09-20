# Rationale: Duplicate JSON key detection for key responses

A SHOULD is likely too weak for duplicate JSON key detection in key response
payloads, for the following reasons: **The MSC already says MUST at
[proposals/4499-key-caching.md](../proposals/4499-key-caching.md#L299-L313):**

> _"A single key response payload MUST NOT contain multiple different public key
> bodies for the same key ID (e.g., across `verify_keys` and `old_verify_keys`,
> **or duplicated within the same dictionary**) ... the entire response MUST be
> rejected as malformed."_

So the normative requirement is already MUST reject. The only question is
whether _detecting_ it should be SHOULD or MUST. I'd argue: keep the MUST
reject, and upgrade the detection guidance too. Here's the case:

1. **This is a known attack class.** Parser divergence attacks exploit exactly
   this — two implementations see different values for the same key. That's the
   _precise threat model_ MSC4499 exists to address.
2. **The payload is a cryptographic identity document.** This isn't a chat
   message where leniency is tolerable. It's server signing keys. The bar should
   be high.
3. **Detection is cheap.** Every major language has a path:
   - Python: `json.loads(data, object_pairs_hook=check_dupes)` — trivial
     one-liner
   - Go: `json.Decoder` token-by-token scan, parser hooks, custom visitors, or
     equivalent parser configuration that observes every object member before
     deserialization
   - Rust: `serde_json` with a custom `Visitor`, tokenizer hooks, or equivalent
     parser configuration that observes every object member before
     deserialization
4. **A SHOULD here means "you can skip it."** And if implementors skip it, you
   get exactly the parser-divergence attack the MSC is trying to prevent — just
   at a different layer.

**Recommendation:**

The canonical proposal in
[proposals/4499-key-caching.md](../proposals/4499-key-caching.md#L328-L335)
enforces the scoped MUST mandate for key response payloads:

```text
Implementations MUST employ a JSON parser or pre-processing step capable of
detecting duplicate keys within a single JSON object for key response payloads
(/_matrix/key/v2/server and /_matrix/key/v2/query responses), as standard JSON
parsers in most languages silently deduplicate them. This requirement is specific
to key response payloads due to their cryptographic sensitivity; it does not
impose a general duplicate-key detection mandate on all Matrix JSON parsing.
The cross-map collision check (same key ID appearing in both verify_keys and
old_verify_keys with different key material) MUST be implemented regardless.
```
