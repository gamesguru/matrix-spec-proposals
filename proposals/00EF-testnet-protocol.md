# MSCXXXX: Matrix Testnet Segregation via Room Versions and Traffic Bypassing

**Authors:** [Your Name/Handle]
**Date:** 2026-06-16
**Version:** 1.4
**Status:** Draft

---

## Introduction

As the Matrix ecosystem grows, developers and server administrators today increasingly rely on the production ecosystem to evaluate new features, scale-test homeservers, and debug federation issues in the aims of stability.

This proposal introduces an extensible, parallel **multi-network framework** to support a `testnet` (a free-for-all developer playground) and a relatively stable `stagenet` (pre-production validation). To prevent cross-contamination with the `mainnet`, this MSC recommends strict application-layer isolation and clean ingress defenses at the network layer.

## Operational philosophy

### The `testnet` (dev playground - networkID: `1`)

The `testnet` operates under a "wild west" philosophy:

- **Anything goes:** This network is designed for abuse. Spam waves, intentional state-resolution forks, malicious federation payloads, and crafted attacks by constructed PDU are permitted within reason.
- **No take backs:** There are no SLAs, no database recovery guarantees, and (generally) no admin interventions. If a new feature corrupts a `testnet` deployment's database, the recommendation is to leave rooms where possible, wipe the database, and restart.
- **Record incidents, keep moving:** Server admins are encouraged to log, profile, and record incidents (such as memory leaks or state-reset bugs) and report them through appropriate channels. Servers should remain running to the extent possible (and not undergo excessive downtime for maintenance).

### The `stagenet` (staging/pre-prod - networkID: `2`)

The `stagenet` operates as a mirror of production, a stricter pre-prod environment:

- **Pre-release validation:** Restricted to validating release candidate software, migration scripts, and stable app integrations before prod deployments.
- **Constructive use only:** Unlike `testnet`, power level attacks, spam waves, and other malicious payloads are strictly prohibited.
- **State preservation:** State is ideally preserved across software upgrades. Wipes are rare and coordinated only around major specification milestones or permitted by smaller instances.

## Motivation

Federation testing currently often involves isolated local setups (`Complement`, internal/non-federated room version tests) or else it involves running half-baked server code that risk corrupting the state of the `mainnet` if improperly implemented or configured (degrading mainnet performance and polluting production databases).

A formal parallel network framework requires strict isolation; we can achieve this by leveraging standard Matrix federation protocol-level validation barriers (room versions and signatures) coupled with HTTP and/or DNS bypasses at the network/kernel layer.

Additionally, to prevent CPU or network overhead on the `mainnet` from "junk" HTTP requests, production deployments can safely drop non-`mainnet` network traffic (at the network or reverse-proxy layer using simple, standard infrastructure patterns).

## Proposal

This MSC proposes a parallel network framework defined by the following specifications:

### Integer network IDs

To distinguish federation traffic across networks, this proposal establishes an integer-based **Network ID** passed via a custom HTTP header `Matrix-Network-Id` with values:

- `0` (or absent): **Mainnet** (Production)
- `1`: **Testnet** (Experimental)
- `2`: **Stagenet** (Staging / Release Candidates)
- `3+`: **Reserved / Private / Local Networks**

Homeservers federating over `testnet` or `stagenet` traffic MUST explicitly include the respective Network ID as an integer value in the `Matrix-Network-Id` HTTP header on all outgoing federation requests (e.g., `Matrix-Network-Id: 1` for traffic on `testnet`).

### Application-layer isolation

To guarantee that production servers efficiently reject non-`mainnet` payloads, the proposal introduces native protocol-level boundaries.

#### Network-specific room versions

All rooms created or federated on parallel networks MUST use a room version string prefixed with their respective network identifier:

- **Testnet Rooms:** MUST use room versions prefixed with `org.matrix.testnet-` (e.g., `org.matrix.testnet-v10`).
- **Stagenet Rooms:** MUST use room versions prefixed with `org.matrix.stagenet-` (e.g., `org.matrix.stagenet-v10`).

**Enforcement:**

- **Mainnet Homeservers:** MUST reject any room-creation, join request, or message payload containing a room version with the `org.matrix.testnet-*` or `org.matrix.stagenet-*` prefix, returning an `M_UNSUPPORTED_ROOM_VERSION` error.
- **Testnet/Stagenet Homeservers:** MUST reject standard `mainnet` room versions (e.g., `"10"`, `"11"`, or any `mainnet` deployed `org.*` room version) and exclusively permit room versions matching their respective network prefix.

_Impact:_ If an event accidentally leaks, `mainnet` homeservers may parse the JSON but will immediately drop the event upon seeing the unsupported room version, eliminating any risk of corruption or significant CPU usage.

#### Room version semantics

To preserve test fidelity and minimize the need for codebase refactors, homeservers MUST natively alias network-specific room versions to their underlying `mainnet` algorithm.

