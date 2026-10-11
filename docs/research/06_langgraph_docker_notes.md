# 06 — Builder implementation stack: LangGraph 1.x + LangChain 1.x + Docker (uv) + user conventions

> Status: pre-kit research note (10 October 2026), kept for background. Where it disagrees with the kit ([research/11](11_hackathon_rules_and_kit.md), `docs/api.md`, `docs/workflow-format.md`) or with the code, those win.

Research date: 2026-10-10. Scope: the BUILDER program (Python, LangGraph, Docker) that reads a collections brief and creates/updates the Freya phone agent, then evaluates and refines it.

Legend: **VERIFIED** = read in source/spec/PyPI today; **DOCS** = stated in official docs page fetched today; **INFERRED** = my reasoning, not stated anywhere; **USER-CONV** = taken from the user's LanggraphPlayground repo.

---

## 0. TL;DR (decisions)

| Decision | Recommendation | Basis |
|---|---|---|
| Container Python | **3.13** (`python:3.13-slim-trixie` + uv). `requires-python = ">=3.12,<3.14"` | `langgraph` 1.2.14 classifiers stop at 3.13 (VERIFIED, PyPI). `langgraph-cli` `python_version` accepts only 3.11/3.12/3.13 (DOCS). The user's playground pins `.python-version` = 3.13 and `requires-python = ">=3.11,<3.14"` (USER-CONV). LangChain issue tracker still has an open "3.14 support timeline" request; langgraph release notes call 3.14 support "cursory". Compiled deps (ormsgpack, xxhash, pydantic-core, orjson, psycopg-binary, jiter) all ship cp314 wheels (VERIFIED), so 3.14 would probably install. It is just not declared. |
| Host venv | The current `FreyaHackaton/.venv` is **CPython 3.14.6**, made by virtualenv/pip, with fastapi 0.143.0 and pydantic 2.14.0 only. **`uv` is not on PATH** in Git-Bash. `py -0` shows 3.14 and **3.12.10**. Docker 29.6.1 is installed, but **the daemon was not running** (`dockerDesktopLinuxEngine` pipe missing). | VERIFIED locally |
| Graph state | `TypedDict` (total=False) for the graph state. Pydantic v2 models for the LLM I/O and the domain spec. Reducers: `Annotated[list, operator.add]` for fan-in, and `Overwrite([])` to reset between refine iterations. | DOCS + USER-CONV |
| Structured output on Claude | `llm.with_structured_output(Model, method="json_schema")`, which is native `output_config.format`. **Do NOT use the default `function_calling`** on `claude-opus-5-5` / `claude-sonnet-5-5`: forced `tool_choice` returns a 400 on these models. | VERIFIED (langchain-anthropic source, claude-api skill) |
| Models | Primary (extract/refine/judge): `claude-opus-5-5`. Simulated customers: `claude-sonnet-5-5` or `claude-haiku-5-5`, configurable. **Never pass `temperature`/`top_p`/`top_k`** to these models (400). | claude-api skill (cached 2026-10-06), langchain-anthropic docs |
| Checkpointer | `SqliteSaver`/`AsyncSqliteSaver` on a Docker volume for the CLI. `AsyncPostgresSaver` only if we add the FastAPI service. `durability="sync"` for runs that deploy to Freya. | DOCS + source (default durability is `"async"`) |
| HITL | `interrupt()` at (a) unresolved spec ambiguities, (b) pre-deploy approval, (c) pre-final-publish. **All gated by `settings.hitl` (default False in Docker)**, so a headless grader run never blocks. | DOCS + INFERRED |
| Retries | Freya HTTP: tenacity inside the client (USER-CONV, lesson 30), idempotent verbs only. LangGraph `RetryPolicy` on LLM nodes. ChatAnthropic already retries (`max_retries=2`), so keep the outer `max_attempts` small. | VERIFIED |
| Docker | Multi-stage build: uv `COPY --from=ghcr.io/astral-sh/uv:<pinned>`, `uv sync --locked --no-install-project --no-editable` with cache and bind mounts, non-root runtime, `.venv` copied, `.env` never baked in. Compose profiles: `builder` (default), `api`, `pg`, `dev` (Studio), `test` (stubs + pytest). | DOCS (uv Docker guide) |

---

## 1. User conventions extracted (LanggraphPlayground)

Files read: `README.md` (skim), `pyproject.toml`, `uv.lock` (versions), `shared/settings.py`, `shared/__init__.py`, `shared/llm/*` (README, `__init__`, `base`, `anthropic_adapter`, `openai_adapter`, `google_adapter`, `local_adapter`, `schema_sanitizer`), `shared/pretty.py` (head), skills (`langgraph-1x-engineering`, `langchain-1x-engineering`, `python-clean-code`, `python-design-patterns-applied`, `fastapi-pytest-functional`, `llm-expert`), `projects/research_assistant_pro/*`, `projects/customer_support_bot_pro/graph.py`, `projects/rag_qa_api_pro/{Dockerfile,docker-compose.yml,graph.py,app.py}`, `projects/rag_qa_api/langgraph.json`, `lessons/30_advanced_graphs/example.py` (retry), `lessons/32_prompt_engineering_lab/example.py` (prompt registry), `tests/test_lesson_08_graph.py`, `.env.example`, `.python-version`.

