# MSC00EF: Matrix Testnet Segregation via Sigil Inversion and Traffic Bypassing

**Authors:** [Your Name/Handle]  
**Date:** 2026-06-16  
**Version:** 1.0  
**Status:** Draft

---

## 1. Introduction

As the Matrix ecosystem grows, developers and server administrators need a globally federated, standardized testnet to evaluate new features, scale-test homeservers, and debug federation issues without risking mainnet stability.

This proposal introduces a parallel Matrix Testnet framework. To ensure zero cross-contamination with the mainnet, this MSC proposes "inverting" standard sigil requirements for testnet entities and implementing explicit traffic bypass mechanisms at the network layer.

## 2. Motivation

Currently, testing federation often involves running isolated local deployments (like Complement) or ad-hoc federations that risk leaking into the public Matrix network if improperly configured. If a test homeserver accidentally connects to a mainnet server, it can pollute databases with dummy users, invalid events, and unresolvable room aliases.

A formal testnet requires strict isolation. By altering the fundamental identifiers (sigils) and the discovery protocol, we can guarantee that mainnet servers will cleanly reject testnet traffic before it consumes significant computational resources.

## 3. Proposal

This MSC proposes a parallel network, "Matrix Testnet," governed by two primary technical alterations:

### 3.1. Sigil Inversion (Namespace Segregation)

Matrix currently uses standard prefixes (sigils) to identify entity types (e.g., `@` for users, `!` for room IDs, `#` for room aliases, `$` for events).

For the testnet, the validation logic is "inverted" by requiring an entirely separate set of sigils. Testnet entities MUST use the following sigils:

- **Users:** `~` (Replaces `@`) — e.g., `~alice:testnet.example.com`
- **Room IDs:** `?` (Replaces `!`) — e.g., `?roomid:testnet.example.com`
- **Room Aliases:** `*` (Replaces `#`) — e.g., `*general:testnet.example.com`
- **Events:** `&` (Replaces `$`) — e.g., `&eventhash:testnet.example.com`

**Enforcement:**

- **Mainnet Homeservers:** MUST reject any identifier beginning with testnet sigils with an `M_INVALID_PARAM` error.
- **Testnet Homeservers:** MUST reject standard mainnet sigils and exclusively accept inverted sigils.

### 3.2. Clever Traffic Bypassing (Federation Layer)

To prevent mainnet servers from even attempting to parse testnet JSON, federation traffic must bypass mainnet infrastructure entirely.

1.  **Distinct SRV Records:** Testnet federation discovery MUST look for `_matrix-testnet._tcp` instead of `_matrix._tcp`. If a domain only publishes a mainnet record, testnet servers will fail to resolve it, preventing accidental connection attempts.
2.  **Custom HTTP Header:** All testnet federation requests MUST include the header `X-Matrix-Network: testnet`.
3.  **Ingress Rejection:** Mainnet homeserver deployments can configure their reverse proxies (Nginx, Traefik, HAProxy) to immediately drop requests containing `X-Matrix-Network: testnet` with a `403 Forbidden` or `421 Misdirected Request` status, bypassing the homeserver application logic entirely.

## 4. Drawbacks

- **Client Compatibility:** Existing Matrix clients will not recognize `~`, `?`, `*`, or `&` as valid Matrix entities. Clients will need a "Testnet Mode" toggle that updates their internal regex parsers to support inverted sigils.
- **Implementation Overhead:** Homeservers (Synapse, Dendrite, Conduit) will need core refactoring to abstract sigil validation rather than hardcoding `@`, `!`, etc.

## 5. Security Considerations

The primary security goal of this MSC is _preventing denial-of-service and state pollution_. By handling the `X-Matrix-Network` header at the reverse-proxy layer, mainnet servers are protected from resource-exhaustion attacks originating from rogue or buggy testnet federations. Furthermore, inverted sigils ensure that even if a payload bypasses the proxy, the homeserver's database constraints and validation logic will immediately reject the malformed IDs.

## 6. Alternatives

- **TLD Restriction:** Restricting the testnet to specific Top Level Domains (e.g., `.test` or `.local`). This was rejected because developers often need to test using real domains and valid TLS certificates.
- **Appservices:** Simulating a testnet via Application Services. This was rejected because it does not adequately test true server-to-server federation mechanics.

## 7. Unresolved Questions

- Should standard Matrix URIs (`matrix:`) be updated to support a network parameter (e.g., `matrix:u/alice:example.com?network=testnet`), or should a new scheme (`matrix-testnet:`) be introduced?
- How should testnet identity servers (managing 3PIDs like email/phone) isolate their namespaces?
