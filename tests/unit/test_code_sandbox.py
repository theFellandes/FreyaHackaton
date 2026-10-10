"""Tests for builder.checks.code_sandbox: the local stand-in for the platform's code sandbox."""

from __future__ import annotations

import pytest

from builder.checks.code_sandbox import SnippetError, extract_code_snippets, run_hook, run_snippet
from builder.compiler.nodes import code_node
from builder.compiler.tools import code_exec


def test_snippet_reads_and_writes_vars_without_touching_the_input() -> None:
    state = {"a": 2}
    out = run_snippet("vars['b'] = vars.get('a') * 3\n", state)
    assert out == {"a": 2, "b": 6}
    assert state == {"a": 2}


def test_allowed_modules_work() -> None:
    code = "import json\nimport re\nimport datetime\nimport math\nvars['x'] = json.dumps([1]) + re.sub('a', 'b', 'aa') + str(datetime.date(2026, 1, 2)) + str(math.floor(1.5))\n"
    assert run_snippet(code, {})["x"] == "[1]bb2026-01-021"


@pytest.mark.parametrize("module", ["os", "sys", "subprocess", "socket", "time", "random", "pathlib"])
def test_other_modules_are_refused(module: str) -> None:
    with pytest.raises(SnippetError, match="not allowed"):
        run_snippet(f"import {module}\n", {})


def test_strftime_needs_the_time_module_so_generated_code_avoids_it() -> None:
    with pytest.raises(SnippetError):
        run_snippet("import datetime\nvars['t'] = datetime.datetime(2026, 1, 1, 9, 5).strftime('%H:%M')\n", {})


def test_dangerous_builtins_are_missing() -> None:
    for code in ("open('x')", "exec('1')", "eval('1')", "__import__('os')", "compile('1', 'a', 'eval')", "input()"):
        with pytest.raises(SnippetError):
            run_snippet(code + "\n", {})


def test_errors_are_wrapped_with_the_exception_type() -> None:
    with pytest.raises(SnippetError, match="ZeroDivisionError"):
        run_snippet("vars['x'] = 1 / 0\n", {})
    with pytest.raises(SnippetError, match="syntax error"):
        run_snippet("def (:\n", {})


def test_a_runaway_loop_times_out() -> None:
    with pytest.raises(SnippetError, match=r"exceeded|did not finish"):
        run_snippet("while True:\n    pass\n", {}, timeout_s=0.3)


def test_functions_and_lambdas_work_in_snippets() -> None:
    code = "def f(x):\n    return x + 1\nvars['y'] = sorted([3, 1, 2], key=lambda n: -n)[0] + f(1)\n"
    assert run_snippet(code, {})["y"] == 5


def test_run_hook_returns_copies_of_all_three_objects() -> None:
    out = run_hook("config['body']['a'] = 1\nvars['v'] = 2\nresult['r'] = 3\n", vars={}, config={"body": {}}, result={})
    assert out == {"vars": {"v": 2}, "config": {"body": {"a": 1}}, "result": {"r": 3}}


def test_extract_code_snippets_finds_nodes_hooks_and_workflow_tools() -> None:
    tool = {"type": "function", "function": {"name": "t", "handler": "__function__:api_call"},
            "pre_process_code": "a = 1\n", "post_process_code": "b = 2\n"}
    workflow = {
        "initial_node": "setup",
        "nodes": {
            "setup": code_node("setup", "vars['x'] = 1\n", "talk"),
            "talk": {"node_type": "conversation", "tools": [tool, {"function": {"name": "u"}, "post_process_code": " "}]},
            "api": {"node_type": "api_request", "pre_actions": [code_exec("y = 1\n")]},
        },
        "tools": [{"function": {"name": "w"}, "pre_process_code": "c = 3\n"}],
    }
    found = extract_code_snippets(workflow)
    assert found["setup"] == "vars['x'] = 1\n"
    assert found["talk/t.pre_process_code"] == "a = 1\n"
    assert found["talk/t.post_process_code"] == "b = 2\n"
    assert found["api"] == "y = 1\n"
    assert found["tools/w.pre_process_code"] == "c = 3\n"
    assert "talk/u.post_process_code" not in found
