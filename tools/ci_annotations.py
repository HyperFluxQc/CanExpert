"""Turn a failed test run's output into GitHub annotations, which show on the pull request and the run's page -
also to readers who are not signed in, unlike the job's log.

    python -m unittest discover -s tests -v 2>&1 | tee test-output.txt
    python tools/ci_annotations.py test-output.txt       (in a step that runs on failure)

Each FAIL and ERROR becomes one annotation with the end of its traceback; a crash (a Python fatal error, as
PYTHONFAULTHANDLER=1 prints it) becomes one naming the test that was running and the stack it died in.
"""
import re
import sys
from pathlib import Path

MAX_ANNOTATIONS = 10          # GitHub keeps ten error annotations per step
TRACEBACK_LINES = 25


def escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def annotations(output: str) -> list[tuple[str, str]]:
    """(title, message) for every failure and crash in unittest -v output."""
    found = []
    for block in re.split(r"^={50,}$", output, flags=re.M)[1:]:
        lines = [line for line in block.strip().splitlines() if not re.fullmatch(r"-{50,}", line)]
        if lines and re.match(r"(FAIL|ERROR): ", lines[0]):
            body = [line for line in lines[1:] if not line.startswith(("Ran ", "FAILED", "OK"))]
            found.append((lines[0], "\n".join(body[-TRACEBACK_LINES:])))
    crash = re.search(r"Fatal Python error: [^\n]*", output)      # often on the running test's own line
    if crash:
        before = output[:crash.start()].splitlines()
        running = next((line.split(" ... ")[0] for line in reversed(before) if " ... " in line), "an unknown test")
        stack = output[crash.start():].splitlines()
        found.append((f"Crash while running {running.strip()}", "\n".join(stack[:TRACEBACK_LINES + 10])))
    return found[:MAX_ANNOTATIONS]


def main(argv) -> int:
    output = Path(argv[1]).read_text(encoding="utf-8", errors="replace")
    found = annotations(output)
    for title, message in found:
        print(f"::error title={escape(title)}::{escape(message)}")
    if not found:
        print(f"::error title=Tests failed::{escape(chr(10).join(output.splitlines()[-TRACEBACK_LINES:]))}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
