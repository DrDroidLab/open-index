"""A REAL MCP server (JSON-RPC 2.0 over HTTP) that serves a Kubernetes-style
inventory of *stable* assets — namespaces, nodes, services, deployments (200
total). Ephemeral pods are deliberately NOT served: they churn and aren't
knowledge worth keeping (see entity-management.md). No SDK, no external deps.

    python examples/cloud-brain/tools_server.py 9920      # then, in another shell:
    CLOUD_MCP_URL=http://127.0.0.1:9920 droid-brain ingest k8s --brain examples/cloud-brain
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

NS = [f"ns-{i}" for i in range(8)]
NODES = [{"id": f"node-{i}", "name": f"node-{i}", "namespace": NS[i % 8], "zone": f"zone-{i % 3}"}
         for i in range(24)]
SERVICES = [{"id": f"svc-{i}", "name": f"service-{i}", "namespace": NS[i % 8],
             "port": 8000 + i} for i in range(84)]
DEPLOYMENTS = [{"id": f"dep-{i}", "name": f"deployment-{i}", "namespace": NS[i % 8],
                "service": f"svc-{i}", "replicas": (i % 5) + 1} for i in range(84)]

TOOLS = {
    "list_namespaces": [{"id": n, "name": n} for n in NS],
    "list_nodes": NODES,
    "list_services": SERVICES,
    "list_deployments": DEPLOYMENTS,
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method, rid = body.get("method"), body.get("id")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "cloud-inventory", "version": "1.0"}}
        elif method == "notifications/initialized":
            self.send_response(202); self.end_headers(); return
        elif method == "tools/list":
            result = {"tools": [{"name": t, "description": f"List {t.split('_')[1]}",
                                 "inputSchema": {"type": "object", "properties": {}}}
                                for t in TOOLS]}
        elif method == "tools/call":
            name = body["params"]["name"]
            result = {"content": [{"type": "text", "text": json.dumps(TOOLS.get(name, []))}],
                      "isError": name not in TOOLS}
        else:
            result = {}
        payload = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9920
    print(f"cloud-inventory MCP server on http://127.0.0.1:{port}  "
          f"({len(NS)} ns, {len(NODES)} nodes, {len(SERVICES)} services, "
          f"{len(DEPLOYMENTS)} deployments)")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
