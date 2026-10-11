"""Tests for builder.viz: the builder-graph diagram (drawn from the real LangGraph) and the Freya workflow diagram.

The builder diagram is compared edge by edge with ``build_graph().get_graph()`` / ``edge_list()``, so a node or an edge
added to the graph without regenerating ``docs/graphs/builder_graph*.mmd`` fails here. The workflow diagram is checked on
small hand-built workflows (one assertion per rule: shapes, labels, dashes, folding) and on the committed reference
workflows (every arrow joins two declared boxes).
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

import pytest
from langgraph.graph import END, START

from builder import viz
from builder.graph.build_graph import build_graph, edge_list
from builder.graph.nodes import NODES

REPO = pathlib.Path(__file__).resolve().parents[2]
GRAPHS = REPO / "docs" / "graphs"

_DECLARATION = re.compile(r"^ {4,}([A-Za-z_]\w*)[\(\[\{]")
_EDGE = re.compile(r'^    ([A-Za-z_]\w*) (-->|-\.->)(?:\|"([^"]*)"\|)? ([A-Za-z_]\w*)$')


def parse(text: str) -> tuple[set[str], list[tuple[str, str, str, str]]]:
    """Ids of every box drawn (declared, or only named by an arrow) and ``(source, arrow, label, target)`` per arrow."""
    declared: set[str] = set()
    edges: list[tuple[str, str, str, str]] = []
    for line in text.splitlines():
        edge = _EDGE.match(line)
        if edge:
            edges.append((edge.group(1), edge.group(2), edge.group(3) or "", edge.group(4)))
            declared.update((edge.group(1), edge.group(4)))
            continue
        decl = _DECLARATION.match(line)
        if decl:
            declared.add(decl.group(1))
    return declared, edges


def assert_well_formed(text: str) -> tuple[set[str], list[tuple[str, str, str, str]]]:
    """Every class / link-style reference is valid and the text is one flowchart ending in a single newline."""
    declared, edges = parse(text)
    assert text.startswith("flowchart ") and text.endswith("\n") and not text.endswith("\n\n")
    assert text.count("flowchart ") == 1 and "\r" not in text
    for line in text.splitlines():
        match = re.match(r"^    linkStyle ([\d,]+) ", line)
        if match:
            assert all(int(i) < len(edges) for i in match.group(1).split(","))
        match = re.match(r"^    class ([\w,]+) \w+$", line)
        if match:
            assert set(match.group(1).split(",")) <= declared
    return declared, edges


# ------------------------------------------------------------------------------------------------ builder graph


def real_edges() -> set[tuple[str, str]]:
    return {(a, b) for a, b, _ in edge_list()}


def test_langgraph_output_is_parsed_into_nodes_and_edges() -> None:
    nodes, edges = viz.parse_langgraph_mermaid(build_graph().get_graph().draw_mermaid())
    assert set(nodes) == set(NODES) | {START, END}
    assert {(a, b) for a, b, _ in edges} == real_edges()
    assert len(edges) == len(build_graph().get_graph().edges)


def test_unreadable_langgraph_output_is_an_error() -> None:
    with pytest.raises(ValueError, match="could not read"):
        viz.parse_langgraph_mermaid("graph TD;\n")


def test_full_view_draws_every_edge_of_the_real_graph() -> None:
    declared, edges = assert_well_formed(viz.builder_mermaid("full"))
    assert declared == set(NODES) | {START, END}
    assert {(s, t) for s, _a, _l, t in edges} == real_edges()
    assert len(edges) == len(edge_list())  # nothing dropped, nothing duplicated


def test_compact_view_only_folds_failure_exits() -> None:
    declared, edges = assert_well_formed(viz.builder_mermaid("compact"))
    assert declared == set(NODES) | {START, END, "exits"}
    drawn = {(s, t) for s, _a, _l, t in edges if s != "exits"}
    assert drawn < real_edges()
    for source, target in real_edges() - drawn:
        assert target in ("finalize", "agentic_build") and source != target
    assert {("guard_workflow", "finalize"), ("decide", "finalize"), ("guard_inputs", "agentic_build")} <= drawn
    assert {t for s, _a, _l, t in edges if s == "exits"} == {"finalize", "agentic_build"}


def test_every_node_has_a_role_and_every_label_names_a_real_edge() -> None:
    assert set(viz.NODE_ROLES) == set(NODES)
    assert set(viz.EDGE_LABELS) <= real_edges()


def class_of(text: str) -> dict[str, str]:
    """``{box id: class name}`` from the ``class a,b name`` statements of a diagram."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^    class ([\w,]+) (\w+)$", line)
        if match:
            out.update({member: match.group(2) for member in match.group(1).split(",")})
    return out


