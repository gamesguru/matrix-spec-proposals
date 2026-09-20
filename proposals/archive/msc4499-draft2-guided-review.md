# MSC4499 Draft #2 — Guided Review

**Verdict up front:** compact draft #2 is the right base, and the advisor note's
recommendation stands. I verified the claim structurally:
`04-diff-tag-to-current.patch` shows draft #2 is the tagged draft plus purely
additive, targeted insertions — no rewrites of existing prose, no voice drift in
retained text. That diff (tag → current) is also your best defense artifact for
the "deletion-heavy diff" concern: show reviewers _that_ diff, not head →
current. The deletion-heavy staged stat is an artifact of which baseline you
diff against, and the tag-relative diff proves the reconstruction preserved the
original.

I also audited draft #2 against all 13 Complement tests in the suite. **Every
tested behavior is covered by normative text in the compact draft** — including
the two-compliant-behaviors shape of the `minimum_valid_until_ts` test (the
draft's "return pinned key or omit the server; never the colliding key" exactly
matches the test's documented acceptance of both outcomes, and the follow-up
cache-not-poisoned assertion). Nothing test-backed was silently dropped. The
rest of this review is the polish pass: what to fix in place, what to restore
from HEAD in compact form, what _not_ to restore, and two technical flags in the
staged MSC45XX material.

---

## 1. Surgical edits within draft #2 (fix in place, no restructuring)

Ordered roughly by importance. Line numbers refer to
`03-4499-current-compact-draft2.md`.

**1.1 — Bound the park queue (L54–57).** "PDU verification should instead park
and retry rather than punch through the negative cache" introduces an unbounded
holding area with no semantics — how long, how many, retried when? An attacker
who floods you with events referencing dead servers turns the park queue into a
memory-exhaustion vector; you'd be trading the CPU DoS this MSC closes for a new
memory DoS. Neither draft bounds it (HEAD names the "parked-PDU-and-retry
pattern" but also leaves it open). Two sentences fix it, in voice:

> PDU verification should instead park and retry rather than punch through the
> negative cache. Parked PDUs MUST be bounded in count and age, retried when the
> backoff interval expires, and failed normally when the bound is hit — an
> unbounded park queue is just a new exhaustion vector.

Also capitalize the normative "should" in that first sentence (SHOULD), or
demote the whole clause to guidance deliberately — right now it reads normative
but isn't keyworded.

**1.2 — Resolve the floor/test-knob tension (L52, L63–64).** A MUST floor of 60
seconds followed by "SHOULD allow the floor to be shortened in tests" is
formally self-contradictory spec text, and a pedantic reviewer will say so.
Cheapest fix: scope the floor — "with a floor of 60 seconds in production
configurations and a recommended cap of 1 hour." The existing test-knob sentence
then reads as elaboration rather than exception. (Optionally name the knob
non-normatively — HEAD used `org.matrix.msc4499_backoff_secs`, and the
Complement suite's skip logic assumes something like it exists.)

**1.3 — Make the 7-day clamp actually normative (L41–42 vs. L29–30).** The
"Relationship" section claims this MSC "formalizes the `valid_until_ts` 7-day
validity clamp as a normative cache constraint," but the body only says "(e.g.,
restricted to 7 days from fetch)" — an _e.g._ is not a norm. Replace the
parenthetical with "(clamped to at most 7 days from fetch, matching the existing
spec rule for event verification)". One line, and the intro's claim becomes
true.

**1.4 — Paragraph break before "Permanent binding" (L135).** The bold run-in
header is currently glued mid-paragraph: "…defeats this purpose. **Permanent
binding.** The cryptographic…". Every other run-in header in the doc starts a
paragraph. Add the break.

**1.5 — Fix the confusing parenthetical in collision rule 3 (L161–163).**
"(except against the first nominally promoted instance)" collides with the
now-precise meaning of "promotion" (provisional → permanent) and will confuse
exactly the careful readers you want. Replace with "(other than against the
single bound key body)" — or delete the parenthetical; the sentence is complete
without it. While there, consider one clause acknowledging the deliberate notary
exception so nobody flags a contradiction with the internal-digest-indexing
paragraph: "(notaries excepted, which index historical bodies internally for
forensics — see above)".

