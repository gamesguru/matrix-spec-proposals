# MSC00EF: Matrix Testnet Segregation via Room Versions and Traffic Bypassing

**Authors:** [Your Name/Handle]
**Date:** 2026-06-16
**Version:** 1.3
**Status:** Draft

---

## Introduction

As the Matrix ecosystem grows, developers and server administrators need globally federated, parallel networks to evaluate new features, scale-test homeservers, and debug federation issues without risking mainnet stability.

This proposal introduces an extensible, parallel **Matrix Multi-Network Framework** designed to support both a highly chaotic **Testnet** (a developer playground) and a highly stable **Stagenet** (pre-production release candidate validation). To ensure zero cross-contamination with the mainnet, this MSC proposes strict application-layer isolation through network-specific room versions and distinct cryptographic trust roots, supplemented by strict, no-fallback server/client discovery and clean ingress defenses at the network layer.

## Operational Philosophy

Before detailing the technical implementation, it is crucial to establish the social and operational expectations of these parallel networks.

### The Testnet (Developer Playground - Network ID: `1`)

The Matrix Testnet operates under a strict "Wild West" philosophy:

- **Anything Goes:** This network is designed for abuse. Spam waves, intentional state-resolution forks, malicious federation payloads, and massive room-join floods are expected and permitted.
- **No Take Backs:** There are no SLAs, no database recovery guarantees, and no administrative interventions. If an experimental feature corrupts a testnet deployment's database, the accepted resolution is to wipe the database and restart.
- **Record Incidents, Keep Moving:** Server admins are encouraged to aggressively log, profile, and record incidents (such as memory leaks or state-reset vulnerabilities) to generate bug reports for mainnet implementations. Document the carnage, patch the software, and keep moving.

### The Stagenet (Pre-Production Staging - Network ID: `2`)

The Matrix Stagenet operates as a high-fidelity mirror of the production mainnet:

- **Pre-Release Validation:** Used solely for validating release candidate software, migration scripts, and stable app integrations before mainnet deployment.
- **Constructive Use Only:** DoS testing, intentional spamming, or malicious payload distribution are strictly prohibited.
- **State Continuity:** State is preserved across software upgrades. Wipes are rare and coordinated only around major specification milestones.

## Motivation

Currently, testing federation often involves running isolated local deployments (like Complement) or ad-hoc federations that risk leaking into the public Matrix network if improperly configured. If a test homeserver accidentally connects to a mainnet server during a deliberate spam wave or extreme load test, it can severely degrade mainnet performance and pollute production databases.

A formal parallel network framework requires strict isolation. Rather than modifying the fundamental identifiers (sigils) which introduces massive parser fragmentation and ecosystem-wide overhead, we can achieve absolute isolation by leveraging standard Matrix protocol-level validation barriers (room versions and cryptographic signatures) coupled with DNS and HTTP bypasses at the federation layer.

Additionally, to prevent CPU or network overhead on mainnet homeservers from processing junk HTTP requests, mainnet deployments can easily drop parallel network traffic at the network or reverse-proxy layer using simple, standard infrastructure patterns.

## Proposal

This MSC proposes a parallel network framework governed by the following technical specifications:

### Extensible Integer Network IDs

To distinguish federation traffic across parallel networks, this proposal establishes an extensible integer-based **Network ID** passed via the custom HTTP header `Matrix-Network-Id` (compliant with RFC 6648 deprecating the `X-` prefix):

- `0` (or absent): **Mainnet** (Production)
- `1`: **Testnet** (Chaos Playground)
- `2`: **Stagenet** (Staging / Release Candidates)
- `3+`: **Reserved / Private / Local Networks**

Testnet and Stagenet homeservers MUST explicitly include their respective Network ID as an integer value in the `Matrix-Network-Id` HTTP header on all outgoing federation requests (e.g., `Matrix-Network-Id: 1` for the Testnet).

### Application-Layer Isolation

