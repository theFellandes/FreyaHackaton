"""evals/triage.py: which calls does the failure triage fetch for a ``tools/run_cases.py --out`` file?

The hidden benchmark counts a case only when it passes in every repetition, so the cases to look at are the ones that
failed in *some* repetition, not just in the last one (``rows`` of the results file holds the last repetition only;
``all_rows`` holds them all and ``final`` the verdict per case). No network: the kit's ``freya`` module is replaced
by a stub that records the call ids it is asked for.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from builder.settings import Settings

REPO = Path(__file__).resolve().parents[2]
TRIAGE = REPO / "evals" / "triage.py"


def load_triage() -> ModuleType:
    spec = importlib.util.spec_from_file_location("triage_under_test", TRIAGE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(case: str, family: str, rep: int, passed: bool, *, failed: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": case,
        "level": "easy",
        "family": family,
        "pass": passed,
        "failed": [] if passed else (failed or ["ledger: wrong promise"]),
        "call_id": f"call-{case}-r{rep}",
        "tools": ["verify_identity", "get_account", "close_call"],
        "customer_id": f"c-{case}-{rep}",
    }


def write_results(tmp_path: Path, *, pass_by_rep: dict[str, list[bool]], with_all_rows: bool = True) -> Path:
    """A results file for three repetitions; ``pass_by_rep`` maps a case id to its pass flag per repetition."""
    families = {"PUB-01": "Happy path", "PUB-05": "Hardship"}
    reps = [
        [row(case, families[case], index + 1, flags[index]) for case, flags in pass_by_rep.items()]
        for index in range(3)
    ]
    final = [
        {"id": case, "family": families[case], "passes": sum(flags), "runs": 3, "pass": all(flags)}
        for case, flags in pass_by_rep.items()
    ]
    data: dict[str, Any] = {"agent_id": "agent-1", "runs": 3, "final": final, "rows": reps[-1]}
    if with_all_rows:
        data["all_rows"] = reps
    path = tmp_path / "run.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def fetched(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the kit's ``freya`` module with a stub; the list collects the call ids asked for."""
    asked: list[str] = []
    stub = ModuleType("freya")

    def get_call(call_id: str) -> dict[str, Any]:
        asked.append(call_id)
        return {
            "messages": [{"role": "assistant", "message": f"hello from {call_id}", "secondsFromStart": 1.0}],
            "endedReason": "assistant-ended-call",
            "duration": 5,
        }

    stub.get_call = get_call  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "freya", stub)
    return asked


def run_triage(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["triage.py", *args])
    load_triage().main()


