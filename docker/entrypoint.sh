#!/usr/bin/env sh
# Container entrypoint for open-index.
#
# Two jobs beyond exec'ing the CLI:
#
#   1. Default --brain to $OPEN_INDEX_BRAIN (/brain) so every command works
#      without repeating the flag in compose files and `docker run` lines.
#   2. Reconcile file-backed entities into the search index before serving.
#      Entities under entities/**/*.json are the git source of truth; a fresh
#      container (or a fresh OpenSearch cluster) starts with an empty index, so
#      without this the brain answers every query with "no results" and looks
#      broken. Index-backed entities are untouched by this — see Brain.index().
#
# Skip step 2 with OPEN_INDEX_SKIP_INDEX=1 (e.g. a read replica, or when the
# index is large and managed out of band).
set -eu

BRAIN="${OPEN_INDEX_BRAIN:-/brain}"

if [ ! -f "$BRAIN/brain.yaml" ]; then
  echo "open-index: no brain.yaml at $BRAIN" >&2
  echo "  mount your brain directory there, e.g. -v \"\$PWD/my-brain:/brain\"" >&2
  echo "  (create one first with: open-index init my-brain)" >&2
  exit 1
fi

# Wait for OpenSearch when configured — compose starts containers in parallel and
# the cluster is not accepting connections the moment its port opens.
if [ "${OPEN_INDEX_SEARCH_BACKEND:-}" = "opensearch" ]; then
  HOSTS="${OPEN_INDEX_OPENSEARCH_HOSTS:-http://opensearch:9200}"
  FIRST_HOST=$(echo "$HOSTS" | cut -d, -f1)
  echo "open-index: waiting for OpenSearch at $FIRST_HOST ..."
  i=0
  until python -c "
import sys, urllib.request
try:
    urllib.request.urlopen('$FIRST_HOST', timeout=3)
except urllib.error.HTTPError:
    pass  # 401/403 means it is up and asking for auth
except Exception:
    sys.exit(1)
" 2>/dev/null; do
    i=$((i + 1))
    if [ "$i" -ge 60 ]; then
      echo "open-index: OpenSearch not reachable at $FIRST_HOST after 60 tries" >&2
      exit 1
    fi
    sleep 2
  done
  echo "open-index: OpenSearch is up."
fi

case "${1:-serve}" in
  serve|ui|index|search|validate|run|ingest|mcp|mcp-config|list-connectors|add-entity|add-doc-type)
    COMMAND="$1"
    shift

    if [ "$COMMAND" = "serve" ] && [ "${OPEN_INDEX_SKIP_INDEX:-0}" != "1" ]; then
      echo "open-index: reconciling file-backed entities into the index ..."
      open-index index --brain "$BRAIN"
    fi

    set -- "$COMMAND" --brain "$BRAIN" "$@"
    ;;
  *)
    # Anything else (sh, python, a raw open-index invocation) runs verbatim.
    exec "$@"
    ;;
esac

exec open-index "$@"