To guarantee that mainnet servers cleanly reject parallel network payloads without mutating standard parsers, identifiers (sigils), or database schemas, the framework utilizes native protocol-level boundaries.

#### Network-Specific Room Versions

All rooms created or federated on parallel networks MUST use a room version string prefixed with their respective network identifier:

- **Testnet Rooms:** MUST use room versions prefixed with `org.matrix.testnet-` (e.g., `org.matrix.testnet-v10`).
- **Stagenet Rooms:** MUST use room versions prefixed with `org.matrix.stagenet-` (e.g., `org.matrix.stagenet-v10`).

**Enforcement:**

- **Mainnet Homeservers:** MUST reject any room-creation or join request containing a room version with the `org.matrix.testnet-` or `org.matrix.stagenet-` prefix, returning an `M_UNSUPPORTED_ROOM_VERSION` error.
- **Testnet/Stagenet Homeservers:** MUST reject standard mainnet room versions (e.g., `"10"`, `"11"`) and exclusively permit room versions matching their respective network prefix.

_Impact:_ If an event accidentally leaks, mainnet homeservers parse the JSON natively but immediately reject processing the event upon seeing the unsupported room version, eliminating any risk of state corruption.

#### Room Version Algorithmic Inheritance

To preserve maximum test fidelity and avoid the "mutant codebase" problem, homeservers MUST natively alias network-specific room versions to their underlying mainnet algorithm.

- **Behavior:** A homeserver MUST process a room version prefixed with `org.matrix.testnet-` or `org.matrix.stagenet-` using the exact same algorithmic state-resolution rules, event ID formats, and cryptographic signing schemas as its corresponding standard mainnet room version. For example, `org.matrix.testnet-v10` and `org.matrix.stagenet-v10` MUST be processed identically to standard mainnet Room Version `10`.

#### Cryptographic Key Separation (Distinct Trust Roots)

Matrix federation relies on server keys (Ed25519) to sign and authenticate events.

- Testnet and Stagenet homeservers MUST use distinct cryptographic key pairs that are not registered or published on mainnet key servers or DNS records.
- Mainnet homeservers MUST NOT trust or fetch keys from parallel network servers, and parallel network homeservers MUST reject signatures from mainnet server keys, ensuring mutual cryptographic isolation.
- **Default Key Notaries:** Because Matrix homeservers rely on key notaries to verify historical signing keys for offline or unreachable servers, parallel network homeservers MUST NOT query mainnet key notaries. Instead, dedicated fallback notaries must be operated:
  - **Testnet Notary:** `notary.testnet.matrix.org` (exclusive fallback for Testnet)
  - **Stagenet Notary:** `notary.stagenet.matrix.org` (exclusive fallback for Stagenet)

### Network-Layer Traffic Bypassing & Strict Server Discovery

To prevent mainnet servers from incurring any TCP handshake, TLS negotiation, or HTTP processing overhead due to accidental parallel network queries, strict discovery and ingress dropping are enforced.

#### Strict "No-Fallback" Server Discovery

Testnet and Stagenet homeservers MUST adhere to a strict discovery algorithm:

1. **Distinct `.well-known` path:**
   - Testnet servers MUST query `/.well-known/matrix/testnet-server` (instead of `server`).
   - Stagenet servers MUST query `/.well-known/matrix/stagenet-server` (instead of `server`).
   - **Schema:** The JSON schema for these parallel `.well-known` endpoints MUST be strictly identical to the standard `/.well-known/matrix/server` file (e.g., returning an `m.server` key mapping to the target host and port).
2. **Distinct SRV Records:**
   - Testnet federation discovery MUST look for `_matrix-testnet-fed._tcp`.
   - Stagenet federation discovery MUST look for `_matrix-stagenet-fed._tcp`.
3. **Halt Discovery:** If discovery fails to resolve a valid destination via either the network-specific `.well-known` endpoint or the network-specific SRV record, the homeserver MUST immediately abort discovery and raise an error.
4. **No Fallback:** Parallel network homeservers MUST NOT fall back to standard mainnet `.well-known` paths (`/.well-known/matrix/server`), standard mainnet SRV records (`_matrix-fed._tcp`), or perform direct IP/port fallback connections on port 8448 or 443.

