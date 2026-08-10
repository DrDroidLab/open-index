#!/usr/bin/env sh
# Container entrypoint for open-index.
#
# It runs in one of two modes:
#
#   single-brain  one brain mounted at $OPEN_INDEX_BRAIN (/brain). The --brain
#                 flag is filled in so compose files and `docker run` lines do
#                 not repeat it.
#   many-brains   a directory of brains at $OPEN_INDEX_BRAINS_ROOT (/brains),
#                 selected by `serve --brains` or by the explorer. Nothing is
#                 prepended; the command already knows where to look.
#
# In both modes, file-backed entities are reconciled into the search index
# before serving. Entities under entities/**/*.json are the git source of truth,
# so a fresh container starts with an empty index and would otherwise answer
# every query with "no results" and look broken.
#
# Skip the reconcile with OPEN_INDEX_SKIP_INDEX=1 (a read replica, or an index
# large enough to be managed out of band).
set -eu

BRAIN="${OPEN_INDEX_BRAIN:-/brain}"
BRAINS_ROOT="${OPEN_INDEX_BRAINS_ROOT:-}"

# `serve --brains <root>` selects many-brains mode even without the env var.
for arg in "$@"; do
  if [ "$arg" = "--brains" ]; then MULTI_FLAG=1; fi
done

if [ -n "$BRAINS_ROOT" ] || [ "${MULTI_FLAG:-0}" = "1" ]; then
  MULTI=1
  ROOT="${BRAINS_ROOT:-/brains}"
else
  MULTI=0
fi

if [ "$MULTI" = "1" ]; then
  if [ ! -d "$ROOT" ]; then
    echo "open-index: no directory at $ROOT" >&2
    echo "  mount a directory of brains there, e.g. -v \"\$PWD/brains:/brains\"" >&2
    exit 1
  fi
  # A brain is a subdirectory holding brain.yaml. An empty root is almost
  # always a mis-mounted volume, so say so rather than serving nothing.
  if [ -z "$(find "$ROOT" -mindepth 2 -maxdepth 2 -name brain.yaml -print -quit)" ]; then
    echo "open-index: no brains under $ROOT" >&2
    echo "  each brain is a subdirectory containing brain.yaml" >&2
    echo "  (create one with: open-index init <name>)" >&2
    exit 1
  fi
elif [ ! -f "$BRAIN/brain.yaml" ]; then
  echo "open-index: no brain.yaml at $BRAIN" >&2
  echo "  mount your brain directory there, e.g. -v \"\$PWD/my-brain:/brain\"" >&2
  echo "  (create one first with: open-index init my-brain)" >&2
  echo "  (or mount a directory of brains and set OPEN_INDEX_BRAINS_ROOT)" >&2
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

reconcile_all() {
  # Reconcile every brain under the root. Done here rather than inside `serve`
  # so it happens once at container start instead of on every request.
  find "$ROOT" -mindepth 2 -maxdepth 2 -name brain.yaml | while read -r f; do
    d=$(dirname "$f")
    echo "open-index: indexing $(basename "$d") ..."
    open-index index --brain "$d" >/dev/null || \
      echo "open-index: WARNING $(basename "$d") failed to index" >&2
  done
}

case "${1:-serve}" in
  serve|ui|index|search|validate|run|ingest|mcp|mcp-config|list-connectors|add-entity|add-doc-type)
    COMMAND="$1"
    shift

    if [ "$COMMAND" = "serve" ] && [ "${OPEN_INDEX_SKIP_INDEX:-0}" != "1" ]; then
      if [ "$MULTI" = "1" ]; then
        reconcile_all
      else
        echo "open-index: reconciling file-backed entities into the index ..."
        open-index index --brain "$BRAIN"
      fi
    fi

    if [ "$MULTI" = "1" ]; then
      set -- "$COMMAND" "$@"          # the command carries its own --brains
    else
      set -- "$COMMAND" --brain "$BRAIN" "$@"
    fi
    ;;
  *)
    # Anything else (sh, python, a raw open-index invocation) runs verbatim.
    exec "$@"
    ;;
esac

exec open-index "$@"
