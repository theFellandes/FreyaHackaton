"""Tests for builder.inputs: tool API detection, directory expansion, errors and slot selection."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from builder.inputs import InputBundle, InputError, classify_inputs, is_tool_api, slug_for
from builder.settings import Settings

MakeSettings = Callable[..., Settings]

YAML_API = """\
openapi: 3.0.3
info: {title: Bank tools, version: "1"}
paths:
  /close_call:
    post:
      summary: End the call.
"""


@pytest.fixture
def kit_copy(tmp_path: Path, brief_a_path: Path, brief_b_path: Path, kit_openapi_path: Path) -> Path:
    """Copies of the kit inputs in a scratch directory (kit files are never touched)."""
    directory = tmp_path / "inputs"
    directory.mkdir()
    for source in (brief_a_path, brief_b_path, kit_openapi_path):
        shutil.copy(source, directory / source.name)
    return directory


def test_organiser_command_line_shape(kit_copy: Path, settings: Settings) -> None:
    bundle = classify_inputs([kit_copy / "brief-a.md", kit_copy / "openapi.json"], settings=settings)
    assert isinstance(bundle, InputBundle)
    assert bundle.brief_paths == [(kit_copy / "brief-a.md").resolve()]
    assert bundle.tool_api_path == (kit_copy / "openapi.json").resolve()
    assert bundle.slot == "brief-a"
    assert bundle.as_dict()["slot"] == "brief-a"


def test_tool_api_may_come_first_and_brief_order_is_kept(kit_copy: Path, settings: Settings) -> None:
    bundle = classify_inputs(
        [str(kit_copy / "openapi.json"), str(kit_copy / "brief-b.md"), str(kit_copy / "brief-a.md")],
        settings=settings,
    )
    assert [path.name for path in bundle.brief_paths] == ["brief-b.md", "brief-a.md"]
    assert bundle.slot == "brief-b-brief-a"


def test_directory_is_expanded_sorted_without_junk(kit_copy: Path, settings: Settings) -> None:
    (kit_copy / ".hidden.md").write_text("ignored", encoding="utf-8")
    (kit_copy / "~$lock.docx").write_text("ignored", encoding="utf-8")
    (kit_copy / ".git").mkdir()
    (kit_copy / ".git" / "config").write_text("ignored", encoding="utf-8")
    (kit_copy / "z-memo").mkdir()
    (kit_copy / "z-memo" / "amendment.txt").write_text("Rule 5 now reads ...", encoding="utf-8")

    bundle = classify_inputs([kit_copy], settings=settings)

    assert [path.name for path in bundle.brief_paths] == ["brief-a.md", "brief-b.md", "amendment.txt"]
    assert bundle.tool_api_path.name == "openapi.json"
    assert bundle.slot == "brief-a-brief-b-amendment"


def test_duplicate_arguments_are_collapsed(kit_copy: Path, settings: Settings) -> None:
    brief = kit_copy / "brief-a.md"
    bundle = classify_inputs([brief, kit_copy, brief], settings=settings)
    assert [path.name for path in bundle.brief_paths] == ["brief-a.md", "brief-b.md"]


def test_yaml_tool_api_with_any_extension(tmp_path: Path, brief_a_path: Path, settings: Settings) -> None:
    api = tmp_path / "bank-tools.txt"
    api.write_text(YAML_API, encoding="utf-8")
    assert is_tool_api(api)
    bundle = classify_inputs([brief_a_path, api], settings=settings)
    assert bundle.tool_api_path == api.resolve()


@pytest.mark.parametrize(
    "content",
    [
        '{"title": "a JSON code list", "codes": ["A", "B"]}',
        "openapi_version: 3\nnot: the tool api\n",
        "# Markdown\n\nopenapi: mentioned in prose\n",
        "- openapi\n- swagger\n",
        "",
    ],
)
def test_non_api_files_are_briefs(tmp_path: Path, content: str) -> None:
    path = tmp_path / "file.json"
    path.write_text(content, encoding="utf-8")
    assert not is_tool_api(path)


def test_binary_and_swagger_detection(tmp_path: Path) -> None:
    binary = tmp_path / "scan.pdf"
    binary.write_bytes(b"%PDF-1.4\n\xff\xfe\x00binary")
    assert not is_tool_api(binary)
    swagger = tmp_path / "old.json"
    swagger.write_text('{"swagger": "2.0", "paths": {}}', encoding="utf-8")
    assert is_tool_api(swagger)
    bom = tmp_path / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf" + b'{"openapi": "3.1.0"}')
    assert is_tool_api(bom)


def test_missing_input_is_reported(tmp_path: Path, settings: Settings) -> None:
    with pytest.raises(InputError, match="Input not found"):
        classify_inputs([tmp_path / "nope.md"], settings=settings)


def test_no_tool_api_is_reported(brief_a_path: Path, settings: Settings) -> None:
    with pytest.raises(InputError, match="No tool API file found"):
        classify_inputs([brief_a_path], settings=settings)


def test_two_tool_apis_are_reported(tmp_path: Path, kit_copy: Path, settings: Settings) -> None:
    second = tmp_path / "second.yaml"
    second.write_text(YAML_API, encoding="utf-8")
    with pytest.raises(InputError, match="exactly one tool API") as info:
        classify_inputs([kit_copy, second], settings=settings)
    assert "openapi.json" in str(info.value) and "second.yaml" in str(info.value)


def test_tool_api_alone_is_not_enough(kit_openapi_path: Path, settings: Settings) -> None:
    with pytest.raises(InputError, match="No brief file"):
        classify_inputs([kit_openapi_path], settings=settings)


def test_empty_inputs_are_reported(tmp_path: Path, settings: Settings) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(InputError, match="contains no files"):
        classify_inputs([empty], settings=settings)
    with pytest.raises(InputError, match="No input files"):
        classify_inputs([], settings=settings)


# --- slot -------------------------------------------------------------------------------------------------------------


def test_explicit_slot_beats_configured_slot(kit_copy: Path, make_settings: MakeSettings) -> None:
    args = [kit_copy / "brief-a.md", kit_copy / "openapi.json"]
    configured = make_settings(build_slot="from-settings")
    assert classify_inputs(args, slot="Explicit_1.0", settings=configured).slot == "Explicit_1.0"
    assert classify_inputs(args, settings=configured).slot == "from-settings"
    assert classify_inputs(args, slot="  ", settings=configured).slot == "from-settings"


def test_slot_falls_back_to_get_settings(kit_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUILD_SLOT", "env-slot")
    bundle = classify_inputs([kit_copy / "brief-a.md", kit_copy / "openapi.json"])
    assert bundle.slot == "env-slot"


@pytest.mark.parametrize("bad", ["has space", "../escape", "a/b", "-leading", "x" * 65])
def test_unsafe_explicit_slots_are_rejected(kit_copy: Path, settings: Settings, bad: str) -> None:
    with pytest.raises(InputError, match="Invalid slot"):
        classify_inputs([kit_copy / "brief-a.md", kit_copy / "openapi.json"], slot=bad, settings=settings)


@pytest.mark.parametrize(
    ("stems", "expected"),
    [
        (["brief-a"], "brief-a"),
        (["Brief A", "brief_b"], "brief-a-brief-b"),
        (["Politika (v2) — FINAL!!"], "politika-v2-final"),
        (["___"], "brief"),
        (["a" * 30, "b" * 30], "a" * 30 + "-" + "b" * 17),
        (["x" * 47 + "-", "tail"], "x" * 47),
    ],
)
def test_slug_rules(stems: list[str], expected: str) -> None:
    slug = slug_for(stems)
    assert slug == expected
    assert len(slug) <= 48
    assert all(ch.isascii() and (ch.islower() or ch.isdigit() or ch == "-") for ch in slug)
    assert not slug.startswith("-") and not slug.endswith("-")
