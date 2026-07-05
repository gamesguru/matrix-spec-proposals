# MSC0F04: Mandatory Lexicographic Production Room SemVer Format

**Authors:** [Your Name/Handle]
**Date:** 2026-07-03
**Version:** 1.0
**Status:** Draft

---

## Introduction

In the Matrix protocol, room versions govern the authorization rules, state
resolution algorithms, and event formatting within a room. Currently, stable
production room versions are designated by simple sequential integers (e.g.,
`"1"`, `"2"`, `"10"`, `"11"`, `"12"`).

While sequential integers are simple, they do not allow homeservers, clients,
or application services to negotiate incremental, backward-compatible updates
(such as minor feature additions, localized event schema tweaks, or internal
bug fixes) without performing a full room upgrade. In the current protocol,
any change to room-version-gated behavior requires a complete room upgrade.
Room upgrades are highly disruptive: they require closing the existing room
DAG, inviting all members to a new room, recreating aliases, and re-syncing
room state.

To address this, we propose introducing Semantic Versioning (SemVer 2.0.0) to
future stable room versions. However, standard SemVer strings (e.g.,
`"1.10.0"` vs. `"1.2.0"`) are not lexicographically sortable. Standard
string comparisons would incorrectly evaluate `"1.2.0"` as greater than
`"1.10.0"` (since `"2"` > `"1"` in ASCII character comparison). This forces
homeservers and database backends to parse and decompose version strings
before performing comparisons or sorting, adding computational overhead and
creating vectors for validation discrepancies.

This proposal specifies a mandatory, zero-padded, lexicographically sortable
SemVer format (`vMMMM.NNNN.PPPP`) for all future production-ready, stable
room versions. This enables homeservers, database queries, and clients to
compare room versions using native, extremely fast string comparisons.

---

## Proposal

All future stable room versions (starting from the version following the
adoption of this MSC) MUST adhere to a strict, fixed-width, zero-padded
SemVer format. This format is designed to be directly compatible with
standard lexicographical string comparison.

### 1. Version String Format

A compliant room version string MUST match the following format exactly:

$$\mathbf{v[MAJOR].[MINOR].[PATCH]}$$

Where:

- `v` is a literal lowercase character prefix.
- `[MAJOR]` is a 4-digit, zero-padded decimal integer (representing the
  major version, `0000` to `9999`).
- `[MINOR]` is a 4-digit, zero-padded decimal integer (representing the
  minor version, `0000` to `9999`).
- `[PATCH]` is a 4-digit, zero-padded decimal integer (representing the
  patch version, `0000` to `9999`).

Each field MUST be separated by a literal period (`.`).

#### Regex Validation Pattern

```regex
^v[0-9]{4}\.[0-9]{4}\.[0-9]{4}$
```

#### Examples

- **Version 1.0.0** is formatted as: `v0001.0000.0000`
- **Version 1.2.3** is formatted as: `v0001.0002.0003`
- **Version 1.10.0** is formatted as: `v0001.0010.0000`
- **Version 12.0.4** is formatted as: `v0012.0000.0004`

---

### 2. Lexicographical Sorting and Precedence

The main advantage of this format is that standard lexicographical (byte-wise
or Unicode code-point) string comparison matches semantic version precedence
perfectly.

For any two compliant version strings $A$ and $B$:

- $A < B$ (i.e., $A$ is an older/lesser version than $B$) if and only if $A$
  lexicographically sorts before $B$ as a raw string.
- $A > B$ (i.e., $A$ is a newer/greater version than $B$) if and only if $A$
  lexicographically sorts after $B$ as a raw string.
- $A = B$ if and only if $A$ and $B$ are identical strings.

#### Example Comparison

Compare `v0001.0002.0003` (SemVer 1.2.3) and
`v0001.0010.0000` (SemVer 1.10.0):

- Standard string sorting: `"v0001.0002.0003"` sorts before
  `"v0001.0010.0000"`.
- This correctly yields $1.2.3 < 1.10.0$.

This eliminates the need for homeserver implementations to perform complex
runtime string splitting, integer conversions, or custom version-parsing
logic during database queries (such as SQL `ORDER BY` or
`WHERE room_version > ?`) or state validation loops.

---

### 3. Room Upgrade and Compatibility Rules

