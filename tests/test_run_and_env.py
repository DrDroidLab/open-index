from open_index.connectors.runner import _resolve_env, run_due


def test_resolve_env_string(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "secret123")
    assert _resolve_env("Bearer ${MY_TOKEN}") == "Bearer secret123"


def test_resolve_env_dict(monkeypatch):
    monkeypatch.setenv("MY_URL", "https://x")
    assert _resolve_env({"Authorization": "${MY_TOKEN}", "url": "${MY_URL}"}) == \
        {"Authorization": "", "url": "https://x"}


def test_run_due_runs_scheduled_connector(brain):
    # example-issues has schedule "daily"; never run -> due -> creates 2 demo issues
    results = run_due(brain)
    ran = [r for r in results if not r.skipped]
    assert any(r.connector == "example-issues" and r.created == 2 for r in ran)
    assert brain.get_entity("issue:cart-abandonment") is not None


def test_run_due_skips_when_recent(brain):
    run_due(brain)              # first run records last_run
    results = run_due(brain)    # immediately again -> not due
    assert all(r.skipped for r in results if r.connector == "example-issues")


def test_run_due_force(brain):
    run_due(brain)
    results = run_due(brain, force=True)
    assert any(not r.skipped and r.connector == "example-issues" for r in results)
