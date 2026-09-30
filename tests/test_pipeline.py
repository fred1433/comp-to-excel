"""The whole controlled entry against a real throwaway PostgreSQL cluster (skipped when initdb is absent)."""
import pytest

from conftest import have_postgres

pytestmark = pytest.mark.skipif(not have_postgres(), reason="PostgreSQL (initdb, pg_ctl) not installed")


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    import run_demo
    base = tmp_path_factory.mktemp("run")
    return run_demo.main(use_excel=False, run_dir=base / "run", wb_dir=base / "workbooks")


def outcomes(result, step):
    return [s for s in result["steps"] if s["step"] == step]


def test_write_before_approval_is_refused(result):
    first = outcomes(result, "write")[0]
    assert first["outcome"] == "refused" and "writer role" in first["reason"] and not first["file_created"]


def test_change_after_approval_invalidates_the_write(result):
    w = outcomes(result, "write")[1]
    assert w["outcome"] == "refused" and "changed after approval" in w["reason"]


def test_resume_after_crash_finishes_without_rewriting(result):
    w = outcomes(result, "write")
    assert [x["outcome"] for x in w[2:5]] == ["worker_stopped_after_save", "resumed_without_rewrite", "already_written"]
    assert w[2]["file_sha256"] == w[3]["output_sha256"] == result["files"]["delivered"]["sha256"]
    assert w[5]["rows"] == [["done", 1]]


def test_matching_cases(result):
    m = {(s.get("doc"), s["outcome"]) for s in outcomes(result, "match")}
    assert ("county", "transaction_created_from_recorded_instrument") in m
    assert ("county", "resale_kept_apart") in m
    assert ("mls-cb", "same_sale_other_source") in m
    assert ("minutes", "held_for_review") in m
    assert (None, "refused_by_database") in m
    assert any(s["outcome"] == "replay_ignored" for s in outcomes(result, "ingest"))


def test_acceptance_contract_on_the_delivered_file(result):
    a = result["acceptance"]
    assert a["part_diff"]["ok"] and a["manifest"]["ok"]
    assert a["manifest"]["before"]["protected_sha256"] == a["manifest"]["after"]["protected_sha256"]
    assert all(t["passed"] for t in result["tamper_tests"])


def test_audit_is_append_only_for_application_roles(result):
    a = outcomes(result, "audit")[0]
    assert a["chain_verified"] and all(r["passed"] for r in a["refusals"]) and len(a["refusals"]) == 9
    assert a["owner_edit_detected_at_rows"] == [3]