- **Behavior:** A homeserver MUST process a room version prefixed with `org.matrix.testnet-` or `org.matrix.stagenet-` using the exact same algorithmic state-resolution rules, event ID formats, and cryptographic signing schemas as its corresponding standard `mainnet` room version. For example, `org.matrix.testnet-v10` and `org.matrix.stagenet-v10` MUST be processed identically to standard `mainnet` Room Version `10`.

To the extent strict "PDU format adherence" is not possible, it is required that unstable room version suffixes be affixed (e.g., `org.matrix.testnet-org.matrix.mscXXXX`).

#### Separations of concern and root trust

Matrix federation relies on server keys (Ed25519) to sign and authenticate events.

- Testnet and Stagenet homeservers MUST use distinct cryptographic key pairs that are not registered or published on mainnet key servers or DNS records.
- Mainnet homeservers MUST NOT trust or fetch keys from parallel network servers, and parallel network homeservers MUST reject signatures from mainnet server keys, ensuring mutual cryptographic isolation.
- **Default Key Notaries:** Because Matrix homeservers rely on key notaries to verify historical signing keys for offline or unreachable servers, parallel network homeservers MUST NOT query mainnet key notaries. Instead, dedicated fallback notaries must be operated:
  - **Testnet Notary:** `notary.testnet.matrix.org` (exclusive fallback for `testnet`)
  - **Stagenet Notary:** `notary.stagenet.matrix.org` (exclusive fallback for `stagenet`)

### Traffic bypass; negotiation and server discovery

To prevent `mainnet` servers from incurring any TCP handshake, TLS negotiation, or HTTP processing overhead due to accidental parallel network queries, strict discovery and ingress dropping are enforced.

#### "No-fallback" server discovery

Testnet and Stagenet homeservers MUST adhere to a strict discovery algorithm:

1. **Distinct `.well-known` path:**
   - Testnet servers MUST query `/.well-known/matrix/testnet-server` (instead of `server`).
   - Stagenet servers MUST query `/.well-known/matrix/stagenet-server` (instead of `server`).
   - **Schema:** The JSON schema for these parallel `.well-known` endpoints MUST be strictly identical to the standard `/.well-known/matrix/server` file (e.g., returning an `m.server` key mapping to the target host and port).
2. **Distinct SRV Records:**
   - Testnet federation discovery MUST look for `_matrix-testnet-fed._tcp`.
   - Stagenet federation discovery MUST look for `_matrix-stagenet-fed._tcp`.
3. **Halt Discovery:** If discovery fails to resolve a valid destination via either the network-specific `.well-known` endpoint or the network-specific SRV record, the homeserver MUST immediately abort discovery and raise an error.
4. **No Fallback:** Parallel network homeservers MUST NOT fall back to standard Mainnet `.well-known` paths (`/.well-known/matrix/server`), standard `mainnet` SRV records (`_matrix-fed._tcp`), or perform direct IP/port fallback connections on port `8448` or `443`.

_Endpoint constraints:_ These `.well-known` endpoints require no authentication, have no specific rate-limiting requirements, do not apply to guest access, and MUST return an HTTP `404 Not Found` error (with standard `M_NOT_FOUND` errcode) if the requested network is not supported by the host.

_Why this works:_ If a `testnet` server accidentally targets `matrix.org`, it queries the Testnet `.well-known` (returning `404`) and the `testnet` SRV (returning `NXDOMAIN`). Because the server cannot fall back to `mainnet` resolution paths, it immediately halts before opening a TCP connection to the `mainnet` server.

### Client-to-Server (C2S) discovery

To allow clients to securely discover homeservers on parallel networks when triggered via network-specific URIs or custom Client settings:

- **Distinct `.well-known` Client Paths:**
  - Clients operating on the Testnet MUST query `/.well-known/matrix/testnet-client`.
  - Clients operating on the Stagenet MUST query `/.well-known/matrix/stagenet-client`.
- **Schema:** The JSON schema for these endpoints MUST be strictly identical to the standard `/.well-known/matrix/client` file (e.g., returning homeserver base URLs and identity server addresses).
- **No Fallback:** Clients MUST NOT fall back to querying the Mainnet `/.well-known/matrix/client` endpoint when attempting discovery on a parallel network.
- **Endpoint constraints:** Similar to server discovery, these `.well-known` endpoints require no authentication, have no specific rate-limiting requirements, do not apply to guest access, and MUST return an HTTP `404 Not Found` error (with standard `M_NOT_FOUND` errcode) if the requested network is not supported by the host.

#### Ingress protection (Implementation Guide)

Historically, the Matrix specification designated port `8448` for federation. However, **most modern deployments now federate over standard HTTPS port `443`** to easily bypass restrictive corporate and consumer ISP firewalls.

Depending on an administrator's deployment strategy, three highly efficient ingress-dropping architectures can be used to isolate parallel network traffic:

##### Host-level isolation (subdomains/HTTPS)

Since modern servers multiplex federation over port `443`, the best practice is to separate networks by subdomain (e.g., `matrix.org` for `mainnet`, `testnet.matrix.org` for Testnet, and `stagenet.matrix.org` for Stagenet).

