# open-index — runs a brain as an MCP endpoint (or the UI) in a container.
#
# The brain directory is NOT baked into this image. Mount it at /brain so the
# same image serves any brain, and so doc_types/entities stay in your git repo
# rather than in an image layer:
#
#   docker build -t open-index .
#   docker run -p 8080:8080 -v "$PWD/my-brain:/brain" \
#     -e OPEN_INDEX_TOKEN=secret open-index serve
#
# Extras are installed at build time via EXTRAS. The default covers both
# backends and the UI; slim it down for a smaller image if you only need one
# (e.g. --build-arg EXTRAS=serve).
FROM python:3.12-slim AS base

ARG EXTRAS=serve,opensearch,ui,semantic

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    OPEN_INDEX_BRAIN=/brain

WORKDIR /app

# Install dependencies from the project metadata first so this layer caches
# across source edits.
COPY pyproject.toml README.md LICENSE ./
COPY open_index ./open_index
RUN pip install --no-cache-dir ".[${EXTRAS}]"

# The mount point for the brain directory. Declared so `docker run` without -v
# still starts (with an empty brain) instead of failing on a missing path.
RUN mkdir -p /brain

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Run as a non-root user. The brain dir is written to (brain.db, entity files),
# so give the mount point to that user.
RUN useradd --create-home --uid 10001 openindex && chown -R openindex /brain
USER openindex

EXPOSE 8080 8501

ENTRYPOINT ["entrypoint.sh"]
CMD ["serve"]
