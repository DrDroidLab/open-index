"""Demo content for new brains (--seed-demo / "Load demo entities").

A small software-infrastructure brain so search/boosting is immediately
visible: services are boosted 2x over runbooks and dashboards.
"""

from __future__ import annotations

from typing import Any

from .store import Brain

DEMO_DOC_TYPES: list[dict[str, Any]] = [
    {
        "name": "service",
        "description": "A production service: ownership, stack and operational metadata.",
        "boost": 2.0,
    },
    {
        "name": "runbook",
        "description": "Operational runbook for mitigating a class of incidents.",
        "boost": 1.0,
    },
    {
        "name": "dashboard",
        "description": "A monitoring dashboard and the panels it contains.",
        "boost": 1.0,
    },
]

DEMO_ENTITIES: list[dict[str, Any]] = [
    {
        "doc_type": "service",
        "name": "api-gateway",
        "data": {
            "description": "Edge ingress for all external API traffic. Routes to internal services and enforces auth.",
            "team": "platform",
            "language": "go",
            "tier": "critical",
            "dependencies": ["user-service", "payments-service"],
        },
    },
    {
        "doc_type": "service",
        "name": "payments-service",
        "data": {
            "description": "Processes card payments and refunds via Stripe. PCI-scoped.",
            "team": "payments",
            "language": "java",
            "tier": "critical",
            "dependencies": ["postgres-payments", "stripe"],
        },
    },
    {
        "doc_type": "service",
        "name": "user-service",
        "data": {
            "description": "User accounts, profiles and authentication sessions.",
            "team": "platform",
            "language": "python",
            "tier": "high",
            "dependencies": ["postgres-users", "redis-sessions"],
        },
    },
    {
        "doc_type": "runbook",
        "name": "api-gateway-5xx-spike",
        "data": {
            "service": "api-gateway",
            "trigger": "alert: api-gateway 5xx rate > 2% for 5m",
            "steps": [
                "Check upstream health of user-service and payments-service",
                "Inspect recent deployments of api-gateway",
                "If upstream healthy, roll back the latest api-gateway deployment",
            ],
        },
    },
    {
        "doc_type": "runbook",
        "name": "db-connection-saturation",
        "data": {
            "service": "postgres-payments",
            "trigger": "alert: postgres connections > 90% of max",
            "steps": [
                "Identify the service leaking connections from pg_stat_activity",
                "Scale the offending service down to 1 replica",
                "Restart the pgbouncer sidecar if connections are not released",
            ],
        },
    },
    {
        "doc_type": "dashboard",
        "name": "platform-overview",
        "data": {
            "url": "https://grafana.example.com/d/platform-overview",
            "panels": ["request rate", "p99 latency", "5xx rate", "pod restarts"],
            "owner": "platform",
        },
    },
    {
        "doc_type": "dashboard",
        "name": "payments-slo",
        "data": {
            "url": "https://grafana.example.com/d/payments-slo",
            "panels": ["payment success rate", "refund latency", "stripe webhook lag"],
            "owner": "payments",
        },
    },
]


def seed_demo(brain: Brain) -> dict[str, int]:
    """Insert demo doc_types and entities. Existing doc_types are skipped."""
    seeded_doc_types = 0
    for dt in DEMO_DOC_TYPES:
        if not brain.doc_type_exists(dt["name"]):
            brain.create_doc_type(dt["name"], dt["description"], dt["boost"])
            seeded_doc_types += 1
    seeded_entities = 0
    for entity in DEMO_ENTITIES:
        if brain.doc_type_exists(entity["doc_type"]):
            brain.upsert_entity(entity["doc_type"], entity["name"], entity["data"])
            seeded_entities += 1
    return {"doc_types": seeded_doc_types, "entities": seeded_entities}
