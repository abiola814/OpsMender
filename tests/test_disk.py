import asyncio
import sqlite3

import pytest

from opsmender import incidents
from opsmender.db import Database, dumps, loads
from opsmender.metrics import disk, ssh

RAW = {
    "v": 2, "ts": 1792370513, "docker": True, "docker_root": "/var/lib/docker", "log_driver": "json-file",
    "log_rotation": False,
    "fs": {"mount": "/", "total_kb": 80_000_000, "used_kb": 72_000_000, "avail_kb": 8_000_000},
    "system_df": [
        {"Active": "23", "Reclaimable": "24.68GB (53%)", "Size": "45.84GB", "TotalCount": "30", "Type": "Images"},
        {"Active": "13", "Reclaimable": "93.1MB (9%)", "Size": "946.4MB", "TotalCount": "25", "Type": "Containers"},
        {"Active": "16", "Reclaimable": "2.033MB (0%)", "Size": "2.277GB", "TotalCount": "37", "Type": "Local Volumes"},
        {"Active": "0", "Reclaimable": "21.47GB", "Size": "21.47GB", "TotalCount": "345", "Type": "Build Cache"},
    ],
    "container_logs": {"total_bytes": 6_000_000_000, "count": 2,
                       "top": [{"bytes": 5_900_000_000, "id": "abc123abc123"}, {"bytes": 100_000_000, "id": "def456def456"}]},
    "var_log_kb": 2_000_000, "journal_kb": 1_500_000, "coolify_backups_kb": 3_000_000,
}


def test_parse_size_handles_docker_units():
    assert disk.parse_size("45.84GB") == 45_840_000_000
    assert disk.parse_size("24.68GB (53%)") == 24_680_000_000
    assert disk.parse_size("93.1MB") == 93_100_000
    assert disk.parse_size("0B") == 0
    assert disk.parse_size("12.5kB") == 12_500
    assert disk.parse_size("1GiB") == 1024**3
    assert disk.parse_size("") is None and disk.parse_size(None) is None


def test_report_ranks_categories_and_flags_risk():
    containers = [{"id": "abc123abc123", "name": "k8s0api1-101530"}]
    resources = [{"uuid": "k8s0api1", "name": "api", "kind": "application"}]
    report = disk.build_report(RAW, containers, resources)
    keys = [i["key"] for i in report["items"]]
    assert keys[:3] == ["images", "build_cache", "container_logs"]  # most reclaimable first
    risks = {i["key"]: i["risk"] for i in report["items"]}
    assert risks == {"images": "medium", "build_cache": "safe", "container_logs": "low", "containers": "low",
                     "volumes": "danger", "journal": "safe", "coolify_backups": "medium"}
    journal = next(i for i in report["items"] if i["key"] == "journal")
    assert journal["reclaimable"] == 1_500_000 * 1024 - disk.JOURNAL_KEEP
    # Low-risk total counts safe + low items only: not images, volumes or backups.
    assert report["low_risk_reclaimable"] == 21_470_000_000 + 6_000_000_000 + 93_100_000 + journal["reclaimable"]
    assert report["log_rotation"] is False
    assert report["top_logs"][0]["resource"]["name"] == "api"
    assert round(report["fs"]["pct"]) == 90


def test_findings_explain_biggest_wins_and_rotation():
    found = disk.findings(disk.build_report(RAW))
    titles = [f["title"] for f in found]
    assert titles[0].startswith("Unused images: 24.7 GB is unused, but needs review")
    assert any(t.startswith("Build cache: 21.5 GB can be freed") for t in titles)
    assert "Docker log rotation is off" in titles
    assert not any(f["rule_id"] == "disk_volumes" for f in found)  # 2 MB isn't worth flagging
    assert all(f["command"] for f in found)


def test_rotation_counts_as_on_for_rotating_drivers():
    assert disk.build_report({**RAW, "log_driver": "local"})["log_rotation"] is True
    assert disk.build_report({**RAW, "log_rotation": True})["log_rotation"] is True


def test_no_docker_report_is_empty_but_valid():
    raw = {"v": 2, "ts": 1, "docker": False, "log_driver": "", "log_rotation": False, "fs": None, "system_df": [],
           "container_logs": {"total_bytes": 0, "count": 0, "top": []}, "var_log_kb": 16, "journal_kb": None,
           "coolify_backups_kb": None}
    report = disk.build_report(raw)
    assert report["items"] == [] and report["low_risk_reclaimable"] == 0
    assert disk.findings(report) == []


def test_old_database_gets_new_columns(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE servers (uuid TEXT PRIMARY KEY, name TEXT NOT NULL)")
    conn.commit()
    conn.close()
    db = Database(path)
    columns = {r["name"] for r in db.all("PRAGMA table_info(servers)")}
    assert {"disk_raw", "disk_at"} <= columns


async def test_disk_incident_gets_breakdown_and_summary(rt, monkeypatch):
    rt.settings.set("ssh_private_key", "unused-in-test")
    rt.db.execute("INSERT INTO servers (uuid, name, ip, metrics_enabled) VALUES ('srv1', 'main', '10.0.0.1', 1)")

    async def fake_disk(host, port, key, known):
        return RAW
    monkeypatch.setattr(ssh, "disk", fake_disk)
    inc_id, created = incidents.open_incident(rt, fingerprint="disk:srv1", kind="disk_pressure", severity="warning",
                                              title="Disk 90% full on main", server_uuid="srv1")
    while incidents._tasks:
        await asyncio.gather(*list(incidents._tasks))
    inc = rt.db.one("SELECT * FROM incidents WHERE id = ?", (inc_id,))
    assert loads(inc["evidence"])["disk"]["items"]
    assert loads(inc["findings"])["findings"][0]["category"] == "disk"
    assert inc["summary"].startswith("Up to ") and "Nothing has been deleted" in inc["summary"]
    assert rt.db.one("SELECT disk_raw FROM servers WHERE uuid = 'srv1'")["disk_raw"]


async def test_outdated_probe_gets_a_clear_message(rt, monkeypatch):
    rt.settings.set("ssh_private_key", "unused-in-test")

    async def old_probe(*args):
        raise ssh.ProbeError("Probe exited with 2: unsupported request")
    monkeypatch.setattr(ssh, "disk", old_probe)
    with pytest.raises(ssh.ProbeError, match="older OpsMender probe"):
        await disk.collect(rt, {"uuid": "srv1", "ip": "x", "metrics_enabled": 1})


def test_prompt_includes_disk_breakdown():
    from opsmender.ai.prompts import incident_prompt
    inc = {"title": "Disk 90% full", "kind": "disk_pressure", "severity": "warning", "first_seen": 0,
           "last_seen": 0, "occurrences": 1, "summary": None}
    prompt = incident_prompt(inc, {"disk": disk.build_report(RAW)}, {}, None)
    assert "Build cache: 21.47 GB total, 21.47 GB reclaimable, risk=safe" in prompt
    assert "rotation OFF" in prompt and "Never suggest bulk volume deletion" in prompt
