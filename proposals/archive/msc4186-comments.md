### SSS Review (MSC4186)

R 107. Sat 27 Jun 2026 -- **5. The O(N) "Always Include Banned Rooms" Trap**

This seems a potential performance bottleneck -- if an account's been
`banned/kicked` from hundreds or thousands of rooms, the server still has to
load and sort all of them? Just to figure out the active window, on every single
long-polling request.

If top-level `membership` becomes required and authoritative for returned rooms,
that at least removes one reason to keep dragging archived rooms back into
active list calculations just to recover missed leaves/bans after a disconnect.

I'd still be curious in follow-up work to see if `SlidingRoomFilter` could grow
a `memberships` array (`filters: { memberships: ["join", "invite"] }`) so
clients can opt out of `banned/kicked` rooms unless they explicitly open an
archived view.

---

R 209. Tue 30 Jun 2026 (11:15 AM) -- **4. The `timeline_limit` Expansion
Paradox**

This seems difficult for the server to accurately maintain and track.

To detect a `timeline_limit` _increase_, the server is forced to heavily cache
previous configs for each client request. If the server loses track, the client
receives no historic events.

Given the removal of "sticky" lists (per Changelog item 10), **clients** should
pass an explicit flag (e.g., `expand_timeline: true`) to intentionally grow the
window. Otherwise, we're forcing the server to memorize past requests and run a
precarious diff... just to figure out if the limit went up.

---

R 477. Sat 27 Jun 2026 -- **3. Deterministic `bump_stamp` Tie-Breaking MUST be
Mandated**

To avoid clients and servers disagreeing on tie-breaks and possibly duplicating
events on the boundary (or missing them entirely), it seems a deterministic tie
breaker, i.e., lexicographically by `room_id`, is needed.

Also, `bump_stamp` never exceeds `2^53 - 1` (which could happen if they use
nanoseconds).

---

R 465. Tue 30 Jun 2026 (11:00 AM) -- **2. The lists Delta Contradiction**

Line 465 seems to have a delta-sync disconnect with line 479.

If `initial: false`, then any _omitted_ field can be assumed unchanged (line
465). But line 479 says omitting `lists` means the room dropped out of all lists
(and is only in the payload due to a subscription).

A single JSON omission _can't_ mean "unchanged" and "empty" at once. If a room
legitimately drops out, the server MUST return an explicit empty array
(`"lists": []`). We need strict key omission to universally signify a lack of
change across the board.

---

R 489. -- **6. StateStub Polymorphism vs. Standard Matrix Deletions**

To handle deleted state, the MSC adds a `StateStub` object that only has `type`
/ `state_key` (no `content`). Parsing `[Event | StateStub]` in strongly-typed
languages (Rust, or `mypy`-strict Python) is tedious/error-prone. Could we
explore alternatives here?

Matrix already has a way to represent cleared state: an event with an empty
`content: {}`; could we drop `StateStub` and use that instead, so client parsers
don't need a special case?

---

R 478. -- **1. Solving the "Left Room" gap & the "Zombie Room" bug** (reply to
Erik's comment on line 106)

<!-- Proofreader marker. -->

I noticed Erik's comment above about clients dropping connections and missing
"Left" rooms (_"I'm not really sure what we can do about that in this API shape
TBH"_).

This ties into a data-retention issue recently reported. If a server purges old
`m.room.member` events (`banned` / `left` rooms), it can't populate the user in
the `required_state`. Since the top-level membership field is optional, clients
(like `matrix-rust-sdk`) might guess and default to `join`. That can resurrect
dead rooms.

Could `membership` just be REQUIRED and authoritative for every returned room
where the server knows the user's current or historical membership
(`join/leave/invite/ban`)? It seems like this should only be omitted for true
peeking / no-membership cases. Servers can populate it from internal membership
tables instead of digging up potentially-purged events.

That'd also fix Erik's edge case above: on a connection reset, a client passes
its known rooms into `room_subscriptions`, and if they were `banned/kicked`
elsewhere, the subscription comes back with an authoritative `membership`
instead of relying on `required_state` inference or full list pagination to
rediscover it.