@pytest.mark.parametrize("view", ["compact", "full"])
def test_every_builder_node_gets_its_role_class(view: str) -> None:
    text = viz.builder_mermaid(view)
    classes = class_of(text)
    for name in NODES:
        assert classes[name] == viz.NODE_ROLES[name], name
    assert classes[START] == classes[END] == "plain"
    for role in set(classes.values()):
        assert f"    classDef {role} " in text  # no class is used without a style


def test_full_view_makes_failure_exits_pale() -> None:
    text = viz.builder_mermaid("full")
    _declared, edges = parse(text)
    pale = {int(i) for i in re.search(r"linkStyle ([\d,]+) stroke:#b4b8c0", text).group(1).split(",")}  # type: ignore[union-attr]
    assert pale
    for index in pale:
        _source, _arrow, _label, target = edges[index]
        assert target in ("finalize", "agentic_build")


def test_builder_direction_and_view_are_validated() -> None:
    assert "flowchart LR" in viz.builder_mermaid(direction="LR")
    with pytest.raises(ValueError, match="view"):
        viz.builder_mermaid("main")
    with pytest.raises(ValueError, match="direction"):
        viz.builder_mermaid(direction="sideways")


def test_builder_output_is_deterministic() -> None:
    assert viz.builder_mermaid() == viz.builder_mermaid()


@pytest.mark.parametrize(("view", "name"), [("compact", "builder_graph.mmd"), ("full", "builder_graph_full.mmd")])
def test_committed_builder_diagrams_are_current(view: str, name: str) -> None:
    committed = (GRAPHS / name).read_text(encoding="utf-8")
    assert committed == viz.builder_mermaid(view), (
        f"docs/graphs/{name} is stale: run `python -m builder.viz builder"
        f"{'' if view == 'compact' else ' --view full'} --out docs/graphs/{name}`"
    )


# ------------------------------------------------------------------------------------------------ workflow diagrams


def tool(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "handler": "__function__:api_call"}}


