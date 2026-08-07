"""A real, functional connector: pulls the cluster inventory from a live MCP
server (examples/cloud-brain/tools_server.py) and maps it to entities with
relationships. Produces 210 entities.

Run the server, then:
    CLOUD_MCP_URL=http://127.0.0.1:9920 open-index ingest k8s --brain examples/cloud-brain
    # or, since schedule is set, let the scheduler decide:
    open-index run --brain examples/cloud-brain
"""
import os

from open_index.connectors import Connector, EntitySpec


class K8sConnector(Connector):
    name = "k8s"
    # A concrete URL (env-overridable) — the connector hits this MCP server.
    mcp_url = os.environ.get("CLOUD_MCP_URL", "http://127.0.0.1:9920")
    schedule = "hourly"

    def extract_namespaces(self):
        for x in self.call("list_namespaces"):
            yield EntitySpec(doc_type="namespace", id=f"namespace:{x['id']}", name=x["name"])

    def extract_nodes(self):
        for x in self.call("list_nodes"):
            yield EntitySpec(
                doc_type="node", id=f"node:{x['id']}", name=x["name"],
                fields={"zone": x.get("zone", "")},
                related_to=[(f"namespace:{x['namespace']}", "in namespace")],
            )

    def extract_services(self):
        for x in self.call("list_services"):
            yield EntitySpec(
                doc_type="service", id=f"service:{x['id']}", name=x["name"],
                fields={"port": x.get("port")},
                related_to=[(f"namespace:{x['namespace']}", "in namespace")],
            )

    def extract_deployments(self):
        for x in self.call("list_deployments"):
            yield EntitySpec(
                doc_type="deployment", id=f"deployment:{x['id']}", name=x["name"],
                fields={"replicas": x.get("replicas")},
                related_to=[
                    (f"namespace:{x['namespace']}", "in namespace"),
                    (f"service:{x['service']}", "exposes"),
                ],
            )