def test_a_case_that_failed_in_an_earlier_repetition_is_triaged(
    tmp_path: Path, fetched: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """REPO-05: PUB-05 fails in repetition 1 and passes in 2 and 3 - exactly the case the 3-of-3 rule loses."""
    results = write_results(tmp_path, pass_by_rep={"PUB-01": [True] * 3, "PUB-05": [False, True, True]})

    run_triage(monkeypatch, str(results))

    assert fetched == ["call-PUB-05-r1"]  # the failing repetition's call, not the last (passing) one
    out = capsys.readouterr().out
    assert "PUB-05" in out and "rep 1/3" in out and "pass=False" in out
    assert "ledger: wrong promise" in out and "hello from call-PUB-05-r1" in out
    assert "PUB-01" not in out  # passing cases stay out


def test_every_failing_repetition_is_listed_in_order(
    tmp_path: Path, fetched: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    results = write_results(tmp_path, pass_by_rep={"PUB-01": [True] * 3, "PUB-05": [False, True, False]})

    run_triage(monkeypatch, str(results))

    assert fetched == ["call-PUB-05-r1", "call-PUB-05-r3"]
    out = capsys.readouterr().out
    assert "rep 1/3" in out and "rep 3/3" in out and "rep 2/3" not in out
    assert "PUB-05 passed 1/3" in out  # the verdict per case is stated up front


def test_a_case_that_passes_in_every_repetition_prints_a_clean_message(
    tmp_path: Path, fetched: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    results = write_results(tmp_path, pass_by_rep={"PUB-01": [True] * 3, "PUB-05": [True] * 3})

    run_triage(monkeypatch, str(results))

    assert fetched == []
    assert "no failing" in capsys.readouterr().out.lower()


def test_only_shows_every_repetition_of_the_chosen_cases(
    tmp_path: Path, fetched: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    results = write_results(tmp_path, pass_by_rep={"PUB-01": [True] * 3, "PUB-05": [False, True, True]})

    run_triage(monkeypatch, str(results), "--only", "PUB-05")

    assert fetched == ["call-PUB-05-r1", "call-PUB-05-r2", "call-PUB-05-r3"]
    out = capsys.readouterr().out
    assert out.index("rep 1/3") < out.index("rep 2/3") < out.index("rep 3/3")
    assert "pass=True" in out and "pass=False" in out


def test_rep_restricts_the_triage_to_one_repetition(
    tmp_path: Path, fetched: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    results = write_results(tmp_path, pass_by_rep={"PUB-01": [True] * 3, "PUB-05": [False, True, True]})

    run_triage(monkeypatch, str(results), "--rep", "3")
    assert fetched == [] and "no failing" in capsys.readouterr().out.lower()

    run_triage(monkeypatch, str(results), "--rep", "1")
    assert fetched == ["call-PUB-05-r1"]

    run_triage(monkeypatch, str(results), "--rep", "last", "--all")
    assert fetched[1:] == ["call-PUB-01-r3", "call-PUB-05-r3"]


def test_all_covers_every_case_and_repetition(
    tmp_path: Path, fetched: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    results = write_results(tmp_path, pass_by_rep={"PUB-01": [True] * 3, "PUB-05": [False, True, True]})

    run_triage(monkeypatch, str(results), "--all")

    assert len(fetched) == 6 and fetched[:3] == ["call-PUB-01-r1", "call-PUB-01-r2", "call-PUB-01-r3"]


def test_a_results_file_without_all_rows_still_works(
    tmp_path: Path, fetched: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Older files (one repetition) only have ``rows``."""
    results = write_results(
        tmp_path,
        pass_by_rep={"PUB-01": [True, True, True], "PUB-05": [True, True, False]},
        with_all_rows=False,
    )

    run_triage(monkeypatch, str(results))

    assert fetched == ["call-PUB-05-r3"]
    assert "PUB-05" in capsys.readouterr().out


def test_a_call_that_cannot_be_fetched_does_not_stop_the_triage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stub = ModuleType("freya")

    def get_call(call_id: str) -> dict[str, Any]:
        raise RuntimeError(f"401 for {call_id}")

    stub.get_call = get_call  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "freya", stub)
    results = write_results(tmp_path, pass_by_rep={"PUB-01": [True] * 3, "PUB-05": [False, False, True]})

    run_triage(monkeypatch, str(results))

    out = capsys.readouterr().out
    assert out.count("could not fetch call") == 2 and "rep 2/3" in out


def edit_results(path: Path, mutate: Any) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_the_strict_final_verdict_chooses_the_cases_even_when_its_pass_flag_is_lenient(
    tmp_path: Path, fetched: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hidden benchmark wants every repetition: 2 of 3 is a lost case whatever the ``pass`` flag says."""
    results = write_results(tmp_path, pass_by_rep={"PUB-01": [True] * 3, "PUB-05": [True, False, True]})

    def lenient(data: dict[str, Any]) -> None:
        for entry in data["final"]:
            entry["pass"] = entry["passes"] >= 2  # a majority rule, as an older run tool used

    edit_results(results, lenient)

    run_triage(monkeypatch, str(results))

    assert fetched == ["call-PUB-05-r2"]
    assert "PUB-05 passed 2/3" in capsys.readouterr().out


def test_a_case_the_final_verdict_calls_passed_is_not_dragged_in_by_a_stale_row(
    tmp_path: Path, fetched: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``final`` is the verdict; a failing row of a case that passed every repetition there is not triaged."""
    results = write_results(tmp_path, pass_by_rep={"PUB-01": [True] * 3, "PUB-05": [True] * 3})

    def stale(data: dict[str, Any]) -> None:
        data["all_rows"][0][1]["pass"] = False

    edit_results(results, stale)

    run_triage(monkeypatch, str(results))

    assert fetched == []
    assert "no failing" in capsys.readouterr().out.lower()


def test_cases_missing_from_final_fall_back_to_their_rows(
    tmp_path: Path, fetched: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    results = write_results(tmp_path, pass_by_rep={"PUB-01": [True] * 3, "PUB-05": [False, True, True]})
    edit_results(results, lambda data: data.pop("final"))

    run_triage(monkeypatch, str(results))

    assert fetched == ["call-PUB-05-r1"]


def test_a_lost_case_without_a_stored_failing_repetition_is_reported_not_silently_skipped(
    tmp_path: Path, fetched: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An old file keeps only the last repetition: ``final`` says PUB-05 was lost, but its last row passed."""
    results = write_results(
        tmp_path, pass_by_rep={"PUB-01": [True] * 3, "PUB-05": [False, True, True]}, with_all_rows=False
    )

    run_triage(monkeypatch, str(results))

    out = capsys.readouterr().out
    assert fetched == []
    assert "PUB-05 passed 2/3" in out
    (note,) = [line for line in out.splitlines() if "no failing repetition is stored" in line]
    assert note.startswith("PUB-05")


def test_the_dotenv_reaches_the_kit_client_before_it_is_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REPO-18: the documented command works with the key in ``.env`` only (the kit reads its key at import)."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FREYA_API_KEY=fak_dotenv_only_value_0001" + chr(10) + "FREYA_API_URL=http://localhost:9" + chr(10),
        encoding="utf-8",
    )
    monkeypatch.setitem(Settings.model_config, "env_file", env_file)
    monkeypatch.setattr(os, "environ", {k: v for k, v in os.environ.items() if not k.startswith("FREYA_")})
    monkeypatch.setitem(sys.modules, "freya", ModuleType("placeholder"))
    del sys.modules["freya"]  # the real kit client is imported afresh; the fixture restores the old entry
    results = write_results(tmp_path, pass_by_rep={"PUB-01": [True] * 3})

    run_triage(monkeypatch, str(results))

    kit = sys.modules["freya"]
    assert kit.HEADERS["Authorization"] == "Bearer fak_dotenv_only_value_0001"
    assert kit.URL == "http://localhost:9"


def test_an_unreadable_dotenv_setting_does_not_stop_the_triage(
    tmp_path: Path, fetched: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DRY_RUN=maybe-SECRETISH-value" + chr(10), encoding="utf-8")
    monkeypatch.setitem(Settings.model_config, "env_file", env_file)
    results = write_results(tmp_path, pass_by_rep={"PUB-05": [False, True, True]})

    run_triage(monkeypatch, str(results))

    captured = capsys.readouterr()
    assert fetched == ["call-PUB-05-r1"]  # the exported environment is used as it is
    assert "SECRETISH" not in captured.out + captured.err  # never echo what the file contained


def test_render_call_prints_the_timeline() -> None:
    triage = load_triage()
    call = {
        "messages": [
            {"role": "assistant", "message": "Good morning.", "secondsFromStart": 0.5},
            {"role": "user", "message": "Hello.", "secondsFromStart": 2.0},
            {
                "role": "system",
                "message": json.dumps({"event": "node_enter", "node": "greet", "from_node": None}),
            },
        ],
        "endedReason": "customer-ended-call",
        "duration": 12,
    }
    lines = triage.render_call(call)
    assert any("AGENT" in line and "Good morning." in line for line in lines)
    assert any("CUSTOMER" in line and "Hello." in line for line in lines)
    assert any("> node greet" in line for line in lines)
    assert lines[-1].strip() == "ended: customer-ended-call  duration: 12"
