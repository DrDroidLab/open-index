# Roadmap

## Status

These are product opportunities, not implemented features, delivery
commitments, or dated plans. The [README](./README.md) describes what ships
today.

## Reproducible ingestion and distillation

Make it easier to reproduce how knowledge entered a brain: built-in immutable
source and run metadata, lineage, output history, and audit support for
connectors and AI-distillation workflows. Today, teams can use user-defined
provenance fields and external immutable run manifests, but Droid Brain does
not generate or validate them.

## Safe collaborative writes and history

Enable teams to evolve shared knowledge confidently with contradiction handling,
patch/merge operations, version preconditions, optimistic concurrency, writer
coordination, and historical versions. Today, a same-ID write replaces the
complete current entity and its outgoing relationships; concurrent writes are
last-write-wins.

## Automated lifecycle management

Help current-state brains stay current with delete and tombstone APIs,
full-source reconciliation, TTL, stale-record cleanup, temporal filters, decay,
and recency ranking. Today, connector runs update records they emit; an omitted
source record is not inferred to be deleted.

## Faster local semantic retrieval

Bring type-level boosts and **SQLite-native vector indexing** to local search
workflows. OpenSearch k-NN semantic search is available now; SQLite semantic
search currently uses a bounded brute-force scan rather than a native vector
index.

## First-class policy-aware access

Make shared access safe by providing:

- authenticated human and service identities with verifiable claims;
- deny-by-default policy evaluation for resources and operations;
- policy-aware exact reads, search, counts, navigation, and graph access before
  ranking or relationships can leak hidden resources; and
- auditable, isolated operations across tenants/workspaces, connectors,
  backends/indexes, embeddings, exports, backups, and migrations.

This work includes migration from unscoped brains and backend-parity,
adversarial no-leak coverage. See the detailed [future authorization test
matrix](./CONTRIBUTING.md#future-authorization-test-matrix). Field-level
redaction is later scope only after resource-level policy enforcement and leak
prevention are correct.

## Reproducible performance and quality evaluation

Help users compare configurations with reproducible corpora and queries, quality
measures, latency and throughput methodology, hardware/backend/model disclosure,
and versioned results. No benchmark suite or published results exist today.

## Contribute

See [CONTRIBUTING.md](./CONTRIBUTING.md) for current ways to help shape these
opportunities.