**1.6 — Tighten the duplicate-JSON closing sentence (L184–186).** "Both
duplicate orderings must reject, otherwise an implementation may only be passing
by parser overwrite behavior" — lowercase must, and "orderings" lands cold.
Suggest: "Both orderings of the duplicate MUST be rejected; an implementation
that accepts one ordering is likely passing only because its parser keeps the
last (or first) value it sees." Same meaning, keyworded, self-explaining.

**1.7 — Give the `minimum_valid_until_ts` paragraph a run-in header
(L122–127).** It's the only normative block in that stretch without one, which
makes it look like an afterthought when it's actually one of the most
attack-relevant rules (and has a dedicated Complement test). Prefix with
**"`minimum_valid_until_ts` is not a bypass."** and capitalize the "may" → MAY
in "It may return the pinned key." Also consider tightening "if locally
acceptable" to mirror the test's framing: "It MAY return the pinned key even if
it does not satisfy the requested validity window, or omit the key/server
entirely."

**1.8 — Scope the 50-key ceiling for batch responses (L70–71).** "A single
server-key response MUST NOT contain more than 50 active keys in `verify_keys`"
— for a notary `/query` batch, is that per response or per server entry? The
test only exercises the direct case. Add "(per server entry, in batch
responses)". Three words, closes the hole.

**1.9 — Wording fixes flagged in the advisor note, with rulings:**

- **"calcifying" (L92, L348): keep it.** It's tag-draft vocabulary, it's vivid,
  and it's unambiguous. Swapping to "ossifying" is precisely the AI-polish
  direction you're trying to avoid. It appears twice, consistently — that's a
  feature.
- **"2-FA" (L350): drop it rather than fix it.** "allowing admins to configure
  2-FA or a Global Settings Lock" — 2FA of _what_ is never said, and the Global
  Settings Lock alone carries the point. "…hint at the need for follow-up work
  (e.g., a Global Settings Lock)." Shorter and stronger.
- **"perform cache merges or manually overrides" (L288):** → "perform cache
  merges or manual overrides." Straight grammar fix.
- **"computationally intractable simulation" (L481):** → "computationally
  intractable search" — the advisor's instinct is right; the underlying concept
  is a second-preimage search, and "search" says it without jargon.
- **One the note missed (L335):** "since it conflicts the zero-trust federation
  model" → "conflicts _with_."
- **"annoying loophole" (L13): keep.** Tag voice. It's doing exactly what the
  original did.

---

## 2. What to restore from HEAD — compact, and in voice

Four restorations, three of them one-bullet-or-less. Paste-ready text provided;
trim freely.

**2.1 — Stolen retired keys / backdated forgeries (Security considerations, ~3
sentences).** Reviewers _will_ spot this limitation of `expired_ts`; owning it
costs one bullet and buys credibility. Condensed from HEAD's version:

> - **Stolen retired keys and backdated forgeries.** Enforcing `expired_ts`
>   stops an attacker holding a compromised retired key from signing current
>   events. It does not stop them from backdating `origin_server_ts` to before
>   `expired_ts` and forging plausible historical events. Limiting a stolen key
>   to backdated forgeries is still a large reduction in power — backdated
>   events have limited reach thanks to `prev_events` and depth — but
>   `expired_ts` is not forward secrecy for room history.

**2.2 — Observation-phase rollout (Unstable prefix section, ~4 sentences,
explicitly operational).** First Seen Wins will break real misconfigured servers
on day one; "how do we deploy this without partitioning federation?" is a
guaranteed review question, and answering it pre-empts the fight rather than
inviting one. Keep it SHOULD-level and clearly non-normative to the core rules:

> Because strict collision rejection can break federation with misconfigured
> servers already in the wild, implementations SHOULD ship an initial
> collision-observation phase: log detected collisions as warnings without
> rejecting the new key, gather real-world breakage data, then enable strict
> enforcement. A configuration flag (e.g., `org.matrix.msc4499_strict_caching`)
> is the obvious gate. This is rollout guidance only; the normative rules above
> are unchanged by it.

**2.3 — One linking sentence between the freeze and manual eviction (append to
the Provisional override freeze paragraph).** This is also the direct answer to
your security question about whether the freeze over-weakens recovery from a
malicious notary — it doesn't, _because_ recovery exists, but the draft never
connects the two mechanisms:

> If the frozen binding itself turns out to be notary poison, recovery is the
> manual cache eviction below — not an automated override.

The freeze is correct as specified. Analysis: for a frozen poisoned binding to
occur, the notary must poison a binding _and_ the origin must remain unreachable
for the entire validity window (otherwise prompt promotion catches it) — at
which point there is little live traffic to verify anyway, and the operator
escape hatch covers the remainder. The trade against domain re-registration
attacks is clearly worth it. If you want one more sentence of honesty, HEAD's
observation about the residual ≤7-day window (origin dies abruptly before
`valid_until_ts` passes) is the only piece of its long domain-re-registration
subsection worth importing, and it fits in the "Two-tier binding" bullet under
Potential issues.

**2.4 — Minimal Open Questions section.** MSC reviewers expect the section;
absence invites "did you consider…" comments that the section would have
absorbed. Restore only the moderation-tooling question — explicitly _not_ the
ACL-eviction question, which the draft now answers normatively (L289–291):

> ## Open questions
>
> - How should moderation tooling (community ban lists, Draunir) treat a server
>   locally isolated by a key collision — as a temporary outage, or as a signal
>   worth surfacing to operators?

---

## 3. What _not_ to restore — including one HEAD idea that's actually wrong

**Complement results and implementation links:** keep out of the proposal body.
MSC convention puts implementation status in the PR description; an HTML comment
in the file is acceptable if you want it colocated. The advisor note has this
right.

**The long "why not room-version auth rules" section:** draft #2's seven-line
version (L317–322) is _better_ than HEAD's — it makes the one argument that
matters (encoding local observations into auth rules makes the split-brain
permanent) and stops. Restore nothing.

**HEAD's notary key-demotion rule — deliberately leave this out, and here's the
defense if asked.** HEAD says notaries SHOULD move an origin's overflow active
keys into `old_verify_keys` to keep responses under the 50-key ceiling. That
rule contradicts the MSC's own logic: a >50-key `verify_keys` payload is defined
as malformed/hostile and MUST be rejected as a whole — a notary "laundering" it
into an acceptable shape both violates whole-payload rejection and has the
notary re-signing an assertion (this key is retired, with an invented
`expired_ts`) that the origin never made. The consistent behavior is already in
draft #2: the notary treats the oversized upstream payload as malformed and
serves previously-cached valid entries per the batch rule. The compact draft is
right by omission; now it's right on purpose.

**HEAD's expanded Relationship/Backwards-compat lists:** the compact versions
cover the same ground. The only line from HEAD's backwards-compat worth
considering is the "incremental adoption" pointer, and restoring 2.2 gives you a
natural one-clause cross-reference instead.

---

## 4. Answers to the review questions

