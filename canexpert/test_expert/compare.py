"""
Comparing two runs - of two software versions, two ECUs, before and after a change: what changed verdict (a
regression, a fix), the tests only one of them had, the steps whose answer changed, and the ECU's
identification in each. Every run leaves its results as JSON beside its reports (results_dict()); two of them
are compared here, in the window or on the command line (test_expert.py --compare before.json after.json).
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

RESULTS_FORMAT = "TestExpert results"
RESULTS_VERSION = 1
GOOD, BAD = {"passed"}, {"failed", "error", "blocked"}
CHECKED_STEPS = {"pass", "fail", "accepted", "warn"}     # info lines are not compared
# Bytes a positive response echoes of its request (a DID, a routine and its control); the others: one.
ECHOED = {"62": 2, "6E": 2, "6F": 2, "71": 3, "63": 0, "77": 0, "54": 0}


def results_dict(report, description=None, identification=None, coverage=None, plan_path=None) -> dict:
    """A run's results as JSON values: its facts, the ECU's identification, each test with its steps, and its
    coverage."""
    def case(result):
        return {"name": result.name, "title": result.title, "verdict": result.verdict,
                "duration": result.duration, "error": result.error.strip().splitlines()[-1] if result.error.strip()
                else "", "steps": [{"description": step.description, "verdict": step.verdict, "detail": step.detail}
                                   for step in result.steps]}
    return {
        "format": RESULTS_FORMAT, "version": RESULTS_VERSION, "title": report.title, "path": report.path,
        "plan": str(plan_path or ""), "configuration": report.configuration, "started": report.started,
        "duration": report.duration, "verdict": report.verdict, "counts": report.counts(), "stopped": report.stopped,
        "description": {"name": description.name, "source": description.source,
                        "summary": description.summary()} if description is not None else {},
        "identification": {f"{did:04X}": [name, value] for did, (name, value) in sorted((identification or {}).items())},
        "setup": case(report.setup) if report.setup else None,
        "teardown": case(report.teardown) if report.teardown else None,
        "cases": [case(result) for result in report.cases],
        "coverage": coverage.to_dict() if coverage is not None else {},
    }


class ResultsError(ValueError):
    """Not the results of a TestExpert run."""


def load_results(path) -> dict:
    try:
        values = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ResultsError(f"{Path(path).name} cannot be read: {exc}") from None
    if not isinstance(values, dict) or values.get("format") != RESULTS_FORMAT:
        raise ResultsError(f"{Path(path).name} is not the results of a TestExpert run (the .json beside a report)")
    values["file"] = str(path)
    return values


def previous_results(folder, results: dict) -> Path | None:
    """The results file, in folder, of the last run before this one of the same description."""
    best = None
    for path in Path(folder).glob("*.json"):
        if str(path) == results.get("file"):
            continue
        try:
            other = load_results(path)
        except ResultsError:
            continue
        if other.get("path") == results.get("path") and other.get("started", 0) < results.get("started", 0) and \
                (best is None or other["started"] > best[0]):
            best = (other["started"], path)
    return best[1] if best else None


def _without_times(text: str) -> str:
    """A step's detail without what differs from run to run: "(4 ms, after 1 response pending (first at 0 ms))"
    and other times in ms."""
    result, index = [], 0
    for match in re.finditer(r"\(\d+ ms", text):
        if match.start() < index:
            continue
        depth, end = 0, len(text)
        for position in range(match.start(), len(text)):
            depth += {"(": 1, ")": -1}.get(text[position], 0)
            if depth == 0:
                end = position + 1
                break
        result.append(text[index:match.start()].rstrip())
        index = end
    result.append(text[index:])
    return re.sub(r"\d+ ms", "_ ms", "".join(result)).strip()


def answer_of(detail: str) -> str:
    """What a step's detail says the ECU answered, without times: a negative response whole, a positive one by
    its first bytes (the response and what it echoes - its data, a seed, an uptime, differ by nature; the
    values that matter are compared by the steps that check them), else the detail itself."""
    text = _without_times(detail)
    if "->" not in text:
        return text
    answer = text.split("->", 1)[1].split(" - ", 1)[0].strip()
    tokens = answer.split()
    if not tokens or tokens[0] == "7F" or not all(len(token) == 2 for token in tokens[:4]):
        return answer
    kept = 1 + ECHOED.get(tokens[0], 1)
    return " ".join(tokens[:kept]) + (" ..." if len(tokens) > kept else "")


@dataclass
class StepChange:
    description: str
    before: str                # verdict and answer before ("" when the step is new)
    after: str


@dataclass
class TestChange:
    kind: str                  # "regression", "fixed", "changed", "new", "gone", "steps"
    name: str
    title: str
    before: str                # its verdict before ("" when new)
    after: str
    steps: list = field(default_factory=list)      # StepChange


@dataclass
class Comparison:
    before: dict
    after: dict
    changes: list = field(default_factory=list)        # TestChange
    identification: list = field(default_factory=list)   # (DID, name, before, after)

    def of(self, kind) -> list:
        return [change for change in self.changes if change.kind == kind]

    @property
    def regressions(self) -> list:
        return self.of("regression")

    def summary(self) -> str:
        counts = {kind: len(self.of(kind)) for kind in ("regression", "fixed", "changed", "new", "gone", "steps")}
        return (f"{counts['regression']} regression{'s' if counts['regression'] != 1 else ''}, {counts['fixed']} fixed, "
                f"{counts['changed']} other verdict changes, {counts['new']} new, {counts['gone']} gone, "
                f"{counts['steps']} with other answers")


def _steps(case) -> dict:
    """(description, occurrence) -> (verdict, answer) of a test's checked steps."""
    found, seen = {}, {}
    for step in (case or {}).get("steps", ()):
        if step.get("verdict") not in CHECKED_STEPS:
            continue
        description = step.get("description", "")
        seen[description] = seen.get(description, 0) + 1
        found[(description, seen[description])] = (step.get("verdict", ""), answer_of(step.get("detail", "")))
    return found


