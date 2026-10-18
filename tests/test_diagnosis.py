from opsmender.diagnosis.logs import bound, fingerprint_of, group_errors
from opsmender.diagnosis.redact import redact
from opsmender.diagnosis.rules import analyze


def rule_ids(text: str) -> list[str]:
    return [f["rule_id"] for f in analyze(text)["findings"]]


def test_db_connection_refused_names_postgres():
    logs = "\n".join([
        "2026-10-18T10:00:00Z Server starting on :3000",
        "2026-10-18T10:00:01Z Error: connect ECONNREFUSED 10.0.1.5:5432",
        "2026-10-18T10:00:02Z Error: connect ECONNREFUSED 10.0.1.5:5432",
    ])
    result = analyze(logs)
    top = result["findings"][0]
    assert top["rule_id"] == "db_connection_refused"
    assert top["matches"] == 2
    assert top["details"]["dependency"] == "PostgreSQL"
    assert result["headline"] == "PostgreSQL refused the connection"


def test_out_of_memory_and_build_failures():
    assert "out_of_memory" in rule_ids("FATAL ERROR: Reached heap limit\nJavaScript heap out of memory")
    assert "build_failed" in rule_ids("npm ERR! code ELIFECYCLE\nnpm ERR! errno 1")
    assert "missing_module" in rule_ids("ModuleNotFoundError: No module named 'flask'")
    assert "port_in_use" in rule_ids("Error: listen EADDRINUSE: address already in use :::3000")
    assert "disk_full" in rule_ids("write /var/lib/x: no space left on device")


def test_clean_logs_have_no_findings():
    result = analyze("GET /health 200 2ms\nGET / 200 14ms\nlistening on 0.0.0.0:8080")
    assert result["findings"] == []
    assert result["error_groups"] == []
    assert result["headline"] is None


def test_error_groups_dedupe_by_template():
    logs = "\n".join(f"ERROR request {i} failed after {i * 10}ms id=abcdef{i:06d}" for i in range(50))
    groups = group_errors(logs)
    assert len(groups) == 1
    assert groups[0].count == 50


def test_stack_trace_context_is_attached():
    logs = "Error: boom\n    at handler (/app/server.js:10:5)\n    at next (/app/router.js:4:1)\nok"
    group = group_errors(logs)[0]
    assert group.sample == "Error: boom"
    assert group.context[0].strip().startswith("at handler")


def test_fingerprint_ignores_numbers_and_timestamps():
    a = fingerprint_of("2026-10-18T10:00:00Z ERROR user 123 not found")
    b = fingerprint_of("2026-10-19T11:30:00Z ERROR user 456 not found")
    assert a == b


def test_bound_keeps_tail_within_budget():
    text = "\n".join(f"line {i}" for i in range(1000))
    out = bound(text, max_lines=100, max_bytes=300)
    assert out.endswith("line 999")
    assert len(out.encode()) <= 300
    assert len(out.splitlines()) <= 100


def test_redaction_covers_common_secrets():
    raw = "\n".join([
        "DATABASE_URL=postgres://app:SuperSecret123@db:5432/app",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
        "api_key=sk-ant-REALKEY1234567890abcdefghij",
        "password: hunter2hunter2",
        "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "aws AKIAIOSFODNN7EXAMPLE",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
        "normal line with port 5432 and user id 42",
    ])
    out = redact(raw)
    for secret in ("SuperSecret123", "abcdefghijklmnopqrstuvwxyz123456", "REALKEY1234567890", "hunter2hunter2",
                   "dozjgNryP4J3jVmNHl0w5N", "AKIAIOSFODNN7EXAMPLE", "OPENSSH PRIVATE KEY-----\nabc"):
        assert secret not in out, secret
    assert "postgres://app:[REDACTED]@db:5432/app" in out
    assert "normal line with port 5432 and user id 42" in out
