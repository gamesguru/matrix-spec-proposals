# Homomorphic Set Reconciliation over Causal Event Graphs

_A formal treatment, with application to Matrix federation and MSC0501_

---

## Abstract

Reconciliation protocols for replicated event graphs are usually designed by
picking a probabilistic data structure and then engineering around its failure
modes. We argue the choice is not free: the operational properties that matter —
communication cost, extensibility, detectability of failure, and convergence —
are all determined by a single algebraic property of the digest, namely whether
it is a homomorphism onto a _group_ or merely onto an idempotent _monoid_.

Under this lens the design space collapses. The XOR accumulator, the bucketed
digest, the BCH sketch (PinSketch), and the rateless IBLT are not four
mechanisms but four truncations of one object: the syndrome map
$\sigma_k(S) = \big(\sum_{e \in S} h(e)^{2i-1}\big)_{i=1}^{k}$. Increasing $k$
moves continuously from _detection_ through _localization_ to _extraction_, and
"sketch extension" is literally incrementing $k$. The Bloom filter is the sole
candidate outside this family, and we show that each of its pathologies —
$\Theta(n)$ rather than $\Theta(\Delta)$ cost, non-extensibility, silent false
negatives, and absorbing failure states — is a corollary of the single fact that
$(\{0,1\}^m, \vee)$ has no inverses.

We then prove what we take to be the central structural result for causal
graphs: exact recovery of the symmetric difference preserves causal closure, and
_partial_ recovery does not, except along ancestor-closed fronts. Probabilistic
membership filters produce neither. We give a quantitative form: at the
false-positive rate implied by MSC0501's parameters, a single Bloom exchange
yields a causally closed result with probability $\approx 0.1\%$ at
$\Delta = 500$.

Finally we address the objection that motivated this line of work — the
"capacity cliff" of exact reconciliation. We show the cliff is not a defect of
BCH decoding but a theorem: no bounded-message non-adaptive protocol can
reconcile unbounded differences. Every correct protocol is adaptive. The real
axis of comparison is therefore what happens when adaptation is required, on
which the three possibilities are _additive_ (sketches), _destructive_ (naive
retry), and _silent_ (Bloom). Bloom filters are the only candidate in the last
category, and silence is the only failure mode that is not self-correcting.

We conclude with a concrete protocol variant, a bucketed resident structure
(approximately 23 KiB per room in the design analyzed here), and a
self-stabilization theorem identifying exactly which three design choices are
load-bearing.

---

## 1. Introduction

### 1.1 Setting

Matrix homeservers replicate a per-room directed acyclic graph of events.
Federation is lossy: transactions are dropped under rate limiting, spam
mitigation rejects events asymmetrically, partial-state joins resolve
incompletely, and servers restart mid-transaction. The result is _Swiss cheese_:
two servers whose event sets differ by a small, arbitrarily-distributed set of
holes, with no topological hint as to where the holes are.

MSC0501 proposes to repair this with a digest exchange: a 16-byte accumulator as
an agreement check, plus `algebraic_v1` sketch/estimator layers (MSC0501) to
localize and extract small symmetric differences over the room's known-event
set, with loud decode failure and bounded fallbacks when differences are large.

### 1.2 The question this paper answers

An earlier round of analysis rejected exact set reconciliation (CPISync, IBLTs)
on the grounds of the _capacity cliff_: these algorithms require a bound
$k \geq \Delta$ on the symmetric difference chosen before the difference is
known, and fail entirely if it is underestimated. Bloom filters, the argument
went, degrade gracefully.

We show this comparison is malformed in three ways. It compares against the
weakest member of the exact family; it treats an information-theoretic necessity
as an implementation flaw; and it counts a silent, permanent failure as graceful
degradation. Correcting all three inverts the conclusion.

### 1.3 Contributions

We claim no new results in set reconciliation. The algorithms are all standard.
What we claim is:

1. **An algebraic classification** (§3) that derives the operational properties
   of every candidate digest from one structural fact, and shows that four of
   the five candidates are truncations of a single object.
2. **A lower bound** (§4) establishing that adaptivity is mandatory, reframing
   the capacity cliff and supplying the correct comparison axis.
3. **The closure dichotomy** (§5) — the formal reason exactness matters more in
   a causal graph than in an unstructured set, which is what distinguishes this
   application from the blockchain settings where these algorithms were
   developed.
4. **An absorbing-state analysis** (§6) showing MSC0501 as drafted is not
   self-stabilizing, and a self-stabilization theorem for the replacement with
   its hypotheses made explicit.
5. **A provisioning theory** (§7) that sizes sketch capacity from measurements
   rather than guesses, including an exactness result for the one-sided case
   that covers the dominant workload.
6. **A concrete protocol and resident data structure** (§8–9), collapsing three
   digest modes into one endpoint and one parameter.

Sections 10 and 11 treat the adversarial model and the dissemination layer.

---

## 2. Model

### 2.1 The causal poset

Let $\mathcal{U}$ be the universe of event identifiers. Each event $e$ carries a
set of parents $\mathrm{par}(e) \subseteq \mathcal{U}$, the union of its
`prev_events` and `auth_events`. Let $\preceq$ be the reflexive-transitive
closure of the parent relation. Because event identifiers are content hashes
over the parent set, $\preceq$ is a partial order and cycles are computationally
infeasible to construct; we treat $(\mathcal{U}, \preceq)$ as a poset.

For $S \subseteq \mathcal{U}$, write
$\downarrow\! S = \{f : \exists e \in S,\, f \preceq e\}$ for the ancestor
closure.

> **Definition 1 (Closure).** $S$ is _causally closed_ if $\downarrow\! S = S$.

A homeserver's store should be causally closed: an event whose parents are
absent cannot be authorized or state-resolved, and is held as an outlier rather
than integrated into the DAG.

> **Proposition 1.** The causally closed subsets of $\mathcal{U}$ are closed
> under arbitrary unions and intersections.

