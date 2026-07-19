# MSC45XX: Topological peek/query API via sparse fieldsets and Merkleized metadata

Currently the Matrix protocol relies on fetching entire events to perform
backfills or otherwise retrieve previous or missing events. Often we do not know
the shape of the graph we are traversing, or whether it is a dead end.

This proposal seeks to remedy such inefficiencies and blockades by allowing
homeservers to return customized queries of highly granular data, including:

- `prev_event` edges, up to a recursion limit.
- `origin` for a missing event (potentially useful for retrieving it).
