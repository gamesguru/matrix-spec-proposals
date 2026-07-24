# Mathematical Foundations of Federation Reconciliation

This note records the mathematical claims supporting MSC0501 and MSC0503. It is
explanatory, not normative. The MSCs define the wire behavior and security
requirements; this note gives the proofs and assumptions behind them.

## 1. Group-Valued Reconciliation

Let `U` be an identifier universe and let `S` be a finite subset of `U`.

### Theorem 1: subtractability

Let

$$
\phi : (\mathcal P(U), \triangle) \to (G,+)
$$

be a homomorphism into an abelian group. Then two peers can compute the digest
of their symmetric difference from their individual digests:

$$
\phi(A \triangle B) = \phi(A) - \phi(B).
$$

**Proof.** In the Boolean group of finite subsets, symmetric difference is the
group operation and every element is its own inverse. Homomorphism preservation
therefore gives

$$
\phi(A \triangle B)=\phi(A)+\phi(B)
=\phi(A)-\phi(B).
$$

The difference digest is computable without enumerating either complete set.
$$\square$$

For a Bloom filter, the operation is bitwise OR. Its codomain is an idempotent
commutative monoid, not a group. There is no inverse for a set bit, so the
residual cannot be computed from two filters alone. The responder must test its
elements individually, making the work depend on population size rather than the
symmetric difference.

## 2. Information Lower Bound

Let identifiers have `ell` bits and suppose the responder sends one message of
`M` bits. The requester must recover a responder-only difference of size `d`.

### Theorem 2: bounded messages must adapt

Any non-adaptive one-message protocol that is correct for every difference of
size `d` requires

$$
M \geq \log_2 {2^\ell \choose d}
\geq d(\ell-\log_2 d).
$$

**Proof.** Set the requester's set to the empty set. The responder's message
must distinguish every possible `d`-element responder set, so the message map
must be injective over `\binom{2^\ell}{d}` possibilities. Therefore

$$
M \geq \log_2 {2^\ell \choose d}.
$$

Using

$$
{n \choose d} \geq (n/d)^d
$$

with `n=2^ell` yields the second inequality. Since `d` is unbounded, no fixed
message size can reconcile every possible difference exactly. A correct system
must adapt by extending its sketch, partitioning the difference, streaming a
rateless representation, or falling back to traversal/backfill. $$\square$$

## 3. Causal Closure

Let `A` and `B` be the event sets held by two peers. Let `par(e)` contain the
`prev_events` and `auth_events` parents of `e`, restricted to the negotiated
frame. A set is closed when it contains the relevant parents of every event it
contains.

### Theorem 3: closure criterion

For `R \subseteq B \\ A`, the recovered set `A \cup R` is closed if and only if

$$
\forall e \in R:\quad
\operatorname{par}(e) \subseteq A \cup R.
$$

**Proof.** If the condition holds, every event in `A` already has its required
parents in `A`, and every event in `R` has its required parents in `A \cup R`.
Thus the union is closed. Conversely, if the union is closed, every parent of
each `e \in R` must belong to the union, which is exactly the stated condition.
$$\square$$

Exact set recovery satisfies the criterion only under these operational
preconditions:

1. the peers use the same frame;
2. the retained event bodies and required auth-chain events are retrievable;
3. returned IDs are fetched as validated PDUs; and
4. rejected tombstones are treated as knowledge of an ID, not as event bodies.

A truncated backward walk generally fails the criterion because it discovers
descendants before their missing ancestors. It transfers information, but it is
not evidence that the DAG has been repaired.

## 4. Bloom Yield on a Causal Graph

Let `D = B \\ A` be the missing set and let `a_e` be the number of ancestors of
`e` that also lie in `D`. Suppose each missing event is independently omitted
with false-positive probability `epsilon`.

### Proposition 4: expected integrable yield

The expected fraction of returned events that can be integrated immediately is

$$
Y = \frac{1}{|D|}\sum_{e\in D}
(1-\varepsilon)^{a_e+1}.
$$

**Proof.** Event `e` is integrable exactly when neither `e` nor any of its
in-difference ancestors is omitted. There are `a_e+1` independently tested
events in that condition, so its probability is `(1-epsilon)^(a_e+1)`. Linearity
of expectation gives the average over `D`. $$\square$$

For an antichain, `a_e=0` and `Y=1-epsilon`. For a causal chain, the yield is