### 1.1 Packaging and tooling
- **uv**-managed project, `hatchling` build backend, flat top-level packages listed in `[tool.hatch.build.targets.wheel] packages = ["shared", "lessons", "projects", ...]`. No `src/` layout. Optional-dependency groups: `api`, `dev`, `eval`, `obs`, and others.
- `[tool.pytest.ini_options] asyncio_mode = "auto"`, `testpaths = ["tests"]`.
- Locked versions in the playground (older than today's PyPI): langgraph 1.2.0, langgraph-checkpoint 4.1.0, langgraph-checkpoint-sqlite 3.1.0, langgraph-checkpoint-postgres 3.1.0, langchain 1.3.1, langchain-core 1.4.0, langchain-anthropic 1.4.3, anthropic 0.102.0, pydantic 2.13.4, pydantic-settings 2.14.1, tenacity 9.1.4, httpx 0.28.1, fastapi 0.136.1.
- Style tooling in the `python-clean-code` skill: Ruff (rules `E,W,F,I,N,UP,B,SIM,RUF`), line length 100, mypy/pyright, Google-style docstrings, a module docstring on every non-trivial file, PEP 604 unions, keyword-only boolean flags, functions of 30 lines or fewer with 5 args or fewer, no bare `except Exception` unless re-raised or explicitly best-effort, `raise X from e`, specific exception classes.
- Data-class choice table (skill): `@dataclass(frozen=True, slots=True)` for value objects, Pydantic for anything loaded from JSON/env/LLM, `TypedDict` for graph state.

### 1.2 Settings (`shared/settings.py`)
- `pydantic_settings.BaseSettings` with `SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False)`.
- Module-level singleton `settings = Settings()`. Callers do `from shared.settings import settings`. No direct `os.environ` reads; the rag_qa_api_pro capstone is an exception.
- `Provider = Literal["anthropic", "openai", "google", "local"]`. Fields: `llm_provider: Provider = "anthropic"`, `llm_model: str | None`, `*_api_key: str | None`, LangSmith fields (side-effect: sets `LANGSMITH_*` env when tracing is enabled), `postgres_url` default `postgresql://postgres:postgres@localhost:5432/langgraph`, and a `data_dir` property that auto-creates `ROOT/data`.
- Deviation to adopt for the builder: use **`SecretStr`** for `freya_key` and `anthropic_api_key`. The playground uses plain `str`. Never log the key.

### 1.3 LLM layer (`shared/llm/`): Adapter + factory + fallback
- `get_llm(provider=None, model=None, *, with_fallback=True, fallback_chain=None, **kwargs) -> Runnable`. `_ADAPTERS = {"anthropic", "openai", "google", "local"}` is an ordered registry, and each adapter module exposes `build(model=None, **kwargs) -> BaseChatModel` plus `DEFAULT_MODEL`.
- If the requested provider has no key, `get_llm` auto-promotes to the first provider that has one, with a warning. Fallback uses `primary.with_fallbacks([...])`. Fallbacks ignore `model` and use each provider's default.
- `anthropic_adapter.DEFAULT_MODEL = "claude-sonnet-4-6"`. This is **stale for the builder**: use `claude-opus-5-5` (see §3.4).
- `with_structured_output_safe(schema, *, llm=None, method="json_schema")` sanitizes the Pydantic JSON schema (strips `format`, `default`, `examples`, `$schema`, and turns `additionalProperties: {}` into `false`), then re-validates the dict output into the Pydantic class through a `RunnableLambda`. **It does not strip `minimum`/`maximum`/`minLength`/`maxLength`, which Anthropic structured outputs do not support.** Extend `_STRIP_KEYS` for the builder, or keep LLM-facing schemas free of numeric and length constraints and validate in `field_validator`s.
- Anti-patterns stated by the user: no module-level LLM construction (build inside the node or factory, so tests can patch and the provider can switch), never mutate state in nodes, always cap cycles, and don't use MemorySaver in production.
- Note: `with_fallbacks` wraps the model, so `.with_structured_output()` must be called on the bare model (`get_llm(with_fallback=False)`). This is what `with_structured_output_safe` does.

### 1.4 Graph layout in the `_pro` capstones
- Each capstone has a single `graph.py` with this section order: module docstring containing an ASCII topology diagram and run command, then imports, constants, `PROMPTS_DIR`, Jinja `env`, then sections marked `# --- state ---`, `# --- tools ---`, `# --- nodes ---`, `# --- graph ---`, then `build_graph()` and `main()`.
- A separate `concurrency.py` holds module-level primitives: `LLM_SEM = asyncio.Semaphore(5)`, `SEARCH_SEM`, a per-key lock map `defaultdict(asyncio.Lock)`, and a `CircuitBreaker(threshold, cooldown)` class.
- State: `class ResearchState(TypedDict, total=False)` with `claims: Annotated[list[str], add]`; `QAState(MessagesState)`.
- Fan-out: `dispatch(state) -> list[Send]` with `g.add_conditional_edges("planner", dispatch, ["researcher"])`. The revision loop is bounded by a `revisions` counter (max 3) in `route_after_critic`.
- The playground has **no `nodes/` subpackage and no `state.py`**; everything lives in `graph.py`. For the builder (~12 nodes) a split is justified. §6 below proposes `graph/state.py`, `graph/nodes/*.py`, `graph/routing.py`, `graph/build.py`. This is consistent with the "one screen per function" and "package re-exports public surface" rules.
- Async nodes (`async def`) with `await get_llm().ainvoke(...)` under `async with LLM_SEM:`.
- Run with `uv run python -m projects.<name>.graph`. Console output goes through `rich` (`shared.pretty.console`, `section()`).

### 1.5 Prompts: Jinja2 + registry
- Jinja environment (identical in every capstone):
  ```python
  env = Environment(loader=FileSystemLoader(PROMPTS_DIR),
                    autoescape=select_autoescape(disabled_extensions=("j2",)),
                    trim_blocks=True, lstrip_blocks=True, undefined=StrictUndefined)
  ```
- Folder layout: `prompts/_base/persona.j2` (defines `{% block role %}`, `{% block extras %}`, includes `_base/safety_rules.j2`) and `prompts/agents/<name>.j2` (`{% extends "_base/persona.j2" %}`). The rag pro capstone uses **versioned filenames** `qa.v1.j2`, `grader.v1.j2`, pinned by a `PROMPT_VERSION` env var.
- Lesson 32 `PromptRegistry(prompts_dir)`: files are named `{name}.{version}.j2`; `PromptArtefact(name, version, template_source, sha256[:16], metadata, tags)` is a frozen dataclass; methods are `list_versions`, `set_current`, `get(name, version="current")`, `render(name, *, version="current", **vars)`, and `async reload()` under an `asyncio.Lock`. It also has eval-driven promotion: `promote_if_better(reg, name, candidate, epsilon=0.02)`.
- The builder should use this registry for the **Freya agent's prompt templates** and record `name@version#sha` of every template in the run report and in the Freya version description (INFERRED good practice).

### 1.6 Retry and resilience
- Lesson 30 retry pattern: `@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=0.1, max=1))` from tenacity, used inside an async node. Circuit breaker class as above. Tools return structured recoverable errors (`{"error": "recoverable", "message": ...}`), and only unrecoverable faults raise (USER-CONV, lesson 25 and the guardrails skill).
- Idempotency: tools take `idempotency_key` and the result is cached by key (customer_support_bot_pro).

### 1.7 Testing style
- **Plain functions, no classes** (`fastapi-pytest-functional` skill). Fixtures live in `conftest.py`, `@pytest.mark.parametrize` with `pytest.param(..., id=...)`, naming is `test_<subject>_<outcome>_when_<condition>`, respx mocks outbound httpx, and responses are validated by round-tripping through Pydantic models. Layout is `tests/unit`, `tests/api`, `tests/integration`, with markers `slow` and `integration` and `addopts = "-q --strict-markers"`.
- Graph tests (tests/test_lesson_08_graph.py): `FakeListChatModel(responses=[...])`, patch with `patch.object(module, "get_llm", return_value=fake)`, assert on the final state rather than on prose, and run a topology test via `set(graph.get_graph().nodes)`.
- Caveat (INFERRED): `FakeListChatModel` has no `bind_tools`/structured output, so nodes that use `with_structured_output` should receive their chain via dependency injection (see §2.6 Runtime context) so tests can pass a `RunnableLambda(lambda _: SpecModel(...))`.

### 1.8 Docker conventions in the playground (to improve on)
- `FROM python:3.12-slim`, `RUN pip install uv==0.5.4`, `COPY pyproject.toml uv.lock*`, `RUN uv sync --frozen --extra api || uv sync --extra api`, `COPY shared projects data`, `ENV PATH=/app/.venv/bin:$PATH`, and `CMD uvicorn ...`. The build context is the repo root (`context: ../..`). Compose uses healthchecked Postgres plus `depends_on: condition: service_healthy`, `env_file: ../../.env`, and named volumes.
- Gaps compared with current best practice: no multi-stage build, no cache mounts, runs as root, uses an old uv, the `--frozen || sync` fallback hides lock drift, and source is copied into the runtime image. §5 fixes these.

---

## 2. LangGraph 1.x API facts (current: langgraph **1.2.14**, released 2026-10-06)

### 2.1 Versions on PyPI today (VERIFIED 2026-10-10)
| package | latest | released | notes |
|---|---|---|---|
| langgraph | 1.2.14 | 2026-10-06 | requires `langchain-core>=1.4.7,<2`, `langgraph-checkpoint>=4.1.0,<5`, `langgraph-prebuilt>=1.1.0,<1.2`, `langgraph-sdk>=0.4.6,<0.5`, `pydantic>=2.7.4`, `xxhash>=3.5.0`. Classifiers 3.10–3.13. |
| langgraph-checkpoint | 4.2.0 | 2026-08-07 | needs `ormsgpack>=1.12.0` |
| langgraph-checkpoint-sqlite | 3.1.1 | 2026-07-30 | deps `aiosqlite>=0.20`, `sqlite-vec>=0.1.6` |
| langgraph-checkpoint-postgres | 3.1.2 | 2026-08-07 | deps `psycopg>=3.2.0`, `psycopg-pool>=3.2.0`, `orjson` |
| langgraph-cli | 0.4.33 | 2026-10-07 | `[inmem]` extra for `langgraph dev` |
| langchain | 1.4.4 | 2026-10-08 | requires `langgraph>=1.2.11,<1.3` |
| langchain-core | 1.6.9 | 2026-10-08 | classifiers include 3.14 |
| langchain-anthropic | 1.7.5 | 2026-09-29 | requires `anthropic>=0.120.0,<2` |
| langchain-openai | 1.7.0 | 2026-10-08 | |
| anthropic | 1.13.0 | 2026-10-09 | 1.x is built on **httpx2**, not httpx |
| pydantic / pydantic-settings | 2.14.0 / 2.15.0 | 2026-10-08 / 2026-08-07 | |
| jinja2 | 3.1.6 | 2025-03-05 | |
| tenacity | 9.2.1 | 2026-10-07 | |
| httpx | 0.28.1 | 2024-12-06 | |
| respx | 0.23.1 | | mocks `httpx`, not `httpx2` |
| uv | 0.13.0 | **2026-10-09** (1 day old) | 0.12.24 released 2026-10-08, 0.12.20 on 2026-09-28 |
| openevals | 0.2.0 | 2026-04-07 | pulls `langchain-openai` and `langsmith` |
| langsmith | 0.14.7 | 2026-10-09 | uses httpx2 |

### 2.2 StateGraph core (VERIFIED in the 1.2.14 source `graph/state.py` + DOCS)
- `StateGraph(state_schema, context_schema=None, input_schema=None, output_schema=None)`. `config_schema`/`input`/`output` kwargs are deprecated in favour of `context_schema`/`input_schema`/`output_schema`.
- State schema can be a TypedDict, a dataclass (use it if you need defaults), or a Pydantic BaseModel. With Pydantic, **validation runs only on input to the first node, graph output is not a Pydantic instance, and error traces don't name the failing node**. Pydantic is also slower (DOCS). `create_agent` does not support Pydantic state.
- Reducers: `Annotated[list[X], operator.add]`; `add_messages` (from `langgraph.graph.message`); `MessagesState`. To bypass a reducer use **`Overwrite(value)`** (`from langgraph.types import Overwrite`, in 1.2.14). Multiple `Overwrite`s for the same channel in one super-step is an error.
- `add_node(node, action=None, *, defer=False, metadata=None, input_schema=None, retry_policy: RetryPolicy | Sequence[RetryPolicy] | None = None, cache_policy: CachePolicy | None = None, error_handler=None, destinations=None, timeout: float | timedelta | TimeoutPolicy | None = None, trace_policy=None)`. If a sequence of retry policies is given, "the first matching policy will be applied".
- **New in 1.2.x:** `StateGraph.set_node_defaults(*, retry_policy=None, cache_policy=None, error_handler=None, timeout=None) -> Self`. These are graph-wide defaults; per-node values win. They are **not inherited by subgraphs**. `retry_policy` and `timeout` defaults also apply to error-handler nodes.
- `TimeoutPolicy(run_timeout=None, idle_timeout=None, refresh_on: Literal["auto","heartbeat"]="auto")`. It relies on asyncio cancellation, so a sync `time.sleep` blocks it. `runtime.heartbeat()` refreshes `idle_timeout`.
- `error_handler`: an optional node-level handler that runs when the node raises. Handler failures fail the run. Exact handler input semantics were **not verified**; read the docstring before use.
- `add_conditional_edges(source, path, path_map=None)`. `path` may return a node name, a list of names, or a `list[Send]`. Also `add_edge(["a","b"], "c")` (a join), `add_sequence([...])`, and `defer=True` to fan in after all pending tasks.
- `compile(checkpointer=None, *, cache=None, store=None, interrupt_before=None, interrupt_after=None, debug=False, name=None, transformers=None)`. **There is no `durability` argument on compile()** (VERIFIED). Pass `durability` per run.
- `RetryPolicy` (NamedTuple, VERIFIED): `initial_interval=0.5, backoff_factor=2.0, max_interval=128.0, max_attempts=3, jitter=True, retry_on=default_retry_on`. The default `retry_on` does **not** retry `ValueError`, `TypeError`, `OSError`, `RuntimeError` and similar. For `requests`/`httpx` it retries **only 5xx** (DOCS). Pass `retry_on=` a predicate to include 429 and connection errors.
- `CachePolicy(key_func=default_cache_key, ttl: int | None = None)` plus `compile(cache=InMemoryCache())` (`langgraph.cache.memory`). A `SqliteCache` also exists.
- `Send(node, arg, *, timeout=None)`, `Command(graph=None, update=None, resume=None, goto=())`, and `Command.PARENT`. Type a node that returns a `Command` as `-> Command[Literal["a","b"]]` (needed for graph rendering). Static edges out of the same node still execute.
- `recursion_limit` default is **1000** since 1.0.6 (DOCS). It is a top-level config key: `config={"recursion_limit": N}`. `GraphRecursionError` is in `langgraph.errors`. `RemainingSteps` (`langgraph.managed`) can be a state field for graceful exit.
- Runtime context and dependency injection: `from langgraph.runtime import Runtime`. Node signature `def node(state: S, runtime: Runtime[Ctx])`. Invoke with `graph.invoke(inputs, config, context=Ctx(...))`. `runtime.context`, `runtime.execution_info.thread_id`. Context is **not checkpointed**; pass it again on resume (INFERRED from "runtime" semantics).
- Concurrency: the config key **`max_concurrency`** is honoured by the async executor (VERIFIED: `pregel/_executor.py` creates `asyncio.Semaphore(max_concurrency)`). Use `config={"max_concurrency": settings.eval_concurrency}` to bound the `Send` fan-out of scenario runs, in addition to the user's `LLM_SEM` pattern.
- Streaming: `graph.stream(..., stream_mode="updates"|"values"|"messages"|"custom"|"checkpoints"|"tasks"|"debug")` (StreamMode literal VERIFIED). `get_stream_writer()` for custom progress events. The docs now also show **`graph.stream_events(input, config, version="v3")`** with projections (`stream.messages`, `stream.subgraphs`, `stream.interrupts`). It is available in 1.2.14 (`stream_events` defined in `pregel/main.py`). Use it optionally for the CLI progress view.

### 2.3 Persistence and durability
- Checkpointers: `InMemorySaver`/`MemorySaver` (`langgraph.checkpoint.memory`), `SqliteSaver` (`langgraph.checkpoint.sqlite`), `AsyncSqliteSaver` (`langgraph.checkpoint.sqlite.aio`), `PostgresSaver` (`langgraph.checkpoint.postgres`), `AsyncPostgresSaver` (`langgraph.checkpoint.postgres.aio`). Postgres needs `checkpointer.setup()` once and `thread_id` under 255 characters. `from_conn_string(...)` is used as a context manager in the user's code (`with SqliteSaver.from_conn_string(path) as cp:`).
- `durability` per run, set on `invoke/stream/ainvoke/astream`: `Literal["sync","async","exit"]`. **Default `"async"`** (VERIFIED in the source: `config.get(CONF, {}).get(CONFIG_KEY_DURABILITY, "async")`). Use **`durability="sync"`** for builder runs because nodes call paid or mutating remote APIs. This guarantees the checkpoint is written before the next step starts, so a crash after `deploy` won't redeploy on resume. `checkpoint_during` is deprecated.
- Resume after a crash: invoke again with `None` as input on the same `thread_id`. Nodes re-execute from their start. Successful sibling tasks in the same super-step keep their pending writes (INFERRED from persistence semantics; verify). **Make every side-effecting node idempotent**, for example by looking up an existing Freya agent by name/tag before creating one.
- State inspection: `get_state(config)`, `get_state_history(config)` (newest first), `update_state(config, values, as_node=...)` (which creates a new checkpoint), and replay from a `checkpoint_id`.
- **Serializer gotcha (security CVE-2026-28277, fixed in langgraph 1.0.10):** `JsonPlusSerializer` (ormsgpack) logs `Deserializing unregistered type ... will be blocked in a future version` for custom classes, including Pydantic models stored in state. If `LANGGRAPH_STRICT_MSGPACK=1`, unregistered types are blocked. Options: (a) store `model_dump(mode="json")` dicts in state and re-validate at node boundaries; or (b) `SqliteSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=[SpecModel, MoneyTable, ...]))`. Passing an explicit list switches to strict mode, so list every custom type. Recommendation: **(a) for simplicity**, with typed accessor helpers. Sources were secondary (CVE DBs, LangChain forum); verify against the installed version.
- Subgraph persistence (DOCS): `subgraph.compile(checkpointer=None)` (default) is per-invocation, inherits the parent checkpointer, and supports interrupts. `checkpointer=True` is per-thread memory and **does not support parallel calls to the same subgraph**. `False` is stateless (no interrupts). Since the scenario runner subgraph is fanned out in parallel, keep the default `None`.

### 2.4 Human-in-the-loop (DOCS + VERIFIED signature)
- `interrupt(value, *, response_schema=None)`. Typed `response_schema` (Pydantic, TypedDict, or dataclass) validates resume values and **requires langgraph>=1.2.12**. A plain JSON-Schema dict is exposed but not validated.
- Requires a checkpointer and a `thread_id`. The caller sees `result["__interrupt__"]` (invoke) or `stream.interrupts` (stream_events v3). Resume with `graph.invoke(Command(resume=value), config)`. With several parallel interrupts, resume with `{interrupt.id: value}`.
- Rules: the node **re-executes from its start** on resume. Do not wrap `interrupt()` in a bare try/except. Keep interrupt order deterministic. Put side effects **after** the interrupt or make them idempotent. Don't loop `interrupt()` inside one node; loop through a conditional edge instead. Static `interrupt_before/after` is for debugging only.

### 2.5 Eval / self-refinement loop guidance
- Official "evaluator-optimizer" pattern (DOCS, workflows-agents page): the generator reads `state["feedback"]`, the evaluator uses `llm.with_structured_output(Feedback)` where `Feedback(grade: Literal[...], feedback: str)`, and a conditional edge routes `"Rejected + Feedback"` back to the generator and `"Accepted"` to END. **The docs example has no iteration cap.** The user's own rule (and lesson 30 "bounded cycles") requires a counter cap. Add a budget cap too.
- Official "orchestrator-worker" pattern: `[Send("llm_call", {"section": s}) for s in sections]`, worker outputs merged by `completed_sections: Annotated[list, operator.add]`, then a synthesizer. This is the right shape for **scenario fan-out**.
- Multi-turn simulation helper (DOCS, LangSmith): `pip install openevals`. Use `from openevals.simulators import run_multiturn_simulation, create_llm_simulated_user` and `from openevals.llm import create_llm_as_judge`. The app contract is `def app(inputs: ChatCompletionMessage, *, thread_id: str, **kwargs)` returning `{role, content}`. Arguments: `max_turns`, `stopping_condition`, `trajectory_evaluators=[...]`. The result has `"trajectory"` and `"evaluator_results"`. Since the Freya chat API is stateful per `callId` and we need async plus a budget, a **hand-rolled LangGraph subgraph is preferable** (about 40 lines, no langchain-openai dependency). openevals is an optional reference.

### 2.6 LangChain 1.x structured output (DOCS + VERIFIED source)
- `model.with_structured_output(schema, *, method=..., include_raw=False)`. Methods: `"json_schema"` (provider-native), `"function_calling"` (tool call), and `"json_mode"`. `include_raw=True` returns `{"raw": AIMessage, "parsed": ..., "parsing_error": ...}`. Pre-bound models (after `bind_tools`) are not supported.
- **ChatAnthropic** (langchain-anthropic master, read today): `method: Literal["function_calling","json_schema"] = "function_calling"`. For `function_calling` it binds the tool **without** `tool_choice` when thinking is enabled/adaptive **or `not _supports_forced_tool_choice(self.model)`**, and warns that a missing tool call raises `OutputParserException`. `"json_schema"` binds `output_config` with the converted schema and parses with `PydanticOutputParser`. Native support needs `langchain-anthropic>=1.1.0` (DOCS). **Use `method="json_schema"` for every Claude structured call.** It is guaranteed-valid JSON and avoids the forced-tool 400 on Opus 5.5 / Sonnet 5.5 / Fable 5.1 (claude-api skill). INFERRED: whether the 1.7.5 release already contains `_supports_forced_tool_choice` was not checked against the tag, so do not rely on it.
- Anthropic JSON-schema limits (claude-api skill): **all objects need `additionalProperties: false`**. Recursive schemas, `minimum`/`maximum`/`multipleOf`, `minLength`/`maxLength`, and complex array constraints are **not supported**. String formats (`date`, `date-time`, `email`, `uri`, `uuid`, ...) **are** supported. The Anthropic SDK strips unsupported constraints automatically; whether langchain-anthropic does the same is INFERRED-unknown. Builder rule: LLM-facing Pydantic models get `model_config = ConfigDict(extra="forbid")` (emits `additionalProperties: false`), no numeric or length constraints in `Field(...)` (enforce them in `field_validator`s or the validation node), and no recursion.
- `create_agent(..., response_format=ToolStrategy(...) | ProviderStrategy(...) | SchemaType)` puts the result in `result["structured_response"]`. `ToolStrategy(handle_errors=True)` retries on `StructuredOutputValidationError`/`MultipleStructuredOutputsError` (`from langchain.agents.structured_output import ToolStrategy, ProviderStrategy`). The builder doesn't need `create_agent`: its LLM steps are single structured calls, not tool loops.
- `init_chat_model("anthropic:claude-opus-5-5")` uses the format `"{provider}:{model}"`. `max_retries` defaults to 6 in the generic doc, but **ChatAnthropic's own field defaults to `max_retries=2`** (VERIFIED source). `timeout` (alias `default_request_timeout`, default None). `rate_limiter=InMemoryRateLimiter(requests_per_second=..., check_every_n_seconds=..., max_bucket_size=...)` from `langchain.rate_limiters` (or `langchain_core.rate_limiters`).
- ChatAnthropic `max_tokens` defaults to the model profile's `max_output_tokens` (fallback 4096) (VERIFIED). INFERRED risk: a profile value like 128K on a non-streaming call can trip the Anthropic SDK's long-request guard or time out. **Set `max_tokens=16000` explicitly** for non-streaming structured calls (the claude-api skill recommends about 16K for non-streaming).
- `effort=` on ChatAnthropic is an alias of `reasoning_effort` and needs `langchain-anthropic>=1.5.3` (DOCS). Opus 5.5's default effort is `medium`, so set `effort="high"` for spec extraction and refinement, and `"low"`/`"medium"` for simulated customers.

### 2.7 Claude model facts relevant to the builder (claude-api skill, cached 2026-10-06)
| Model ID | Price in/out per MTok | Use in builder |
|---|---|---|
| `claude-opus-5-5` | $4 / $20 | default: spec extraction, repair, refinement, LLM judge |
| `claude-sonnet-5-5` | $2 / $10 | simulated customers (higher volume) or cheaper judge |
| `claude-haiku-5-5` | $0.10 / $0.50 (≤100K prompt) | cheap simulated customers or quick classifiers |
| `claude-fable-5-1` | $10 / $50 | only if the user explicitly asks |
- Opus 5.5: thinking is always on (`{type:"disabled"}` and `budget_tokens` return 400), effort defaults to `medium`, **forced `tool_choice` any/tool returns 400**, **sampling params return 400**, no assistant prefill, and structured outputs are supported. Sonnet 5.5: forced tool_choice returns 400, non-default sampling returns 400, and effort defaults to `high`.
- Use exact IDs with no date suffix. The user's adapter default `claude-sonnet-4-6` still works but is previous-generation.
- **Do not pass `temperature=0`** through `get_llm(**kwargs)` for these models. Get determinism from structured outputs plus deterministic validators, not sampling.

---

## 3. Recommended dependencies (pyproject sketch)

```toml
[project]
name = "freya-collections-builder"
version = "0.1.0"
requires-python = ">=3.12,<3.14"
dependencies = [
  "langgraph>=1.2.12,<1.3",          # 1.2.12+: interrupt(response_schema=...)
  "langgraph-checkpoint-sqlite>=3.1.1",
  "langchain-core>=1.6.9,<2",
  "langchain-anthropic>=1.7.5,<2",    # pulls anthropic 1.x (httpx2)
  "pydantic>=2.13", "pydantic-settings>=2.14",
  "jinja2>=3.1.6", "tenacity>=9.2", "httpx>=0.28.1",
  "rich>=13.7", "babel>=2.17",        # locale-aware money formatting (USER-CONV lesson 23)
]
[project.optional-dependencies]
openai = ["langchain-openai>=1.7"]                       # fallback provider (USER-CONV adapter)
api    = ["fastapi>=0.136", "uvicorn[standard]>=0.32",
          "langgraph-checkpoint-postgres>=3.1.2", "psycopg[binary,pool]>=3.2"]
dev    = ["pytest>=8", "pytest-asyncio>=0.24", "respx>=0.23.1", "ruff>=0.17", "mypy",
          "langgraph-cli[inmem]>=0.4.33"]
[project.scripts]
freya-builder = "builder.cli:main"
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
[tool.hatch.build.targets.wheel]
packages = ["builder"]          # flat package, USER-CONV; *.j2 under builder/ ship as package data (INFERRED hatch default)
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-q --strict-markers"
markers = ["integration: needs docker services", "live: hits Freya staging (spends credits)"]
[tool.ruff]
line-length = 100
[tool.ruff.lint]
select = ["E","W","F","I","N","UP","B","SIM","RUF"]
```
- `uv.lock` must be generated, but **uv is not installed on the host**. Options: `py -3.12 -m pip install uv` / `winget install astral-sh.uv`, or lock inside a container: `docker run --rm -v "$PWD":/app -w /app ghcr.io/astral-sh/uv:0.12.24-python3.13-trixie-slim uv lock` (INFERRED tag exists; uv publishes `{version}-python3.13-trixie-slim`-style derived tags. The docs list `python3.13-trixie-slim`).
- Recreate the host venv for 3.13 or 3.12 (`uv venv --python 3.13` lets uv download a managed CPython). The existing 3.14 venv violates `<3.14`.

---

## 4. Settings sketch (pydantic-settings, USER-CONV shape)

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8",
                                      extra="ignore", case_sensitive=False)
    # Freya
    freya_key: SecretStr                                   # env FREYA_KEY (exists in .env)
    freya_base_url: str = "https://staging-app.freyavoice.ai/api/v2"   # key is staging-only
    freya_workspace_id: str = "01e07b69-486a-4070-a7fd-3a2fc3120bf2"
    # LLM (adapter pattern)
    llm_provider: Literal["anthropic", "openai"] = "anthropic"
    llm_model: str = "claude-opus-5-5"
    sim_model: str = "claude-sonnet-5-5"
    judge_model: str = "claude-opus-5-5"
    anthropic_api_key: SecretStr | None = None
    # Builder behaviour
    hitl: bool = False                       # interrupts only when True (never in headless Docker)
    dry_run: bool = False                    # render+lint only, no Freya writes
    max_extract_attempts: int = 3
    max_refine_iters: int = 2
    eval_max_scenarios: int = 12
    eval_max_turns: int = 14
    eval_concurrency: int = 4
    target_pass_rate: float = 0.9
    checkpoint_backend: Literal["memory", "sqlite", "postgres"] = "sqlite"
    checkpoint_path: Path = ROOT / "data" / "checkpoints.sqlite"
    postgres_url: str = "postgresql://postgres:postgres@localhost:5432/langgraph"
    out_dir: Path = ROOT / "out"
