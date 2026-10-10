"""The ``deploy`` node and the closing summary against the in-memory platform of the Freya client tests.

Regression tests for two defects of the pipeline audit:

* **export integrity (P-01)** - ``out/<slot>.exported.json`` must be the platform's export of the version that was just
  published. A failed or mismatching export must never leave an older export behind, and the closing summary must
  not substitute the locally sent definition for it;
* **publication (P-02)** - a version that is not confirmed published is not "deployed": the publish step is retried,
  the self-tests do not run against whatever else is live, and the final status says so.

The real :class:`FreyaClient` and the real node code run against :class:`FakePlatform` (no network, no LLM).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from builder.budget import Budget
from builder.freya.client import FreyaClient
from builder.graph import routing
from builder.graph.context import BuilderContext
from builder.graph.nodes import deploy
from builder.graph.nodes.finalize import SUCCESS_STATUSES, decide_status, finalize_run
from builder.trace import NullTrace
from tests.unit.test_freya_client import EVENT_LLM, FakePlatform, http_error, make_kit

WF = {"initial_node": "end", "nodes": {"end": {"type": "end_call"}}}


def workflow(text: str) -> dict[str, Any]:
    return {**WF, "system_prompt": text}


@pytest.fixture(autouse=True)
def no_retry_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    """The node waits between its own retries; tests do not."""
    for name in ("EXPORT_RETRY_DELAY_S", "PUBLISH_RETRY_DELAY_S"):
        monkeypatch.setattr(deploy, name, 0.0, raising=False)


class LaggyPlatform(FakePlatform):
    """Ignores the first ``ignore_first`` publish requests (accepted, but nothing is published)."""

    def __init__(self, ignore_first: int, **kwargs: Any) -> None:
        super().__init__(honour_publish=False, **kwargs)
        self.ignore_first = ignore_first
        self.publish_posts = 0

    def api(self, method: str, path: str, body: Any = None, **params: Any) -> Any:
        if method == "POST" and path.endswith("/publish"):
            self.publish_posts += 1
            self.honour_publish = self.publish_posts > self.ignore_first
        return super().api(method, path, body, **params)


class Rig:
    """The deploy node, a real client and the closing summary over one in-memory platform."""

    def __init__(self, tmp_path: Path, make_settings: Any, platform: FakePlatform | None = None) -> None:
        self.platform = platform or FakePlatform(default_llm=EVENT_LLM)
        self.kit = make_kit(self.platform)
        self.trace = NullTrace()
        self.client = FreyaClient(
            self.trace, kit=self.kit, state_file=tmp_path / "state.json", limiter=False, sleep=lambda s: None
        )
        self.ctx = BuilderContext(
            settings=make_settings(builder_track="compiled", self_test="none"),
            llm=object(),
            freya=self.client,
            trace=self.trace,
            budget=Budget(1680),
        )
        self.state: dict[str, Any] = {"slot": "s", "run_id": "r", "track": "compiled", "lint": {"ok": True}}

    async def deploy(self, text: str) -> dict[str, Any]:
        """Run the node once like the graph does (accumulating the ``add`` keys)."""
        self.state["compiled"] = {"workflow": workflow(text)}
        update = await deploy._body(self.state, self.ctx)
        for key in ("notes", "errors"):
            self.state[key] = [*self.state.get(key, []), *update.get(key, [])]
        self.state.update({k: v for k, v in update.items() if k not in ("notes", "errors")})
        return update

    def final(self) -> dict[str, Any]:
        return finalize_run(self.state, self.ctx)

    @property
    def export_path(self) -> Path:
        from builder import paths

        return paths.OUT_DIR / "s.exported.json"


@pytest.fixture
def rig(tmp_dirs: SimpleNamespace, tmp_path: Path, make_settings: Any) -> Rig:
    return Rig(tmp_path, make_settings)


def fail_exports_after(rig: Rig, good: int) -> dict[str, int]:
    """Let the first ``good`` kit exports work, make every later one fail with HTTP 503."""
    real = rig.kit.export_workflow
    calls = {"n": 0}

    def flaky(workflow_id: str, path: str) -> str:
        calls["n"] += 1
        if calls["n"] > good:
            raise http_error("GET", f"/api/v2/workflow/{workflow_id}", 503, "service unavailable")
        return real(workflow_id, path)

    rig.kit.export_workflow = flaky
    return calls


# ------------------------------------------------------------------------------------------------ P-01 export


async def test_a_failed_reexport_leaves_no_stale_export_and_the_summary_says_so(rig: Rig) -> None:
    fail_exports_after(rig, good=1)
    await rig.deploy("version one")
    assert "version one" in rig.export_path.read_text(encoding="utf-8")  # the first export worked

    update = await rig.deploy("version two")  # published and live, but the export of it fails

    assert update["exported"] is None  # explicit: the first round's path must not survive in the state
    assert not rig.export_path.exists()  # the file of version one is stale now
    assert any("export" in error for error in update["errors"])
    final = rig.final()
    assert final["status"] == "deployed_unexported" and final["status"] not in SUCCESS_STATUSES
    assert final["version"] == 2
    assert final["exported"] is None and final["export_ok"] is False
    assert any("export" in error for error in final["errors"])
    live = rig.platform.workflows[final["workflow_id"]]["workflow"]
    assert live["system_prompt"] == "version two"


async def test_a_failed_first_export_is_not_replaced_by_the_local_definition(rig: Rig) -> None:
    fail_exports_after(rig, good=0)
    update = await rig.deploy("only version")
    assert update.get("exported", "missing") is None
    final = rig.final()
    assert final["status"] == "deployed_unexported"
    assert final["exported"] is None and final["export_ok"] is False
    assert final["exported"] != final["workflow_file"]  # the local file is not a platform export
    assert any("export" in error for error in final["errors"])


async def test_the_export_is_retried_before_giving_up(rig: Rig) -> None:
    real = rig.kit.export_workflow
    calls = {"n": 0}

    def recovering(workflow_id: str, path: str) -> str:
        calls["n"] += 1
        if calls["n"] <= 3:  # the client's own three attempts fail; the node tries again and succeeds
            raise http_error("GET", f"/api/v2/workflow/{workflow_id}", 503, "service unavailable")
        return real(workflow_id, path)

    rig.kit.export_workflow = recovering
    update = await rig.deploy("recovers")
    assert update["exported"] == str(rig.export_path)
    assert json.loads(rig.export_path.read_text(encoding="utf-8"))["workflow"]["system_prompt"] == "recovers"
    final = rig.final()
    assert final["export_ok"] is True and final["exported"] == str(rig.export_path)


async def test_an_export_of_a_different_version_than_the_stored_one_is_rejected(rig: Rig) -> None:
    real = rig.kit.export_workflow

    def older(workflow_id: str, path: str) -> str:
        real(workflow_id, path)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data["workflow"] = {**data["workflow"], "system_prompt": "an older version"}
        Path(path).write_text(json.dumps(data), encoding="utf-8")
        return path

    rig.kit.export_workflow = older
    update = await rig.deploy("the published one")
    assert update["exported"] is None and not rig.export_path.exists()
    assert any("does not match" in error for error in update["errors"])
    assert rig.final()["export_ok"] is False


async def test_an_export_of_another_workflow_is_rejected(rig: Rig) -> None:
    real = rig.kit.export_workflow

    def foreign(workflow_id: str, path: str) -> str:
        real(workflow_id, path)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data["workflow_id"] = "wf_somebody_else"
        Path(path).write_text(json.dumps(data), encoding="utf-8")
        return path

    rig.kit.export_workflow = foreign
    update = await rig.deploy("mine")
    assert update["exported"] is None and not rig.export_path.exists()


async def test_a_good_export_is_the_platforms_file_for_the_deployed_version(rig: Rig) -> None:
    await rig.deploy("version one")
    await rig.deploy("version two")
    final = rig.final()
    assert final["export_ok"] is True and Path(final["exported"]) == rig.export_path
    exported = json.loads(rig.export_path.read_text(encoding="utf-8"))
    assert exported["workflow_id"] == final["workflow_id"]
    assert exported["workflow"]["system_prompt"] == "version two"
    assert not rig.export_path.with_name("s.exported.json.part").exists()  # no temporary file is left behind


def test_the_local_file_stands_in_for_the_export_only_in_a_dry_run(
    tmp_dirs: SimpleNamespace, make_settings: Any
) -> None:
    ctx = BuilderContext(
        settings=make_settings(dry_run=True), llm=None, freya=None, trace=NullTrace(), budget=Budget(60)
    )
    dry = finalize_run(
        {"slot": "d", "run_id": "d1", "dry_run": True, "lint": {"ok": True}, "compiled": {"workflow": WF}},
        ctx,
    )
    assert dry["status"] == "dry_run" and dry["exported"] == dry["workflow_file"]
    failed_ctx = BuilderContext(
        settings=make_settings(), llm=None, freya=None, trace=NullTrace(), budget=Budget(60)
    )
    failed = finalize_run(
        {"slot": "f", "run_id": "f1", "lint": {"ok": False}, "compiled": {"workflow": WF}}, failed_ctx
    )
    assert failed["status"] == "failed" and failed["exported"] is None


# ------------------------------------------------------------------------------------------------ P-02 publication


async def test_an_unconfirmed_publication_is_not_a_deployment_and_skips_the_self_tests(
    tmp_dirs: SimpleNamespace, tmp_path: Path, make_settings: Any
) -> None:
    rig = Rig(tmp_path, make_settings, FakePlatform(default_llm=EVENT_LLM, honour_publish=False))
    update = await rig.deploy("never published")

    assert update["failed"] == "deploy"
    assert any("published" in error for error in update["errors"])
    assert update["deploy"]["published"] is False  # the ids are still reported
    assert (
        "exported" not in update or update["exported"] is None
    )  # nothing is exported for an unpublished version
    assert (
        routing.after_deploy({**rig.state, **update}) == routing.FINALIZE
    )  # no self-test on a stale version
    final = rig.final()
    assert final["status"] == "deployed_unpublished" and final["status"] not in SUCCESS_STATUSES
    assert final["workflow_id"] == "wf_1"
    assert any("published" in error for error in final["errors"])
    assert rig.platform.count("POST", r"/api/v2/versions/workflow/[^/]+/publish") >= 2  # it was retried


async def test_a_publication_that_lags_is_retried_and_confirmed(
    tmp_dirs: SimpleNamespace, tmp_path: Path, make_settings: Any
) -> None:
    platform = LaggyPlatform(ignore_first=1, default_llm=EVENT_LLM)
    rig = Rig(tmp_path, make_settings, platform)
    update = await rig.deploy("published at the second try")

    assert update.get("failed", "") == ""
    assert update["deploy"]["published"] is True and update["deploy"]["version"] == 1
    assert platform.publish_posts == 2
    assert rig.final()["status"] == "deployed"


async def test_a_failed_republication_keeps_the_earlier_published_version(
    tmp_dirs: SimpleNamespace, tmp_path: Path, make_settings: Any
) -> None:
    platform = LaggyPlatform(ignore_first=0, default_llm=EVENT_LLM)
    rig = Rig(tmp_path, make_settings, platform)
    await rig.deploy("version one")  # published: the first publish request is honoured
    assert rig.state["deploy"]["published"] is True
    platform.ignore_first = 10**6  # from now on nothing is published any more
    platform.publish_posts = 0
    update = await rig.deploy("version two")

    assert (
        update["failed"] == "deploy" and "deploy" not in update
    )  # the published version one stays the record
    final = rig.final()
    assert final["status"] == "deployed" and final["version"] == 1
    assert Path(final["exported"]).read_text(encoding="utf-8").count("version one") == 1
    assert any("published" in error for error in final["errors"])


async def test_a_stale_reference_read_does_not_cost_the_export(rig: Rig) -> None:
    """The read-back the export is compared with lagged behind once: a fresh reference is taken, no failure."""

    class Lagging:
        def __init__(self, client: FreyaClient) -> None:
            self.client = client
            self.reads = 0

        def __getattr__(self, name: str) -> Any:
            return getattr(self.client, name)

        def get_workflow(self, workflow_id: str) -> dict[str, Any]:
            self.reads += 1
            stored = self.client.get_workflow(workflow_id)
            return {**stored, "system_prompt": "an older read"} if self.reads == 1 else stored

    proxy = Lagging(rig.client)
    rig.ctx.freya = proxy
    update = await rig.deploy("the current version")
    assert update["exported"] == str(rig.export_path) and proxy.reads == 2
    assert "the current version" in rig.export_path.read_text(encoding="utf-8")


async def test_an_export_without_any_read_back_is_accepted_but_flagged(rig: Rig) -> None:
    """With no reference to compare with, the export can only be checked for naming the deployed workflow."""

    class NoReadBack:
        def __init__(self, client: FreyaClient) -> None:
            self.client = client
            self.reads = 0

        def __getattr__(self, name: str) -> Any:
            return getattr(self.client, name)

        def get_workflow(self, workflow_id: str) -> dict[str, Any]:
            self.reads += 1
            raise http_error("GET", f"/api/v2/workflow/{workflow_id}", 503, "unavailable")

    proxy = NoReadBack(rig.client)
    rig.ctx.freya = proxy
    update = await rig.deploy("exported blind")
    assert update["exported"] == str(rig.export_path) and proxy.reads >= 2  # the read-back was tried again
    assert any("could not be compared" in note for note in update["notes"])


def test_the_final_status_names_an_unpublished_deployment() -> None:
    assert decide_status({"deploy": {"workflow_id": "wf", "published": False}}) == "deployed_unpublished"
    assert decide_status({"deploy": {"workflow_id": "wf", "published": True}}) == "deployed"
    assert decide_status({"deploy": {"workflow_id": "wf"}}) == "deployed"  # unknown: a hand-built record
    assert "deployed_unpublished" not in SUCCESS_STATUSES


def test_the_final_status_names_a_deployment_without_a_verified_export() -> None:
    live = {"workflow_id": "wf", "published": True}
    assert (
        decide_status({"deploy": live, "exported": None}) == "deployed_unexported"
    )  # the deploy node said: failed
    assert decide_status({"deploy": live, "exported": "out/s.exported.json"}) == "deployed"
    assert decide_status({"deploy": live}) == "deployed"  # no information about the export: not a failure
    assert decide_status({"deploy": {**live, "published": False}, "exported": None}) == "deployed_unpublished"
    assert "deployed_unexported" not in SUCCESS_STATUSES
