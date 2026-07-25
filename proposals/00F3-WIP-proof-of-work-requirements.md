# MSC0F03: Configurable Room Proof-of-Work Requirements

Spam, Sybil join-floods, and automated abuse are severe and ongoing threats to
public federated rooms in the Matrix ecosystem. Mitigating these attacks
historically required relying on centralized CAPTCHA services, third-party
identity verification, or restrictive room invite-only settings—all of which
compromise user privacy, introduce centralized friction, or place an
unsustainable burden on room administrators.

This proposal specifies a native, decentralized, and cryptographic anti-spam
mechanism: **configurable, room-level Proof-of-Work (PoW) requirements**. By
introducing a new room state event, `m.room.proof_of_work_requirement`, room
administrators can enforce that any event sent to a room must contain a valid
cryptographic proof of computational effort. This increases the physical,
financial, and time cost of automated spam and Sybil campaigns by orders of
magnitude, making distributed attacks economically unviable.

Crucially, this design focuses on protocol-level security, addressing key
distributed-systems requirements:

1. **Asymmetric Verification Cost:** Proof of work is moderately hard to compute
   but near-instantaneous to verify, preventing verification-based Denial of
   Service (DoS) attacks on verifying homeservers.
2. **Precomputation Resistance:** The cryptographic puzzle incorporates dynamic
   DAG state (the room's current tips), forcing computations to happen in
   real-time and preventing offline pre-mining.
3. **Client-Server API Decoupling:** The client solves the puzzle using only
   client-visible data, avoiding the "catch-22" of server-side Protocol Data
   Unit (PDU) fields.
4. **Room Versioning and Redaction Survival:** Implemented as a core Room
   Version update where the PoW payload survives redactions.
5. **Infrastructure Protection:** Supports Power Level (PL) exemptions to
   protect application services, bridges, and moderators.

---

## Proposal

### 1. Cryptographic Primitive: Cuckoo Cycle

To achieve strict **asymmetric verification**, this MSC mandates the use of
**Cuckoo Cycle** (specifically, the bipartite graph cycle-finding algorithm
designed by John Tromp).

- **How it works:** Finding a cycle of length $L = 42$ in a bipartite graph of
  $2^N$ nodes requires a memory-hard search with a configurable memory footprint
  (e.g., 64 MiB or 512 MiB), making it highly resistant to GPU/ASIC
  acceleration.
- **Asymmetry:** While the sender must spend considerable memory and CPU cycles
  to traverse the graph and find a cycle, a verifying homeserver can confirm the
  validity of the cycle in negligible CPU time (requiring exactly 42 hash
  lookups) and **zero memory overhead**, entirely closing verification DoS
  vectors.

---

### 2. The Configuration State Event: `m.room.proof_of_work_requirement`

A room's proof-of-work policy is configured via a state event of type
`m.room.proof_of_work_requirement` with an empty `state_key` (`""`).

#### Event Schema

The `content` of the state event contains the following fields:

- `algorithm` (string; required): The hashing algorithm required. Supported
  values:
  - `"cuckoo_cycle"`: The bipartite graph cycle-finding algorithm.
- `parameters` (object; optional): Required if `algorithm` is `"cuckoo_cycle"`:
  - `edge_bits` (integer; required): The bipartite graph size parameter $N$
    where the number of edges is $2^{N}$ (e.g., `19` for $2^{19}$ edges,
    requiring approx. 32 MiB of memory to solve).
- `requirements` (object; required): Mapping of event categories or types to
  their respective difficulties:
  - Each key is an event type (e.g., `"m.room.member"`, `"m.room.message"`) or
    the wildcard `"*"` (applying to all event types not explicitly listed).
  - The value is an object containing:
    - `difficulty` (integer; required): The suffix hash difficulty or
      probability threshold required on the generated block (e.g., requiring the
      first $K$ bits of the block hash to be zero).
    - `exempt_power_level` (integer; optional): Any user whose active Power
      Level (PL) in the room is equal to or greater than this value is exempt
      from the PoW requirement. Defaults to `0`.

#### Example Event Content

```json
{
  "type": "m.room.proof_of_work_requirement",
  "state_key": "",
  "sender": "@admin:example.org",
  "content": {
    "algorithm": "cuckoo_cycle",
    "parameters": {
      "edge_bits": 19
    },
    "requirements": {
      "m.room.member": {
        "difficulty": 16,
        "exempt_power_level": 0
      },
      "*": {
        "difficulty": 10,
        "exempt_power_level": 1
      }
    }
  }
}
```

In this room, sending an `m.room.member` event (e.g., joining or inviting)
requires solving a 16-bit Cuckoo Cycle puzzle for any user with $\text{PL} < 0$.
Standard messages (`*`) require a 10-bit difficulty, but any user with
$\text{PL} \ge 1$ (moderators, bridges, or admin-approved users) is completely
exempt.

---

### 3. Dynamic Salt (Preventing Precomputation)

To prevent attackers from pre-mining valid proof-of-work solutions offline and
unleashing them simultaneously, the puzzle challenge MUST incorporate dynamic,
real-time room state.

The **Dynamic Challenge** ($CH$) is a 32-byte SHA-256 hash computed over the
concatenation of:

1. The `room_id` of the target room.
2. The `sender` user ID of the sending client.
3. The latest tip (event ID) of the room's DAG (known as the current
   `prev_event` tips).
