"""A Rolldown stop means a hung build or a starved machine, never bad source.

Apply/install (``compile_jsx``) and the local validator share one wall-time
limit. A build that outlives it is stopped and labelled an environment stall,
so nobody edits correct source because the machine was busy.
"""

import runpy
import sys
from pathlib import Path

import pytest

from app import app_compile_contract, compiler
from app.app_compile_contract import RolldownStalled
from app.compiler import CompileError, compile_jsx

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate-app.py"
SOURCE = "export default function App(){ return null }\n"
# Far longer than the shortened test limit, so the stall cannot race a finish.
HUNG = [sys.executable, "-c", "import time; time.sleep(300)"]


def _write_app(root: Path) -> None:
  root.mkdir(parents=True, exist_ok=True)
  (root / "mobius.json").write_text(
    '{"id": "limits-test", "name": "Limits test", "version": "1",'
    ' "description": "test", "entry": "index.jsx"}'
  )
  (root / "index.jsx").write_text(SOURCE)


@pytest.mark.asyncio
async def test_compile_stops_hung_build_as_environment_stall(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(compiler, "ROLLDOWN_WALL_LIMIT_SECS", 1)
  monkeypatch.setattr(compiler, "rolldown_command", lambda *_a, **_k: HUNG)

  with pytest.raises(RolldownStalled, match="source was not judged") as exc:
    await compile_jsx(SOURCE, out_path=tmp_path / "app.js")

  assert not isinstance(exc.value, CompileError)


def test_validator_labels_hung_build_as_environment_not_source(
  tmp_path, monkeypatch, capsys,
):
  _write_app(tmp_path / "app")
  script = runpy.run_path(str(SCRIPT))
  module_globals = script["main"].__globals__
  monkeypatch.setitem(module_globals, "ROLLDOWN_WALL_LIMIT_SECS", 1)
  monkeypatch.setitem(module_globals, "rolldown_command", lambda *_a, **_k: HUNG)
  monkeypatch.setattr(sys, "argv", ["validate-app.py", str(tmp_path / "app")])

  code = script["main"]()
  err = capsys.readouterr().err

  assert code == 3
  assert "[ENVIRONMENT]" in err
  assert "INCONCLUSIVE" in err
  assert "FAIL" not in err
  assert "compile failed" not in err


def test_wall_limit_leaves_room_for_a_starved_machine():
  """A starved 1 GB instance stretched a sub-second build past 30 s; the
  limit must clear that while staying under apply's 120 s client deadline."""
  assert 60 <= app_compile_contract.ROLLDOWN_WALL_LIMIT_SECS < 120