_Proof._ If $e \in \bigcup_\alpha S_\alpha$ then $e \in S_\beta$ for some
$\beta$, so
$\downarrow\!\{e\} \subseteq S_\beta \subseteq \bigcup_\alpha S_\alpha$. If
$e \in \bigcap_\alpha S_\alpha$ then $\downarrow\!\{e\} \subseteq S_\alpha$ for
every $\alpha$. $\square$

Equivalently, the closed sets are the open sets of the Alexandrov topology on
$(\mathcal{U}, \preceq)$. This is not decoration: it is the reason exactness
composes, and we use it in §5.

### 2.2 Frames

Two servers that joined a room at different times have legitimately different
stores, and neither is a subset of the other in any useful sense. Reconciling
"everything" is therefore ill-posed. We relativize.

> **Definition 2 (Frame).** For an antichain $\Phi \subseteq \mathcal{U}$ (the
> _anchor_), the frame is
> $\mathrm{Fr}(\Phi) = \{e : \exists \phi \in \Phi,\ \phi \preceq e\}$. A set
> $S$ is _$\Phi$-closed_ if $\downarrow\! S \cap \mathrm{Fr}(\Phi) \subseteq S$.

Frame membership is computable by a server from an event's own ancestry, so two
servers holding a common $\Phi$ agree on the predicate for every event either of
them holds. Proposition 1 relativizes verbatim: $\Phi$-closed sets are closed
under union and intersection.

This gives a clean separation that MSC0501's 5,000-event window conflates:

- **Reconciliation** repairs holes _inside_ a frame. It is a set problem.
- **Backfill** extends the frame downward. It is a traversal problem, and Matrix
  already specifies it separately.

A size-bounded window attempts both and achieves neither: it is too small to
cover deep divergence (§6) and too coarse to express "we joined at different
points."

### 2.3 The problem

Two parties hold $\Phi$-closed sets $A, B \subseteq \mathrm{Fr}(\Phi)$. Write
$\Delta = |A \,\triangle\, B|$. The _requester_ (holding $A$) must learn
$B \setminus A$ and integrate it while preserving $\Phi$-closure. We measure:
bits exchanged, round trips, decode work, resident memory, and — the criterion
usually omitted — whether the protocol converges from every reachable state.

---

## 3. The algebraic classification

### 3.1 Digests as homomorphisms

Every digest under discussion is a map $\phi$ from subsets of $\mathcal{U}$ into
a small algebraic structure, computable incrementally as events are persisted.
Fix notation:

| Digest                      | Codomain                               | Combining operation    |
| --------------------------- | -------------------------------------- | ---------------------- |
| Bloom filter $F$            | $\{0,1\}^m$                            | $\vee$                 |
| XOR ETag $\chi$             | $\mathbb{F}_2^{128}$                   | $\oplus$               |
| Bucketed XOR $\chi_\bullet$ | $(\mathbb{F}_2^{\ell})^{b}$            | $\oplus$ componentwise |
| BCH syndrome $\sigma_k$     | $\mathbb{F}_{2^\ell}^{k}$              | $+$                    |
| Rateless IBLT               | $\varinjlim_k \mathbb{F}_{2^\ell}^{k}$ | $+$                    |
| LtHash                      | $\mathbb{Z}_q^{n}$                     | $+$                    |

All are monoid homomorphisms from $(\mathcal{P}(\mathcal{U}), \cup)$ or
$(\mathcal{P}(\mathcal{U}), \triangle)$ into the listed structure. The
distinction that matters is whether the codomain is a **group**.

> **Theorem 1 (Candidate classification).** For the candidate profiles listed in
> Table 1, each profile is either group-valued or an idempotent semilattice; the
> latter category contains the Bloom filter. This is not a universal
> classification of all algebraic codomains, and the trivial group is not
> treated as a semilattice case.
>
> **(G)** The codomain is a group. Then
> $\phi(A \triangle B) = \phi(A) - \phi(B)$ is computable from the two digests
> alone, without reference to $A$ or $B$.
>
> **(S)** The codomain is idempotent (a semilattice). Then no nontrivial
> cancellation exists: for $x \neq 0$, $x \vee x = x$, so $x$ has no inverse,
> and $\phi(A \triangle B)$ is not a function of $(\phi(A), \phi(B))$.

_Proof._ For the listed group-valued profiles, (G) is immediate. For the listed
Bloom profile, (S) follows because idempotence plus invertibility forces
$x = x \vee x \Rightarrow 0 = x$ for all $x$. For the second claim, take
$A = \{a\}$, $B = \{a\}$ and $A' = \{a\}, B' = \emptyset$ with
$F(\{a\}) = F(\{a\})$; the pairs of digests coincide while the symmetric
differences differ. $\square$

The Bloom filter is the unique case (S) among the candidates. Everything below
follows.

### 3.2 Corollaries of the dichotomy

> **Corollary 1.1 (Cost).** A digest in case (S) cannot compute the difference
> from digests alone, so the holder of $B$ must test each of its $|B|$ elements
> against the received filter. Communication is therefore
> $\Theta(|B| \log(1/\varepsilon))$ — a function of _set size_, not difference
> size. A digest in case (G) yields the difference by subtraction, and
> communication is $\Theta(\Delta \ell)$.

This is the whole ballgame. It is why a windowed Bloom filter costs about 6 KB
to report that three events are missing, while a syndrome costs about 30 bytes
for the same answer, and it is not a tuning issue — it is forced by the algebra.

> **Corollary 1.2 (Silence).** Testing membership against a lossy filter admits
> false positives. In the reconciliation direction, a false positive means the
> responder concludes the requester already holds an event and **does not send
> it**. The requester receives no signal. Group digests have no analogue:
> subtraction is exact, and decode failure is a detected event.

> **Corollary 1.3 (Non-extensibility).** In case (G), an under-provisioned
> digest is _extended_ by transmitting further coordinates, which combine
> additively with what was already sent; nothing is discarded. In case (S), the
> only way to add information is an independent re-salted filter, which costs a
> further $\Theta(|B|)$ — the residual difference cannot be isolated, precisely
> because there is no subtraction.