By introducing SemVer-structured room versions, we establish clear semantic
rules for room state and event processing compatibility:

#### Major Version Bumps (`MMMM` changes)

- Indicates breaking changes to event authorization rules, state resolution
  algorithms, or cryptographic signing requirements.
- **Action:** Upgrading a room to a new major version requires a standard,
  disruptive room upgrade (creating a new room DAG and migrating state).

#### Minor Version Bumps (`NNNN` changes)

- Indicates backward-compatible feature additions (e.g., supporting a new
  optional state event type, relaxing validation rules, or introducing
  optional fields).
- **Action:** A room can be upgraded to a new minor version **without** a
  full room upgrade. Homeservers that support the minor version can process
  and produce the new features, while older homeservers (which only support the
  same major version up to an older minor version) can safely ignore the
  unrecognized optional fields or treat them under standard fallback rules,
  without causing a split-brain consensus fork.

#### Patch Version Bumps (`PPPP` changes)

- Indicates backward-compatible bug fixes or clarifications in parsing,
  rendering, or local validation behaviors that do not affect the
  consensus-critical auth DAG.
- **Action:** Fully compatible; does not require any room-level action.

---

### 4. Unstable and Experimental Room Versions

This MSC does not restrict the format of unstable, local, or experimental
room versions. Implementations may continue to use custom strings (such as
`org.matrix.mscXXXX` or reverse-DNS notation) to test experimental room
versions.

However, once a room version transitions to a **stable, production-ready**
specification in the Matrix protocol, it MUST be assigned an identifier
complying with this lexicographic SemVer format.

---

## Potential issues

### 1. Maximum Limits

The 4-digit padding restricts the maximum version numbers to
`9999.9999.9999`. Given the speed of spec progression (the protocol has
reached room version 12 in approximately 10 years), a limit of 9,999 major
versions is effectively infinite and poses no realistic risk of exhaustion.

### 2. Room Version String Length

Representing room versions as 15-character strings (e.g., `v0001.0002.0003`)
increases storage requirements by a negligible amount (approx. 13 additional
bytes per room creation event) compared to short strings like `"1"`. The
performance gains from fast, native database string indexing and comparison
far outweigh this overhead.

---

## Alternatives

### 1. Standard SemVer Strings without Padding

Under this alternative, stable room versions would use standard SemVer strings
(e.g., `"1.2.3"` and `"1.10.0"`).

- **Disadvantage:** Simple string sorting fails (e.g., `"1.2.3"` > `"1.10.0"`
  lexicographically). Homeservers and database engines would be forced to
  implement custom parsing, sorting, and indexing mechanisms. This increases
  complexity, increases the likelihood of developer bugs, and prevents native
  database index sorting.

### 2. Date-based Versioning (e.g., `YYYY.MM.DD`)

Stable room versions could be versioned by their release date.

- **Disadvantage:** Date-based version strings do not convey semantic
  compatibility. It would be impossible to determine whether a change from
  `2026.01.15` to `2026.03.12` represents a minor compatible feature addition
  or a major breaking rewrite of the state resolution algorithm.

---

## Security considerations

### 1. Validation and Injection Prevention

Homeservers MUST strictly validate the room version string against the regular
expression `^v[0-9]{4}\.[0-9]{4}\.[0-9]{4}$` before using it in any file
system, database query, or execution path. Failing to validate the string
could lead to SQL injection, path traversal, or resource exhaustion attacks if
malformed or excessively long strings are accepted.

### 2. Consensus Divergence Prevention

By formalizing minor/patch updates without room upgrades, there is a risk
that a homeserver running older software (e.g., supporting only
`v0001.0000.0000`) might process a room updated to `v0001.0001.0000`
differently than a newer homeserver, potentially leading to state
divergence.
To prevent this, any specification utilizing minor version bumps must
explicitly define the fallback behavior for older implementations (e.g.,
mandate that unknown event fields are preserved but not evaluated in the
core authorization loop).

---

## Unstable prefix

While this proposal is in draft status, experimental implementations of this
specification should use an unstable room version ID prefixed with
`org.matrix.msc0F04` (e.g., `org.matrix.msc0F04.v0001.0000.0000`).

---

## Dependencies

This proposal does not depend on any outstanding, unaccepted MSCs.