def transition(target: str, condition: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {"target_node": target, "condition": condition or {"operator": "always"}, **extra}


def sample_workflow() -> dict[str, Any]:
    intent = {"operator": "matches_intent", "description": "The customer confirmed"}
    flag = {
        "logic": "or",
        "conditions": [
            {"variable": "check.ok", "operator": "equals", "operand": "true"},
            {"variable": "check.ok", "operator": "equals", "operand": "True"},
        ],
    }
    return {
        "initial_node": "prep",
        "nodes": {
            "prep": {
                "node_type": "code",
                "passthrough": True,
                "passthrough_default_transition": "talk",
                "transitions": [transition("talk")],
            },
            "talk": {
                "node_type": "conversation",
                "tools": [tool("verify_identity")],
                "max_turn_transition": "bye",
                "transitions": [
                    transition("lookup", flag),
                    transition("bye", intent, is_tool_call=True),
                    transition("later", intent),
                ],
            },
            "later": {"node_type": "conversation", "passthrough": True, "transitions": [transition("lookup")]},
            "lookup": {
                "node_type": "api_request",
                "passthrough": True,
                "passthrough_default_transition": "bye",
                "pre_actions": [tool("get_account"), tool("code_exec")],
                "transitions": [
                    transition("bye", {"variable": "n", "operator": "greater_than", "operand": 0}),
                    transition("bye"),
                ],
            },
            "bye": {"node_type": "end_call", "transitions": []},
        },
    }


def test_each_node_type_has_its_own_shape_and_class() -> None:
    declared, _edges = assert_well_formed(viz.workflow_mermaid(sample_workflow()))
    text = viz.workflow_mermaid(sample_workflow())
    assert 'prep["prep"]' not in text  # a plain rectangle that an arrow names needs no declaration of its own
    assert 'talk("talk<br/><i>verify_identity</i>")' in text  # conversation: rounded, with its model-called tools
    assert 'lookup{{"lookup<br/><i>get_account</i>"}}' in text  # api_request: hexagon, code_exec is not an API
    assert 'bye(["bye"])' in text  # end_call: stadium
    assert re.search(r"^    class prep code$", text, re.MULTILINE)
    assert re.search(r"^    class talk conv$", text, re.MULTILINE)
    assert re.search(r"^    class later say$", text, re.MULTILINE)  # passthrough conversation: speaks and moves on
    assert re.search(r"^    class lookup api$", text, re.MULTILINE)
    assert re.search(r"^    class bye term$", text, re.MULTILINE)
    assert {"prep", "talk", "later", "lookup", "bye"} <= declared


def test_unreferenced_and_oddly_named_boxes_are_still_declared() -> None:
    wf = sample_workflow()
    wf["nodes"]["orphan"] = {"node_type": "code", "transitions": []}  # no arrow names it
    wf["nodes"]["two words"] = {"node_type": "code", "transitions": []}  # its id differs from its label
    text = viz.workflow_mermaid(wf)
    assert 'orphan["orphan"]' in text and 'two_words["two words"]' in text
    assert class_of(text)["orphan"] == "code"


def test_edge_labels_and_dashes() -> None:
    _declared, edges = assert_well_formed(viz.workflow_mermaid(sample_workflow()))
    by = {(s, t, label): arrow for s, arrow, label, t in edges}
    # start arrow, then the passthrough 'always' of prep (dashed, drawn once although it is also the default)
    assert by[("prep", "talk", "always")] == "-.->"
    assert sum(1 for s, _a, _l, t in edges if (s, t) == ("prep", "talk")) == 1
    assert by[("talk", "lookup", "check.ok = true")] == "-->"  # 'true' / 'True' read as one test
    assert by[("talk", "bye", "tool:transition_to_bye")] == "-->"  # tool-call transition
    assert by[("talk", "later", "intent: The customer confirmed")] == "-->"
    assert by[("talk", "bye", "max turns")] == "-.->"
    assert by[("later", "lookup", "always")] == "-.->"
    assert by[("lookup", "bye", "n > 0")] == "-->"
    assert by[("lookup", "bye", "always")] == "-.->"


def test_tool_call_edges_are_coloured() -> None:
    text = viz.workflow_mermaid(sample_workflow())
    _declared, edges = parse(text)
    index = next(i for i, e in enumerate(edges) if e[2] == "tool:transition_to_bye")
    assert f"linkStyle {index} stroke:#2f6fdb" in text


def test_passthrough_default_without_a_matching_transition_gets_its_own_dashed_edge() -> None:
    wf = {
        "initial_node": "a",
        "nodes": {
            "a": {"node_type": "code", "passthrough": True, "passthrough_default_transition": "b", "transitions": []},
            "b": {"node_type": "end_call", "transitions": []},
        },
    }
    _declared, edges = assert_well_formed(viz.workflow_mermaid(wf))
    assert ("a", "-.->", "passthrough default", "b") in edges


def test_unknown_targets_and_global_nodes_are_drawn() -> None:
    wf = sample_workflow()
    wf["global_nodes"] = {
        "off_topic": {
            "node_type": "conversation",
            "passthrough": True,
            "condition": {"operator": "matches_intent", "description": "Talks about something else"},
            "passthrough_default_transition": "__previous__",
            "transitions": [transition("__previous__")],
        }
    }
    wf["nodes"]["talk"]["transitions"].append(transition("nowhere"))
    text = viz.workflow_mermaid(wf)
    declared, edges = assert_well_formed(text)
    assert 'subgraph global_nodes ["Global nodes: can fire from any node"]' in text
    assert {"off_topic", "missing___previous__", "missing_nowhere"} <= declared
    assert ("off_topic", "-.->", "always", "missing___previous__") in edges
    assert any(label == "intent: Talks about something else" and t == "off_topic" for _s, _a, label, t in edges)


def test_a_record_that_wraps_the_workflow_is_unwrapped() -> None:
    assert viz.workflow_mermaid({"workflow": sample_workflow()}) == viz.workflow_mermaid(sample_workflow())


def test_awkward_keys_and_labels_stay_valid() -> None:
    wf = {
        "initial_node": "end",
        "nodes": {
            "end": {"node_type": "conversation", "transitions": [transition("my node", {"operator": "matches_intent", "description": 'Says "no" <now> & leaves'})]},
            "my node": {"node_type": "end_call", "transitions": []},
        },
    }
    text = viz.workflow_mermaid(wf)
    declared, edges = assert_well_formed(text)
    assert "n_end" in declared and "my_node" in declared  # 'end' is a Mermaid keyword
    label = next(label for _s, _a, label, _t in edges if label.startswith("intent"))
    assert '"' not in label and "<" not in label and "#quot;" in label and "#lt;" in label


def test_long_labels_are_shortened() -> None:
    long = "intent " * 30
    wf = {
        "initial_node": "a",
        "nodes": {
            "a": {"node_type": "conversation", "transitions": [transition("b", {"operator": "matches_intent", "description": long})]},
            "b": {"node_type": "end_call", "transitions": []},
        },
    }
    _declared, edges = assert_well_formed(viz.workflow_mermaid(wf))
    label = next(label for _s, _a, label, _t in edges if label.startswith("intent"))
    assert label.endswith("...") and len(label) <= 70


def family_workflow() -> dict[str, Any]:
    nodes: dict[str, Any] = {
        "talk": {
            "node_type": "conversation",
            "transitions": [transition(f"end_{reason}", is_tool_call=True) for reason in ("a", "b", "c")]
            + [transition("end_d", {"operator": "matches_intent", "description": "goodbye"})],
        },
        "decide": {"node_type": "code", "passthrough": True, "passthrough_default_transition": "bye", "transitions": [transition("bye")]},
        "bye": {"node_type": "end_call", "transitions": []},
    }
    for reason in ("a", "b", "c", "d"):
        nodes[f"end_{reason}"] = {
            "node_type": "code",
            "passthrough": True,
            "passthrough_default_transition": "decide",
            "transitions": [transition("decide")],
        }
    return {"initial_node": "talk", "nodes": nodes}


def test_compact_view_folds_a_family_of_sibling_nodes() -> None:
    text = viz.workflow_mermaid(family_workflow())
    declared, edges = assert_well_formed(text)
    assert not {f"end_{r}" for r in "abcd"} & declared
    assert 'grp_end["end_*<br/><i>4 nodes</i>"]' in text
    assert ("talk", "-->", "tool:transition_to_end_* x3; intent: goodbye", "grp_end") in edges
    assert [e for e in edges if e[0] == "grp_end"] == [("grp_end", "-.->", "always", "decide")]
    assert re.search(r"^    class (?:\w+,)*grp_end(?:,\w+)* code$", text, re.MULTILINE)  # the family is a code box


def test_full_view_keeps_every_node_and_edge() -> None:
    declared, edges = assert_well_formed(viz.workflow_mermaid(family_workflow(), view="full"))
    assert {f"end_{r}" for r in "abcd"} <= declared
    assert len([e for e in edges if e[0] == "talk"]) == 4
    assert len([e for e in edges if e[3] == "decide"]) == 4


def test_two_look_alikes_are_not_folded() -> None:
    wf = family_workflow()
    del wf["nodes"]["end_c"], wf["nodes"]["end_d"]
    wf["nodes"]["talk"]["transitions"] = [transition("end_a", is_tool_call=True), transition("end_b", is_tool_call=True)]
    declared, _edges = assert_well_formed(viz.workflow_mermaid(wf))
    assert {"end_a", "end_b"} <= declared


def test_workflow_input_errors() -> None:
    with pytest.raises(ValueError, match="no nodes"):
        viz.workflow_mermaid({"nodes": {}})
    with pytest.raises(ValueError, match="view"):
        viz.workflow_mermaid(sample_workflow(), view="tiny")
    with pytest.raises(ValueError, match="direction"):
        viz.workflow_mermaid(sample_workflow(), direction="up")


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        (None, ""),
        ({"operator": "always"}, "always"),
        ({"variable": "x", "operator": "is_set"}, "x is set"),
        ({"variable": "x", "operator": "not_equals", "operand": "a"}, "x != a"),
        ({"variable": "n", "operator": "less_than", "operand": 3}, "n < 3"),
        ({"logic": "and", "conditions": [{"variable": "a", "operator": "equals", "operand": "1"}, {"variable": "b", "operator": "contains", "operand": "z"}]}, "a = 1 and b contains z"),
        ({"logic": "or", "conditions": [{"logic": "and", "conditions": [{"variable": "a", "operator": "equals", "operand": "1"}, {"variable": "b", "operator": "equals", "operand": "2"}]}, {"variable": "c", "operator": "equals", "operand": "3"}]}, "(a = 1 and b = 2) or c = 3"),
    ],
)
def test_condition_labels(condition: dict[str, Any] | None, expected: str) -> None:
    assert viz.condition_label(condition) == expected