**Performance.** The 60s floor / 1h cap matches the order of magnitude
production servers already use for destination backoff; fine. Fetch coalescing
is standard and cheap. Park/retry is sound _once bounded_ (edit 1.1 — as written
it's a latent DoS). The 3,000-key guideline is generous even for post-quantum
keys (~900-byte FN-DSA-512 bodies ≈ 2.7 MB per hostile server, bounded
per-server) and the draft's own notary note already anticipates large bodies.
The 50-key active ceiling is an order of magnitude above legitimate practice
(single digits) while making the eviction-exemption logic satisfiable — the
cross-reference at L433–434 explaining _why_ the ceiling exists is one of draft
#2's best additions.

**Security.** Freeze: correct, keep; add the eviction linking sentence (2.3).
TOFU framing: honest and appropriately unapologetic — "an inherent limitation of
TOFU, not a flaw in the proposal" is the right posture. Add the
stolen-retired-key bullet (2.1) to complete the honesty.

**Duplicate JSON-key detection scope.** Keep it a MUST for both `/server` and
`/query` key payloads. Scoping it to direct fetches only would leave the notary
path — the aggregation point and the more attractive distribution vector —
unprotected, and the draft's "not a general Matrix JSON parsing mandate; it is a
key-payload rule" disclaimer is exactly the right containment. Whole-payload
raw-byte scanning is also the _easiest_ implementation shape (no per-field
surgery), so the strict scope is cheap to comply with.

**Maintainability.** Yes — with the §1 edits, draft #2 is precise enough to
implement from (the Complement suite is de facto proof: all 13 behaviors trace
to specific normative sentences) while staying under 520 lines after
restorations.

**Rollout.** Restore the observation phase (2.2), as SHOULD-level operational
guidance in Unstable prefix — recommended, not normative. Making it normative
would entangle the core rules with deployment sequencing and invite exactly the
scope fights the compact draft avoids.

**Historical events.** Yes, add the caveat (2.1). It sharpens rather than
distracts: it shows the authors know precisely what `expired_ts` does and does
not buy.

**Voice.** Draft #2 passes. The retained text is untouched tag prose; of the
insertions, the room-version paragraph and the `minimum_valid_until_ts` rule
read fully in voice (short, declarative, slightly combative). The two that read
most "inserted" are the duplicate-JSON closer (fixed by 1.6) and the
test-configuration sentence at L63–64 (fine after 1.2 scopes the floor). Nothing
reads like the HEAD rewrite's over-explained register.

---

## 5. Flags on the staged MSC45XX changes (00E1/E4/E5)

The self-signed expiry claim design is good — self-authenticating, gossip-safe,
smallest-cutoff-wins is the correct anti-extension monotonicity, and the E4
notary-carriage rule correctly keeps claims claim-authenticated rather than
notary-trusted. Two things need attention before this stages further:

**5.1 — The immediate-kill sentence is ambiguous and, read literally, an
operational footgun.** "Once a receiver has observed _any_ valid expiry claim
for a key, it MUST reject new live federation HTTP authentication using that
key, regardless of remote timestamps or local clock skew." Read literally,
publishing a _planned rotation_ claim with `not_valid_after_ts` next week kills
the key **now** — announcing a rotation becomes performing one. I suspect the
intent is: for live auth, the receiver's own clock is compared against the
cached cutoff, and origin-supplied timestamps (`origin_ts_at`) can never extend
acceptance — because an attacker holding the key controls `origin_ts_at` and
would backdate it. If so, say that: "Live federation HTTP authentication using a
closed key MUST be rejected once the receiver's local clock passes the cached
`not_valid_after_ts` (subject to the same 5-minute skew allowance as MSC4499);
remote-supplied timestamps, including `origin_ts_at`, MUST NOT extend
acceptance." If immediate revocation-on-observation _is_ intended for the
compromise case, split by `reason` explicitly — but note that `reason` is
attacker-choosable on a stolen key, so gating semantics on it is fragile; the
local-clock-vs-cutoff rule is the robust shape for both cases.

**5.2 — `origin_ts_at` needs a verification rule or it's dead weight.** It's now
required, signature-covered, and threaded through `X-Matrix-PQC` and the session
MAC — but the shown hunks never say what a verifier _does_ with it on a live
request (skew window? staleness rejection? replay narrowing?). A
signature-covered field with no check adds parsing surface and nothing else.
Either state the check (e.g., reject when `|now − origin_ts_at|` exceeds the
skew allowance, which also gives you cheap replay narrowing) or state explicitly
that it exists solely as the signed timestamp for post-hoc/historical evaluation
against expiry claims and is not checked on live requests. One sentence either
way; ambiguity here is worse than either choice.

Minor: the emergency-claim path (signed by replacement key + existing Ed25519
trust) is sound and non-circular since replacement-key publication has its own
trust path — good design.

---

## 6. Suggested execution order

Do §1 edits in one commit ("polish pass, no semantic changes" — 1.1–1.3 are
arguably semantic tightenings, so name them in the message). Do §2 restorations
in a second commit ("restore condensed HEAD material: rollout, retired-key
caveat, open questions"). Re-diff against the tag and confirm the diff is still
purely additive and under ~13 KB — that artifact plus this review is your
reviewer-facing story: _original draft, surgically extended to cover everything
the conformance suite tests, nothing rewritten._ Then resolve 5.1/5.2 in the
MSC45XX files before those stage further, since 5.1 changes observable behavior.

Net effect on draft #2: roughly +20 lines, no restructuring, voice intact — a
polish pass, exactly as the advisor note prescribed.