_Why this works:_ If a testnet server accidentally targets `matrix.org`, it queries the testnet `.well-known` (returning 404) and the testnet SRV (returning NXDOMAIN). Because the server cannot fall back to mainnet resolution paths, it immediately halts before opening a TCP connection to the mainnet server.

### Client-to-Server (C2S) Discovery

To allow clients to securely discover homeservers on parallel networks when triggered via network-specific URIs or custom Client settings:

- **Distinct `.well-known` Client Paths:**
  - Clients operating on the Testnet MUST query `/.well-known/matrix/testnet-client`.
  - Clients operating on the Stagenet MUST query `/.well-known/matrix/stagenet-client`.
  - **Schema:** The JSON schema for these endpoints MUST be strictly identical to the standard `/.well-known/matrix/client` file (e.g., returning homeserver base URLs and identity server addresses).
- **No Fallback:** Clients MUST NOT fall back to querying the mainnet `/.well-known/matrix/client` endpoint when attempting discovery on a parallel network.

#### Ingress Traffic Protection (Suggested Implementation Guides)

Historically, the Matrix specification designated port `8448` for federation. However, **most modern deployments now federate over standard HTTPS port `443`** to easily bypass restrictive corporate and consumer ISP firewalls.

Depending on an administrator's deployment strategy, three highly efficient ingress-dropping architectures can be used to isolate parallel network traffic:

##### Host-Level Isolation (Subdomains on Port `443` - Highly Recommended)

Since modern servers multiplex federation over port `443`, the best practice is to separate networks by subdomain (e.g., `matrix.org` for mainnet, `testnet.matrix.org` for testnet, and `stagenet.matrix.org` for stagenet).

- **Mechanism:** Nginx/reverse proxies evaluate the **Server Name Indication (SNI)** during the initial TLS handshake.
- **Efficiency:** Attempts to send testnet traffic to `matrix.org` are rejected at the TLS handshake level before Nginx ever reads or parses HTTP headers or payload bytes, resulting in virtually zero CPU overhead.

##### Port-Level Isolation (Standardized Ports - Zero-Config Firewall Dropping)

If subdomains are not used and isolation is handled via ports, this MSC defines standard default ports for parallel networks:

- **Mainnet:** Port `443` or `8448`
- **Testnet (ID `1`):** Port `8449`
- **Stagenet (ID `2`):** Port `8450`

- **Mechanism:** Mainnet homeservers do not listen on ports `8449` or `8450`.
- **Efficiency:** The mainnet server's host firewall (e.g., iptables, nftables, or security groups) or OS kernel drops incoming packets immediately at the TCP layer with an `RST` (Reset) packet. This uses zero Nginx CPU cycles, generates zero log noise, and completely avoids user-space processing.

##### Header-Level Isolation (Shared Host/Port - Ultra-Simple Nginx Block)

If same host and same port must be shared across parallel networks, administrators can implement a single-line Nginx directive to immediately close the connection upon detecting the parallel network header:

```nginx
server {
    listen 443 ssl;
    server_name matrix.org;

    # If the parallel network header is present, close the TCP connection instantly
    # with zero HTTP response headers or body transmitted
    if ($http_matrix_network_id) {
        return 444;
    }
}
```

_Note on `return 444`:_ The non-standard status code `444` instructs Nginx to instantly teardown the TCP connection without sending standard HTTP error wrappers, saving CPU, egress bandwidth, and worker socket state under extreme testnet loads.

### Client & URI Integration

To prevent users from clicking a testnet/stagenet link and having it open in their mainnet daily-driver client, distinct URI schemes are introduced:

- **Testnet URIs:** MUST use the scheme `matrix-testnet:` (e.g., `matrix-testnet:r/someroom:example.com`).
- **Stagenet URIs:** MUST use the scheme `matrix-stagenet:` (e.g., `matrix-stagenet:r/someroom:example.com`).
- **OS Resolution:** Since operating systems register handlers per URI scheme, this allows developers to install separate client builds (e.g., Element Nightly for Testnet, Element Beta for Staging) which register solely to their respective schemes, completely eliminating UX collisions.

### Ephemeral Lifespans

- **Testnet Epochs:** To ensure volunteer node operators do not run out of disk space from extreme spam waves, the testnet operates on strict **6-month epochs**. Scheduled database resets and network-wide data wipes occur twice a year on **January 1st** and **July 1st** (coordinated as out-of-band community consensus). Upon epoch rollover, server administrators wipe local databases and start clean.
- **Automated Epoch Signaling:** The active epoch integer and next scheduled wipe timestamp MUST be published as a DNS TXT record on the root of the testnet notary domain (e.g., querying `TXT _epoch.testnet.matrix.org` returns `"v=matrix-epoch; epoch=5; next_wipe=1782864000"`). Server administrators MAY query this record periodically; if the published integer exceeds the server's locally stored epoch state, automated local scripts can dynamically halt the daemon, trigger a database wipe, update the local state, and restart clean with zero human intervention.
- **Stagenet Durability:** The Stagenet does not operate on scheduled epochs. Data is preserved indefinitely to support long-term migration testing, with resets occurring only during major specification milestones.

## Drawbacks

- **Server-Side Configuration:** Server administrators must maintain separate configuration profiles for parallel networks (e.g., generating separate signing keys, configuring distinct reverse proxy auto-bans, and defining network-specific room version support).
- **Client Implementation:** Clients wishing to support parallel networks must register separate URI handlers (`matrix-testnet:` / `matrix-stagenet:`) and toggle their server selection accordingly.

## Security Considerations

The primary security goal of this MSC is _containment_. By utilizing network-specific room versions, mainnet servers are cryptographically and logically protected from state-resolution attacks or malformed payloads originating from parallel networks.

Furthermore, the combination of strict "No-Fallback" server discovery and the Nginx dynamic IP auto-ban configuration protects mainnet servers from resource-exhaustion, TCP connection starvation, or JSON-parsing attacks, preserving CPU and memory under extreme testnet loads.

## Alternatives

- **Sigil Inversion (Draft 1.0):** Inverting sigils (e.g., `~` for users, `?` for rooms) was proposed to segregate namespaces. This was rejected because it introduces a "Mutant Codebase" problem—forcing homeservers and SDKs to use custom regex parsers, string validators, and DB schemas, which completely compromises test fidelity. It also carries astronomical ecosystem-wide refactoring overhead.
- **TLD Restriction:** Restricting parallel networks to specific Top Level Domains (e.g., `.test` or `.local`). This was rejected because developers often need to test using real-world DNS routing and valid TLS certificates.
- **Appservices:** Simulating parallel networks via Application Services. This was rejected because it does not adequately replicate true server-to-server federation mechanics necessary for stress testing.

## Unstable Prefixes

During the draft and development phase, this proposal uses the following unstable prefixes (replace `00EF` with the PR number once assigned):

- **Testnet Room Versions:** `org.matrix.msc00ef.testnet-` (e.g., `org.matrix.msc00ef.testnet-v10`)
- **Stagenet Room Versions:** `org.matrix.msc00ef.stagenet-` (e.g., `org.matrix.msc00ef.stagenet-v10`)
- **HTTP Header:** `Matrix-MSC00EF-Network-Id`
- **Testnet Server Discovery:** `/.well-known/matrix/msc00ef.testnet-server`
- **Stagenet Server Discovery:** `/.well-known/matrix/msc00ef.stagenet-server`
- **Testnet Client Discovery:** `/.well-known/matrix/msc00ef.testnet-client`
- **Stagenet Client Discovery:** `/.well-known/matrix/msc00ef.stagenet-client`

Once this MSC is approved and merged, these identifiers will be stabilized to their official names without the `msc00ef` namespace prefix.

## Unresolved Questions

- None.