> **Corollary 1.4 (Bloom's parameters).** With
> $m/n = \log_2(1/\varepsilon)/\ln 2$ bits per element, the optimal hash count
> is $k^\ast = (m/n)\ln 2$. MSC0501 specifies $k=4$; at
> $\varepsilon_{\text{nominal}} = 1\%$ we have $m/n = 9.59$ and $k^\ast = 6.6$,
> so the realised rate is $(1 - e^{-4/9.59})^4 = 1.36\%$, not $1\%$.

Minor, but it compounds: the quantity that matters in §5 is
$(1-\varepsilon)^\Delta$, which is exponentially sensitive to this.

### 3.3 One object

The remaining candidates are not alternatives to each other. Work in
$\mathbb{F}_{2^\ell}$ and let $h : \mathcal{U} \to \mathbb{F}_{2^\ell}$ be a
short-identifier map. Define the syndrome

$$\sigma_k(S) \;=\; \Big(\textstyle\sum_{e \in S} h(e),\ \sum_{e \in S} h(e)^3,\ \sum_{e \in S} h(e)^5,\ \dots,\ \sum_{e \in S} h(e)^{2k-1}\Big) \in \mathbb{F}_{2^\ell}^{k}.$$

Even powers are omitted because the Frobenius endomorphism gives
$s_{2i} = s_i^2$ in characteristic 2; they carry no information. Then:

> **Theorem 2 (Filtration).** $\sigma_k$ is $\mathbb{F}_2$-linear, so
> $\sigma_k(A) + \sigma_k(B) = \sigma_k(A \triangle B)$. The maps
> $\sigma_1 \leftarrow \sigma_2 \leftarrow \cdots$ form a filtration under
> coordinate projection, and:
>
> - $\sigma_1$ **is** the XOR accumulator: $s_1 = \bigoplus_{e \in S} h(e)$.
>   MSC0501's `room_xor_sum` is $\sigma_1$ at $\ell = 128$.
> - $\sigma_1$ computed on a partition of $\mathcal{U}$ into $b$ blocks is the
>   bucketed digest.
> - $\sigma_k$ is exactly a PinSketch of capacity $k$: Berlekamp–Massey recovers
>   the error-locator polynomial from the syndromes and root-finding yields
>   $A \triangle B$, provided $\Delta \leq k$.
> - A rateless IBLT is the same construction with the truncation index chosen
>   adaptively at transmission time rather than in advance.

Three consequences, each of which simplifies the protocol.

**(a) The tiers are resolutions of one structure, not three mechanisms.**
MSC0501 has an ETag mode, a Bloom mode, and an extremity mode with distinct wire
formats and distinct failure semantics. Under Theorem 2 there is one wire
format, $\sigma_k$, and one parameter, $k$.

**(b) Sketch extension is incrementing $k$.** BIP-330's "request a sketch
extension, receive a higher-capacity sketch minus what was already sent" is, in
this language, transmitting coordinates $k+1 \ldots k'$ of a filtration whose
earlier coordinates the receiver already holds. Corollary 1.3 is the statement
that this operation exists.

**(c) The ETag is the sketch's verifier, for free.** After decoding a candidate
difference $R$ from $\sigma_k$, apply it and recompute $\sigma_1$. Decoding was
correct only if the accumulator now matches the peer's. Since $\sigma_1$ is a
coordinate of $\sigma_k$ this costs nothing to transmit, and against an
independently-sized 128-bit accumulator the probability of accepting a wrong
decode is $2^{-128}$.

Point (c) closes the last gap in the exactness argument. BCH decoding beyond
capacity fails loudly with overwhelming probability, but not with certainty; the
accumulator check converts "overwhelming" into "cryptographic."

### 3.4 Classification table

| Digest                 | Structure         | Invertible | Extractable | Comm. cost                       | Failure mode           |
| ---------------------- | ----------------- | ---------- | ----------- | -------------------------------- | ---------------------- |
| Bloom                  | idempotent monoid | no         | no          | $\Theta(n\log\frac1\varepsilon)$ | **silent**             |
| $\sigma_1$ (ETag)      | group             | yes        | no          | $\Theta(1)$                      | detect only            |
| $\sigma_1$ bucketed    | group             | yes        | localize    | $\Theta(b)$                      | detect + locate        |
| $\sigma_k$ (PinSketch) | group             | yes        | up to $k$   | $\Theta(k\ell)$                  | loud, additive         |
| RIBLT                  | group             | yes        | unbounded   | $\Theta(1.35\Delta)$             | none (rateless)        |
| LtHash                 | group             | yes        | no          | $\Theta(1)$                      | detect only, SIS-sound |

---

## 4. The capacity cliff is a theorem, not a defect

### 4.1 Adaptivity is necessary

> **Theorem 3.** Let $\Pi$ be a one-message protocol in which the responder
> sends at most $M$ bits and the requester must output $B \setminus A$ exactly.
> Then $\Pi$ fails whenever $\Delta > M/(\ell - \log_2 \Delta)$, where $2^\ell$
> bounds the identifier universe.

_Proof._ Specialize to $A = \emptyset$, so the requester's output is a function
of the message alone. Correctness requires the message map to be injective on
the set of possible $B$; there are $\binom{2^\ell}{d}$ sets of size $d$, so
$M \geq \log_2\binom{2^\ell}{d} \geq d(\ell - \log_2 d)$. $\square$

Since $\Delta$ is unbounded a priori, **no bounded-message protocol reconciles
exactly without adaptation.** The capacity cliff of PinSketch, CPISync and IBLTs
is not an artifact of finite-field decoding. It is this bound, made visible.

The apparent exception is instructive. A Bloom exchange never reports failure —
but it does not achieve exact reconciliation either. It achieves reconciliation
with a $(1-\varepsilon)$-fraction residue, and clearing that residue takes
further rounds. Bloom is adaptive too. It differs only in that its adaptivity is
implicit, uninstrumented, and (§6) not guaranteed to terminate.

> **Corollary 3.1 (The correct comparison).** Since all correct protocols are
> adaptive, they should be compared on the behaviour of the adaptive step. Three
> possibilities exist:
>
> - **Additive.** Prior transmission is retained; extension transmits only the
>   increment. Cost of reaching capacity $k$ via any doubling schedule is
>   $< 2k\ell$, i.e. within $2\times$ optimal. _(Group digests.)_
> - **Destructive.** Prior transmission is discarded and the exchange restarts.
>   Cost is $\sum_i k_i \ell$ with full retransmission. _(Naive retry; the
>   failure mode attributed to CPISync in the earlier analysis, and avoidable.)_
> - **Silent.** Failure is not signalled, so no adaptive step is triggered at
>   all. _(Bloom.)_

Only the third is not self-correcting, and it is the one MSC0501 selects.

### 4.2 Optimality of the syndrome prefix

> **Theorem 4.** Any protocol reconciling a difference of size $d$ over
> $\ell$-bit identifiers transmits at least $d\ell - d\log_2 d + O(d)$ bits in
> the worst case. A PinSketch of capacity $t \geq d$ transmits $t\ell$ bits. The
> syndrome prefix is therefore optimal up to the provisioning ratio $t/d$ and a
> $\log_2 d$ per-element term.

Combined with Corollary 3.1, over-provisioning is cheap in exactly the regime
where round trips are expensive. We make this quantitative in §7.5.

---

## 5. Causal closure: why exactness matters more here

Set reconciliation was developed for unstructured sets — Bitcoin mempools,
Ethereum state, CRDT replicas. In those settings a partial recovery is simply a
partial success. In a causal graph it is usually not a success at all.

> **Theorem 5 (Closure dichotomy).** Let $A, B$ be $\Phi$-closed and let
> $R \subseteq B \setminus A$ be a recovery set. Then $A \cup R$ is
> $\Phi$-closed **iff** every event in $R$ has all of its in-frame ancestors in
> $A \cup R$. In particular:
>
> 1. _(Exact.)_ $R = B \setminus A$ always satisfies this, by Proposition 1
>    applied to $A \cup B$.
> 2. _(Frontier-ordered.)_ $R$ generated by backward traversal from $B$'s
>    extremities satisfies this only when traversal reaches known ancestry or
>    otherwise returns an ancestor-closed set. A walk stopped before that
>    condition may contain descendants whose missing ancestors are not in
>    $A \cup R$.
> 3. _(Arbitrary.)_ A uniformly random $R \subsetneq B \setminus A$ does not, in
>    general.

_Proof._ The condition is the definition of relative closure. (1):
$A \cup (B\setminus A) = A \cup B$, closed by Proposition 1. (2): when the
traversal reaches known ancestry, or explicitly returns an ancestor-closed set,
the condition holds; without that restriction a stopped walk is upward-generated
and can fail it. (3): counterexample by taking $R$ to exclude a single minimal
element of $B \setminus A$. $\square$

This classifies the two mechanisms that are _sound_ on a DAG. Exact set
reconciliation is sound. A bounded graph walk is sound. A Bloom filter is
neither, and the residue it produces is category (3).

### 5.1 Quantitative form

Suppose each of the $\Delta$ missing events is independently masked by a false
positive with probability $\varepsilon$. Because the missing set is typically a
causally contiguous region, a single mask near the bottom orphans every
descendant of it in the batch. Then

$$\Pr[\,A \cup R \text{ is } \Phi\text{-closed}\,] \;\approx\; (1-\varepsilon)^{\Delta}.$$

At MSC0501's realised $\varepsilon = 1.36\%$ (Corollary 1.4):

| $\Delta$ | $\Pr[\text{closed batch}]$ | expected masked events |
| -------- | -------------------------- | ---------------------- |
| 10       | 87%                        | 0.14                   |
| 100      | 25%                        | 1.4                    |
| 500      | 0.1%                       | 6.8                    |
| 1000     | $10^{-6}$                  | 13.6                   |

The unmasked events that depend on a masked one cannot be integrated; they are
retained as outliers. So a Bloom round at $\Delta = 500$ does not recover 493 of
500 events — it recovers whatever prefix of the causal order survives the first
mask, and stores the rest as garbage. The 6.8 masked events, meanwhile, become
the subject of §6.

---

## 6. Absorbing states and self-stabilization

Repair protocols should be _self-stabilizing_ in Dijkstra's sense: from every
reachable state, including arbitrarily corrupted ones, the system converges to
the legitimate state ($\Delta = 0$). This is the right correctness criterion for
a mechanism whose entire purpose is recovering from unspecified prior faults.

> **Definition 3 (Absorbing hole).** A state with $\Delta \geq 1$ from which
> $\Pr[\Delta \text{ decreases in any future round}] = 0$.

> **Theorem 6.** MSC0501's `bloom` mode admits absorbing holes, and is therefore
> not self-stabilizing.

_Proof sketch._ Let $e^\ast \in B \setminus A$ be masked in round $t$. Two
mechanisms are available in later rounds. The `bloom` mode tests only events
within the responder's window of the $W$ most recent events. Once
$|\{f \in B : \mathrm{depth}(f) > \mathrm{depth}(e^\ast)\}| > W$, the responder
never tests $e^\ast$ again; the probability of recovery via this path is exactly
zero, not merely small. The `extremity` mode walks backward from the requester's
frontier and terminates on reaching known events. Since $A$ contains events
causally above $e^\ast$ (having continued to accept the DAG around the hole),
the walk terminates before reaching $e^\ast$. No other path exists, so recovery
probability is zero. Meanwhile $\sigma_1$ is computed over the whole room and
remains permanently mismatched, so the requester re-triggers the diff
indefinitely. $\square$

Two remarks. First, per-round salting of the filter — which would make false
positives independent across rounds — does _not_ fix this. It addresses only the
persistence of the collision, while the absorbing property comes from the
window. The defect is structural.

Second, this is worse than a livelock: the servers poll forever, correctly
detecting divergence they are constitutionally unable to locate. The $O(1)$
whole-room ETag, which is the design's best feature, is what makes the failure
permanent and visible rather than merely permanent.

> **Theorem 7 (Self-stabilization).** Consider the protocol of §8. Suppose:
>
> **(H1)** _Full-frame digest._ The level-1 digest partitions the entire frame,
> not a suffix of it. **(H2)** _Loud, additive failure._ Decode failure is
> detected (via §3.3(c)) and extension is additive (Corollary 1.3). **(H3)**
> _Ergodic peer sampling._ Every in-room server is selected with probability at
> least $\epsilon/N$ per round.
>
> Then from any reachable state, $\Delta \to 0$ with probability 1. H1--H3 do
> not imply a universal round bound; latency depends on $\Delta$, escalation,
> RTT, useful-peer probability, and the dissemination model.

_Proof sketch._ (H1) removes absorbing holes: every divergent bucket is examined
every round. (H2) makes failure visible and permits additive escalation. (H3)
gives every useful peer a nonzero sampling probability, so each missing event is
eventually encountered with probability 1; repeated successful exchanges then
drive $\Delta$ to zero. The time to do so depends on the factors listed above.
$\square$

The three hypotheses are the design. (H1) is why buckets replace windows. (H2)
is why the digest must be a group homomorphism. (H3) is why peer selection needs
a uniform floor even though hub-weighted sampling is more efficient — the floor
is not a heuristic, it is the hypothesis that makes the theorem true.

---

## 7. Estimation and provisioning

Theorem 3 says we must sometimes adapt. Good engineering makes adaptation rare.
This requires estimating $\Delta$ before choosing $k$ — the step the earlier
analysis assumed was impossible.

Partition $\mathcal{U}$ into $b$ buckets by the leading bits of $h(e)$. Because
identifiers are content hashes, the partition is uniform. Each party maintains
per bucket $j$: the accumulator $\chi_j = \sigma_1(S \cap \mathcal{B}_j)$ and
the count $n_j = |S \cap \mathcal{B}_j|$.

### 7.1 The count vector gives a deterministic bound

Let $\delta_j^{+} = |B_j \setminus A_j|$ and
$\delta_j^{-} = |A_j \setminus B_j|$, so $\delta_j = \delta_j^+ + \delta_j^-$
and $\Delta = \sum_j \delta_j$. Define

$$L \;=\; \sum_{j=1}^{b} \big| n^A_j - n^B_j \big|.$$

> **Proposition 2.** $L \leq \Delta$ always, with equality **iff** every bucket
> is one-sided, i.e. $\min(\delta_j^+, \delta_j^-) = 0$ for all $j$. In
> particular equality holds whenever $A \subseteq B$ or $B \subseteq A$.

_Proof._ $n^B_j - n^A_j = \delta_j^+ - \delta_j^-$, and
$|\delta_j^+ - \delta_j^-| \le \delta_j^+ + \delta_j^-$ with equality iff one
term vanishes. $\square$

This matters because the dominant workload is one-sided: a lagging server is
_missing_ events, not holding surplus ones. In that regime the count vector
determines $\Delta$ **exactly**, with no probabilistic error, at a cost of
$b\lceil \log_2 n\rceil$ bits. The capacity cliff cannot occur, because there is
nothing to estimate.

### 7.2 Occupancy gives a two-sided estimate

Let $\mathcal{D} = \{j : \chi^A_j \neq \chi^B_j\}$ be the set of differing
buckets, and $D = |\mathcal{D}|$ its count. A bucket registers as differing iff
$\delta_j \geq 1$ and the XOR of the differing identifiers is nonzero, which
fails with probability $2^{-\ell}$. So $D$ is, up to negligible error, the
number of occupied bins when $\Delta$ balls are thrown into $b$ bins, and the
standard linear-counting estimator (Whang, Vander-Zanden and Taylor, 1990)
applies:

$$\hat\Delta_{\mathrm{occ}} \;=\; \frac{\ln\!\big(1 - D/b\big)}{\ln\!\big(1 - 1/b\big)}, \qquad \frac{\mathrm{SE}(\hat\Delta)}{\Delta} = \frac{\sqrt{b\,(e^{t} - t - 1)}}{\Delta},\quad t = \Delta/b.$$

At $b = 256$: relative standard error 4.7% at $\Delta = 100$, 6.5% at
$\Delta = 500$, 10.7% at $\Delta = 1000$, degrading to 40% at $\Delta = 2000$ as
the buckets saturate. Note that $\hat\Delta_{\mathrm{occ}}$ is insensitive to
sidedness, exactly where $L$ is weak. Take

$$\hat\Delta \;=\; \max\big(L,\ \hat\Delta_{\mathrm{occ}}\big).$$

### 7.3 Provisioning rule and overflow bound

Bucket capacities are sized independently. Conditioned on $\hat\Delta$, the
per-bucket difference is approximately $\mathrm{Poisson}(\hat\Delta/b)$, giving
a union bound on the probability that any bucket exceeds capacity $t$:

$$\Pr[\text{some bucket overflows}] \;\leq\; b \cdot \Pr\big[\mathrm{Poisson}(\hat\Delta/b) > t\big].$$

This is the design equation. At $b = 256$, $t = 8$:

| $\Delta$ | $\lambda$ | $\Pr[\text{overflow}]$ | expected overflowing buckets |
| -------- | --------- | ---------------------- | ---------------------------- |
| 128      | 0.5       | $\sim 10^{-7}$         | negligible                   |
| 256      | 1.0       | 0.03%                  | —                            |
| 512      | 2.0       | 6.1%                   | 0.06                         |
| 1024     | 4.0       | —                      | 5.5                          |

So a fixed capacity of 8 syndromes per bucket resolves $\Delta \lesssim 300$ in
a single round trip essentially always, $\Delta \approx 500$ with a 6% chance of
one cheap additive extension, and remains usable at $\Delta \approx 1000$ where
a handful of buckets need extending.

### 7.4 Sub-partitioning as an alternative to extension

Because $\sigma_k$ is linear, the bisection trick from BIP-330 applies:
transmitting the syndrome of one half of a bucket's identifier subrange lets the
receiver derive the other half by subtraction, doubling effective capacity for
the cost of one sketch. This has a second benefit — BCH decoding is $O(k^2)$
field operations, so splitting a capacity-$k$ decode into $b$ decodes of
capacity $k/b$ reduces work by a factor of $b$. Bucketing is therefore
simultaneously the estimator, the cliff mitigation, and the decode-complexity
fix.

### 7.5 Overhead of speculation

> **Theorem 8.** Fix a target failure probability $\eta$ and provision each
> bucket at the $(1-\eta/b)$ quantile of $\mathrm{Poisson}(\hat\Delta/b)$. Then
> expected sketch traffic is $\Delta\ell(1 + \rho) + O(b)$ bits where
> $\rho \to 0$ as $\Delta/b \to \infty$, and
> $\Pr[\text{more than one round trip}] \le \eta + \Pr[\hat\Delta \text{ underestimates}]$.

The practical reading: in the small-$\Delta$ regime the optimal sketch is on the
order of 100 bytes, and provisioning at $3$–$4\times$ costs a few hundred bytes
to buy a near-certain single round trip. This is the regime covering the
overwhelming majority of exchanges. minisketch's own protocol notes reach the
same conclusion from the opposite direction, observing that elaborate difference
estimators can consume more bandwidth than they save, and that deliberate
over-provisioning is often the better trade against packet and round-trip
overheads.

---

## 8. The protocol

Theorem 2 permits a single endpoint at three resolutions of the same object.

### 8.1 Levels

**Level 0 — agreement.** Exchange $\sigma_1$ over the frame, 16 bytes. Match
$\Rightarrow$ `304`. This is $\approx 99\%$ of exchanges and is what makes the
gossip layer affordable (§11.4).

**Level 1 — localization.** On mismatch, exchange the bucketed digest: $b$ pairs
of $(\chi_j, n_j)$. At $b = 256$, $\ell = 64$, 24-bit counts, this is 2.8 KB,
fixed, independent of room size and of $\Delta$. Yields the differing bucket set
$\mathcal{D}$ (count $D$), the count bound $L$, and $\hat\Delta_{\mathrm{occ}}$.

**Level 2 — extraction.** For each differing bucket, exchange $\sigma_{t_j}$
with $t_j$ from §7.3. Decode per bucket via Berlekamp–Massey and root-finding.
Verify by recomputing $\sigma_1$ (§3.3(c)). Extend additively for any bucket
that fails.

**Fallback.** If $\hat\Delta$ exceeds the point where sketch cost is dominated
by payload cost — see §8.3 — skip to a bounded frontier-ordered walk, which
Theorem 5(2) certifies as closure-preserving.

### 8.2 Integration

The recovered identifier set $B \setminus A$ is fetched in topological order and
applied. By Theorem 5(1) closure is guaranteed on completion; by construction it
is also maintained at every prefix of the topological order, so an interrupted
fetch leaves a valid, smaller closed set rather than a pile of outliers.

### 8.3 Where to stop caring

A Matrix PDU is roughly 1–1.5 KB on the wire once signatures, hashes and parent
lists are counted. The sketch is therefore never the dominant cost once $\Delta$
is large:

| $\Delta$ | payload | level-2 sketch | sketch as % of transfer |
| -------- | ------- | -------------- | ----------------------- |
| 10       | 15 KB   | 100 B          | 0.7%                    |
| 100      | 150 KB  | 1 KB           | 0.7%                    |
| 1,000    | 1.5 MB  | 10 KB          | 0.7%                    |
| 10,000   | 15 MB   | 100 KB         | 0.7%                    |

The ratio is constant because both are $\Theta(\Delta)$ — which is the point. A
Bloom filter's ratio is $\Theta(n/\Delta)$: 40% overhead at $\Delta = 10$,
negligible at $\Delta = 10{,}000$. The two mechanisms are thus optimal in
_disjoint_ regimes, and MSC0501 chose the one that is efficient exactly where
efficiency does not matter.

The switch to a graph walk should therefore be made not on sketch efficiency but
on decode work and on whether a frame is even shared: at
$\Delta \gtrsim 50{,}000$ the transfer is tens of megabytes and a resumable walk
is preferable for reasons of flow control, not bandwidth.

### 8.4 Summary of costs

| Regime                     | Mechanism                | Wire                        | Round trips  | Decode                  |
| -------------------------- | ------------------------ | --------------------------- | ------------ | ----------------------- |
| $\Delta = 0$               | $\sigma_1$               | 16 B                        | 1            | —                       |
| $\Delta \lesssim 300$      | levels 1–2, resident     | 2.8 KB + $10\Delta$ B       | 2            | $O(\Delta^2/b)$         |
| $\Delta \lesssim 5{,}000$  | levels 1–2, extended     | 2.8 KB + $\sim\!20\Delta$ B | 2–3          | $O(\Delta^2/b)$         |
| $\Delta \lesssim 50{,}000$ | rateless stream          | $\sim\!27\Delta$ B          | 1 (streamed) | $O(\Delta \log \Delta)$ |
| beyond, or no shared frame | frontier walk + backfill | —                           | resumable    | —                       |

---

## 9. Implementation: the resident syndrome array

The estimator and the sketch must both be maintainable in $O(1)$ per persisted
event, or none of this is deployable.

Maintain per room an array of $b \times t$ field elements: $b$ buckets, $t$
syndromes each. On persisting event $e$: compute $x = h(e)$, select bucket $j$
from its leading bits, compute $x^2$ once, then accumulate
$x, x^3, \dots, x^{2t-1}$ by repeated multiplication by $x^2$, XOR-ing each into
the corresponding cell; increment $n_j$. On purge, apply the identical operation
— the map is an involution in characteristic 2, so removal and insertion are the
same code path.

At $b = 256$, $t = 8$, $\ell = 64$:

- **Profile resident size:** MSC0501's complete structure is approximately **23
  KiB per room**, including the room and bucket $h_{128}$ accumulators, counts,
  eight-syndrome arrays, and the 32-strata estimator.
- **Update cost:** approximately 618 ns per event with the portable reference
  implementation and 52 ns with PCLMULQDQ; the two paths are bit-identical.
- **Coverage:** the complete structure supports the profile's full-frame
  summaries; any particular $\Delta$ range remains subject to bucket capacity
  and local decode policy.

For larger $\Delta$, higher-capacity sketches are built by scanning the affected
buckets only — approximately $n/b$ events each, so 390 events per bucket in a
100,000-event room.

The comparison to state: MSC0501's windowed Bloom filter is about 6 KB per room,
covers 5,000 events, and fails silently. The complete structure above is
approximately 23 KiB per room, covers the entire frame, is exact, and
self-verifies. The exact mechanism is _cheaper in every dimension that scales_
and more expensive only in a fixed 11 KB.

---

## 10. Adversarial model

### 10.1 Why $\mathbb{F}_2$ is not enough

The accumulator $\chi(S) = \bigoplus_{e \in S} H(e)$ is $\mathbb{F}_2$-linear in
the indicator vector of $S$. Consequently:

> **Proposition 3.** Given $m > \ell$ candidate event vectors that span
> $\mathbb{F}_2^{\ell}$, an adversary finds a subset $S$ with $\chi(S) = \tau$
> by Gaussian elimination over $\mathbb{F}_2$, in $O(\ell^2 m)$ time.

XOR accumulators are error-_detecting_ codes, not authenticators. A malicious
homeserver can present a matching ETag while withholding events, provided it has
latitude in which events to include.

The comparison with LtHash is now precise, and vindicates the intuition that
opened this line of inquiry. LtHash uses $\chi(S) = \sum_{e\in S} H(e)$ over
$\mathbb{Z}_q^{n}$; finding a collision means finding a short nonzero integer
vector in the kernel of a random matrix mod $q$, which is the Short Integer
Solution problem, hard under standard lattice assumptions. The structural
difference is _not_ that one is "cryptographic" and the other is not. It is that
over $\mathbb{F}_2$ every vector is a $0/1$ vector, so the norm constraint that
makes SIS hard is vacuous. **$\mathbb{F}_2$ collapses SIS.** That is the entire
content of the distinction.

### 10.2 Layered response

The costs differ by three orders of magnitude — LtHash at
$(n,q) = (1024, 2^{16})$ is 2 KB per accumulator, so $b = 256$ buckets would be
512 KB — so layer them:

- **Root ($\sigma_1$ over the frame):** the only value a peer can profitably lie
  about across many exchanges. Use LtHash here if the deployment's threat model
  includes malicious homeservers; 2 KB resident, 2 KB on the wire, still $O(1)$.
- **Buckets and sketches:** use the unsalted, profile-defined $h_{64}(e)$
  mapping. Per-link salting is reserved for a separately negotiated future
  digest profile; it MUST NOT be introduced into `algebraic_v1`, whose fixed
  mapping is required for interoperability.
- **Verification:** §3.3(c) is a soundness check against a _faulty_ peer, not a
  malicious one, since the peer supplies both the sketch and the accumulator.
  Against a malicious peer, verification must be against the event signatures,
  which Matrix already requires.

### 10.3 What remains open

Hub-weighted peer selection (§11.3) is an eclipse-attack surface: an adversary
who can appear to be a hub attracts reconciliation traffic. The uniform floor of
(H3) bounds this, but the quantitative relationship between the floor $\epsilon$
and eclipse resistance is not worked out here.

---

## 11. Dissemination

Reconciliation is a pairwise primitive. Room-wide convergence needs a schedule.

### 11.1 Correcting the fanout

A natural proposal is: every 64 events, reconcile with $\ln N$ random peers. The
fanout is over-specified by a factor of $\ln N$.

The $\ln N$ figure comes from Erdős–Rényi connectivity: a _static_ random graph
on $N$ nodes is connected w.h.p. once average degree exceeds $\ln N$.
Anti-entropy does not build a static graph. It resamples every round, so the
relevant object is the union of $T$ independent random graphs, which is
connected w.h.p. for $T\cdot f \gtrsim \ln N$ — the fanout and the round count
are interchangeable. Pull-based rumor spreading informs all nodes in
$\log_2 N + \ln N + O(1)$ rounds with fanout 1 (Karp, Schindelhauer, Shenker and
Vöcking, 2000).

So $f \in \{2, 3\}$ suffices, and $f = \ln N$ costs a factor of
$\ln N \approx 7$ in messages at $N = 1000$ for no asymptotic gain.

### 11.2 Triggers

Binding reconciliation to an event counter is unsound in both limits:

- **Dormant room.** Miss the 63rd event, room goes quiet: the 64th never
  arrives, the trigger never fires, $T_{\text{sync}} \to \infty$.
- **Partitioned server.** Receiving nothing, the counter never advances, so a
  server that has fallen completely behind is precisely the one that never
  initiates repair.

Both are instances of the same error: making the repair rate proportional to the
health signal. The rule is:

$$T_{\text{eff}} \;=\; \min\big(T_{\text{periodic}} \cdot U(0.5,\,1.5),\ \ \text{time to } c \text{ events},\ \ \text{first unknown } \texttt{prev\_event}\big)$$

The periodic term bounds worst-case detection latency and must not depend on
traffic. The event counter is an accelerator only. The reactive term — observing
a reference to an event one does not hold — is the fast path, and bounds typical
latency to a single round trip.

### 11.3 Ergodic peer selection

Uniform sampling is inefficient in a scale-free topology: most servers are
single-user leaves as likely to be missing an event as the requester. Weighting
toward recent originators or degree centrality is correct. But pure weighting
breaks (H3), and with it Theorem 7. The rule is a mixture with a floor:

$$\Pr[\text{select } i] \;=\; \alpha \cdot w_i \;+\; (1-\alpha)\cdot \frac{1}{N}, \qquad \alpha < 1.$$

Efficiency comes from $\alpha w_i$; the convergence proof, and eclipse
resistance, come from $(1-\alpha)/N$. Setting $\alpha = 1$ is the optimization
that destroys the guarantee.

### 11.4 The thundering herd dissolves

If a hub fails and 5,000 leaves cross their timers simultaneously, the resulting
load is $5000 \times 16\,\mathrm{B} = 80\,\mathrm{KB}$ of level-0 digests. This
is not an incident.

The same event under a 6 KB Bloom digest is 30 MB of burst traffic against a
server that has just recovered, which is an incident, and which is why MSC0501
needs jitter and backoff to be load-bearing rather than merely prudent.

This is the payoff of Theorem 1 at the system level: because the happy-path
digest is a group element rather than a filter, it is $O(1)$ rather than $O(n)$,
and the entire gossip layer becomes affordable. Jitter remains good practice; it
is no longer the only thing standing between the room and a retry storm.

---

## 12. Summary of the argument

1. Digests are homomorphisms. Group-valued ones subtract; semilattice-valued
   ones do not. (Theorem 1)
2. Everything except the Bloom filter is one object, $\sigma_k$, at different
   truncations. Extension is incrementing $k$; the ETag is $\sigma_1$; the
   verifier is free. (Theorem 2)
3. Bloom's $\Theta(n)$ cost, silent misses, and non-extensibility are
   corollaries of non-invertibility, not tuning problems. (Cor 1.1–1.3)
4. The capacity cliff is an information-theoretic necessity. Every correct
   protocol is adaptive. (Theorem 3)
5. Protocols therefore differ in _how_ they adapt: additively, destructively, or
   silently. Only silence is not self-correcting. (Cor 3.1)
6. On a causal graph, exact recovery preserves closure and arbitrary partial
   recovery does not — so silence is not merely inefficient, it produces
   unusable batches. (Theorem 5)
7. A windowed probabilistic filter has absorbing failure states and is not
   self-stabilizing. A full-frame group digest with loud additive failure and
   ergodic sampling is. (Theorems 6 and 7)
8. $\Delta$ need not be guessed. Bucket counts bound it deterministically and
   exactly in the one-sided case; occupancy estimates it in the two-sided case.
   (Prop 2, §7.2)
9. The resulting complete structure is approximately 23 KiB resident per room,
   updated in approximately 618 ns portably or 52 ns with PCLMULQDQ. (§9)
10. XOR accumulators are unforgeable only against faults, not adversaries,
    because $\mathbb{F}_2$ collapses the SIS norm. LtHash is the correct root
    accumulator if malicious peers are in scope. (Prop 3)

The single sentence: **MSC0501's digest is the only candidate mechanism that is
not a group homomorphism, and every operational pathology in the design descends
from that one algebraic fact.**

---

## 13. Related work

**Set reconciliation.** Minsky, Trachtenberg and Zippel (2003) introduced
characteristic-polynomial reconciliation (CPISync) with $O(\Delta)$
communication and $O(\Delta^3)$ decoding. Dodis, Katz, Reyzin and Smith (2004)
gave PinSketch via BCH syndromes, matching the communication bound at
$O(\Delta^2)$ decoding. Goodrich and Mitzenmacher (2011) and Eppstein, Goodrich,
Uyeda and Varghese (2011) developed invertible Bloom lookup tables and the
strata estimator for sizing them. Yang, Gilad and Alizadeh (2024) introduced
rateless IBLTs, removing the capacity parameter entirely at an overhead of
1.35–1.72 coded symbols per difference.

**Deployment.** BIP-330 and the `minisketch` library apply PinSketch to Bitcoin
transaction relay, and are the source of the sketch-extension and bisection
techniques used in §7.4, the capacity heuristic in §7, and the per-link salting
in §10.2.

**Estimation.** Whang, Vander-Zanden and Taylor (1990) gave the linear-counting
estimator and its variance, used in §7.2.

**Homomorphic hashing.** Bellare and Micciancio (1997) introduced incremental
hashing by randomize-then-combine; LtHash instantiates the lattice variant,
whose security reduces to SIS (Ajtai, 1996).

**Dissemination.** Demers et al. (1987) introduced anti-entropy and rumor
mongering; Karp, Schindelhauer, Shenker and Vöcking (2000) gave the tight round
bounds for push, pull and push-pull used in §11.1. Self-stabilization is due to
Dijkstra (1974).

**Range-based reconciliation.** Merkle search trees and their descendants
(Negentropy and similar) reconcile by recursive range hashing, at $O(\log n)$
round trips and $O(\Delta \log n)$ communication. The construction here is a
two-level version whose second level _inverts_ rather than merely comparing,
which is why the recursion terminates at depth 2 instead of $\log n$.

---

## 14. Open problems

1. **Frame negotiation.** §2.2 defines frames but not the protocol for agreeing
   on $\Phi$. The natural candidate is the join-point antichain, but servers
   with multiple join and leave cycles have non-trivial frames, and the
   interaction with redaction and history purging is unexamined.

2. **Rejection asymmetry.** Two servers may permanently disagree about an
   event's admissibility. Including rejected events in the reconciled set
   prevents fetch loops but means $\Delta$ never reaches zero if the
   disagreement is genuine and persistent. A quotient construction — reconciling
   equivalence classes rather than events — may be the right formulation, and is
   not developed here.

3. **The floor–eclipse trade.** §10.3.

4. **Optimal $b$.** We fix $b = 256$ for concreteness. The true optimum trades
   level-1 digest size ($\Theta(b)$) against decode work ($\Theta(\Delta^2/b)$)
   and estimator accuracy (degrading as $\Delta/b$ grows), and should probably
   adapt to room size.

5. **Empirical $\Delta$ distribution.** The entire provisioning argument rests
   on the claim that real divergence is small and one-sided in the common case.
   This is plausible and consistent with the failure modes MSC0501 enumerates,
   but it is an empirical claim and has not been measured on federation traffic.
   If $\Delta$ is heavy-tailed in practice, the rateless construction should be
   the baseline rather than the fallback.