```
- The `.env` in the hackathon repo also contains `FREYA_DASHBOARD_PASSWORD`. `extra="ignore"` means it is not loaded as a field. In compose, pass only the variables the container needs through `environment:` (`FREYA_KEY: ${FREYA_KEY}`) instead of `env_file: .env` to avoid exposing the password (INFERRED hardening).

---

## 5. Docker (uv-managed, multi-stage, non-root)

### 5.1 Facts (uv Docker guide, DOCS)
- Install uv with `COPY --from=ghcr.io/astral-sh/uv:<version> /uv /uvx /bin/` and pin a version (or a sha256 digest for reproducibility). Derived images exist with tags like `python3.13-trixie-slim`.
- Env: `UV_COMPILE_BYTECODE=1`, `UV_LINK_MODE=copy` (silences hardlink warnings with cache mounts), `UV_PYTHON_DOWNLOADS=0` (use the image's Python), `UV_NO_DEV=1`, `UV_PROJECT_ENVIRONMENT`.
- Layering: first `uv sync --frozen --no-install-project` with `--mount=type=cache,target=/root/.cache/uv` and `--mount=type=bind,source=uv.lock,...`, then `COPY . /app`, then `uv sync --locked`. Use `--locked` when pyproject is present (it asserts the lock is current). Use `--no-editable` plus multi-stage to copy only `.venv` into the runtime image.
- `.dockerignore` must include `.venv` (platform-specific).
- Dev loop: `docker compose watch` (Compose ≥ 2.22) with `action: sync` and `action: rebuild` on `pyproject.toml`.
- Pin choice (INFERRED): `ghcr.io/astral-sh/uv:0.12.24` (2026-10-08). 0.13.0 came out 2026-10-09, a fresh minor release; avoid it during a hackathon.

### 5.2 Dockerfile (proposed)
```dockerfile
# syntax=docker/dockerfile:1.7
ARG PYTHON_VERSION=3.13
FROM ghcr.io/astral-sh/uv:0.12.24 AS uv

