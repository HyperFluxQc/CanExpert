"""
Test reports: a self-contained HTML page for people, and JUnit XML for CI servers (Jenkins, GitLab, Azure
DevOps and GitHub all read it).
"""
from __future__ import annotations

import html
import platform
import xml.etree.ElementTree as ElementTree
from datetime import datetime
from pathlib import Path

import canexpert
from canexpert.testing.runner import (ACCEPTED, BLOCKED, ERROR, FAILED, PASSED, SKIPPED, WARN, CaseResult,
                                      TestReport)

COLOURS = {PASSED: "#15803d", FAILED: "#b91c1c", ERROR: "#b45309", SKIPPED: "#6b7280", BLOCKED: "#7c3aed",
           "pass": "#15803d", "fail": "#b91c1c", "info": "#6b7280", WARN: "#c2410c", ACCEPTED: "#0369a1"}


def _when(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def _badge(verdict: str) -> str:
    return (f'<span class="badge" style="background:{COLOURS.get(verdict, "#6b7280")}">'
            f'{html.escape(verdict)}</span>')


def _steps_table(result: CaseResult) -> str:
    rows = "".join(
        f'<tr class="{step.verdict}"><td class="num">{step.time:.3f}</td><td>{_badge(step.verdict)}</td>'
        f'<td>{html.escape(step.description)}</td><td class="detail">{html.escape(step.detail)}</td></tr>'
        for step in result.steps)
    error = f'<pre class="error">{html.escape(result.error)}</pre>' if result.error else ""
    if not rows:
        return error or '<p class="none">No steps.</p>'
    return ('<table class="steps"><tr><th>Time (s)</th><th>Verdict</th><th>Step</th><th>Detail</th></tr>'
            f"{rows}</table>{error}")


def _section(result: CaseResult, open_it: bool) -> str:
    summary = f"{len(result.steps)} step{'s' if len(result.steps) != 1 else ''}"
    for count, what in ((len(result.failures()), "failed"), (len(result.warnings()), "with a warning"),
                        (len(result.accepted()), "accepted")):
        if count:
            summary += f", {count} {what}"
    return (f'<details{" open" if open_it else ""}><summary>{_badge(result.verdict)} '
            f'<b>{html.escape(result.title)}</b> <span class="muted">{html.escape(result.name)} - {summary} - '
            f'{result.duration:.3f} s</span></summary>{_steps_table(result)}</details>')


def html_report(report: TestReport, facts=(), sections: str = "") -> str:
    """The report as one HTML page: the verdict, the counts, and every test case with its steps (the ones
    that did not pass, or passed with warnings or accepted deviations, are opened). facts: more (name, value)
    rows for the table at the top; sections: HTML put between the test cases and their steps."""
    counts = report.counts()
    rows = "".join(
        f"<tr><td>{_badge(case.verdict)}</td><td>{html.escape(case.title)}</td>"
        f'<td class="num">{len(case.steps)}</td><td class="num">{len(case.failures())}</td>'
        f'<td class="num">{case.duration:.3f}</td></tr>' for case in report.cases)
    hooks = [hook for hook in (report.setup,) if hook is not None]
    steps = "".join(_section(result, result.verdict != PASSED or bool(result.warnings() or result.accepted()))
                    for result in hooks + report.cases + ([report.teardown] if report.teardown else []))
    rows_facts = [("Test module", report.path), ("Configuration", report.configuration or "-"),
                  ("Started", _when(report.started)), ("Duration", f"{report.duration:.3f} s"),
                  ("CAN Expert", canexpert.__version__), ("Computer", platform.node() or "-"), *facts]
    if report.stopped:
        rows_facts.append(("Stopped", "by the user, before every test case had run"))
    facts_html = "".join(f"<tr><th>{html.escape(str(name))}</th><td>{html.escape(str(value))}</td></tr>"
                         for name, value in rows_facts)
    blocked = f", {counts[BLOCKED]} blocked" if counts[BLOCKED] else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{html.escape(report.title)} - test report</title>
<style>
 body {{ font-family: Segoe UI, Helvetica, Arial, sans-serif; margin: 24px; color: #1f2937; }}
 h1 {{ margin-bottom: 4px; }} h2 {{ margin-top: 28px; }}
 .badge {{ color: white; border-radius: 4px; padding: 1px 7px; font-size: 12px; text-transform: uppercase; }}
 .verdict {{ font-size: 18px; margin: 8px 0 16px; }}
 table {{ border-collapse: collapse; margin: 6px 0 12px; }}
 th, td {{ border: 1px solid #d1d5db; padding: 4px 8px; text-align: left; vertical-align: top; }}
 th {{ background: #f3f4f6; }}
 .facts th {{ width: 130px; }}
 .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
 .detail {{ font-family: Consolas, monospace; font-size: 12px; white-space: pre-wrap; }}
 tr.fail td {{ background: #fef2f2; }}
 details {{ border: 1px solid #e5e7eb; border-radius: 6px; padding: 6px 10px; margin: 6px 0; }}
 summary {{ cursor: pointer; }}
 tr.warn td {{ background: #fff7ed; }}
 tr.accepted td {{ background: #f0f9ff; }}
 .muted, .none {{ color: #6b7280; }}
 pre.error {{ background: #fff7ed; border: 1px solid #fdba74; padding: 8px; white-space: pre-wrap; }}
</style></head><body>
<h1>{html.escape(report.title)}</h1>
<div class="verdict">{_badge(report.verdict)} {counts[PASSED]} passed, {counts[FAILED]} failed, {counts[ERROR]} with
an error, {counts[SKIPPED]} skipped{blocked}</div>
<table class="facts">{facts_html}</table>
<h2>Test cases</h2>
<table><tr><th>Verdict</th><th>Test case</th><th>Steps</th><th>Failed</th><th>Time (s)</th></tr>{rows}</table>
{sections}
<h2>Steps</h2>
{steps}
</body></html>
"""


def summary_text(report: TestReport) -> str:
    """"PASSED: 12 passed, 0 failed, 0 error, 1 skipped in 3.2 s" (and the blocked ones, when any were)."""
    counts = report.counts()
    blocked = f", {counts[BLOCKED]} blocked" if counts[BLOCKED] else ""
    return (f"{report.verdict.upper()}: {counts[PASSED]} passed, {counts[FAILED]} failed, {counts[ERROR]} error, "
            f"{counts[SKIPPED]} skipped{blocked} in {report.duration:.1f} s")


def _text(result: CaseResult) -> str:
    return "\n".join(f"[{step.time:8.3f}] {step.verdict.upper():4} {step.description}"
                     + (f" - {step.detail}" if step.detail else "") for step in result.steps)


def junit_report(report: TestReport) -> str:
    """The report as JUnit XML: one testsuite, a testcase per test case; setup and teardown are testcases
    too when they did not pass, so a CI server shows why everything was skipped."""
    counts = report.counts()
    cases = list(report.cases)
    for hook in (report.setup, report.teardown):
        if hook is not None and hook.verdict in (FAILED, ERROR):
            cases.append(hook)
    failures = sum(case.verdict == FAILED for case in cases)
    errors = sum(case.verdict in (ERROR, BLOCKED) for case in cases)
    attributes = {"tests": str(len(cases)), "failures": str(failures), "errors": str(errors),
                  "skipped": str(counts[SKIPPED]), "time": f"{report.duration:.3f}"}
    root = ElementTree.Element("testsuites", name="CAN Expert", **attributes)
    suite = ElementTree.SubElement(root, "testsuite", name=report.title,
                                   timestamp=datetime.fromtimestamp(report.started).isoformat(timespec="seconds"),
                                   hostname=platform.node() or "localhost", **attributes)
    properties = ElementTree.SubElement(suite, "properties")
    for name, value in (("test_module", report.path), ("configuration", report.configuration),
                        ("can_expert", canexpert.__version__), ("stopped", str(report.stopped).lower())):
        ElementTree.SubElement(properties, "property", name=name, value=value)
    classname = Path(report.path).stem
    for case in cases:
        element = ElementTree.SubElement(suite, "testcase", classname=classname, name=case.title,
                                         time=f"{case.duration:.3f}")
        failed = case.failures()
        if case.verdict == FAILED:
            first = failed[0] if failed else None
            message = f"{first.description}: {first.detail}" if first and first.detail else \
                (first.description if first else "failed")
            ElementTree.SubElement(element, "failure", message=message, type="StepFailed").text = _text(case)
        elif case.verdict == ERROR:
            last = case.error.strip().splitlines()[-1] if case.error.strip() else "error"
            ElementTree.SubElement(element, "error", message=last, type="Exception").text = case.error
        elif case.verdict == BLOCKED:
            ElementTree.SubElement(element, "error", message=case.error or "blocked", type="Blocked").text = \
                _text(case)
        elif case.verdict == SKIPPED:
            ElementTree.SubElement(element, "skipped", message=case.error or "skipped")
        if case.steps:
            ElementTree.SubElement(element, "system-out").text = _text(case)
    ElementTree.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ElementTree.tostring(root, encoding="unicode") + "\n"


def report_stem(report: TestReport) -> str:
    """<module>_<date-time>: the name of a run's report files."""
    return f"{Path(report.path).stem}_{datetime.fromtimestamp(report.started).strftime('%Y%m%d-%H%M%S')}"


def save_reports(report: TestReport, folder, facts=(), sections: str = "") -> tuple[Path, Path]:
    """Write <module>_<date-time>.html and .xml into folder; returns their paths."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stem = report_stem(report)
    html_path, xml_path = folder / f"{stem}.html", folder / f"{stem}.xml"
    html_path.write_text(html_report(report, facts, sections), encoding="utf-8")
    xml_path.write_text(junit_report(report), encoding="utf-8")
    return html_path, xml_path

