# MSC00EF: Matrix Testnet Segregation via Room Versions and Traffic Bypassing

**Authors:** [Your Name/Handle]
**Date:** 2026-06-16
**Version:** 1.2
**Status:** Draft

---

## Introduction

As the Matrix ecosystem grows, developers and server administrators need a globally federated, standardized testnet to evaluate new features, scale-test homeservers, and debug federation issues without risking mainnet stability.

This proposal introduces a parallel Matrix Testnet framework. It is explicitly designed as a **developer playground**—a chaotic, consequence-free environment to push the protocol to its absolute limits. To ensure zero cross-contamination with the mainnet during these high-stress operations, this MSC proposes strict application-layer isolation through testnet-specific room versions and distinct cryptographic trust roots, supplemented by strict, no-fallback server discovery and traffic bypass mechanisms at the network layer.

## Operational Philosophy: The Developer Playground

Before detailing the technical implementation, it is crucial to establish the social and operational expectations of this network. The Matrix Testnet operates under a strict "Wild West" philosophy:

- **Anything Goes:** This network is designed for abuse. Spam waves, intentional state-resolution forks, malicious federation payloads, and massive room-join floods are expected and permitted.
- **No Take Backs:** There are no SLAs, no database recovery guarantees, and no administrative interventions to save a broken homeserver. If an experimental feature corrupts a testnet deployment's database, the accepted resolution is to wipe the database and restart.
- **Record Incidents, Keep Moving:** The purpose of the chaos is discovery. Server admins are encouraged to aggressively log, profile, and record incidents (such as memory leaks caused by spam waves or state-reset vulnerabilities) to generate bug reports for mainnet implementations. However, the network itself will not be paused or moderated to address these incidents. Document the carnage, patch the software, and keep moving.

## Motivation

Currently, testing federation often involves running isolated local deployments (like Complement) or ad-hoc federations that risk leaking into the public Matrix network if improperly configured. If a test homeserver accidentally connects to a mainnet server during a deliberate spam wave or extreme load test, it can severely degrade mainnet performance and pollute production databases.

A formal testnet requires strict isolation. Rather than modifying the fundamental identifiers (sigils) which introduces massive parser fragmentation and ecosystem-wide overhead, we can achieve absolute isolation by leveraging standard Matrix protocol-level validation barriers (room versions and cryptographic signatures) coupled with DNS and HTTP bypasses at the federation layer.

Additionally, to prevent CPU or network overhead on mainnet homeservers from processing junk HTTP requests, testnet-configured servers must fail early at the discovery layer before a TCP handshake or TLS negotiation ever occurs.

## Proposal

This MSC proposes a parallel network, "Matrix Testnet," governed by the following technical specifications:

### Application-Layer Isolation

To guarantee that mainnet servers cleanly reject testnet payloads without mutating standard parsers, identifiers (sigils), or database schemas, the network utilizes native protocol-level boundaries.

#### Testnet-Specific Room Versions

All rooms created or federated on the testnet MUST use a room version string prefixed with `org.matrix.testnet-` (e.g., `org.matrix.testnet-v10`).

- **Mainnet Homeservers:** MUST reject any room-creation or join request containing a room version with the `org.matrix.testnet-` prefix, returning an `M_UNSUPPORTED_ROOM_VERSION` error.
- **Testnet Homeservers:** MUST reject standard mainnet room versions (e.g., `"10"`, `"11"`) and exclusively permit `org.matrix.testnet-` prefixed versions.
- **Impact:** If a testnet event or room accidentally leaks to the mainnet, mainnet homeservers will parse the JSON natively but immediately reject processing the event upon seeing the unsupported room version, eliminating any risk of state corruption.

#### Cryptographic Key Separation (Distinct Trust Roots)

Matrix federation relies on server keys (Ed25519) to sign and authenticate events.

- Testnet homeservers MUST use distinct cryptographic key pairs that are not registered or published on mainnet key servers or DNS records.
- Mainnet homeservers MUST NOT trust or fetch keys from testnet-only servers, and testnet homeservers MUST reject signatures from mainnet server keys, ensuring mutual cryptographic isolation.
- **Default Testnet Notary Key-Server:** Because Matrix homeservers rely on key notaries to verify historical signing keys for offline or unreachable servers, testnet homeservers MUST NOT query mainnet key notaries (such as `matrix.org`). Instead, the testnet community will operate a default notary at `notary.testnet.matrix.org`. Testnet homeservers MUST configure this address as their exclusive fallback notary.

### Network-Layer Traffic Bypassing & Strict Server Discovery

To prevent mainnet servers from even attempting to parse testnet JSON during massive spam waves, federation traffic must bypass mainnet infrastructure entirely at the transport and DNS layer.

#### Strict "No-Fallback" Server Discovery

To prevent mainnet servers from incurring any TCP handshake, TLS negotiation, or HTTP processing overhead due to accidental testnet queries, testnet homeservers MUST adhere to a strict discovery algorithm:

1. **Distinct `.well-known` path:** Instead of querying `/.well-known/matrix/server`, testnet homeservers MUST query `/.well-known/matrix/testnet-server`.
2. **Halt Discovery:** If discovery fails to resolve a valid destination via either the `/.well-known/matrix/testnet-server` endpoint or the `_matrix-testnet-fed._tcp` SRV record, the homeserver MUST immediately abort discovery and raise an error.
3. **No Fallback:** Testnet homeservers MUST NOT fall back to standard mainnet `.well-known` paths (`/.well-known/matrix/server`), standard mainnet SRV records (`_matrix-fed._tcp` or `_matrix._tcp`), or perform direct IP/port fallback connections on port 8448 or 443.

_Why this works:_ If a testnet server accidentally targets a mainnet domain (e.g., `matrix.org`), it will query the testnet `.well-known` (returning 404) and the testnet SRV (returning NXDOMAIN). Because the server cannot fall back to mainnet resolution paths, it immediately halts. Zero TCP connections are opened, and zero HTTP overhead reaches mainnet servers.

#### Transport & Proxy Defense-in-Depth

1. **Distinct SRV Records:** Testnet federation discovery MUST look for `_matrix-testnet-fed._tcp`. This allows server administrators to optionally isolate testnet traffic to a dedicated port (e.g., `TCP 8449`) at the DNS layer if desired, without mandating non-standard ports that could be blocked by firewalls.
2. **Custom HTTP Header:** All testnet federation requests MUST include the HTTP header `X-Matrix-Network: testnet`.
3. **Ingress Rejection:** Mainnet homeserver deployments can configure their reverse proxies (Nginx, Traefik, HAProxy) to immediately drop requests containing `X-Matrix-Network: testnet` with a `403 Forbidden` or `421 Misdirected Request` status, safeguarding the application layer.

### Client & URI Integration (`matrix-testnet:`)

To prevent users from clicking a testnet link and having it open in their mainnet daily-driver client, this proposal introduces a distinct URI scheme for testnet resources.

- **URI Scheme:** The scheme `matrix-testnet:` MUST be used for testnet URIs (e.g., `matrix-testnet:r/someroom:example.com`).
- **OS Resolution:** Since operating systems register handlers per URI scheme, this allows developers to install a dedicated testnet client (e.g., Element Nightly) which registers solely to `matrix-testnet:`, completely eliminating UX collisions.

### Ephemeral Lifespans and Testnet Epochs

Given the ephemeral nature of a testnet, periodic database resets are necessary to prevent state bloat and performance degradation under heavy load-testing.

- **No Protocol-Level Purge broadcasts:** Building an automated, protocol-level "purge" command introduces a critical remote execution or DoS vulnerability and is explicitly rejected.
- **6-Month Testnet Epochs:** To ensure volunteer node operators do not run out of disk space from spam waves and state-resolution forks, the testnet operates on strict **6-month epochs**. Scheduled database resets and network-wide data wipes occur twice a year on **January 1st** and **July 1st** (coordinated as out-of-band community consensus). Upon epoch rollover, server administrators wipe local databases and restart with a clean slate.

## Drawbacks

- **Server-Side Configuration:** Server administrators must maintain separate configuration profiles for testnet and mainnet deployments (e.g., generating separate signing keys, configuring fallback key notaries, and defining testnet-specific room version support).
- **Client Implementation:** Clients wishing to support the testnet must register a separate URI handler for `matrix-testnet:` and toggle their server selection accordingly.

## Security Considerations

The primary security goal of this MSC is _containment_. By utilizing testnet-specific room versions, mainnet servers are cryptographically and logically protected from state-resolution attacks or malformed payloads originating from the testnet.

Furthermore, the combination of strict "No-Fallback" server discovery and reverse-proxy dropping of `X-Matrix-Network: testnet` headers protects mainnet servers from resource-exhaustion, TCP connection starvation, or JSON-parsing attacks, preserving CPU and memory under extreme testnet loads.

## Alternatives

- **Sigil Inversion (Draft 1.0):** Inverting sigils (e.g., `~` for users, `?` for rooms) was proposed to segregate namespaces. This was rejected because it introduces a "Mutant Codebase" problem—forcing homeservers and SDKs to use custom regex parsers, string validators, and DB schemas, which completely compromises test fidelity. It also carries astronomical ecosystem-wide refactoring overhead.
- **TLD Restriction:** Restricting the testnet to specific Top Level Domains (e.g., `.test` or `.local`). This was rejected because developers often need to test using real-world DNS routing and valid TLS certificates.
- **Appservices:** Simulating a testnet via Application Services. This was rejected because it does not adequately replicate true server-to-server federation mechanics necessary for stress testing.

## Unresolved Questions

- What specific mechanism should the community use to signal epoch rollovers (e.g., a simple designated DNS TXT record or out-of-band mailing lists)?
