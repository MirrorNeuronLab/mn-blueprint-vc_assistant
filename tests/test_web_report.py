import json

import pytest
from mn_blueprint_blocks.context import BlockContext
from mn_sdk.blueprint.outputs import deliver_files, publish_files
from mn_sdk_common.committed_artifacts import CommittedJsonStore
from mn_sdk_common.publication import ArtifactWrite


def test_report_presents_saved_scores_and_qualified_claims():
    from domain.web_report import render_web_report

    artifact = {
        "method_ids": ["berkus_method", "venture_capital_method"],
        "run_health": {"status": "warning"},
        "company_reports": [{
            "company_name": "Acme <trial>",
            "investment_score": 0,
            "cached_from_previous_run": True,
            "methods": {
                "berkus_method": {"status": "scored", "score": 0},
                "venture_capital_method": {
                    "status": "insufficient_evidence",
                    "score": None,
                    "missing_evidence": ["Exit assumptions"],
                },
            },
            "claim_records": [{
                "canonical_claim": "Founder reported 10 pilot users.",
                "verification_status": "self_reported",
                "net_confidence": 45,
                "evidence_ids": ["evidence-1"],
                "required_next_evidence": ["Confirm pilot participation."],
            }],
            "evidence_items": [{
                "evidence_id": "evidence-1",
                "filename": "/private/source/pitch.pdf",
                "raw_excerpt": "SOURCE BODY MUST STAY IN ORIGINAL",
            }],
        }],
    }
    report = artifact["company_reports"][0]
    report["claim_records"].append(dict(report["claim_records"][0]))
    report["claim_records"].append({
        "canonical_claim": '{"raw_detail": "MACHINE CALCULATION"}',
        "evidence_ids": ["calculation"],
    })
    report["evidence_items"].append({
        "evidence_id": "calculation",
        "source_type": "deterministic_financial_tool",
    })
    page = render_web_report(artifact, title="VC screening report")

    assert "Acme &lt;trial&gt;" in page
    assert "<td>0</td>" in page
    assert "<td>—</td>" in page
    for text in (
        "Reused", "Insufficient evidence", "Exit assumptions", "Founder reported 10 pilot users.",
        "Self-reported", "pitch.pdf", "Confirm pilot participation.", "completed with warnings",
    ):
        assert text in page
    assert "/private/source/" not in page
    assert "SOURCE BODY MUST STAY IN ORIGINAL" not in page
    assert "MACHINE CALCULATION" not in page
    assert page.count("Founder reported 10 pilot users.") == 1


def test_report_handles_a_run_without_company_results():
    from domain.web_report import render_web_report

    page = render_web_report({"company_reports": []}, title="VC screening report")
    assert "This run produced no company reports." in page
    assert "Insufficient evidence" not in page


@pytest.mark.parametrize("failure", ["render", "handle"])
def test_report_failures_leave_screening_results_available(tmp_path, monkeypatch, failure):
    from domain import web_report
    import mn_sdk_web_ui

    output = tmp_path / "output"
    block = BlockContext(
        CommittedJsonStore(tmp_path / "run"), "batch", "test",
        config={"web_ui": {"enabled": True, "title": "VC screening report"}},
        environment={"MN_JOB_OUTPUT_DIR": str(output)},
    )
    artifact = {"company_reports": []}
    authoritative = publish_files(block, [
        ArtifactWrite("final", "final_artifact.json", json.dumps(artifact).encode(), authoritative=True),
        ArtifactWrite("summary", "run_summary.md", b"# Run summary"),
    ])
    deliver_files(block, authoritative)

    def unavailable(*args, **kwargs):
        raise OSError("preview unavailable")

    if failure == "render":
        monkeypatch.setattr(web_report, "render_web_report", unavailable)
    else:
        monkeypatch.setattr(mn_sdk_web_ui, "static_run_report_handle", unavailable)

    assert web_report.publish_web_report(block, artifact) is None
    assert json.loads((output / "final_artifact.json").read_text()) == artifact
    assert (output / "run_summary.md").read_text() == "# Run summary"
    assert not (output / "web_ui.json").exists()
