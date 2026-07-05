# MSC 4499: Strict signing key caching and `KeyID` uniqueness

<!--
[Rendered](https://github.com/gamesguru/matrix-spec-proposals/blob/proposals/4499-notary-caching.md)

Implementation (pending revision of this proposal's draft)

_feat: add server key notary endpoints by gamesguru · Pull Request #75 · gamesguru/continuwuity_
https://github.com/gamesguru/continuwuity/pull/75https://github.com/gamesguru/continuwuity/pull/75
-->

Because the specification lacks a strict caching contract, new homeserver
implementations often attempt to be "helpful." Without explicit guidance,
developers may design flexible caches that store multiple key bodies for a
single Key ID and perform trial verification—testing a signature against every
known key body until one passes.

While some legacy implementations (like Synapse) avoid this purely due to rigid
database schemas that happen to enforce a unique `(server_name, key_id)`
constraint, the protocol itself does not forbid the loop. This ambiguity
introduces a catastrophic CPU-exhaustion DoS vector for any implementation that
attempts to gracefully handle key collisions.

## The Rule: First Seen Wins

A Key ID (`algorithm:key_id`) MUST map to exactly one public key body for a
given remote server. This is a strict, permanent 1:1 binding.

When a server observes a key response (whether fetched directly or via a notary)
presenting a new key body for an already-cached Key ID, it MUST:

1. **Retain the original key.** The first observed key body remains
   authoritative. The conflicting key MUST NOT replace it.
2. **Log the collision.** The receiver SHOULD log the collision loudly to alert
   the operator of a remote misconfiguration.
3. **Never perform trial verification.** The server MUST NOT cache the new key
   alongside the old one.

This First Seen Wins rule intentionally causes a localized DAG divergence. Peers
that cached the original key will reject new events; peers that boot up later
and only see the new key will accept them. This is the unavoidable consequence
of out-of-band key resolution in a distributed system. We are not trying to
prevent the split; we are making it deterministic. Localized isolation is the
correct cryptographic punishment for an admin violating the protocol by reusing
a Key ID.

## Admin Startup Guardrails

To prevent an administrator from accidentally poisoning their own federation
standing, homeserver implementations SHOULD proactively refuse to boot if they
detect a local key collision.

If the server's currently configured signing key body differs from what was
previously persisted in the database for that Key ID, the server MUST halt
startup and emit a fatal error. The admin must either restore the original
private key or assign a novel Key ID.

Because total database loss circumvents local startup checks, homeservers SHOULD
ensure that default Key ID generation incorporates high-entropy or timestamped
components (e.g., `ed25519:a7B_93k` rather than `ed25519:auto`). If keys are
regenerated after a wipe, a novel Key ID is structurally guaranteed, entirely
bypassing the federation collision problem.

## Manual cache eviction

Because the 1:1 binding is permanent, a successful TOFU poisoning attack (or a
remote admin who refuses to fix their config) results in permanent federation
failure with that specific server.

Homeservers MUST provide a manual, operator-gated escape hatch (e.g., an Admin
API or CLI command) to evict the cached key-body bindings for a specific remote
server name. This allows a human sysadmin to break the binding and re-initiate
TOFU. This eviction MUST trigger high-priority audit logging. Under no
circumstances should this eviction be automatable or triggerable via inbound
federation traffic.
