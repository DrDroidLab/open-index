# Roadmap

## Purpose and status

This document records possible future work. Every item below is **not
implemented**, is **not a delivery commitment**, and has **no promised date**.
For behavior that ships today, use the [README](./README.md).

## Standard provenance and distillation manifests

Future support could provide built-in immutable source and run metadata,
lineage, output history, and audit support for ingestion and AI-distillation
workflows. Today, provenance fields and immutable run manifests are
user-defined or maintained in external systems; Droid Brain does not generate
or validate them.

## Conflict and write control

Potential work includes contradiction handling, patch/merge operations, version
preconditions, optimistic concurrency, writer coordination, and historical
versions. Today, a same-ID write replaces the complete current entity and its
outgoing relationships; concurrent writes are last-write-wins. Read,
construct, and write is a completeness practice, not concurrency control.

## Lifecycle and reconciliation

Potential lifecycle controls include delete and tombstone APIs, full-source
reconciliation, TTL, stale-record cleanup, temporal filters, decay, and
recency ranking. Current connector runs update the records they emit; an
omitted source record is not inferred to be deleted.

## Search controls

Potential search controls include type-level boosts and **SQLite-native vector
indexing**. OpenSearch k-NN semantic search is available now; SQLite semantic
search currently uses a bounded brute-force scan rather than a native vector
index.

## Policy-aware authorization and data segregation

First-class policy-aware authorization and data segregation are future work,
not a security footnote. A complete design needs all of the following:

- authenticated human and service principals with verifiable claims;
- deny-by-default RBAC and/or ABAC policy evaluation;
- explicit tenant/workspace boundaries and a backend/index isolation strategy;
- permissions for exact reads, search, writes, doc-type and schema changes,
  connector and service operations, and administration;
- policy filtering before candidate ranking, vector, keyword, or hybrid
  scoring, result counts, aggregations, or navigation examples are produced;
- prevention of incoming and outgoing relationship or traversal leakage,
  including edges to hidden entities;
- connector and service identities, destination scopes, write ownership, and
  schema-write scopes;
- durable authorization and audit logs with explainable allow/deny outcomes;
- secure policy propagation through embeddings and indexes, exports, backups,
  reindexing, re-embedding, and migration paths; and
- migration from existing unscoped brains, backend-parity coverage, and
  adversarial no-leak tests.

Field-level redaction is possible later scope only after resource-level policy
enforcement and leak prevention are correct.

## Benchmarks and compatibility

Future benchmark and compatibility work could publish reproducible corpora and
queries; quality measures; latency and throughput methodology; hardware,
backend, and model disclosure; and versioned results. No benchmark suite or
published results exist today.

## Contribute

See [CONTRIBUTING.md](./CONTRIBUTING.md) for the current contributor workflow.
