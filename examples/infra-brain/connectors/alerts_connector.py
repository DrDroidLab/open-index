"""Pull alerts into the brain (index-backed, temporal — Pattern B).

Alerts are high-volume and short-lived, so `alert` is `storage: index` — these
entities live in the DB, not in git. Point `mcp_url` at your alerting tool's MCP
server (PagerDuty, Grafana, Opsgenie…) and map firings to `alert` entities linked
to the service they fire on. The offline demo below lets `run`/`ingest` work with
no server so you can see it end-to-end.

    open-index ingest infra-alerts --brain examples/infra-brain
    open-index run --brain examples/infra-brain      # runs it when the schedule is due
"""

from open_index.connectors import Connector, EntitySpec


class InfraAlertsConnector(Connector):
    name = "infra-alerts"

    # mcp_url = "${ALERTS_MCP_URL}"
    # mcp_auth_headers = {"Authorization": "Bearer ${ALERTS_TOKEN}"}
    schedule = "hourly"          # alerts refresh often; "manual" to disable

    tool_name = "list_alerts"
    target_doc_type = "alert"

    def extract_alerts(self):
        if self.mcp is None:
            # offline demo data so ingest works without a live server
            sample = [
                {"id": "checkout-5xx", "title": "Checkout 5xx rate elevated",
                 "severity": "high", "state": "firing", "service": "checkout"},
                {"id": "pg-connections", "title": "Postgres connections near max",
                 "severity": "critical", "state": "firing", "service": "payments"},
            ]
        else:
            sample = self.paginate(self.tool_name, result_key="alerts")

        for item in sample:
            related = []
            svc = item.get("service")
            if svc:
                related.append((f"service:{svc}", "fires on"))
            yield EntitySpec(
                doc_type=self.target_doc_type,
                id=f"alert:{item['id']}",
                name=item.get("title", item["id"]),
                fields={
                    "description": item.get("title", ""),
                    "severity": item.get("severity", "unknown"),
                    "status": item.get("state", "firing"),
                },
                related_to=related,
            )
