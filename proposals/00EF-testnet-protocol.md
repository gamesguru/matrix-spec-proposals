# MSCXXXX: Matrix Testnet Segregation via Room Versions and Traffic Bypassing

<!--
[Rendered](https://github.com/gamesguru/matrix-spec-proposals/blob/guru/testnet-protocol/proposals/00EF-testnet-protocol.md)

Server impl: **TBD**

Complement tests: **TBD**

Client changes limited/optional but recommended (register `matrix-testnet:` / `matrix-stagenet:` URI schemes and network-specific client discovery).
-->

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

#### Ingress Header Validation & Logging

To ensure consistent network isolation and identify misconfigured nodes, homeservers MUST validate the `Matrix-Network-Id` header on all incoming federation requests as follows:

- **Parallel Network (Testnet/Stagenet) Homeservers:** Upon receiving an incoming federation request lacking the `Matrix-Network-Id` header, or containing a header value that does not match their configured network ID, the homeserver MUST reject the request at the HTTP layer, returning an HTTP `400 Bad Request` with an `M_INVALID_NETWORK` error code (unstable: `org.matrix.mscXXXX.invalid_network`). This prevents accidental mainnet or cross-network leaks before processing any payloads.
- **Mainnet Homeservers:** Because production traffic natively lacks this header, mainnet homeservers MUST NOT log warnings or reject requests when the header is absent. However, if a mainnet homeserver receives an incoming request containing a non-zero parallel network ID (e.g., `1` or `2`), it SHOULD log a rate-limited warning to assist operators in identifying misconfigured peer nodes.

### Application-layer isolation

To guarantee that production servers efficiently reject non-`mainnet` payloads, the proposal introduces native protocol-level boundaries.

#### Network-specific room versions

All rooms created or federated on parallel networks MUST use a room version string prefixed with their respective network identifier:

- **Testnet Rooms:** MUST use room versions prefixed with `org.matrix.testnet-` (e.g., `org.matrix.testnet-v10`).
- **Stagenet Rooms:** MUST use room versions prefixed with `org.matrix.stagenet-` (e.g., `org.matrix.stagenet-v10`).

**Enforcement:**

- **Mainnet Homeservers:** MUST reject any room-creation, join request, or message payload containing a room version with the `org.matrix.testnet-*` or `org.matrix.stagenet-*` prefix, returning an HTTP `400 Bad Request` with an `M_UNSUPPORTED_ROOM_VERSION` error code.
- **Testnet/Stagenet Homeservers:** MUST reject standard `mainnet` room versions (e.g., `"10"`, `"11"`, or any `mainnet` deployed `org.*` room version) and exclusively permit room versions matching their respective network prefix, returning an HTTP `400 Bad Request` with an `M_UNSUPPORTED_ROOM_VERSION` error code upon receiving mainnet room version payloads.

_Impact:_ If an event accidentally leaks, `mainnet` homeservers may parse the JSON but will immediately drop the event upon seeing the unsupported room version, eliminating any risk of corruption or significant CPU usage.

#### Room version semantics

To preserve test fidelity and minimize the need for codebase refactors, homeservers MUST natively alias network-specific room versions to their underlying `mainnet` algorithm.

- **Behavior & Adoption Timeline:** A homeserver MUST process a room version prefixed with `org.matrix.testnet-` or `org.matrix.stagenet-` using the identical algorithmic state-resolution rules, event ID formats, and cryptographic signing schemas as its corresponding standard `mainnet` room version. For example, `org.matrix.testnet-v10` and `org.matrix.stagenet-v10` MUST be processed identically to standard `mainnet` Room Version `10`.

  Parallel networks SHOULD automatically adopt new stable mainnet room versions as their basis within 30 days of the mainnet room version stabilizing in the Matrix specification, creating the corresponding prefixed alias (e.g., `org.matrix.testnet-v11` corresponding to Room Version `11`).

- **PDU Format Escape Hatch:** To allow for testing radical experiments (e.g., custom state resolution engines or experimental signature formats) where strict PDU format adherence is not possible, unstable room version suffixes MAY be appended (e.g., `org.matrix.testnet-org.matrix.mscXXXX`).

  To invoke this escape hatch, the modification MUST meet specific incompatibility conditions (e.g., containing structural JSON alterations that would otherwise cause a standard mainnet parser to crash or throw signature validation errors), and MUST be formally registered as an unstable MSC prefix in the public directory rather than using ad-hoc unregistered suffixes.

#### Separations of concern and root trust

Matrix federation relies on server keys (Ed25519) to sign and authenticate events.

- Testnet and Stagenet homeservers MUST use distinct cryptographic key pairs that are not registered or published on mainnet key servers or DNS records.
- Mainnet homeservers MUST NOT trust or fetch keys from parallel network servers, and parallel network homeservers MUST reject signatures from mainnet server keys, ensuring mutual cryptographic isolation.

* **Default Key Notaries:** Because Matrix homeservers rely on key notaries to verify historical signing keys for offline or unreachable servers, parallel network homeservers MUST NOT query mainnet key notaries. Instead, dedicated fallback notaries must be operated:
  - **Testnet Notary:** `notary.testnet.matrix.org` (exclusive fallback for `testnet`)
  - **Stagenet Notary:** `notary.stagenet.matrix.org` (exclusive fallback for `stagenet`)

  **Outage Handling & Redundancy:**
  - **Fallback Chain:** Parallel networks SHOULD define secondary and tertiary backup notaries (e.g., `notary2.testnet.matrix.org`) in their configuration files to provide redundancy.
  - **Timeout & Retry Behavior:** If a primary parallel notary is unreachable, query attempts MUST time out after 10 seconds. Homeservers MUST retry the query following an exponential backoff loop with a maximum of 3 retries over a 1-hour window before declaring a transient outage.
  - **Failure Mode (Strict Containment):** If all configured parallel network notaries are offline or return verification failures, key verification MUST fail immediately (hard failure). Homeservers MUST NOT fall back to mainnet notaries or bypass key verification, ensuring that the strict "no-fallback" isolation policy is maintained even under catastrophic notary outages.

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

#### Discovery Error Classification & Handling

To prevent implementation divergence and ensure strict, consistent isolation during transient network disruptions, homeservers MUST handle discovery failures according to the following error classification:

| Failure Mode               | Description / Type                                                                 | Expected Behavior     | Handling Details                                                                                                                                                                      |
| :------------------------- | :--------------------------------------------------------------------------------- | :-------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **HTTP 404**               | Parallel network `.well-known` endpoint returns HTTP 404 (Not Found).              | **Halt Immediately**  | The requested parallel network is not supported. Homeserver MUST immediately abort discovery and raise an error; do not fall back.                                                    |
| **HTTP 5xx / TCP Timeout** | Parallel network `.well-known` endpoint queries time out or return a server error. | **Retry with Limits** | Transient server-side issue. Homeserver MAY retry the query following standard exponential backoff (up to 3 times or for a maximum of 1 hour) before halting and raising an error.    |
| **DNS NXDOMAIN**           | Parallel network SRV records query resolves to NXDOMAIN (non-existent domain).     | **Halt Immediately**  | The requested parallel network SRV records do not exist. Homeserver MUST immediately abort discovery and raise an error; do not fall back.                                            |
| **DNS Timeout**            | DNS queries for SRV records or `.well-known` domains time out.                     | **Retry with Limits** | Transient network or DNS issue. Homeserver MAY retry the query following standard DNS resolver timeouts and retry limits (e.g., up to 3 retries) before halting and raising an error. |

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

These port assignments are recommended defaults and administrative conventions, rather than strict protocol-level requirements. Administrators are free to configure alternative ports provided proper network-level isolation is maintained. Because core protocol-level containment (the `Matrix-Network-Id` header and strict discovery/room version validation) operates independently of port selection, full interoperability and network isolation are guaranteed regardless of the specific ports chosen.

- **Mechanism:** Mainnet homeservers do not listen on ports `8449` or `8450`.
- **Efficiency:** The `mainnet` server's host firewall (e.g., `iptables`, `nftables`, or security groups) or OS kernel drops incoming packets immediately at the TCP layer with an `RST` (Reset) packet. This uses zero Nginx CPU cycles, generates zero log noise, and completely avoids user-space processing.

##### Header-level isolation (web server)

If the same host and port must be shared across parallel networks, administrators can implement header-filtering rules to immediately terminate incoming connections upon detecting a parallel network ID header.

_Note on Nginx `return 444`:_ The non-standard status code `444` is an Nginx-specific directive instructing the server to instantly tear down the TCP connection without sending standard HTTP response headers or wrappers, saving CPU, egress bandwidth, and socket worker state under extreme parallel network loads.

Depending on the edge reverse proxy, administrators can configure equivalent connection termination behaviors:

###### Nginx

```nginx
server {
    listen 443 ssl;
    server_name matrix.org;

    # Terminate the connection instantly if the header is present
    if ($http_matrix_network_id) {
        return 444;
    }
}
```

###### Apache HTTP Server

For true connection termination/dropping, Apache requires ModSecurity (`mod_security`) with the `drop` action to instantly tear down the TCP connection. Alternatively, standard `mod_rewrite` can be used to return a `403 Forbidden` response, though it does not instantly drop the TCP connection.

**Using ModSecurity (Recommended for dropping):**

```apache
# Requires ModSecurity (mod_security). Instantly drops/tears down the TCP connection
SecRule REQUEST_HEADERS:Matrix-Network-Id "@rx ." \
    "id:100001,phase:1,drop,nolog,msg:'Parallel network traffic dropped'"
```

**Using mod_rewrite (Fallback, returns 403 Forbidden):**

```apache
# Requires mod_rewrite. Returns an HTTP 403 Forbidden response (does not terminate TCP)
RewriteEngine On
RewriteCond %{HTTP:Matrix-Network-Id} . [NC]
RewriteRule ^ - [F]
```

###### Caddy

```caddy
# Match the presence of the header and abort/terminate the connection instantly
@parallel_traffic {
    header Matrix-Network-Id *
}
abort @parallel_traffic
```

###### HAProxy

```haproxy
# Silent-drop / close connection at TCP layer if parallel network header matches
acl is_parallel_network req.hdr(Matrix-Network-Id) -m found
http-request silent-drop if is_parallel_network
```

###### Envoy

```yaml
# Envoy route action configuration to terminate with local direct reply
routes:
  - match:
      prefix: "/"
      headers:
        - name: "Matrix-Network-Id"
          present_match: true
    direct_response:
      status: 403
      body:
        inline_string: "Parallel network traffic blocked."
```

### Client & URI integration

To prevent users from clicking a `testnet`/`stagenet` link and having it open in their `mainnet` daily-driver client, distinct URI schemes are introduced:

- **Testnet URIs:** MUST use the scheme `matrix-testnet:` (e.g., `matrix-testnet:r/someroom:example.com`).
- **Stagenet URIs:** MUST use the scheme `matrix-stagenet:` (e.g., `matrix-stagenet:r/someroom:example.com`).
- **OS Resolution:** Since operating systems register handlers per URI scheme, this allows developers to install separate client builds (e.g., Element Nightly for Testnet, Element Beta for Staging) which register solely to their respective schemes, eliminating UX collisions.

  **Platform-Specific Nuances & Limitations:**
  - **Native Mobile Clients:** URI scheme registration acts as a recommended best practice rather than an ironclad protocol-level guarantee. Mobile operating systems manage scheme registrations via distinct metadata manifests (e.g., `Info.plist` on iOS, `AndroidManifest.xml` on Android) which may support different arbitrary scheme resolutions.
  - **OS App-Choice Dialogs:** If a user installs multiple native clients configured for the same parallel network (e.g., both Element Nightly and a custom test client registering `matrix-testnet:`), the OS will present standard app-selection or disambiguation dialogs rather than cleanly launching a single default application.
  - **Web-Based Clients:** Web applications (like Element Web) cannot natively register arbitrary OS-level URI schemes. Instead, web-based clients rely on traditional HTTP-based `.well-known` C2S discovery paths to route the user's connection to the appropriate homeserver endpoints.

### Ephemeral lifespans

Given the nature of the `testnet`, data accumulation from extreme stress tests will inevitably exhaust volunteer node resources. Therefore, the `testnet` is strictly ephemeral.

While this MSC refrains from introducing protocol-level mechanisms for automated state resets, periodic database wipes MUST be formally coordinated and announced via designated community channels.

- **Coordination Channel:** Reset schedules and epoch rollovers MUST be formally announced via a dedicated public Matrix room (e.g., `#testnet-announcements:testnet.matrix.org`) or published on a formal testnet status feed (such as `status.testnet.matrix.org`).
- **Client-Side Cache Invalidation:** When a testnet reset occurs, cached cryptographic keys and room state databases become completely invalid.
  - **Invalidation Trigger:** Clients operating on the `testnet` MUST invalidate all local database caches (including room states, historical message timelines, member lists, and device keys) upon detecting that the homeserver epoch has reset (e.g., encountering events signed by deprecated/untrusted testnet keys, or when receiving key-verification failures on existing timelines).
  - **Behavior:** When encountering such invalidation triggers, clients MUST discard the affected timeline and request fresh state directly from the homeserver's Client-Server API.

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
- **Error Code:** `org.matrix.mscXXXX.invalid_network` (stabilizes to `M_INVALID_NETWORK`)
  - **HTTP Status:** `400 Bad Request`
  - **Definition:** Indicates that an incoming federation request either lacks the `Matrix-Network-Id` header required by the configured parallel network or specifies an incorrect/mismatched network ID.
  - **Justification:** Standard Matrix error codes (e.g., `M_UNSUPPORTED`, `M_INVALID_PARAM`, or `M_UNKNOWN`) are insufficient to clearly isolate network-routing or network-isolation violations from normal application-level parameter errors or unsupported features. Using a dedicated error code enables federating homeservers and client SDKs to explicitly detect network configuration issues, log them accurately, and prevent silent routing issues or misdiagnosed protocol/endpoint failures.

Homeservers supporting this framework SHOULD advertise support to clients by adding `"org.matrix.mscXXXX": true` to the `unstable_features` dictionary of their `/_matrix/client/versions` endpoint response.

Once this MSC is approved and merged, these identifiers will be stabilized to their official names without the `mscXXXX` namespace prefix.

## Appendix: Administrative Room Cloning & State Population

To facilitate certain testing scenarios, administrators and developers may wish to populate test servers with existing production room data. Because parallel networks enforce strict cryptographic separation and do not trust `mainnet` signing keys, administrators can use two standard administrative methodologies to clone room state:

### Client-Side State Translation (For Client & Widget Integration Testing)

This method is recommended for testing client features, widgets, or application-layer integrations where exact historical signatures and server domains are not critical.

- **Import:** An administrative bot or script queries the production room state via the Client-Server API (`/rooms/{roomId}/state`), translates all user ID and server domain namespaces (e.g., mapping `@alice:matrix.org` to `@alice:testnet-matrix.org`), and creates a brand-new room on the `testnet` using the _corresponding_ network-specific room version (e.g., `org.matrix.mscXXXX.testnet-v10`).
- **VPNs:** To simulate activity from translated third-party domains without deploying separate servers (and creating separate signing keys), administrators can register a local Application Service (AS) on their `testnet` homeserver to act as a virtual proxy for those namespaces.

### Database Seeding & Local Key Spoofing (For Server & Federation Scale Testing)

This method is recommended for testing homeserver scale-limits, state-resolution performance, and database migrations where preserving the exact production DAG, user IDs, and timeline is required.

- **DAG Rewriting:** An offline migration script takes a snapshot of a `mainnet` database and rewrites the room versions to their parallel network equivalents. Because event IDs are cryptographic hashes of the event content (which now contains a parallel room version), the script recalculates all event IDs in topological order, updating the `prev_events` and `auth_events` references down the chain.
- **Trust Injection:** Rather than attempting to forge signatures for non-existent domains, the administrator injects dummy signing keys for the associated `mainnet` domains directly into their `testnet` homeserver's local key cache database (e.g., Synapse's `server_signature_keys` table). When the server validates the imported timeline, it finds the "cached" dummy keys locally, verifies the signatures, and completely bypasses any outbound DNS or notary lookups.

## Unresolved Questions

- None.
