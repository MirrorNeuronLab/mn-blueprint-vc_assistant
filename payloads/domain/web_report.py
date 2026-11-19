"""Project the saved screening results into the shared local report template."""

import json
from html import escape
from pathlib import PureWindowsPath

from mn_sdk.blueprint.hooks import report_activity
from mn_sdk.blueprint.outputs import deliver_files, freeze_file_publication, merge_publications
from mn_sdk_common.events import compact_text
from mn_sdk_common.publication import ArtifactWrite


PAGE = "web/index.html"
HANDLE = "web_ui.json"

NAVIGATION = """
const { tabs } = JSON.parse(document.getElementById("report-data").textContent);
function showPanel(view) {
  for (const [id, , panel] of tabs) {
    document.getElementById(panel).hidden = id !== view;
  }
  window.scrollTo(0, 0);
}
MNWebReport.createReport({ tabs, onView: showPanel });
showPanel(tabs[0][0]);
"""

TABLE_STYLE = """
.report-table caption { padding: 6px 10px; text-align: left; }
#overview table { min-width: 30rem; }
#methods table, #evidence .report-table:last-child table { min-width: 40rem; }
#methods th:nth-child(3) { width: 12%; }
#methods th:nth-child(5) { width: 36%; }
#evidence .report-table:last-child th:nth-child(2) { width: 34%; }
#evidence .report-table:last-child th:nth-child(4) { width: 12%; }
"""