$$
Y_{\mathrm{chain}} =
\frac{1}{\Delta}\sum_{j=1}^{\Delta}(1-\varepsilon)^j.
$$

The chain is the damaging extreme. Yield collapse alone is not an absorbing
failure; repeated rounds can eventually clear a finite chain. A bounded filter
window creates the absorbing failure by making an omitted event permanently
unqueryable.

## 5. Self-Stabilization

Call a state with a nonzero difference absorbing if the probability of reducing
the difference in every future round is zero.

### Theorem 5: sufficient conditions for convergence

Suppose a reconciliation protocol satisfies:

- **H1, full-frame coverage:** every divergent bucket in the negotiated frame is
  examined, rather than only a moving suffix;
- **H2, loud additive failure:** decode failure is observable and increasing
  capacity retains the information already transmitted; and
- **H3, ergodic peer sampling:** every relevant peer is selected with positive
  probability in every sufficiently long sequence of rounds.

If every returned PDU is eventually validated and admitted when its dependencies
are available, then the protocol has no digest-induced absorbing hole and
converges to the negotiated-frame fixed point with probability one, assuming an
honest peer holding each missing PDU remains reachable.

**Proof sketch.** H1 ensures that a missing identifier cannot disappear merely
because it falls outside a suffix window. H2 ensures each failed attempt either
decodes successfully or increases the information available to the next attempt;
additive extension cannot discard a prior residual. H3 implies that a peer
holding any particular missing identifier is eventually sampled with probability
one. Once sampled, repeated bounded exchanges eventually deliver the identifier
and its dependencies. The finite negotiated population then decreases
monotonically until the fixed point is reached. State maps are recomputed
locally after DAG admission and are not copied from peers. $$\square$$

This is a sufficient-condition theorem, not a latency bound. It does not cover
permanent partitions, universal peer dishonesty, or a frame that has expired.
Those cases require frame negotiation or historical backfill.

## 6. Provisioning and Buckets

Let the two one-sided differences be `d^+` and `d^-`. With counts already on the
wire, define

$$
c = \left|\,|A|-|B|\,\right|=|d^+-d^-|.
$$

### Proposition 6: count residual

$$
c \leq \Delta=d^++d^-;
$$

with equality exactly when the difference is one-sided. Thus `c` is an exact
capacity measurement in the common lagging-peer case. If `c < Delta`, the
divergence is two-sided and localization is needed.

For `b` buckets, let `d_j^+` and `d_j^-` be the two sides in bucket `j`. The
bucket count residual

$$
L=\sum_{j=1}^{b}|d_j^+-d_j^-|
$$

satisfies `L <= Delta`, with equality exactly when every bucket is one-sided.
Provisioning can therefore use the count residual for the common path and
independently bounded sketches for differing buckets.

## 7. Room-Wide Synchronization

Pairwise reconciliation and room-wide convergence are different claims. The
pairwise protocol repairs a difference when it samples a peer holding the
missing data. The room-wide claim additionally depends on peer sampling.

With `N` servers and fanout `f`, a standard push-pull epidemic model reaches
room-wide dissemination in logarithmic rounds under uniform independent peer
sampling; the exact constant depends on the model and whether each round is
push, pull, or push-pull. The commonly cited bound

$$
\log_2 N + \ln N + O(1)
$$

is a push-style bound, not a universal pull bound. Push-pull has a different
constant, often expressed as

$$
\log_3 N + O(\log\log N),
$$

under the corresponding random-phone-call assumptions. MSC0501 does not claim
one exact constant; it requires a positive peer-selection floor and treats
fanout as an operational/security parameter.

## 8. Eclipse Probability

Let `p` be the probability that one selected peer is adversarial. If the `f`
selections in a round are independent, the round is fully eclipsed with
probability

$$
P_{\mathrm{ecl}}=p^f.
$$

The number of rounds until a non-eclipsed round is geometric:

$$
\mathbb E[T]=\frac{1}{1-p^f},
\qquad
\Pr[T>\tau]=p^{f\tau}.
$$

For weighted peer selection with a uniform floor,

$$
p=(1-\varepsilon)W_{\mathcal M}+\varepsilon\mu,
$$

where `W_M` is adversarial hub weight and `mu` is the adversarial Sybil
fraction. The floor makes `p<1` whenever the adversary lacks a Sybil majority,
but the tail bound should determine `f`; the mean alone is insufficient for a
security target.
