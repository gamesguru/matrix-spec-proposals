# MSC00EF: Matrix Testnet Segregation via Sigil Inversion and Traffic Bypassing

**Authors:** [Your Name/Handle]  
**Date:** 2026-06-16  
**Version:** 1.0  
**Status:** Draft

---

## Introduction

As the Matrix ecosystem grows, developers and server administrators need a globally federated, standardized testnet to evaluate new features, scale-test homeservers, and debug federation issues without risking mainnet stability.

This proposal introduces a parallel Matrix Testnet framework. It is explicitly designed as a **developer playground**—a chaotic, consequence-free environment to push the protocol to its absolute limits. To ensure zero cross-contamination with the mainnet during these high-stress operations, this MSC proposes "inverting" standard sigil requirements for testnet entities and implementing explicit traffic bypass mechanisms at the network layer.

## Operational Philosophy: The Developer Playground

Before detailing the technical implementation, it is crucial to establish the social and operational expectations of this network. The Matrix Testnet operates under a strict "Wild West" philosophy:

- **Anything Goes:** This network is designed for abuse. Spam waves, intentional state-resolution forks, malicious federation payloads, and massive room-join floods are expected and permitted.
- **No Take Backs:** There are no SLAs, no database recovery guarantees, and no administrative interventions to save a broken homeserver. If an experimental feature corrupts a testnet deployment's database, the accepted resolution is to wipe the database and restart.
- **Record Incidents, Keep Moving:** The purpose of the chaos is discovery. Server admins are encouraged to aggressively log, profile, and record incidents (such as memory leaks caused by spam waves or state-reset vulnerabilities) to generate bug reports for mainnet implementations. However, the network itself will not be paused or moderated to address these incidents. Document the carnage, patch the software, and keep moving.

## Motivation

Currently, testing federation often involves running isolated local deployments (like Complement) or ad-hoc federations that risk leaking into the public Matrix network if improperly configured. If a test homeserver accidentally connects to a mainnet server during a deliberate spam wave or extreme load test, it can severely degrade mainnet performance and pollute production databases.

A formal testnet requires strict isolation. By altering the fundamental identifiers (sigils) and the discovery protocol, we can guarantee that mainnet servers will cleanly drop testnet traffic before it consumes significant computational resources, safely containing the playground's chaos.

## Proposal

This MSC proposes a parallel network, "Matrix Testnet," governed by two primary technical alterations:

### Sigil Inversion (Namespace Segregation)

Matrix currently uses standard prefixes (sigils) to identify entity types (e.g., `@` for users, `!` for room IDs, `#` for room aliases, `$` for events).

For the testnet, the validation logic is "inverted" by requiring an entirely separate set of sigils. Testnet entities MUST use the following sigils:

- **Users:** `~` (Replaces `@`) — e.g., `~alice:testnet.example.com`
- **Room IDs:** `?` (Replaces `!`) — e.g., `?roomid:testnet.example.com`
- **Room Aliases:** `*` (Replaces `#`) — e.g., `*general:testnet.example.com`
- **Events:** `&` (Replaces `$`) — e.g., `&eventhash:testnet.example.com`

**Enforcement:**

- **Mainnet Homeservers:** MUST reject any identifier beginning with testnet sigils with an `M_INVALID_PARAM` error.
- **Testnet Homeservers:** MUST reject standard mainnet sigils and exclusively accept inverted sigils.

### Clever Traffic Bypassing (Federation Layer)

To prevent mainnet servers from even attempting to parse testnet JSON during massive spam waves, federation traffic must bypass mainnet infrastructure entirely.

1. **Distinct SRV Records:** Testnet federation discovery MUST look for `_matrix-testnet._tcp` instead of `_matrix._tcp`. If a domain only publishes a mainnet record, testnet servers will fail to resolve it, preventing accidental connection attempts.
2. **Custom HTTP Header:** All testnet federation requests MUST include the header `X-Matrix-Network: testnet`.
3. **Ingress Rejection:** Mainnet homeserver deployments can configure their reverse proxies (Nginx, Traefik, HAProxy) to immediately drop requests containing `X-Matrix-Network: testnet` with a `403 Forbidden` or `421 Misdirected Request` status, bypassing the homeserver application logic entirely.

## Drawbacks

- **Client Compatibility:** Existing Matrix clients will not recognize `~`, `?`, `*`, or `&` as valid Matrix entities. Clients will need a dedicated "Testnet Mode" toggle that updates their internal regex parsers to support inverted sigils.
- **Implementation Overhead:** Homeservers (Synapse, Dendrite, Conduit) will need core refactoring to abstract sigil validation rather than hardcoding `@`, `!`, etc.

## Security Considerations

The primary security goal of this MSC is _containment_. Because the testnet explicitly permits DoS attempts, spam waves, and malformed data generation, protecting the mainnet is paramount.

By handling the `X-Matrix-Network` header at the reverse-proxy layer, mainnet servers are protected from resource-exhaustion attacks originating from the testnet. Furthermore, inverted sigils ensure that even if a payload bypasses the proxy layer, the homeserver's core database constraints and validation logic will immediately reject the malformed IDs without attempting expensive state resolution.

## Alternatives

- **TLD Restriction:** Restricting the testnet to specific Top Level Domains (e.g., `.test` or `.local`). This was rejected because developers often need to test using real-world DNS routing and valid TLS certificates.
- **Appservices:** Simulating a testnet via Application Services. This was rejected because it does not adequately replicate true server-to-server federation mechanics necessary for stress testing.

## Unresolved Questions

- Should standard Matrix URIs (`matrix:`) be updated to support a network parameter (e.g., `matrix:u/alice:example.com?network=testnet`), or should a new scheme (`matrix-testnet:`) be introduced?
- Given the ephemeral nature of the network, should there be an automated, protocol-level mechanism for testnet homeservers to broadcast periodic database purges or state-resets?