def _step_changes(before_case, after_case) -> list:
    before, after = _steps(before_case), _steps(after_case)
    changes = []
    for key in list(before) + [key for key in after if key not in before]:
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        changes.append(StepChange(key[0], f"{old[0]}: {old[1]}" if old else "", f"{new[0]}: {new[1]}" if new else ""))
    return changes


def compare_runs(before: dict, after: dict) -> Comparison:
    comparison = Comparison(before, after)
    old_cases = {case["name"]: case for case in before.get("cases", ())}
    new_cases = {case["name"]: case for case in after.get("cases", ())}
    for name in list(old_cases) + [name for name in new_cases if name not in old_cases]:
        old, new = old_cases.get(name), new_cases.get(name)
        title = (new or old).get("title", name)
        if old is None:
            comparison.changes.append(TestChange("new", name, title, "", new["verdict"]))
            continue
        if new is None:
            comparison.changes.append(TestChange("gone", name, title, old["verdict"], ""))
            continue
        steps = _step_changes(old, new)
        if old["verdict"] != new["verdict"]:
            kind = "regression" if old["verdict"] in GOOD and new["verdict"] in BAD else \
                "fixed" if old["verdict"] in BAD and new["verdict"] in GOOD else "changed"
            comparison.changes.append(TestChange(kind, name, title, old["verdict"], new["verdict"], steps))
        elif steps:
            comparison.changes.append(TestChange("steps", name, title, old["verdict"], new["verdict"], steps))
    old_ids, new_ids = before.get("identification", {}), after.get("identification", {})
    for did in sorted(set(old_ids) | set(new_ids)):
        old, new = old_ids.get(did, ["", ""]), new_ids.get(did, ["", ""])
        if old[1] != new[1]:
            comparison.identification.append((did, new[0] or old[0], old[1], new[1]))
    return comparison


# --- HTML ------------------------------------------------------------------------------------------------------