@pytest.mark.parametrize("view", ["compact", "full"])
@pytest.mark.parametrize("name", sorted(p.name for p in (REPO / "evals" / "reference" / "out").glob("*.workflow.json")))
def test_reference_workflows_render(name: str, view: str) -> None:
    data = json.loads((REPO / "evals" / "reference" / "out" / name).read_text(encoding="utf-8"))
    declared, edges = assert_well_formed(viz.workflow_mermaid(data, view=view))
    nodes = viz.unwrap_workflow(data)["nodes"]
    if view == "full":
        assert len(declared) == len(nodes) + 1  # every node and the START marker
    else:
        assert len(declared) <= len(nodes) + 1
    assert edges


# ------------------------------------------------------------------------------------------------ command line


def test_cli_builder_writes_the_file(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "sub" / "builder.mmd"
    assert viz.main(["builder", "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == viz.builder_mermaid()
    assert b"\r" not in out.read_bytes()
    assert "wrote" in capsys.readouterr().err


def test_cli_agent_prints_to_stdout(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "wf.json"
    path.write_text(json.dumps(sample_workflow()), encoding="utf-8")
    assert viz.main(["agent", str(path), "--view", "full", "--direction", "LR"]) == 0
    assert capsys.readouterr().out == viz.workflow_mermaid(sample_workflow(), view="full", direction="LR")


def test_cli_reports_unreadable_input(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert viz.main(["agent", str(tmp_path / "missing.json")]) == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert viz.main(["agent", str(bad)]) == 2
    assert "builder.viz:" in capsys.readouterr().err


# ------------------------------------------------------------------------------------------------ the documents


def test_sync_replaces_only_marked_blocks(tmp_path: pathlib.Path) -> None:
    (tmp_path / "a.mmd").write_text("flowchart TD\n    a --> b\n", encoding="utf-8")
    doc = tmp_path / "doc.md"
    other = "```mermaid\nflowchart TD\n    keep --> me\n```\n"
    doc.write_text(f"intro\n\n<!-- mmd: a.mmd -->\n```mermaid\nstale\n```\n\n{other}", encoding="utf-8")
    assert viz.sync_embedded(doc, tmp_path, write=False) is False
    assert "stale" in doc.read_text(encoding="utf-8")  # a check never writes
    assert viz.sync_embedded(doc, tmp_path) is False
    assert doc.read_text(encoding="utf-8") == (
        f"intro\n\n<!-- mmd: a.mmd -->\n```mermaid\nflowchart TD\n    a --> b\n```\n\n{other}"
    )
    assert viz.sync_embedded(doc, tmp_path) is True  # second run: nothing to do


def test_sync_rejects_a_marker_without_a_file(tmp_path: pathlib.Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("<!-- mmd: missing.mmd -->\n```mermaid\n```\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"missing\.mmd"):
        viz.sync_embedded(doc, tmp_path)


@pytest.mark.parametrize("doc", viz.EMBEDDING_DOCS)
def test_diagrams_embedded_in_the_docs_are_the_committed_files(doc: str) -> None:
    path = REPO / doc
    text = path.read_text(encoding="utf-8")
    markers = re.findall(r"<!-- mmd: [\w.-]+ -->\n```mermaid\n", text)
    assert markers, f"{doc} embeds no diagram"
    assert len(markers) == text.count("```mermaid"), f"every mermaid block of {doc} needs an <!-- mmd: file --> marker"
    assert viz.sync_embedded(path, GRAPHS, write=False), f"{doc} is stale: run `python -m builder.viz sync`"


def test_cli_sync_check_passes_on_the_repository(capsys: pytest.CaptureFixture[str]) -> None:
    assert viz.main(["sync", "--check"]) == 0
    assert "STALE" not in capsys.readouterr().err