def _table(columns, rows, *, caption):
    heading = "".join(f'<th scope="col">{escape(label)}</th>' for label in columns)
    body = []
    for row in rows:
        cells = "".join(f"<td>{escape(str(value))}</td>" for value in row)
        body.append(f"<tr>{cells}</tr>")
    label = escape(caption)
    return (
        f'<div class="report-table" tabindex="0" role="region" aria-label="{label}">'
        f"<table><caption>{label}</caption><thead><tr>{heading}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


def _label(value):
    if not value:
        return "Unavailable"
    return value.replace("self_reported", "self-reported").replace("_", " ").capitalize()


def _value_or_dash(value):
    return value if value is not None else "—"


def _method_rows(companies, method_ids):
    rows = []
    for company in companies:
        for method_id in method_ids:
            method = company["methods"].get(method_id, {})
            missing = ", ".join(method.get("missing_evidence") or [])
            summary = method.get("evidence_summary") or {}
            explanation = missing or summary.get("method_purpose") or summary.get("status_reason")
            rows.append([
                company["company_name"],
                method_id.removesuffix("_method").replace("_", " ").title(),
                _value_or_dash(method.get("score")),
                _label(method.get("status")),
                compact_text(explanation, 360) or "—",
            ])
    return rows


def _claim_rows(companies):
    evidence_rows, followups = [], []
    for company in companies:
        name = company["company_name"]
        evidence = {item["evidence_id"]: item for item in (company.get("evidence_items") or [])}
        records = {
            source["source_id"]: source
            for source in (company.get("source_records") or [])
        }
        claims = sorted(
            company.get("claim_records") or [],
            key=lambda item: (item.get("importance") or 0, item.get("net_confidence") or 0),
            reverse=True,
        )
        company_rows = {}
        for claim in claims:
            supporting_items = [
                evidence[key] for key in (claim.get("evidence_ids") or []) if key in evidence
            ]
            if supporting_items and all(
                item.get("source_type") == "deterministic_financial_tool" for item in supporting_items
            ):
                continue
            sources = []
            for item in supporting_items:
                filename = PureWindowsPath(item.get("filename") or "").name
                record = records.get(item.get("source_id"), {})
                label = filename or record.get("title") or item.get("source_type")
                if label:
                    captured = compact_text(record.get("retrieved_at"), 40)
                    source_id = compact_text(item.get("source_id"), 80)
                    sources.append(" · ".join(part for part in (compact_text(label, 120), source_id,
                                          "retrieved " + captured if captured else "") if part))
            row = (
                name,
                compact_text(claim.get("canonical_claim"), 480),
                _label(claim.get("verification_status")),
                _value_or_dash(claim.get("net_confidence")),
                ", ".join(list(dict.fromkeys(sources))[:3]) or "—",
            )
            company_rows[row] = None
            if len(company_rows) == 8:
                break
        evidence_rows.extend(company_rows)
        gaps = dict.fromkeys(
            compact_text(gap, 360)
            for claim in claims
            for gap in claim.get("required_next_evidence") or []
            if gap
        )
        followups.extend([name, gap] for gap in list(gaps)[:6])
    return evidence_rows, followups


def _overview(companies, health):
    summary = "<p>Scores are screening heuristics for human review.</p>"
    company_table = _table(
        ["Company", "Screening score / 100", "Evidence quality / 100", "Confidence", "Report"],
        [
            [
                company["company_name"],
                _value_or_dash(company.get("investment_score")),
                _value_or_dash(company.get("evidence_quality_score")),
                _label(company.get("confidence_band")),
                "Reused" if company.get("cached_from_previous_run") else "Current",
            ]
            for company in companies
        ],
        caption="Company screening",
    )
    if health.get("status") == "warning":
        summary += (
            '<p class="workspace-alert" role="note">This run completed with warnings. '
            'Review the methods and evidence before relying on the scores.</p>'
        )
    return summary + company_table


def _coverage_table(companies):
    coverage_rows = []
    for company in companies:
        local = company.get("local_evidence_summary") or {}
        public = company.get("research_source_summary") or {}
        coverage_rows.append([
            company["company_name"],
            _value_or_dash(local.get("readable_record_count")),
            _value_or_dash(public.get("substantive_source_count")),
            _value_or_dash(public.get("source_count")),
        ])
    return _table(
        ["Company", "Readable documents", "Substantive public sources", "Public source records"],
        coverage_rows,
        caption="Research coverage",
    )


def render_web_report(artifact, *, title):
    from mn_web_report_skill import render_document

    companies = artifact.get("company_reports") or []
    sections = [("overview", "Overview", "<p>This run produced no company reports.</p>")]
    if companies:
        claim_rows, followups = _claim_rows(companies)
        methods = _table(
            ["Company", "Method", "Score / 100", "Status", "Evidence or gaps"],
            _method_rows(companies, artifact["method_ids"]),
            caption="Screening methods",
        )
        evidence = _coverage_table(companies)
        evidence += _table(
            ["Company", "Claim", "Verification", "Confidence %", "Sources"],
            claim_rows,
            caption="Key claims",
        ) if claim_rows else "<p>No narrative claims with supporting sources appear in this view.</p>"
        diligence = _table(
            ["Company", "Evidence to gather"],
            followups,
            caption="Diligence questions from the screening",
        ) if followups else "<p>This run recorded no follow-up items.</p>"
        sections = [
            ("overview", "Overview", _overview(companies, artifact.get("run_health") or {})),
            ("methods", "Method scores", methods),
            ("evidence", "Evidence", evidence),
            ("followup", "Follow-up", diligence),
        ]
    panels = "".join(
        f'<section id="{key}" role="tabpanel" aria-labelledby="tab-{key}" tabindex="0">'
        f"<h2>{label}</h2>{body}</section>"
        for key, label, body in sections
    )
    identity = "1 company" if len(companies) == 1 else f"{len(companies)} companies"
    return render_document(
        title=title,
        identity=identity,
        content_html=f"<main>{panels}</main>",
        script=NAVIGATION,
        style=TABLE_STYLE,
        data={"tabs": [[key, label, key] for key, label, _ in sections]},
    )


def publish_web_report(block, artifact):
    settings = block.config.get("web_ui")
    if not isinstance(settings, dict) or settings.get("enabled") is not True:
        return None

    def page():
        html = render_web_report(artifact, title=settings["title"]).encode("utf-8")
        return [ArtifactWrite("web_report", PAGE, html, kind="html", required=False, mode=0o644)]

    def handle():
        from mn_sdk_web_ui import static_run_report_handle

        descriptor = static_run_report_handle(block.store.run_dir, PAGE, title=settings["title"])
        return [ArtifactWrite(
            "web_ui", HANDLE, json.dumps(descriptor).encode("utf-8"),
            kind="json", required=False, mode=0o644,
        )]

    try:
        page_publication = freeze_file_publication(block, page, name="screening-report-page")
        if page_publication["warnings"] or deliver_files(block, page_publication):
            raise OSError("Could not deliver the screening report page")
        handle_publication = freeze_file_publication(block, handle, name="screening-report-handle")
        if handle_publication["warnings"] or deliver_files(block, handle_publication):
            raise OSError("Could not deliver the screening report handle")
        return merge_publications(block, page_publication, handle_publication)
    except Exception as error:
        block.check()
        report_activity(
            "The screening results are ready. The web report could not be published.",
            kind="diagnostic",
            level="warning",
            step_id=block.step_id,
            details={"error_type": type(error).__name__},
        )
        return None