4. A coarse wall-clock timestamp epoch ($T_{epoch}$), computed as the current
   Unix timestamp in seconds integer-divided by 3600 (creating a 1-hour validity
   window).

$$
CH = \text{SHA256}(\text{RoomID} \mathbin{\Vert} \text{SenderID}
\mathbin{\Vert} \text{TipEventIDs} \mathbin{\Vert} T_{epoch})
$$

Because an attacker cannot predict the hashes of future room tips, they cannot
precompute a proof of work in advance. Computation must happen in real-time,
matching the actual state of the room at the moment of submission.

---

### 4. Client-Server Decoupling (PDU Preservation)

To avoid the "catch-22" where a client cannot compute a hash over top-level PDU
routing fields (which only the homeserver assigns at PDU creation), the proof of
work is decoupled from the server-specific canonical JSON.

The client solves the Cuckoo Cycle puzzle over a **Client Intent Payload**
($IP$):

$$
IP = \text{SHA256}(\text{CanonicalJSON}(\text{event.content})
\mathbin{\Vert} \text{event.type} \mathbin{\Vert} CH)
$$

The client iterates a 64-bit `nonce` inside their intent until they find a
Cuckoo Cycle of length 42 in the graph seeded by $IP$. Once solved, the client
attaches the proof to a reserved, top-level metadata object: `m.proof_of_work`.

#### Client-Server API Submission Schema

```json
{
  "type": "m.room.message",
  "content": {
    "body": "Hello world!",
    "msgtype": "m.text"
  },
  "m.proof_of_work": {
    "challenge": "0x3f5c2d...",
    "nonce": 98452104,
    "proof": [14, 521, 982, 1024, "...", 42_node_indices]
  }
}
```

The homeserver receives this payload via the CS-API, performs a zero-memory
validation, and embeds the `m.proof_of_work` block directly into the federated
PDU. Because the proof depends on the client-supplied intent payload rather than
server-side routing keys (like `prev_events` or `depth`), the proof remains
perfectly valid during federation transit.

---

### 5. Room Version and Redaction Whitelist

This MSC introduces a new room version (e.g., **Room Version 13**). Rooms of
older versions cannot process these authorization rules.

#### Redaction Preservation

In Room Version 13, the top-level `m.proof_of_work` key is added to the
**Redaction Preserve Whitelist** (alongside fields like `event_id`, `sender`,
`room_id`, and `hashes`).

When an event is redacted, the core message content is stripped, but the
`m.proof_of_work` block **MUST NOT** be deleted. This ensures that any
homeserver that joins the room later and backfills historical redacted events
can still audit and verify that the original sender performed the necessary
computation, preventing state validation failures during backfill.

---

### 6. Authorization Rules for Room Version 13

When a homeserver processes or replicates an event $E$ in a Room Version 13
room:

1. Retrieve the room state prior to the event $E$.
2. Check for the presence of an active `m.room.proof_of_work_requirement` state
   event.
3. If present:
   - Fetch the sender's active Power Level. If
     $\text{PL} \ge \text{exempt\_power\_level}$ for $E$'s event type, bypass
     validation and allow the event.
   - Verify that $E$ contains a top-level `m.proof_of_work` object. If missing,
     **reject the event**.
   - Reconstruct the expected dynamic challenge $CH$ using the state's latest
     DAG tips and the epoch window.
   - Verify that the client's `proof` forms a valid cycle of length 42 in the
     Cuckoo Graph seeded by:
     $IP = \text{SHA256}(\text{CanonicalJSON}(E.\text{content})
     \mathbin{\Vert} E.\text{type} \mathbin{\Vert} CH)$
   - Verify that the double SHA-256 hash of the cycle proof block contains at
     least `difficulty` leading zero bits.
   - If any verification step fails, **reject the event**.

---

## Potential Issues & Mitigations

### 1. Mobile Client Battery Consumption

Computing Cuckoo Cycle proofs can be heavy for low-end mobile devices.

- **Mitigation:** Room operators should balance security with usability. A
  difficulty requirement for standard messages should be configured low (taking
  less than 1 second on a standard smartphone). High difficulties should be
  reserved for high-value membership changes (joins), which happen infrequently
  and where the slight latency is an acceptable anti-abuse trade-off.

### 2. Time Synchronization Skew

Because the dynamic salt incorporates $T_{epoch}$ (a coarse 1-hour window),
homeservers with minor clock drift could calculate slightly different epochs.

- **Mitigation:** Verifying servers MUST accept proofs computed under either the
  current epoch $T_{epoch}$ or the immediate previous epoch $T_{epoch}-1$,
  providing a robust 1-hour grace window for clock drift.

---

## Alternatives Considered

### 1. Argon2id (Symmetric Cost)

Using Argon2id as the proof-of-work algorithm.

- **Why rejected:** Argon2id is a symmetric Key Derivation Function. Verifying
  an Argon2id solution takes the exact same computational resources (CPU and
  RAM) as generating it. This enables an asymmetric DoS attack where a malicious
  sender floods a homeserver with invalid nonces, instantly exhausting the
  server's resources.

### 2. Time-Based Global Rate Limiting

Enforcing rate limits at the state-resolution layer.

- **Why rejected:** Easily bypassed by Sybil attacks where an attacker
  distributes their traffic across millions of cheap virtual servers and
  thousands of fake Matrix accounts. Only physical, unforgeable computational
  proof can constrain distributed Sybil resources.