TABLE = '<table border="1" cellspacing="0" cellpadding="3">'
KINDS = {"regression": ("Regressions", "#fee2e2", "passed before, not now"),
         "fixed": ("Fixed", "#dcfce7", "did not pass before, passes now"),
         "changed": ("Other verdict changes", "#fef3c7", "skipped, blocked..."),
         "new": ("New tests", "#e0f2fe", "only in the second run"),
         "gone": ("Tests gone", "#f3f4f6", "only in the first run"),
         "steps": ("Other answers", "#f5f3ff", "the same verdict, but steps answered otherwise")}


def _run_text(run: dict) -> str:
    when = datetime.fromtimestamp(run.get("started", 0)).strftime("%Y-%m-%d %H:%M:%S") if run.get("started") else "-"
    description = run.get("description", {})
    counts = run.get("counts", {})
    return (f"{when} - {html.escape(description.get('name', run.get('title', '')))} - "
            f"<b>{html.escape(run.get('verdict', '').upper())}</b>: {counts.get('passed', 0)} passed, "
            f"{counts.get('failed', 0)} failed, {counts.get('error', 0)} error, {counts.get('skipped', 0)} skipped"
            + (f", {counts['blocked']} blocked" if counts.get("blocked") else "")
            + f"<br><span class=\"muted\">{html.escape(run.get('file', ''))}</span>")


def comparison_html(comparison: Comparison) -> str:
    parts = ["<h2>Comparison</h2>",
             f"{TABLE}<tr><th>Before</th><td>{_run_text(comparison.before)}</td></tr>"
             f"<tr><th>After</th><td>{_run_text(comparison.after)}</td></tr></table>",
             f"<p><b>{html.escape(comparison.summary())}</b></p>"]
    if comparison.identification:
        rows = "".join(f"<tr><td>{did} {html.escape(name)}</td><td>{html.escape(old)}</td><td>{html.escape(new)}</td></tr>"
                       for did, name, old, new in comparison.identification)
        parts.append(f"<h3>ECU identification</h3>{TABLE}<tr><th>DID</th><th>Before</th><th>After</th></tr>{rows}</table>")
    for kind, (title, colour, meaning) in KINDS.items():
        changes = comparison.of(kind)
        if not changes:
            continue
        rows = []
        for change in changes:
            steps = "".join(f"<li>{html.escape(step.description)}: <s>{html.escape(step.before) or '-'}</s> &rarr; "
                            f"{html.escape(step.after) or '-'}</li>" for step in change.steps[:12])
            more = f"<li>... {len(change.steps) - 12} more</li>" if len(change.steps) > 12 else ""
            rows.append(f'<tr style="background:{colour}"><td>{html.escape(change.title)}</td>'
                        f"<td>{change.before or '-'}</td><td>{change.after or '-'}</td>"
                        f"<td>{'<ul>' + steps + more + '</ul>' if steps else ''}</td></tr>")
        parts.append(f'<h3>{title} ({len(changes)})</h3><p class="muted">{meaning}</p>{TABLE}<tr><th>Test</th>'
                     f"<th>Before</th><th>After</th><th>Steps</th></tr>{''.join(rows)}</table>")
    if not comparison.changes and not comparison.identification:
        parts.append("<p>The two runs agree on every test and every answer.</p>")
    return "\n".join(parts)


def comparison_page(comparison: Comparison) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>TestExpert comparison</title>
<style>
 body {{ font-family: Segoe UI, Helvetica, Arial, sans-serif; margin: 24px; color: #1f2937; }}
 table {{ border-collapse: collapse; margin: 6px 0 12px; }}
 th, td {{ border: 1px solid #d1d5db; padding: 3px 8px; text-align: left; vertical-align: top; }}
 th {{ background: #f3f4f6; }}
 ul {{ margin: 0; padding-left: 18px; }}
 .muted {{ color: #6b7280; }}
</style></head><body>
{comparison_html(comparison)}
</body></html>
"""
