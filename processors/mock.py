"""Mock processor: exercises the whole agent pipeline with no real
application. Dev/test only — the payload is `sys.executable -c ...`, which
does not work from a frozen (PyInstaller) agent.

Parameters:
  duration      seconds the fake payload sleeps (default 2)
  fail          exit nonzero (default false)
  skip_output   exit 0 but write no output -> validation failure (default false)
  output_path   where to write the output file (default <work_dir>/mock_output.txt)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from processors.base import JobContext, Processor, Progress, Validation

_PAYLOAD = (
    "import sys, time, pathlib\n"
    "duration = float(sys.argv[1]); out = sys.argv[2]\n"
    "fail = sys.argv[3] == '1'; skip = sys.argv[4] == '1'\n"
    "time.sleep(duration)\n"
    "if not skip:\n"
    "    pathlib.Path(out).write_text('mock output\\n')\n"
    "sys.exit(1 if fail else 0)\n"
)


class MockProcessor(Processor):
    job_types = {"MOCK", "MOCK_A", "MOCK_B"}
    requires_desktop = False
    version = "1.0"

    def _output_path(self, ctx: JobContext) -> Path:
        return Path(ctx.parameters.get("output_path") or (ctx.work_dir / "mock_output.txt"))

    def _duration(self, ctx: JobContext) -> float:
        return float(ctx.parameters.get("duration", 2))

    def build_command(self, ctx: JobContext) -> list[str]:
        return [
            sys.executable, "-c", _PAYLOAD,
            str(self._duration(ctx)),
            str(self._output_path(ctx)),
            "1" if ctx.parameters.get("fail") else "0",
            "1" if ctx.parameters.get("skip_output") else "0",
        ]

    def poll(self, ctx: JobContext, elapsed_seconds: float) -> Optional[Progress]:
        duration = max(self._duration(ctx), 0.001)
        percent = min(99.0, elapsed_seconds / duration * 100.0)
        return Progress(percent=percent, stage="mock", message=f"mock running ({percent:.0f}%)")

    def validate_outputs(self, ctx: JobContext) -> Validation:
        out = self._output_path(ctx)
        if out.is_file() and out.stat().st_size > 0:
            return Validation(ok=True, outputs=[str(out)],
                              summary={"size_bytes": out.stat().st_size})
        return Validation(ok=False, errors=[f"expected output missing: {out}"])