FROM python:${PYTHON_VERSION}-slim-trixie AS build
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0 UV_NO_DEV=1
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-editable
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable

FROM build AS dev                        # tests + Studio; includes dev extra
ENV UV_NO_DEV=0
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --extra dev --extra api
ENV PATH="/app/.venv/bin:$PATH"

FROM python:${PYTHON_VERSION}-slim-trixie AS runtime
RUN groupadd --system app && useradd --system --gid app --home-dir /app --no-create-home app \
 && mkdir -p /app/data /app/out && chown -R app:app /app
WORKDIR /app
COPY --from=build --chown=app:app /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER app
VOLUME ["/app/data", "/app/out"]
ENTRYPOINT ["freya-builder"]
CMD ["--help"]
```
Notes:
- Prompts (`builder/prompts/**/*.j2`) must ship inside the wheel because of `--no-editable`. Hatchling includes non-.py files in listed packages by default (INFERRED; verify with `unzip -l` on the wheel). Load them with `importlib.resources.files("builder") / "prompts"`, not `Path(__file__)` tricks that assume a source checkout.
- Do not bake `.env` into the image; secrets arrive only at runtime.
- On a Windows host, add `.gitattributes` with `*.sh text eol=lf`. CRLF line endings break entrypoint scripts.

### 5.3 .dockerignore
```
.venv/
.env
.env.*
!.env.example
.git/
.idea/
.junie/
**/__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
.mypy_cache/
data/
out/
*.sqlite*
main.py          # PyCharm FastAPI hello-world scaffold currently in repo root
test_main.http
```

### 5.4 docker-compose.yml (profiles keep the default path minimal)
```yaml
name: freya-builder
x-app: &app
  build: { context: ., target: runtime }
  environment:
    FREYA_KEY: ${FREYA_KEY}
    ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
    FREYA_BASE_URL: ${FREYA_BASE_URL:-https://staging-app.freyavoice.ai/api/v2}
    LLM_MODEL: ${LLM_MODEL:-claude-opus-5-5}
    HITL: "false"
  volumes:
    - ./briefs:/app/briefs:ro
    - ./out:/app/out
    - builder-data:/app/data

services:
  builder:                                   # default: docker compose run --rm builder build --brief /app/briefs/brief.md
    <<: *app
    command: ["build", "--brief", "/app/briefs/brief.md"]

  api:                                       # optional FastAPI wrapper (POST /builds, GET /builds/{id}, POST /builds/{id}/resume)
    <<: *app
    profiles: ["api"]
    build: { context: ., target: dev }
    entrypoint: ["uvicorn", "builder.api:app", "--host", "0.0.0.0", "--port", "8000"]
    ports: ["8000:8000"]
    environment:
      CHECKPOINT_BACKEND: postgres
      POSTGRES_URL: postgresql://postgres:postgres@postgres:5432/langgraph
    depends_on: { postgres: { condition: service_healthy } }

  postgres:                                  # optional checkpoint store
    image: postgres:17-alpine
    profiles: ["api", "pg"]
    environment: { POSTGRES_USER: postgres, POSTGRES_PASSWORD: postgres, POSTGRES_DB: langgraph }
    healthcheck: { test: ["CMD-SHELL", "pg_isready -U postgres -d langgraph"], interval: 3s, timeout: 3s, retries: 10 }
    volumes: [pg-data:/var/lib/postgresql/data]

  studio:                                    # LangGraph dev server for Studio
    profiles: ["dev"]
    build: { context: ., target: dev }
    entrypoint: ["langgraph", "dev", "--host", "0.0.0.0", "--port", "2024", "--no-browser"]
    ports: ["2024:2024"]
    environment: { FREYA_KEY: ${FREYA_KEY}, ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}, DRY_RUN: "true" }

  mockbank:                                  # offline stub of the 4 mock-bank endpoints (local emulator/tests only)
    profiles: ["test"]
    build: { context: ., target: dev }
    entrypoint: ["uvicorn", "tests.stubs.mockbank:app", "--host", "0.0.0.0", "--port", "8787"]

  tests:
    profiles: ["test"]
    build: { context: ., target: dev }
    entrypoint: ["pytest", "-m", "not live"]
    environment: { MOCKBANK_URL: http://mockbank:8787 }
    depends_on: [mockbank]

volumes: { builder-data: {}, pg-data: {} }
```
- `langgraph.json` for Studio (DOCS keys): `{"dependencies": ["."], "graphs": {"builder": "builder.graph.build:make_graph"}, "env": ".env", "python_version": "3.13"}`. `make_graph()` must return the graph **without** a custom checkpointer, because the dev server supplies persistence (INFERRED from langgraph-api behaviour). `langgraph dev` defaults to host 127.0.0.1 and port 2024 and needs no Docker. `langgraph up` needs a LangSmith API key (DOCS), so don't use it. Studio's UI is served from smith.langchain.com, and an account is likely needed (INFERRED).
- The **mock-bank stub is only for offline tests and a local emulator**. The Freya-hosted agent always calls the real mock bank URL baked into its tool `default`s, so the stub cannot stand in during real Freya evaluation.

---

## 6. Proposed builder project layout (USER-CONV adapted)

```
builder/
  __init__.py            # re-exports: settings, build_graph
  settings.py            # §4
  cli.py                 # argparse + rich (USER-CONV); commands: build | resume | lint | eval
  api.py                 # optional FastAPI (lifespan opens AsyncPostgresSaver)
  llm/                   # copy of shared/llm adapter pattern (+ sanitizer extended for Anthropic limits)
  domain/
    spec.py              # Pydantic: CollectionsSpec, IdentityPolicy, ObjectionPlaybook, HardshipPolicy, ...
    money.py             # Decimal MoneyTable, normalise/format (babel), allowed-figure set, amount regex
    scenarios.py         # Scenario, Persona, matrix generator (covering array)
    workflow.py          # Pydantic mirror of Freya workflow JSON (nodes/tools/transitions) for validation
  freya/
    client.py            # httpx.AsyncClient, Bearer auth, workspaceId param, envelope unwrap, tenacity (GET/PUT only)
    gateway.py           # Protocol FreyaGateway + HttpFreyaGateway + InMemoryFreyaGateway (Repository pattern, USER-CONV)
  prompts/               # Jinja, PromptRegistry naming {name}.v{N}.j2
    _base/persona.j2, _base/safety_rules.j2
    extract/spec.v1.j2, extract/repair.v1.j2
    agent/role.v1.j2, agent/identity.v1.j2, agent/collection.v1.j2, agent/objections/*.j2, agent/close.v1.j2
    sim/customer.v1.j2
    judge/rubric.v1.j2
    refine/patch.v1.j2
  graph/
    state.py             # BuilderState, ScenarioState TypedDicts, BuilderContext dataclass
    nodes/               # one module per node (≤ 1 screen per function)
    routing.py           # all conditional-edge functions (pure; unit-testable)
    eval_subgraph.py     # scenario runner subgraph
    build.py             # build_graph(checkpointer=None) / make_graph() for Studio
    concurrency.py       # LLM_SEM, FREYA_SEM, CircuitBreaker (USER-CONV)
tests/
  conftest.py            # fake gateway, fake LLM runnables, tmp out_dir
  unit/                  # money, lint rules, renderer, grounding validator, routing
  graph/                 # topology + bounded loops with fakes injected via context
  integration/           # respx-mocked Freya client; mockbank stub (marker: integration)
  live/                  # marker: live — real staging, opt-in only
  stubs/mockbank.py
langgraph.json, Dockerfile, docker-compose.yml, .dockerignore, .env.example, pyproject.toml, uv.lock, .python-version (3.13)
```

---

## 7. Builder LangGraph topology (proposal)

```
START
  │
  ▼
ingest_brief ──► extract_spec ──► validate_spec ──┬─(errors & attempts<N)──► repair_spec ──► validate_spec
                                                  ├─(errors & hitl)────────► clarify_spec [interrupt] ──► validate_spec
                                                  └─(ok | give up w/ warnings)
                                                          ▼
                                                 compute_money_table
                                                          ▼
                                                  render_workflow ◄────────────────────────────────┐
                                                          ▼                                        │
                                                    static_lint ──(errors, fixable)──► refine_prompts ─┘
                                                          │ok
                                                          ▼
                                     approve_deploy [interrupt if hitl] ──(reject)──► END(report)
                                                          ▼
                                                  deploy_to_freya         (idempotent; durability="sync")
                                                          ▼
                                                  plan_scenarios          (resets scenario_results via Overwrite([]))
                                                          │ Send × k
                                                          ▼
                                        run_scenario (subgraph: sim-customer ⇄ /chat/message … /chat/end)
                                                          ▼
                                                  score_results           (money validator + gate checks + LLM judge)
                                                          ▼
                                                      decide ──(pass≥target | iter≥max | budget out)──► finalize ──► END
                                                          │ else
                                                          └──► refine_prompts ──► render_workflow (loop)
finalize: pick best_version (never a regressed one) → approve_publish [interrupt if hitl] → publish → write out/report.{json,md}
```

### 7.1 State (TypedDict, total=False)
```python
class BuilderState(TypedDict, total=False):
    run_id: str
    brief_path: str
    brief_text: str
    brief_sha: str
    spec: dict                         # CollectionsSpec.model_dump(mode="json") (serializer-safe, §2.3)
    spec_errors: list[str]
    extract_attempts: int
    money_table: dict                  # MoneyTable dump; includes allowed_figures: list[str]
    prompt_overrides: dict[str, str]   # refine loop writes ONLY editable slots
    workflow_json: dict
    lint_findings: list[dict]
    deploy: dict                       # {agent_id, workflow_id, version_id, template_shas}
    scenarios: list[dict]
    scenario_results: Annotated[list[dict], operator.add]   # Send fan-in; reset with Overwrite([])
    score: dict                        # {pass_rate, per_criterion, failures[...]}
    history: Annotated[list[dict], operator.add]            # one record per iteration
    refine_iter: int
    best: dict                         # best {version_id, pass_rate, overrides}
    spend: dict                        # {llm_usd_est, freya_turns}
    decision: str                      # HITL resume values
```
`BuilderContext` (dataclass, passed as `context=`) holds `freya: FreyaGateway`, `llm_factory`, `sim_llm_factory`, `judge_llm_factory`, and `registry: PromptRegistry`. Tests inject fakes there, which is cleaner than `patch.object(module, "get_llm")`. Both styles work.

### 7.2 Node-by-node
1. **ingest_brief** (deterministic). Read `.md`/`.txt`/`.json` (add `.pdf`/`.docx` if a sample brief needs them), normalise whitespace, compute `brief_sha` (thread_id = `f"{brief_sha[:12]}-{run_id}"`), and detect language/locale. If the brief contains structured tables (fees, test customers), parse them deterministically first.
2. **extract_spec** (LLM, `with_structured_output(CollectionsSpec, method="json_schema")`, Opus 5.5, `effort="high"`, `max_tokens=16000`). The schema requires **`evidence: list[str]` quotes for every policy and money figure**. Content: bank and agent persona, locale and currency, right-party rules, identity fields and attempts, disclosures (recording notice etc.), products and late-payment facts available from tools, fee and penalty rules, promise-to-pay window limits, partial-payment rules, hardship and vulnerability handling, abuse policy (warn then end), callback rules, the objection playbook for the 6 categories (forgot, can't afford, disputes the debt, already paid, wrong person, will pay later), closing requirements, and the tool list.
3. **validate_spec** (deterministic). Pydantic validation plus cross-field checks plus **grounding**: every evidence quote must be a (normalised) substring of the brief, and every money figure in the spec must appear in the brief or be an arithmetic derivation recorded in `MoneyTable` with its formula. Errors go into `spec_errors`.
   - Routing: errors and `extract_attempts < max_extract_attempts` → **repair_spec** (LLM receives the previous JSON plus the error list and returns a full corrected spec; a validation/repair loop). Errors and `hitl` → **clarify_spec** (`interrupt({"missing": [...], "spec": ...}, response_schema=SpecPatch)`). Otherwise proceed with documented safe defaults and flag them in the report.
4. **compute_money_table** (deterministic, `decimal.Decimal`, banker's rounding as the brief specifies). Holds canonical amounts and their formatted variants (`babel.numbers.format_currency(amount, currency, locale=...)`, plus spoken forms if TTS needs them; `num2words` is in the user's stack). Output: `allowed_figures` for the lint and transcript validators. Per-customer balances come at runtime from `get_account`, so prompts must say "quote amounts **only** from the get_account result or from the policy table, verbatim". The LLM must never do arithmetic on the call.
5. **render_workflow** (deterministic). **Jinja renders only prompt text** (role_message/task_message, global prompts), using the user's `env` settings and `StrictUndefined`. **The JSON structure is assembled by typed Python builders** (nodes, transitions, tools copied from the base workflow: `verify_identity`, `get_account`, `create_promise`, `close_call` with `alwaysRunsAt: ["end_of_call"]`, `end_call`) and validated against `domain/workflow.py` (a Pydantic mirror of the OpenAPI schema). If JSON must be templated, use `|tojson` for every interpolation, because raw Jinja-in-JSON escaping is brittle (INFERRED). Editable slots (`{% block objection_cant_afford %}`) take `prompt_overrides`. Gates, money, and tool names live **outside** the editable slots.
6. **static_lint** (deterministic). Each finding has a rule id and severity:
   - MONEY-001: every currency-like token in any prompt is in `allowed_figures` or is a `{{var}}` placeholder.
   - GATE-001: `get_account`/`create_promise` are unreachable before `verify_identity` success (graph reachability over transitions).
   - GATE-002: right-party confirmation precedes any debt disclosure. GATE-003: read-back and an explicit "yes" are required before `create_promise`. GATE-004: tool-success gate (the transition condition references a tool `ok:true`). GATE-005: the close path calls `close_call` and `close_call` has `alwaysRunsAt` end_of_call.
   - OBJ-001: all 6 objection categories handled. TONE-001: abuse policy present. HARD-001: hardship/vulnerability branch present.
   - VAR-001: every `{{var}}` is supplied by customerVariables/test_variables. PII-001: no hard-coded customer data. SCHEMA-001: Freya JSON validates.
   - Routing: no errors → approve_deploy. Errors in editable slots → refine_prompts (bounded). Errors in deterministic parts → fail fast (a builder bug) with the report.
7. **approve_deploy** (`interrupt` only when `hitl`). Payload: spec summary, money table, lint report, diff against the currently deployed version. Resume with `Literal["approve","reject"] | Edit`. Side effects come after this node (an interrupt rule).
8. **deploy_to_freya** (side effect). Look up the agent by name/tag, then create or update and record `version_id`. The exact endpoints are an open question (see other research notes: `POST /agents`, `PUT /agents/{id}`, `POST /versions/{entityType}/{entityId}`, branches). Must be **idempotent**: lookup-before-create, and store the IDs in state before the next step (with `durability="sync"`). `RetryPolicy(max_attempts=1)` on this node; the client retries only GET/PUT. `--dry-run` skips it.
9. **plan_scenarios** (deterministic, seeded). Build a covering array over: right party vs wrong person; identity pass, fail once then pass, or fail out; objection ∈ 6; temperament ∈ {cooperative, evasive, rude, abusive}; vulnerability ∈ {none, hardship, bereavement/illness}; promise ∈ {full now, partial, date beyond the allowed window, refuses}; callback request. Sample `eval_max_scenarios`. Return `{"scenarios": [...], "scenario_results": Overwrite([])}`.
10. **run_scenario** (subgraph via `Send`, bounded by `config["max_concurrency"]` and `FREYA_SEM`). The simulated customer (Sonnet/Haiku 5.5, Jinja persona with secret facts such as DOB, last-4, goal, and temperament) alternates with Freya `POST /chat/message` (`workflowId` or `agentId`, `versionId`, `customerVariables` such as `customer_id`). The loop ends when `endedByAgent` is true, at `eval_max_turns`, or on a customer hang-up; then call `POST /chat/end` (idempotent). Fetch call details (`GET /call/{callId}`) for tool events if available (see 03_freya_calls_reference.md). **Catch errors and return a failed result dict** rather than raising, so one bad scenario doesn't fail the super-step (recoverable vs unrecoverable, USER-CONV). Cost optimisation (INFERRED): drive the common prefix (greeting, identity success) once and branch with **`POST /chat/fork` {parentCallId, fromMessageId, versionId}**.
11. **score_results** (deterministic first, then LLM). Deterministic checks: money validator (every amount in agent turns ∈ allowed_figures ∪ amounts returned by tools in that call), gate order from the transcript and tool events, read-back before `create_promise`, `close_call` fired, no account details to a wrong person, abuse handled (warn then end politely), callback scheduled when requested. LLM judge: `with_structured_output(JudgeVerdict, method="json_schema")` returns per-criterion pass/fail plus an evidence quote from the transcript. A criterion counts as failed only when the evidence quote is a verbatim substring of the transcript (anti-hallucination, INFERRED). Aggregate `pass_rate` = share of scenarios passing all criteria (a proxy for the hackathon score).
12. **decide** (`Command[Literal["refine_prompts","finalize"]]`). Finalize if `pass_rate ≥ target` or `refine_iter ≥ max_refine_iters` or the spend budget is exhausted. Track `best`. If the new pass rate is below best minus epsilon, revert the overrides to best (eval-driven promotion, USER-CONV lesson 32).
13. **refine_prompts** (LLM, structured `PromptPatch{patches:[{slot, new_text, rationale}]}`). Input: failing transcripts (trimmed), rubric failures, and current slot texts. Only whitelisted slots can change, and patched text goes through static_lint again. Increments `refine_iter`. Re-evaluate failing scenarios plus a regression sample rather than the full matrix, to save credits (INFERRED).
14. **finalize**. Optional `interrupt` before publish. Publish the best version, then write `out/report.json` and `out/report.md` (spec, money table, lint, per-scenario results, template SHAs, IDs) and print a rich summary.

### 7.3 Where `interrupt()` fits (all optional, `settings.hitl`)
| Point | Why | Resume schema |
|---|---|---|
| `clarify_spec` | Brief is ambiguous or missing a required policy (e.g. no promise window) | `SpecPatch` (Pydantic) |
| `approve_deploy` | Human checks spec, money table, and lint before the first Freya write / credit spend | `Literal["approve","reject"]` or `{"edit": SpecPatch}` |
| `approve_publish` | Score below target after max iterations; publish anyway? | `Literal["publish","hold"]` |
In headless Docker runs (`HITL=false`) these nodes are bypassed by routing. They do not call `interrupt` at all, so a grader-run container can never hang waiting for input.

### 7.4 Execution config
```python
config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 200,
          "max_concurrency": settings.eval_concurrency}
graph.invoke(inputs, config, context=BuilderContext(...), durability="sync")
# crash-resume:  graph.invoke(None, config, context=..., durability="sync")
# HITL resume:   graph.invoke(Command(resume={"decision": "approve"}), config, context=...)
```
Node policies: `builder.set_node_defaults(retry_policy=RetryPolicy(max_attempts=2, retry_on=_is_transient))`. Override with `max_attempts=1` on `deploy_to_freya`/`finalize`, and set `timeout=TimeoutPolicy(run_timeout=...)` on LLM and network nodes (async only).

### 7.5 Testing plan (functional pytest, USER-CONV)
- Unit: `money.normalise/format/allowed_figures` (parametrized locales), each lint rule (fixtures of good and bad workflows), grounding validator, routing functions (pure), renderer (golden JSON snapshot), and the scenario-matrix generator (deterministic with a seed).
- Graph: topology (`set(graph.get_graph().nodes)`); bounded loops (fake judge always fails, so the run ends after `max_refine_iters` with no `GraphRecursionError`); idempotent deploy (the in-memory gateway sees exactly one create across resume); HITL bypass when `hitl=False`; interrupt/resume with `InMemorySaver` when `hitl=True`.
- Integration: `respx` on the Freya httpx client (envelope unwrap, retries on 429/5xx, no retry on POST); the mockbank stub for the local emulator. **respx can't mock the Anthropic SDK (httpx2)**, so use fake runnables for LLMs.
- Live (marker `live`, opt-in): one scenario against staging, with budget assertions.

---

## 8. Risks and gotchas (consolidated)
- `with_structured_output` default `function_calling` combined with forced tool_choice gives a **400 on claude-opus-5-5/claude-sonnet-5-5**. Always pass `method="json_schema"`.
- Passing `temperature` to Opus 5.5 / Sonnet 5.5 gives a 400. The user's `get_llm(**kwargs)` pattern makes this easy to do by accident.
- The Anthropic schema subset rejects numeric/length constraints and needs `additionalProperties:false`. Pydantic `Field(ge=..., max_length=...)` on LLM schemas can break calls (INFERRED: unknown whether langchain-anthropic strips them).
- ChatAnthropic `max_tokens` defaults to the model profile maximum. Set it explicitly to avoid non-streaming timeouts (INFERRED).
- Checkpoint serializer warnings or blocks for Pydantic objects in state (CVE-2026-28277 hardening). Store dumps or allowlist the types.
- `durability` defaults to `"async"`, so a crash can lose the checkpoint right after a remote write. Use `"sync"` and idempotent nodes.
- The evaluator-optimizer docs example has **no loop cap**. Always add an iteration counter and a budget cap.
- Running the eval against Freya spends credits (about $10 balance) and may create records in the mock bank (`create_promise`/`close_call` are POSTs that may hold grading state). Keep the eval small, add `--dry-run`/`--no-eval` flags, and confirm with the organisers.
- `uv` is missing on the host and the Docker daemon was stopped. The host venv is 3.14, which conflicts with the `<3.14` pin.
- uv 0.13.0 is one day old. Pin 0.12.24.
- Studio (`langgraph dev`) is a nice-to-have: it likely needs a LangSmith login in the browser, and `langgraph up` needs a LangSmith key (DOCS).

## 9. Open questions
- Exact Freya create/update/publish mechanism for workflow agents (no `POST /workflow` in the spec). This decides the `deploy_to_freya` node and its idempotency key.
- Does `POST /chat/message` cost credits per turn, and does `/chat/fork` avoid re-billing the shared prefix?
- Does `GET /call/{callId}` expose tool calls and results for chat calls? This is needed for gate checks and the money validator.
- Which `customer_id`s does the mock bank know (fixtures for simulated customers)? Are mock-bank POSTs from our eval harmful to grading state?
- Does the hackathon grader run our Docker image headless with a brief path or mounted file? What CLI contract does it expect (args, env names, exit codes, output files)?
- Does langchain-anthropic 1.7.5 strip unsupported JSON-schema constraints and add `additionalProperties:false` for `method="json_schema"`? Verify with one live call.
- Error-handler node semantics in langgraph 1.2.x (`add_node(..., error_handler=...)`): what input does it receive? Can it return a state update that lets the graph continue?

## Sources
- LangGraph source at tag 1.2.14: `libs/langgraph/langgraph/{graph/state.py,types.py,pregel/main.py,pregel/_executor.py}` (raw.githubusercontent.com/langchain-ai/langgraph/1.2.14/...); a copy is saved in `research/_lg_src/`.
- [Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api), [Use the graph API](https://docs.langchain.com/oss/python/langgraph/use-graph-api), [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts), [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence), [Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers), [Durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution), [Subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs), [Workflows & agents](https://docs.langchain.com/oss/python/langgraph/workflows-agents)
- [Pregel.stream reference (durability default)](https://reference.langchain.com/python/langgraph/pregel/main/Pregel/stream), [Durability type](https://reference.langchain.com/python/langgraph/types/Durability)
- [LangChain structured output](https://docs.langchain.com/oss/python/langchain/structured-output), [LangChain models](https://docs.langchain.com/oss/python/langchain/models), [ChatAnthropic integration](https://docs.langchain.com/oss/python/integrations/chat/anthropic), langchain-anthropic `chat_models.py` (master)
- [LangGraph CLI](https://docs.langchain.com/langsmith/cli), [Multi-turn simulation (openevals)](https://docs.langchain.com/langsmith/multi-turn-simulation)
- [uv Docker guide](https://docs.astral.sh/uv/guides/integration/docker/)
- CVE-2026-28277 / serializer allowlist: [miggo](https://www.miggo.io/vulnerability-database/cve/CVE-2026-28277), [JsonPlusSerializer ref](https://reference.langchain.com/python/langgraph.checkpoint/checkpoint/serde/jsonplus/JsonPlusSerializer), [forum: register type](https://forum.langchain.com/t/how-to-register-type-in-langgraph/3456), [forum: allowed_msgpack_modules in langgraph.json](https://forum.langchain.com/t/feature-request-wire-allowed-msgpack-modules-in-langgraph-json/3624)
- Python 3.14 status: [langchain issue #34441](https://github.com/langchain-ai/langchain/issues/34441), [langgraph v1 migration](https://docs.langchain.com/oss/python/migrate/langgraph-v1)
- PyPI JSON API (versions, release dates, classifiers, wheels) queried 2026-10-10.
- Claude model facts: the bundled `claude-api` skill (model table cached 2026-10-06; structured outputs and forced tool_choice notes).
- User repo: `C:/Users/Felladien/Documents/Projects/Python/LanggraphPlayground` (files listed in §1).
- Freya chat API schemas: `docs/openapi_prod.json` paths `/chat/message`, `/chat/fork`, `/chat/end` (read-only slice).
