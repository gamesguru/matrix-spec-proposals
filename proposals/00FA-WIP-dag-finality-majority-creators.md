# MSC00FF: DAG Finality via Creator Majority Sign-off

Matrix currently does not enforce Directed Acyclic Graph (DAG) finality: any
participating server can retrospectively append events referencing arbitrary
historical points in the room's history. While this property provides extreme
resilience against arbitrary network partitions, it leaves rooms vulnerable to
"ancient forks"—either intentional (maliciously crafted by compromised servers)
or unintentional (caused by database rollbacks or long-term partitions).

Leveraging the immutable, multilateral creator set introduced in
[MSC4289: Explicitly privilege room creators](4289-privilege-creators.md), this
proposal defines a decentralized finality mechanism. A simple majority of a
room's creators can endorse a specific historical event as a **finality
checkpoint**. Once a checkpoint is established, any new event that is not a
descendant of that checkpoint is rejected by federation authorization rules,
successfully locking the history of the room up to that point and preventing
retroactive changes.

## Proposal

### 1. The Creator Set and Majority Threshold

Under [MSC4289](4289-privilege-creators.md), the set of room creators is
immutable and defined at room creation:

- Let $C$ be the set of creators, consisting of the `sender` of the
  `m.room.create` event and any valid user IDs listed in the create event's
  `content.additional_creators` array.
- Let $N = |C|$ be the total number of creators.
- The simple majority threshold $M$ required for consensus is:
  $$M = \left\lfloor \frac{N}{2} \right\rfloor + 1$$

Examples of majority thresholds:

- **Single Creator ($N = 1$):** $M = 1$. The sole creator can unilaterally
  establish and advance checkpoints.
- **Dual Creators ($N = 2$, e.g. DMs):** $M = 2$. Both parties must sign off on
  any checkpoint, preventing either user from unilaterally locking the other out
  of historical branches.
- **Three Creators ($N = 3$):** $M = 2$. Any two creators can establish or
  advance checkpoints.

### 2. The Finality State Event: `m.room.finality`

Rather than utilizing complex multi-signature cryptographic schemes, consensus
is reached and recorded using standard Matrix state events. We introduce a new
state event type: `m.room.finality`.

- **State Key:** The `sender` (user ID) of the creator.
- **Auth Rules:**
  - The `state_key` of an `m.room.finality` event **MUST** match the `sender` of
    the event.
  - The `sender` **MUST** be a member of the room's creator set $C$ (either the
    create event's `sender` or present in `content.additional_creators`).
  - If these conditions are not met, the event is rejected.

#### Event Schema

The `content` of an `m.room.finality` event contains the following keys:

- `checkpoint_event_id` (string; required): The event ID of the historical event
  being endorsed as the finality checkpoint.
- `depth` (integer; required): The `depth` of the endorsed checkpoint event.
  This is used to optimize validation.

```json
{
  "type": "m.room.finality",
  "state_key": "@alice:example.org",
  "sender": "@alice:example.org",
  "content": {
    "checkpoint_event_id": "$xyz123abc789...",
    "depth": 45210
  }
}
```

### 3. Resolving the Active Finality Checkpoint

A historical event $H$ is considered an **active finality checkpoint** in a
given room state if there exists a subset of creators $S \subseteq C$ such that:

1. The size of the subset is at least the majority threshold: $|S| \ge M$.
2. For each creator $u \in S$, their active `m.room.finality` state event in the
   room state endorses an event $E_u$ such that $E_u = H$ or $E_u$ is a
   descendant of $H$ (i.e. there exists a path of `prev_events` from $E_u$ back
   to $H$ in the room's DAG).

The **Latest Finalized Checkpoint** ($H_{latest}$) is the deepest event in the
DAG that satisfies these conditions.

_Note on advancement:_ Because any descendant of $H$ also implicitly validates
$H$, creators do not need to coordinate to sign off on the exact same event ID.
If Alice signs off on event $X$ and Bob signs off on event $Y$ (which is a
descendant of $X$), then both Alice and Bob are considered to endorse $X$ (since
$Y$ is a descendant of $X$). Thus, $X$ satisfies the majority consensus and
becomes the latest finalized checkpoint.

### 4. Authorization Rules

To enforce DAG finality, the authorization rules for a room version
incorporating this MSC are updated. During event authorization (for federation
and local processing):

1. Retrieve the room state prior to the event $E$ being authorized.
2. Resolve the latest finalized checkpoint $H_{latest}$ based on the
   `m.room.finality` events present in that state.
3. If no active finality checkpoint exists (i.e., fewer than $M$ creators have
   sent valid finality events, or their endorsed events share no common ancestor
   that has majority backing), allow the event to proceed under standard auth
   rules.
4. If an active finality checkpoint $H_{latest}$ exists:
   - The event $E$ **MUST** be a descendant of $H_{latest}$ (there is a path of
     `prev_events` from $E$ back to $H_{latest}$), **OR**
   - The event $E$ **MUST** be $H_{latest}$ itself or an ancestor of
     $H_{latest}$ (to allow backward replication/backfill of historical events
     that occurred prior to finalization).
   - If the event $E$ is on a fork that branches off prior to or concurrently
     with $H_{latest}$ (meaning there is no path of `prev_events` from $E$ to
     $H_{latest}$, and $E$ is not an ancestor of $H_{latest}$), the event
     **MUST** be rejected.

## Potential Issues

### 1. Unreachable or Compromised Creators

If a creator's homeserver goes offline permanently, or their account is
compromised/deactivated, forming a simple majority may become impossible (e.g.,
in a 2-creator room where one goes offline, $M=2$ can never be reached again).

- **Mitigation:** This is an inherent and accepted security trade-off of
  multilateral ownership under MSC4289. If a majority cannot be reached, the
  room continues to operate under standard, non-finalized DAG rules (no new
  checkpoints can be established or advanced). To resolve this, any remaining
  administrator with appropriate power levels can perform a room upgrade,
  establishing a new room version and a revised, active creator set.

### 2. Performance and DAG Traversal

Checking whether an event $E$ is a descendant of $H_{latest}$ requires walking
the DAG via `prev_events`. For highly active rooms with deep DAGs, this walk can
be computationally expensive.

- **Mitigation:**
  1. Derive the checkpoint depth from `checkpoint_event_id`. If the event retains
     a `depth` field, authorization MUST verify that it matches the referenced
     checkpoint's actual depth before using it as a traversal bound.
  2. Homeservers can cache the lineage of finalized checkpoints. Once
     $H_{latest}$ is known and cached, checking whether $E$ is a descendant of
     $H_{latest}$ can be optimized using pre-computed reachability indexes or
     epoch markers.

### 3. Finality Forking/Split-brains during Partitions

If a network partition occurs and different creators reside on different sides
of the partition, they might independently sign off on divergent branches.

- **Mitigation:** A split-brain checkpoint cannot occur if $M > N/2$, because a
  simple majority is strictly non-overlapping. For example:
  - In an $N=3$ room ($M=2$), Alice and Bob can finalize branch A on one side of
    a partition, while Bob and Charlie cannot finalize a divergent branch B on
    the other side unless Bob is part of both. However, Bob's homeserver can
    only hold one active state event for his user ID. Once the partition heals,
    state resolution v2.1 will resolve the state of the room, converging on a
    single active state event for Bob. This will resolve which checkpoint has
    the majority.
  - In an $N=2$ room ($M=2$), no finality can be established during a partition
    because neither side can muster 2/2 creators.

## Alternatives

### 1. Static/Time-based Finality

Instead of active sign-offs, finality could be defined statically (e.g., "any
event older than 30 days is finalized").

- **Why rejected:** Time in decentralized systems is highly unreliable. Network
  partitions lasting longer than the finality window would cause permanent
  split-brains with no path to convergence. Additionally, malicious servers
  could manipulate event timestamps to bypass static finality boundaries.

### 2. Single-creator Finality

Only the primary creator (the sender of the `m.room.create` event) can establish
checkpoints.

- **Why rejected:** This undermines the multilateral decentralization model of
  MSC4289. In joint spaces, such as DMs, both participants should have equal
  say. A single creator could maliciously freeze a room's history to lock out
  other creators.

### 3. Byzantine Fault Tolerant (BFT) Consensuses

Enforce consensus on every individual event using a BFT consensus algorithm
(e.g. Tendermint or Raft) running among the participating servers.

- **Why rejected:** This would fundamentally alter Matrix's architecture,
  removing its asynchronous replication model and adding immense protocol
  complexity, synchronization overhead, and vulnerability to transient server
  outages.

## Security Considerations

### 1. Malicious Creator Majorities

If a majority of creators turn malicious, they could theoretically finalize an
old checkpoint to lock out legitimate room history or force a fork.

- **Mitigation:** Creators are formally defined as the absolute owners of the
  room under MSC4289. If a majority of creators are compromised or malicious,
  the room's security boundary is already breached. Clients should clearly
  indicate to users who the creators of a room are, as they hold ultimate
  administrative authority.

### 2. Validation of Checkpoint Ancestry

A malicious server might send an `m.room.finality` event claiming
`checkpoint_event_id` is an event that does not actually exist or is on an
invalid branch.

- **Mitigation:** The authorization rules require that the checkpoint event
  $H_{latest}$ itself must be validated and known to the verifying server. If a
  server receives an `m.room.finality` event pointing to an unknown event, it
  cannot use that event to establish a checkpoint until the referenced event is
  replicated and authorized.

### 3. Interaction with State Resolution

The `m.room.finality` events are standard state events and are resolved using
standard State Resolution v2.1 (MSC4297). If a state conflict arises (e.g.,
concurrent updates to finality state), the state resolution algorithm will
output a single, deterministic set of active finality events, which ensures all
servers converge on the exact same $H_{latest}$.