- **Mechanism:** Nginx/reverse proxies evaluate the **server name (SNI)** during the initial TLS handshake.
- **Efficiency:** Attempts to send `testnet` traffic to `matrix.org` are rejected at the TLS handshake level before Nginx ever reads or parses HTTP headers or payload bytes, resulting in virtually zero CPU overhead.

##### Port-level isolation (firewalls)

If subdomains are not used and isolation is handled via ports, this MSC defines standard default ports for parallel networks:

- **Mainnet:** Port `443` or `8448`
- **Testnet (ID `1`):** Port `8449`
- **Stagenet (ID `2`):** Port `8450`

- **Mechanism:** Mainnet homeservers do not listen on ports `8449` or `8450`.
- **Efficiency:** The `mainnet` server's host firewall (e.g., `iptables`, `nftables`, or security groups) or OS kernel drops incoming packets immediately at the TCP layer with an `RST` (Reset) packet. This uses zero Nginx CPU cycles, generates zero log noise, and completely avoids user-space processing.

##### Header-level isolation (web server)

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

### Client & URI integration

To prevent users from clicking a `testnet`/`stagenet` link and having it open in their `mainnet` daily-driver client, distinct URI schemes are introduced:

- **Testnet URIs:** MUST use the scheme `matrix-testnet:` (e.g., `matrix-testnet:r/someroom:example.com`).
- **Stagenet URIs:** MUST use the scheme `matrix-stagenet:` (e.g., `matrix-stagenet:r/someroom:example.com`).
- **OS Resolution:** Since operating systems register handlers per URI scheme, this allows developers to install separate client builds (e.g., Element Nightly for Testnet, Element Beta for Staging) which register solely to their respective schemes, completely eliminating UX collisions.

### Ephemeral lifespans

Given the nature of the `testnet`, data accumulation from extreme stress tests will inevitably exhaust volunteer node resources. Therefore, the `testnet` is strictly ephemeral. While this MSC refrains from introducing protocol-level mechanisms for automated state resets, it is highly recommended that the community coordinates periodic database wipes (e.g., out-of-band communication for a bi-annual network reset). Node operators and client developers MUST NOT expect long-term state durability on the `testnet`.

The `stagenet`, however, does not operate on scheduled epochs. Data is preserved indefinitely to support long-term migration testing, with resets occurring only during major specification milestones.

## Potential issues

- **Server-Side Configuration:** Server administrators must maintain separate configuration profiles (e.g., generating separate signing keys, configuring distinct reverse proxy auto-bans, and defining network-specific room version support).
- **Client Implementation:** Clients wishing to support parallel networks must register separate URI handlers (`matrix-testnet:` / `matrix-stagenet:`) and toggle their server selection accordingly.

## Security considerations

The primary security goal of this MSC is _containment_. By utilizing network-specific room versions, `mainnet` servers remain isolated from potential traffic/bandwidth loads or malformed payloads from non-production networks. Furthermore, the cryptographic key separation guarantees that an attacker cannot cross-pollinate event graphs or fork state by introducing `testnet` events into the `mainnet`. The strict no-fallback discovery also prevents resource starvation attacks against production infrastructure.

## Alternatives

- **Sigil Inversion:** Inverting sigils (e.g., `~` for users, `?` for rooms) was proposed to segregate namespaces. This was rejected because of the overhead of forcing homeservers and SDKs to use custom regex parsers, string validators, and DB schemas (completely compromising test fidelity and carrying significant ecosystem-wide refactoring overhead).
- **TLD Restriction:** Restricting parallel networks to specific domains. Rejected due to arbitrary limitations/production collisions.
- **Appservices:** Simulating parallel networks via Application Services. This was rejected because it does not adequately replicate true server-to-server federation mechanics necessary for smoke/stress testing.

## Dependencies

This MSC does not depend on any currently unmerged MSCs.

## Unstable prefix

During the draft and development phase, this proposal uses the following unstable prefixes (replace `XXXX` with the PR number once assigned):

- **Testnet Room Versions:** `org.matrix.mscXXXX.testnet-` (e.g., `org.matrix.mscXXXX.testnet-v10`)
- **Stagenet Room Versions:** `org.matrix.mscXXXX.stagenet-` (e.g., `org.matrix.mscXXXX.stagenet-v10`)
- **HTTP Header:** `Matrix-MSCXXXX-Network-Id`
- **Testnet Server Discovery:** `/.well-known/matrix/mscXXXX.testnet-server`
- **Stagenet Server Discovery:** `/.well-known/matrix/mscXXXX.stagenet-server`
- **Testnet Client Discovery:** `/.well-known/matrix/mscXXXX.testnet-client`
- **Stagenet Client Discovery:** `/.well-known/matrix/mscXXXX.stagenet-client`

Homeservers supporting this framework SHOULD advertise support to clients by adding `"org.matrix.mscXXXX": true` to the `unstable_features` dictionary of their `/_matrix/client/versions` endpoint response.

Once this MSC is approved and merged, these identifiers will be stabilized to their official names without the `mscXXXX` namespace prefix.

## Unresolved Questions

- None.
