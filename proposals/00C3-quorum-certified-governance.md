# MSC00C3: Quorum-certified governance operations

This proposal defines an experimental, non-standard room version for finalizing
privileged governance operations:

```text
room_version: "tk.nutra.quorum-governance.1"
```

It complements, rather than replaces, causal state resolution such as
[MSC00C2](00C2-cdo-certified-governance.md). A server without this room-version
implementation MUST NOT resolve one of its rooms with ordinary Matrix State
Resolution v2.

## Motivation

A hash-linked DAG proves declared causal references. It cannot prove that an
author did _not_ know an omitted event. Thus these histories are identical to a
resolver:

```text
partition:  A has not received promote(B), then kicks B
withholding: A receives promote(B), omits it, then kicks B
```

Merkle commitments, event hashes, causal-set proofs, and Lamport clocks can
authenticate the declared graph. They cannot recover omitted knowledge or
establish a shared physical-time order.

This proposal makes the policy choice explicit: privileged governance changes
need a threshold certificate from the current governance group. A unilateral
administrator therefore cannot produce a stale kick, demotion, ban, or
power-level mutation after another administrator has acquired authority. The
trade-off is liveness: governance pauses when a threshold cannot communicate.

## Non-goals

- Ordering ordinary messages or all room state.
- Inferring chronology from timestamps, PDU `depth`, graph height, event IDs, or
  receipt order.
- Tolerating loss of a threshold of signing shares.
- Protecting a room after enough participants collude to form a certificate.

## Terminology

**Governance operation** is a covered state change: membership ban, kick,
invite, or leave affecting another user; a power-level or join-rule mutation; or
a governance key-set/threshold change.

**Governance checkpoint** is a certified governance operation that records the
new active epoch, governance participants, threshold, and previous checkpoint
ID. Every governance operation is a checkpoint transition, even when it leaves
the roster and threshold unchanged.

**Certificate** is a threshold signature over one exact operation and its
checkpoint. The room version fixes its message encoding and verification.

**Slot** is `(room_id, checkpoint_id)`. A signer MUST issue at most one
signature share for a slot. This deliberately serializes governance: there is
only one successor operation for an active checkpoint. The durable
non-equivocation lock is what makes a quorum certificate finality, rather than a
vote hint.

## Cryptography and identity

The room version MUST bind every governance participant key to an active Matrix
member. A homeserver key alone is insufficient when several privileged people
share a server.

An initial implementation MAY use a FROST-compatible threshold signature.
D-FROST-style distributed signing and CHURP-style proactive share refresh are
compatible implementation choices, but are not protocol semantics on their own.
Any suite MUST verify the same canonical certificate message and preserve the
slot-lock rule.

For Byzantine fault bound `f`, a deployment seeking Byzantine safety SHOULD use
at least `3f + 1` participants and a threshold of at least `2f + 1`. No claim of
safety is made once an adversary controls a signing threshold.

## Wire format

Every governance operation MUST carry this signed extension:

```json
"tk.nutra.quorum_governance": {
  "version": 1,
  "checkpoint": "$checkpoint_event_id",
  "epoch": 8,
  "slot": ["!room:example.org", "$checkpoint_event_id"],
  "operation_hash": "unpadded_base64url_hash",
  "certificate": "unpadded_base64url_threshold_signature"
}
```

`operation_hash` is the canonical hash of the complete operation: room ID, type,
state key, sender, content, causal/state references, checkpoint ID, epoch, and
slot. It excludes only certificate bytes. A certificate for one target,
checkpoint, or transition MUST NOT validate for another.

The governance event itself becomes the next checkpoint. It contains the
monotonically increasing epoch, previous checkpoint ID, canonical participant
keys, and threshold. The creation rule authorizes the initial checkpoint. Every
later checkpoint, including a participant or threshold change, MUST be certified
by its predecessor checkpoint.

## Admission

Before normal authorization and state resolution, receivers MUST:

1. Verify the checkpoint and its complete certified chain to the initial
   checkpoint.
2. Verify the checkpoint roster, threshold, and member/key bindings.
3. Recompute `operation_hash` and verify the threshold certificate.
4. Verify that `epoch` is exactly the parent checkpoint epoch plus one, and that
   the slot is the unique global slot of the referenced checkpoint.
5. Apply ordinary Matrix authorization against both the operation's causal
   branch and the checkpoint's certified governance state.

Missing checkpoint material is incomplete context. It MUST NOT be treated as
concurrency, authorization, or permission to fall back to an older checkpoint.

## Finality rule

A valid certificate authorizes exactly one successor transition for its parent
checkpoint. Honest signers MUST persist their lock before releasing a signature
share.

Two distinct certified operations for the same parent checkpoint are invalid
unless their operation hashes are identical. With a `2f + 1` threshold from a
`3f + 1` roster, two distinct certificates imply honest-signer non-equivocation
was violated; they are evidence of equivocation, not a normal state-resolution
tie.

After a quorum-certified `grant_admin(B)` advances the checkpoint, a kick or
demotion certified only by the preceding checkpoint is stale and MUST fail
admission. A later kick is ordinary finalized moderation only when a threshold
certifies that exact later operation as the active checkpoint's sole successor.

```text
checkpoint e:   threshold certifies grant_admin(B) --> checkpoint e+1
attacker A:     old-checkpoint-e kick(B)           --> reject: stale
governance set: checkpoint-e+1 kick(B)             --> accept: finalized
```

The rule resolves the dueling-admin ambiguity by refusing to infer chronology
from the DAG. The quorum certificate is the authority boundary.

## Interaction with MSC00C2

MSC00C2 contains the blast radius of a concurrent governance conflict: a rival
revocation cannot make independently authorized actions fail merely because a
synthetic resolver sequence processes it first. It cannot determine which peer's
concurrent governance action is rightful.

This proposal finalizes the narrower governance operation set. A room MAY use
both: CDO/V3 preserves causal isolation for ordinary concurrent state, and this
proposal rejects stale governance branches before they reach V3's deterministic
residue.

## Availability and recovery

The proposal intentionally sacrifices governance availability during a partition
that prevents a threshold from communicating. Allowing partitioned unilateral
governance would recreate the indistinguishability problem.

If a threshold is lost, governance cannot change. Recovery MUST use a separately
authorized social or cryptographic mechanism; a timestamp, server assertion, or
new fork MUST NOT silently replace the threshold.

## Security considerations

- Certificate verification MUST bind exact canonical operation bytes, not an
  untrusted event ID alone.
- Slot locks MUST survive crash recovery; forgetting a lock permits
  equivocation.
- Share refresh does not itself establish ordering or finality.
- A certificate proves group authorization, not benevolence: a quorum may
  legitimately remove an active administrator or lock down admissions.
- This requires a room version. Applying it opportunistically to ordinary Matrix
  rooms causes federation divergence.

## Open questions

1. Device-key enrolment, revocation, and replacement.
2. Exact threshold-signature suite and binary certificate encoding.
3. Scope of finalized operations, especially self-leaves and invitations.
4. Public evidence and remediation for signer equivocation.
5. Test vectors for stale certificates, conflicting slots, threshold loss, share
   refresh, device replacement, and partition recovery.
