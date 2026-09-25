"""
A TestExpert run, with or without its window: a plan's suite, tester and runner on a bus, and the reports the
run leaves. The window runs it on its own thread; test_expert.py plan.json --run on the command line.
"""
from __future__ import annotations

import time
from pathlib import Path

from canexpert.test_expert.description import EcuDescription
from canexpert.test_expert.generator import Suite
from canexpert.test_expert.plan import TestPlan
from canexpert.test_expert.tester import Tester
from canexpert.testing.report import save_reports
from canexpert.testing.runner import Runner, TestReport


class RecordingBus:
    """A bus whose frames, both ways, go into a Recorder (canexpert.recording) as they pass."""

    def __init__(self, bus, recorder):
        self.bus, self.recorder = bus, recorder

    def send(self, message, timeout=None):
        self.bus.send(message, timeout)
        self.recorder.write(time.time(), "TX", message.arbitration_id, bytes(message.data), message.is_extended_id)

    def recv(self, timeout=None):
        message = self.bus.recv(timeout)
        if message is not None and not message.is_error_frame:
            self.recorder.write(message.timestamp or time.time(), "RX", message.arbitration_id, bytes(message.data),
                                message.is_extended_id)
        return message


class PlanRun:
    """One run of a plan against the ECU on bus (a bus, or the mailbox of a CAN worker): names, the tests to
    run (None: the plan's - every test it does not leave out); on_event as canexpert.testing.runner's."""

    def __init__(self, plan: TestPlan, description: EcuDescription, bus, names=None, on_event=None):
        self.plan, self.description = plan, description
        self.suite = Suite(description, plan.make_options(), plan.sequences, plan.folder())
        excluded = set(plan.excluded)
        self.names = [case.name for case in self.suite.cases if case.name not in excluded] if names is None \
            else [case.name for case in self.suite.cases if case.name in set(names)]
        self.suite.tester = Tester(bus, plan.connection.transport(), plan.connection.functional_id)
        self.runner = Runner(self.suite.module(self.names), send=self.suite.tester.send_frame, on_event=on_event,
                             configuration=plan.connection.text())
        self.report: TestReport | None = None

    def run(self) -> TestReport:
        """Run the tests on the calling thread."""
        self.report = self.runner.run(self.names)
        return self.report

    def stop(self):
        self.runner.stop()

    def facts(self) -> list[tuple[str, str]]:
        """What the report's first table says besides the run itself."""
        facts = [("Description", f"{self.description.name} - {self.description.source or 'built in'}"),
                 ("Described", self.description.summary())]
        if self.plan.path is not None:
            facts.insert(0, ("Test plan", str(self.plan.path)))
        return facts

    def save(self, folder) -> list[Path]:
        """Write the run's HTML and JUnit reports into folder; returns their paths."""
        html_path, xml_path = save_reports(self.report, Path(folder), self.facts())
        return [html_path, xml_path]
