"""The agent's persona noun and the fallback opening come from the policy's own sentences (audit U-17).

"digital assistant" used to be written into the system prompt template and into the neutral opening, so a brief that calls
the agent something else (or names nothing) still got that noun. Now the noun is read from the IR's robot answer, opening
sentence or robot-question response; a policy that names none gets wording without a noun.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from builder.compiler.compile import compile_workflow
from builder.compiler.context import persona_noun
from builder.ingest.tool_api import load_tool_api
from builder.policy.ir import PolicyIR, RuleLedger

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "policy"
IR_A = PolicyIR.model_validate(json.loads((FIXTURES / "brief_a_ir.json").read_text(encoding="utf-8")))
LEDGER_A = RuleLedger.model_validate(json.loads((FIXTURES / "brief_a_ledger.json").read_text(encoding="utf-8")))
LEDGER_NO_NOTICE = LEDGER_A.model_copy(update={"rules": [r for r in LEDGER_A.rules if r.id != "A-1a"]})
API = load_tool_api(REPO / "tools" / "openapi.json")


def _ir(*, robot: str = "", opening: str = "", bank: str = "Northwind Credit") -> PolicyIR:
    ir = IR_A.model_copy(deep=True)
    ir.bank_name = bank
    ir.phrases.robot_answer = robot
    ir.phrases.opening_sentence = opening
    ir.phrases.right_party_question = ""
    ir.intents = [i for i in ir.intents if i.key != "robot_question"]
    return ir


@pytest.mark.parametrize(("robot", "opening", "expected"), [
    ("I am Northwind Credit's virtual agent.", "", "virtual agent"),
    ("I am Freya Bank's digital assistant.", "", "digital assistant"),  # another bank's name in the sentence
    ("", "Hello, I am a digital assistant calling from Northwind Credit. This call is recorded.", "digital assistant"),
    ("", "Hello, this is the automated assistant of Northwind Credit.", "automated assistant"),
    ("", "Hi, you have reached an AI helper from Northwind Credit.", ""),  # not a "this is / I am" statement
    ("", "Hello, this is Ana from Northwind Credit.", ""),  # a first name is not a noun phrase
    ("I am Ana.", "", ""),
    ("", "", ""),
    ("I am the voice assistant for Northwind Credit.", "Hello, this is a digital assistant.", "voice assistant"),
])
def test_the_persona_noun_is_read_from_the_policys_sentences(robot: str, opening: str, expected: str) -> None:
    assert persona_noun(_ir(robot=robot, opening=opening)) == expected


def test_the_robot_question_response_is_a_source_too() -> None:
    ir = IR_A.model_copy(deep=True)
    ir.phrases.robot_answer = ""
    ir.phrases.opening_sentence = ""
    robot = next(i for i in ir.intents if i.key == "robot_question")
    for response in ("Say that you are Northwind Credit's virtual agent and continue.",
                     "I am Northwind Credit's virtual agent."):
        robot.response = response
        assert persona_noun(ir) == "virtual agent"
    robot.response = "Answer briefly and continue."
    assert persona_noun(ir) == ""


def test_the_system_prompt_uses_the_policys_noun() -> None:
    ir = _ir(robot="I am Northwind Credit's virtual agent.", opening="Hello, I am a virtual agent from Northwind Credit.")
    workflow = compile_workflow(ir, API, LEDGER_A).workflow
    first = workflow["system_prompt"].splitlines()[0]
    assert first == "You are Northwind Credit's virtual agent on an outbound phone call. Speak English."


def test_a_policy_that_names_no_persona_gets_no_invented_noun() -> None:
    workflow = compile_workflow(_ir(), API, LEDGER_NO_NOTICE).workflow
    assert workflow["system_prompt"].splitlines()[0] == "You make an outbound phone call for Northwind Credit. Speak English."
    assert workflow["nodes"]["opening"]["on_enter"]["text"] == (
        "Hello, this is Northwind Credit. Am I speaking with {{title}} {{last_name}}?")


def test_the_fallback_opening_uses_the_policys_noun() -> None:
    ir = _ir(robot="I am Northwind Credit's virtual agent.")
    compiled = compile_workflow(ir, API, LEDGER_NO_NOTICE)
    assert compiled.workflow["nodes"]["opening"]["on_enter"]["text"] == (
        "Hello, this is Northwind Credit's virtual agent. Am I speaking with {{title}} {{last_name}}?")
    assert any("opening sentence" in note for note in compiled.notes)


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_no_compiled_text_invents_a_persona(profile: str) -> None:
    workflow = compile_workflow(_ir(), API, LEDGER_NO_NOTICE, profile=profile).workflow
    assert "digital assistant" not in json.dumps(workflow)


def test_builder_code_and_templates_hard_code_no_persona() -> None:
    offenders = []
    for root in (REPO / "builder" / "compiler", REPO / "builder" / "prompts" / "agent"):
        for path in sorted(root.rglob("*")):
            if path.suffix in {".py", ".j2"} and "digital assistant" in path.read_text(encoding="utf-8").lower():
                offenders.append(str(path.relative_to(REPO)))
    assert offenders == []
