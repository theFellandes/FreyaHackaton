"""The official final command shape end to end, offline: ``build.py {case_folder} {mock_url}``.

The folder is invented (``tests/unit/_case_folder_data.py``) and holds the kit's Brief A text as its brief (so the
fixture ledger and IR answer the model calls), a README, Word / Excel documents, announcements, test customers, example
conversations, an image and junk. With a machine-readable contract in the folder the run goes through ``main()``; with a
prose contract only, the OpenAPI is derived by one model call (answered by the test) and saved next to the workflow.
Either way every tool request of the compiled agent goes to the mock and the test customers never reach the policy stages.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest

from builder.build import main, run_build
from builder.inputs import classify_inputs
from builder.settings import Settings
from builder.testing import FixtureGateway
from builder.trace import NullTrace
from builder.understand.contract import DerivedOpenApi
from tests.graph.conftest import load_ir, load_ledger
from tests.unit._case_folder_data import turkish_api_document, write_case_folder

MOCK = "https://banka-mock.ornek.workers.dev"
URL_RE = re.compile(r"https?://[^\s\"'<>\\]+")


def hosts_in(workflow: dict[str, Any]) -> set[str]:
    return {urlsplit(url).hostname or "" for url in URL_RE.findall(json.dumps(workflow, ensure_ascii=False))}


def case_folder(tmp_path: Path, brief_a_path: Path, *, openapi: bool) -> Path:
    return write_case_folder(
        tmp_path,
        openapi=turkish_api_document() if openapi else None,
        brief=brief_a_path.read_text(encoding="utf-8"),
        brief_name="brief-a.md",
    )


def test_the_official_command_shape_builds_against_the_mock(
    capsys: pytest.CaptureFixture[str], tmp_dirs: SimpleNamespace, tmp_path: Path, brief_a_path: Path,
    fixture_dir: Path,
) -> None:
    folder = case_folder(tmp_path, brief_a_path, openapi=True)
    code = main([str(folder), MOCK, "--dry-run", "--fake-llm", str(fixture_dir)])
    captured = capsys.readouterr()
    summary = json.loads([line for line in captured.out.splitlines() if line.strip()][-1])
    assert code == 0 and summary["status"] == "dry_run", summary
    assert summary["slot"] == "banka-vaka-klasoru"
    workflow = json.loads(Path(summary["workflow_file"]).read_text(encoding="utf-8"))
    assert hosts_in(workflow) == {urlsplit(MOCK).hostname}
    trace = Path(summary["trace"]).read_text(encoding="utf-8")
    assert MOCK in trace  # the base URL is traced (it is not a secret)
    assert "test-musterileri.xlsx" in trace and "akis-semasi.png" in trace
    assert "M-1001" not in trace  # test customers are named in the trace, never read into the policy


async def test_a_prose_contract_is_turned_into_a_saved_openapi(
    make_settings: Any, tmp_dirs: SimpleNamespace, tmp_path: Path, brief_a_path: Path,
) -> None:
    folder = case_folder(tmp_path, brief_a_path, openapi=False)
    settings: Settings = make_settings(dry_run=True)
    inputs = classify_inputs([folder, MOCK], settings=settings)
    document = turkish_api_document()
    trace = NullTrace()
    llm = FixtureGateway(load_ledger("brief_a"), load_ir("brief_a"), trace=trace,
                         responders={"DerivedOpenApi": lambda call: DerivedOpenApi(document=document)})
    final = await run_build(settings, inputs, llm=llm, trace=trace)
    assert final["status"] == "dry_run", final.get("errors")
    saved = tmp_dirs.out / f"{inputs.slot}.openapi.json"
    assert json.loads(saved.read_text(encoding="utf-8")) == document
    derive_calls = llm.purposes("tool API")
    assert len(derive_calls) == 1 and "musteri_no" in derive_calls[0].user
    assert "M-1001" not in "".join(call.user for call in llm.calls)  # customers never reach a model
    workflow = json.loads(Path(final["workflow_file"]).read_text(encoding="utf-8"))
    assert hosts_in(workflow) == {urlsplit(MOCK).hostname}
    assert any(saved.name in json.dumps(event) for event in trace.events)


async def test_a_contract_without_the_required_operations_fails_clearly(
    make_settings: Any, tmp_dirs: SimpleNamespace, tmp_path: Path, brief_a_path: Path,
) -> None:
    folder = case_folder(tmp_path, brief_a_path, openapi=False)
    settings: Settings = make_settings(dry_run=True)
    inputs = classify_inputs([folder, MOCK], settings=settings)
    document = turkish_api_document()
    for path in [p for p, item in document["paths"].items() if item["post"].get("x-freya-role") == "close_call"]:
        document["paths"][path]["post"]["x-freya-role"] = "other"
    trace = NullTrace()
    llm = FixtureGateway(load_ledger("brief_a"), load_ir("brief_a"), trace=trace,
                         responders={"DerivedOpenApi": lambda call: DerivedOpenApi(document=document)})
    final = await run_build(settings, inputs, llm=llm, trace=trace)
    assert final["status"] == "failed"
    assert any("close_call" in error for error in final["errors"]), final["errors"]


async def test_an_unreadable_document_of_a_case_folder_is_skipped_not_fatal(
    make_settings: Any, tmp_dirs: SimpleNamespace, tmp_path: Path, brief_a_path: Path,
) -> None:
    folder = case_folder(tmp_path, brief_a_path, openapi=True)
    (folder / "duyurular" / "eski-duyuru.pdf").write_bytes(b"%PDF-1.4 broken")
    settings: Settings = make_settings(dry_run=True)
    inputs = classify_inputs([folder, MOCK], settings=settings)
    trace = NullTrace()
    llm = FixtureGateway(load_ledger("brief_a"), load_ir("brief_a"), trace=trace)
    final = await run_build(settings, inputs, llm=llm, trace=trace)
    assert final["status"] == "dry_run", final.get("errors")
    assert any("eski-duyuru.pdf" in event.get("message", "") for event in trace.of_kind("error"))
