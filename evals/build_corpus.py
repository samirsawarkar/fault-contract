"""Regenerate evals/corpus/refund.otlp.json: the refund agent's example tests plus the extra runs in
corpus/more_refund_runs.py, exported with faultgate-zero's --fgz-export-otlp. Offline and scripted.

    python evals/build_corpus.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
EXAMPLE = HERE.parent / "examples" / "refund_agent"
OUT = HERE / "corpus" / "refund.otlp.json"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for f in ("refund_agent.py", "fake_llm.py", "test_refund_agent.py", "pyproject.toml"):
            shutil.copy(EXAMPLE / f, work / f)
        shutil.copytree(EXAMPLE / ".faultline", work / ".faultline")
        shutil.copy(HERE / "corpus" / "more_refund_runs.py", work / "test_more_refund_runs.py")
        env_out = work / "export.json"
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--fgz", "warn",
                            "--fgz-export-otlp", str(env_out)], cwd=work, capture_output=True, text=True,
                           env={**__import__("os").environ, "PYTHONHASHSEED": "0"})
        if not env_out.is_file():
            print(r.stdout[-2000:], r.stderr[-2000:])
            return 1
        shutil.copy(env_out, OUT)
    print(f"wrote {OUT.relative_to(HERE.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
