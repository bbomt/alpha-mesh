# PMAT-237 — Resumable reviews with LangGraph checkpoints

Work specification for `mesh review-pull-request`. Written so it can be handed to an engineer
(or an agent) and implemented sub-ticket by sub-ticket.

- Ticket: PMAT-237 (epic PMAT-10 ARCH+DELIVERY, label ALPHA-MESH)
- Related: PMAT-236 (retry policy / per-call deadline), PMAT-235 (parallel reviews), PMAT-262
  (per-call timing and tokens; blocked by this ticket)
- Reference implementation: `~/environment/learn/langgraph/checkpoints` (LangGraph quickstart with
  `DynamoDBSaver` and start/recover/skip logic)

---

## 1. Outcome

> A review that fails, hangs or is killed can be run again with the same command and continues from
> the file it stopped at. Every file already reviewed is kept, including its findings. A resumed run
> may use a different model.

What this enables: run a full review on a free model, and when it throttles after file 5 of 20, rerun
it (same or another free model) until every file is done. Low token cost, more wall-clock time.
Incremental (`--since-last-review`) reviews can keep using paid models.

### What does not change

This ticket changes **how the review executes**, not what it does. All existing business rules stay:

- **Security invariant:** content of an excluded file never reaches the model, by any path. Tokens are
  redacted before any message leaves the process (`Redactor`). Stages reach the model and files only
  through `ReviewSession`.
- Prompts, severities, JSON parsing, read-limit rules, not-found hints, the missing-path report,
  incomplete-file handling, the excluded-files notice, the report format and the
  `--since-last-review` baseline rules (`REVIEW_SIGNATURE`, `Reviewed commit`).
- All existing CLI flags behave as today.

The only intended user-visible changes are listed in section 7.

---

## 2. Current design (before)

`review.py:main()` runs one linear loop:

```
fetch PR info + diff ──► ReviewSession ──► stage1.run_overview (1 call, no tools)
                                       └─► for each file: stage2.run_file_review
                                             (hand-written tool loop: read_file, JSON retry)
                                       ──► formatter.render_report ──► github.post_comment
```

- The model client is `llm.py` (`httpx` POST to `/v1/chat/completions`, own retry/backoff), wrapped by
  `GuardedLLM` (redaction) inside `ReviewSession`.
- All state lives in local variables. Any exception, kill or hang loses every completed file.

---

## 3. Target design (after)

### 3.1 Overview

```
CLI (review.py)
  │  resolve PR, head SHA, diff mode, policy  ──► thread_id
  │  ReviewSession.create(...)  (policy, guarded model, guarded reader, read_file tool)
  ▼
Review graph (LangGraph StateGraph, checkpointer = DynamoDBSaver)
  START ─► begin ─► overview ─► review_next_file ─┐
                                  ▲               │ more files to review?
                                  └───────────────┘
                                                  └─► report ─► post ─► END

review_next_file calls the Reviewer Agent (a compiled subgraph, checkpointer=False):
  agent (guarded model with read_file bound) ◄──► tools (read_file via PolicyFileReader)
```

### 3.2 Checkpoint granularity (security-critical)

A checkpoint is written after each **outer** node: `overview`, each `review_next_file` step, `report`
and `post`. The Reviewer Agent's inner steps (model call ↔ `read_file`) are **never** checkpointed.

Why: inside the agent the message list holds raw `read_file` results *before* redaction (redaction
happens at the model boundary). Checkpointing those steps would write unredacted file content to
DynamoDB/S3. So:

- The Reviewer Agent subgraph is compiled with `checkpointer=False`. In LangGraph a subgraph invoked
  inside a node otherwise *inherits* the parent checkpointer.
- A file whose review was in flight when the run died has no checkpoint and is reviewed again.
  A half-finished file is never reported as reviewed.

### 3.3 Graph state (what is persisted)

Persisted state holds **results only**: no diff, no file contents, no prompts, no tokens.

```python
class FileResult(TypedDict):
    file_path: str
    summary: str
    issues: list[dict]          # Issue as dict: file, line, severity, comment, suggestion
    tool_calls_made: int
    incomplete: bool
    missing: list[str]          # unrecovered not-found paths from this file's review
    model: str                  # model that produced this result
    run: int                    # ReviewState.run of the invocation that produced it

class ReviewState(TypedDict):
    files: list[str]                                   # files to review, in diff order
    overview: str | None
    overview_model: str | None
    results: Annotated[dict[str, FileResult], merge]   # file_path -> result
    resumed_files: list[str]                           # filled at run start, for the report/console
    run: int                                           # invocation number, bumped by `begin`
    report: str | None
    report_id: str | None                              # new UUID each time `report` builds a report (3.6)
    posted_report_id: str | None                       # report_id that `post` last posted
```

The graph starts with a `begin` node (`START ─► begin ─► overview …`) whose only job is
`run = run + 1`. A RESUME (`stream(None)`) continues from the pending node and does not pass `begin`
again, so it stays in the same run; a new invocation after END passes `begin` and starts a new run.

The diff is not stored. On resume it is fetched again; it is deterministic for the same head SHA
(and the same base SHA for incremental reviews), which the thread key guarantees. Chunks are rebuilt
from `SafeDiff`, so excluded files never enter the state.

`session.missing_paths` is no longer the source of truth for the report: each file's misses travel in
its `FileResult.missing`, so misses from checkpointed files survive a resume. `report` rebuilds the
missing-path map from `results`.

### 3.4 Thread key

```
thread_id = "review-pull-request:{owner}/{repo}#{pr}:{head_sha}:{mode}:{policy_hash}"
mode      = "full" | "since-{base_sha}"
policy_hash = sha256 of the sorted, normalised exclusion patterns (first 12 hex chars)
```

- **The model is not in the key.** A resume may switch model. Each `FileResult` records its own model.
- A new head SHA, a different incremental base or a changed exclusion policy gives a new thread, so no
  stale result is ever reused. (Resuming only the files unchanged between two head SHAs is out of
  scope; see section 9.)

### 3.5 Start / resume / skip

Same three cases as the reference `invoke_thread_with_recovery`, using `graph.get_state(config)`:

| Checkpoint state | Action | Console |
|---|---|---|
| none | **START** with the initial input | `Starting review (thread …)` |
| values and `next` non-empty | **RESUME**: `graph.stream(None, config)` | `Resuming: N file(s) from checkpoint, M to review` + the two lists |
| values and `next` empty (reached END) | **SKIP**: print the stored report, post nothing | `Already completed and posted (thread …)` |

`--fresh` discards the existing thread state (`checkpointer.delete_thread(thread_id)`) and starts.

**Incomplete files are retried on resume.** A `FileResult` with `incomplete=True` is stored (so a
finished run reports it as today) but is treated as "to review" when a run resumes. A file needs a
usable result, and a different model may give one.

How that works with a thread that already reached END:

- SKIP applies only when the thread reached END **and** has no incomplete results.
- Otherwise the CLI invokes the graph again on the same thread (input `{}`), so `begin` starts a
  new run. Nodes are idempotent: `overview` returns nothing if an overview exists;
  `review_next_file` picks the next file that has no result, or whose result is incomplete **and**
  has `result.run < state.run`. A file retried in this run gets `result.run == state.run`, so it is
  never picked twice in one run, even if it comes back incomplete again.
- `report` builds a new report from all results with a new `report_id`; `post` posts it.
  This gives a new PR comment, as a rerun does today.

### 3.6 Posting exactly once

- `report` generates a fresh `report_id` (UUID4) every time it builds a report; it is checkpointed
  with the report. `post` sets `posted_report_id = report_id` and does nothing if they are already
  equal.
- Crash window: the comment is posted but the process dies before the checkpoint is saved. To close
  it, the posted comment carries a hidden marker `<!-- mesh-review-run: {report_id} -->`. Before
  posting, `post` lists the PR's comments by the token's account (as
  `incremental.find_last_review_comment` already does) and skips the post if that marker is present.
- A UUID, not a counter: `--fresh`, TTL expiry or a deliberate rerun (3.5) always produce a new
  `report_id`, so an old comment's marker can never suppress a new report. A crash-resume re-enters
  `post` with the same checkpointed `report_id`, so it never posts twice.
- **Fail closed:** if the comment listing fails, `post` raises (the run fails noisily, nothing is
  posted, `posted_report_id` stays unset). The report is still printed; the next resume retries
  the check and the post.
- **Truncation:** `post_comment` cuts oversized bodies at the tail (`MAX_COMMENT_CHARS`). The marker
  is passed to `post_comment` separately and appended **after** truncation, so it always survives.
  It sits after the footer and does not affect `REVIEW_SIGNATURE` or `Reviewed commit` parsing.

### 3.6a One run per thread at a time

Two runs on the same thread (e.g. a manual rerun overlapping a still-running one) would interleave
checkpoints and could both pass the marker check. `DynamoDBSaver` does no locking, so:

- At run start the CLI takes a **lease** on the thread: a conditional `PutItem` of a lock item
  (PK `LOCK#{thread_id}`, in the checkpoint table; 237c checks this prefix cannot collide with the
  saver's own keys) holding host, pid, start time, `expires_at` and an **owner token** (UUID4
  generated by this run).
- **Renewal, fail closed:** a daemon thread renews every 60 s (`expires_at = now + 5 min`) with a
  conditional `UpdateItem` (`owner = :token`). A transient error is retried with backoff within the
  minute. If the condition fails (another run took over) or the lease cannot be renewed before it
  expires, the renewer sets a "lease lost" flag and the run aborts at the next node boundary with
  `ERROR: lost the lease on this thread …`, exit 1, nothing posted. `post` also checks the flag
  immediately before posting.
- **Release, owner only:** in `finally`, the run stops and joins the renewer first, then deletes the
  lease with a conditional `DeleteItem` (`owner = :token`). A run that never acquired the lease
  (the live-lease abort below) deletes nothing.
- If a live lease exists the run aborts at once: `ERROR: another review is running on this thread
  (host, pid, started …)`. Exit 1, nothing reviewed.
- Takeover: a lease is taken over when it has expired, or when it was taken on **this host** by a
  pid that is no longer alive. A run killed by hand (the hung-call case) can therefore be resumed
  immediately on the same machine, and after at most 5 minutes from another.
- Needs `dynamodb:UpdateItem` and `DeleteItem` (in the access list, 3.10).

### 3.7 Model construction and naming

Model names follow `{provider}/{modelname}` (the OpenRouter standard), e.g. `openai/gpt-oss-120b`,
`anthropic/claude-sonnet-5-5`, `minimax/minimax-m2`.

One function in `session.py` builds the LangChain chat model from `CODE_REVIEW_URL` + model name:

| Endpoint host | Class | Model name sent |
|---|---|---|
| `openrouter.ai` | `ChatOpenAI(base_url=…/v1)` (or `ChatOpenRouter`) | full `provider/model` |
| `api.openai.com` | `ChatOpenAI` | `model` (prefix stripped) |
| `api.anthropic.com` | `ChatAnthropic` | `model` (prefix stripped) |
| anything else (MiniMax, Ollama, …) | `ChatOpenAI(base_url=…/v1)` (OpenAI-compatible) | `model` (prefix stripped) |

- A name with no `/` is passed unchanged.
- `/v1` is appended to the base URL if missing, as `llm.py` does today.
- Pick between `ChatOpenAI` and `langchain-openrouter`'s `ChatOpenRouter` for OpenRouter during 237b,
  based on which surfaces OpenRouter's 200-with-error-object responses more reliably (section 3.9).

### 3.8 Redaction wrapper (kept as simple as possible)

```python
# session.py — the only place a chat model is built
redact = RunnableLambda(lambda msgs: redactor.scrub_messages(msgs))
review_model = redact | chat_model.bind_tools([read_file_tool])
overview_model = redact | chat_model
```

- `Redactor.scrub_messages` is the existing `scrub`, adapted to LangChain `BaseMessage` objects
  (content strings, and the string parts of list content). The token patterns do not change.
- `read_file_tool` is a LangChain `@tool` defined in the session. It calls `session.reader`
  (`PolicyFileReader`), so excluded paths are refused exactly as today. Its schema matches
  `READ_FILE_TOOL_SCHEMA` (`path`, `reason`).
- Stages and graph nodes receive the session, never a raw chat model or reader.
  `test_architecture.py` is extended to cover the new modules (no `langchain_openai`,
  `langchain_anthropic`, `httpx` etc. outside `session.py`).

### 3.9 Retries and timeouts

`llm.py`'s HTTP and retry code is removed. Behaviour is kept as follows:

| Today (`llm.py`) | After |
|---|---|
| 3 attempts, backoff 2/4/8 s on 429/500/502/503, timeouts, connection errors | SDK `max_retries=2` (3 attempts): exponential backoff **with jitter**, honours `Retry-After`, also retries 408/409 and all 5xx |
| 120 s request timeout | `timeout=120` |
| Malformed 200 body retried, then `LLMError` | `.with_retry(retry_if_exception_type=(MalformedResponse,), stop_after_attempt=3)` on a small parse check |
| 200 body with an error object: `ProviderError`, fails fast with its message | Same: detect the error object and raise `ProviderError`, not retried (PMAT-236 may change this) |
| 404 with tools: retry without tools | **No fallback without tools (owner decision, 2026-10-14):** without tools the review cannot read files, so the fallback silently gave a weaker review. Instead the **same call, with tools,** is retried twice with backoff: on OpenRouter a 404 here means no provider that supports tools is reachable for the model *right now*, which can be transient. After 3 attempts: `LLMError` (see below). Deliberate behaviour change, covered by tests. PMAT-236 later moves this into its single classification |

`LLMError` / `ProviderError` stay as the exceptions the CLI catches, so the fatal-error message
(model, API host, "nothing was posted") is unchanged. LangChain/SDK exceptions are mapped to them in
`session.py`.

**Knowing to switch model.** The overview call has no tools, so the first call with tools is file
1's review. A model with no tool support therefore fails right at the start, after the overview,
which is checkpointed and kept. The error says what to do:

```
ERROR: LLM API call failed during <file>: model 'x/y' has no endpoint that supports tool calls
  at openrouter.ai (404, after 3 attempts).
  The review needs tool calls (read_file). Resume with a model that supports them:
    mesh review-pull-request <url> --model <provider/model>
  Completed work is kept: overview + N file(s).
```

If the 404 happens mid-review (OpenRouter's tool-capable providers for that model went away), the
same message applies and the resume keeps every finished file. A pre-flight check (OpenRouter's
`/api/v1/models` lists `supported_parameters`, incl. `tools`) could catch this before the overview;
it is not in scope.

For PMAT-236: the SDK retries 429 even when the cause is exhausted credits, which PMAT-236 wants to
fail fast. PMAT-236 will likely set SDK `max_retries=0` and put all classification in one retry
wrapper in `session.py`. 237b keeps the SDK retries as the no-regression baseline.

Not a regression and not fixed here: a call the provider keeps alive (a hang) is bounded by neither
today's `httpx` timeout nor the SDK timeout. PMAT-236 adds the per-call deadline; this ticket makes a
hang cheap to recover from.

### 3.10 Storage and infrastructure

- `DynamoDBSaver` from `langgraph-checkpoint-aws`, with compression and S3 offload for large
  checkpoints, as in the reference. Every non-dry run uses it; no AWS credentials means a clear error
  before the review starts.
- **TTL: configurable, default 2 days** (owner decision: conservative, keeps DynamoDB small; extend
  if development shows reviews need longer to finish). One source: `checkpointTtlDays` in `cdk.json`
  context per environment. The stack writes it to SSM (`…/checkpoint-ttl-days`); the agent reads it
  and passes `ttl_seconds` to `DynamoDBSaver` (the saver stamps the `ttl` attribute on write).
  A checkpoint that has expired simply means the next run starts fresh.
- S3 objects must not outlive their checkpoints. The reference IAM grants
  `PutBucketLifecycleConfiguration`, which suggests `DynamoDBSaver` sets the bucket lifecycle from
  `ttl_seconds` itself. 237a confirms this. If it does, the stack defines no lifecycle rule; if not,
  the stack sets one from the same `checkpointTtlDays`.
- Table and bucket names are read from SSM:
  `/pmat/{env}/alpha-mesh/review-pull-request/checkpoint-table` and `…/checkpoint-bucket`.
  `CODE_REVIEW_CHECKPOINT_TABLE` / `CODE_REVIEW_CHECKPOINT_BUCKET` override them (integration tests).
  `DEPLOY_ENV` defaults to `dev`.
- `--dry-run` uses `InMemorySaver` and a fake model, and needs no AWS.

Infrastructure is AWS CDK (TypeScript, pnpm), in this agent's folder, following
`PMATFE-mfe-sales/packages/be/infra`:

```
agents/review-pull-request/
  infra/cdk/
    bin/cdk.ts              # App; requires -c config=dev|staging|prod
    lib/checkpoint-stack.ts # DynamoDB table (PK/SK strings, PAY_PER_REQUEST, TTL attr "ttl"),
                            # S3 bucket (block public access, SSE-S3), SSM parameters
                            # (table, bucket, ttl-days). No IAM resources (see access note)
    test/checkpoint-stack.test.ts  # CDK assertions
    cdk.json  package.json  tsconfig.json  jest.config.js
  infra/scripts/deploy-pipeline-infra.sh  # validates DEPLOY_ENV/AWS_REGION, pnpm install, cdk deploy
  scripts/deploy-dev-from-ec2.sh          # sets DEPLOY_ENV=dev, AWS_REGION, AWS_ACCOUNT_ID; calls the above
```

- **Access, same as the PMATFE-mfe-* projects:** their CDK grants permissions only to runtime
  principals (Lambdas, via `table.grantReadWriteData(fn)`). People and the EC2 instance act through
  the IAM Identity Center (SSO) `dev-EC2` permission set, e.g. the mfe-sales integration tests use the
  tables directly. This agent runs on EC2 as the developer, so **the stack grants no access
  through IAM** (no policies, users or roles for the agent) and relies on `dev-EC2`. Exception: in
  dev, `autoDeleteObjects` makes CDK add its own Lambda and execution role to empty the bucket on
  teardown; that role is CDK's and grants the agent nothing. The reference's policy attached to an IAM user is a pre-SSO
  setup and is not copied.
- Actions the agent needs: DynamoDB GetItem, PutItem (incl. conditional), UpdateItem, DeleteItem,
  Query, BatchGetItem, BatchWriteItem; S3 PutObject, GetObject, DeleteObject, PutObjectTagging, and
  Get/PutBucketLifecycleConfiguration if the saver sets lifecycle itself.
- 2026-10-14 probe with `dev-EC2`: list/describe tables, `get-item`, and listing an S3 bucket work.
  Writes are not yet proven: **237a's first check** after deploy is a put / conditional put / update /
  delete on its own table and a put / get / delete in its own bucket. If any is denied, stop and
  ask the owner.
- Stack name `PmatAlphaMeshReviewCheckpointsStack-{env}`. Dev uses `RemovalPolicy.DESTROY` and
  `autoDeleteObjects`; staging/prod `RETAIN`.
- The reference shell scripts `create.sh` / `teardown.sh` are not copied; CDK replaces them.

### 3.11 Python and packaging

- Pin Python 3.12 (`requires-python = "~=3.12.0"`, `.python-version`). The reference notes newer
  versions are problematic with these LangGraph modules.
- Add: `langgraph`, `langchain-core`, `langchain-openai`, `langchain-anthropic`,
  `langgraph-checkpoint-aws`, `boto3`; dev: `langgraph-checkpoint-sqlite` (kill tests).
  Use the versions in the reference `uv.lock` as the floor.
- Remove `httpx` only if nothing else still uses it (`tools.py` / `github.py` do today; keep it).

### 3.12 Conventions

Bring the reference project's LangGraph conventions into `alpha-mesh/CLAUDE_LANGGRAPH.md` (typed
state, nodes return only the keys they update, resources through `Runtime` context rather than
globals, conditional edges return node names, idempotent nodes) and import it from
`alpha-mesh/CLAUDE.md`. Leave out the demo-specific parts.

Node idempotency matters here: every outer node may run again on resume. `overview`,
`review_next_file` and `report` are pure apart from model calls. `post` is made idempotent by 3.6.

---

## 4. Module changes

| Module | Change |
|---|---|
| `review.py` | CLI only: args, PR/diff resolution, thread key, session, checkpointer, start/resume/skip, error mapping. The review loop moves to `graph.py` |
| `graph.py` (new) | `ReviewState`, outer nodes, `build_review_graph(session, checkpointer)` |
| `reviewer_agent.py` (new, replaces the loop in `stage2.py`) | Reviewer Agent subgraph: `agent` node and `tools` node, porting the stage 2 rules (dedup reads, read limit then "answer now", not-found hints and misses, JSON retry, incomplete) |
| `stage1.py` | Uses `session.overview_model`; prompt unchanged |
| `stage2.py` | Keeps the prompt, `Issue`/`FileReviewResult`, parsing and severity helpers; the tool loop moves to `reviewer_agent.py` |
| `session.py` | Builds the LangChain model (3.7), the redaction wrapper (3.8), the `read_file` tool and error mapping |
| `guard.py` | `Redactor` gains `scrub_messages` for LangChain messages; `GuardedLLM` removed when nothing uses it |
| `llm.py` | Reduced to `LLMError` / `ProviderError` (or merged into `session.py`); HTTP code removed |
| `checkpoints.py` (new) | `make_checkpointer(config)` (DynamoDB from SSM/env), `thread_id(...)`, `policy_hash(...)` |
| `formatter.py` | Header lists every model used; each file's summary line names its model (also overview); hidden run marker |
| `github.py` | `has_run_marker(pr_url, report_id)` for exactly-once posting |

Seams for tests (no test hooks in production code): `main(argv, *, checkpointer=None,
model_factory=None, github=None)` or an equivalent `run_review(...)` function the CLI calls, so tests
inject a `SqliteSaver`, fake chat models and fake GitHub calls.

---

## 5. Tests (written first, red for the stated reason)

Tests use **fake chat models** (a small `BaseChatModel` subclass with scripted replies that supports
`bind_tools`, and a variant that blocks forever on a chosen file). They may build fakes directly; the
production code still builds models only in `ReviewSession`. No test calls a real model.

### 5.1 Acceptance tests (boundary: the CLI process)

Run the CLI entry in a **subprocess** (`tests/fixtures/run_review.py` wires the seams: `SqliteSaver`
on a temp file, fake model, fake GitHub). A `SqliteSaver` file survives `kill -9`; `InMemorySaver`
does not.

| # | Test | Expectation |
|---|---|---|
| A1 | Kill (SIGKILL) after file N completes, resume | Files N+1.. reviewed; files 1..N not re-sent to the model; report lists every file exactly once |
| A2 | **Hung call, SIGKILL:** fake model blocks on file K; kill; resume | Files 1..K-1 from checkpoint (not re-sent); K.. reviewed; every file once |
| A3 | Hung call, SIGINT and SIGTERM (parametrised) | As A2; no partial result recorded for K |
| A4 | Hung **overview**, killed | Nothing posted; resume re-runs overview; no file results to reuse |
| A5 | Provider error object on file K (`Upstream idle timeout exceeded`) | Run fails noisily as today (exit 1, nothing posted); resume keeps files 1..K-1 **and their findings** |
| A6 | Resume with a **different model** | Files 1..K-1 keep their original model; K.. use the new one; header lists both models; each file's summary names its own model |
| A7 | Head SHA changed between runs | New thread; nothing reused |
| A8 | Exclusion policy changed between runs | New thread; nothing reused |
| A9 | Crash after a successful post (post succeeds, process killed before checkpoint) | Setup asserts `report_id` is already in the checkpoint written by `report`, before `post` runs. Resume reuses it, finds the marker, does not post again |
| A10 | Completed and posted thread run again | SKIP: stored report printed, no model call, no post |
| A11 | Run that finished with an incomplete file, run again | Only the incomplete file is reviewed again; a new report is posted (new `report_id`); other files not re-sent |
| A12 | `--since-last-review` killed and resumed | Same as A1 for the incremental diff; key uses `since-{base}` |
| A13 | `--fresh` | Existing thread discarded; every file reviewed |
| A14 | Resume console output | Lists files from checkpoint and files reviewed now |
| A15 | `--fresh` after a posted run, and a rerun after TTL expiry | A new report is posted (new `report_id`); the old marker does not suppress it |
| A16 | Second run on a thread while the first holds a live lease | Aborts with the lease owner; first run unaffected |
| A17 | Run killed by SIGKILL, resumed at once on the same host | Dead-pid lease is taken over; resume proceeds |
| A18 | Report over `MAX_COMMENT_CHARS`, crash after post, resume | Truncated comment still carries the marker (same checkpointed `report_id`, as A9); no second post |
| A19 | Comment listing fails in `post` | Run fails noisily, nothing posted; next resume posts once |
| A21 | Lease renewal fails (fake DynamoDB rejects the conditional update) | Run aborts noisily at the next node; nothing posted; checkpoints so far kept |
| A22 | Run aborts because another run holds a live lease | The other run's lease is untouched (owner-conditional delete) |
| A20 | File incomplete again on its retry run | Not picked a second time in that run; report marks it incomplete |

### 5.2 Security (canary, `tests/test_canary.py`)

Invariant: *excluded content and tokens never reach the model or the checkpoint store, by any path.*
Paths: overview prompt, per-file prompt, `read_file` results, folder listings and not-found hints
(as today), **plus** the checkpoint blobs (DynamoDB items and S3 objects; `SqliteSaver` rows in
tests).

| # | Test |
|---|---|
| C1 | All existing canary tests pass against the LangChain path (recording fake model sits after the redaction runnable) |
| C2 | After a full run, a killed run and a resumed run, no checkpoint blob contains the canary marker or any planted token (scan every serialised checkpoint and pending write) |
| C3 | **Protection off:** compile the Reviewer Agent *with* the inherited checkpointer and show C2 fails (a token in an allowed file's `read_file` result appears in the store) |
| C4 | A token in an allowed file is redacted before the model sees it (existing test, new path) |

### 5.3 Unit tests

- `test_llm.py` → `test_model_errors.py`: port five existing tests (malformed 200 retried;
  non-JSON body, no `choices`, non-object tool arguments → `LLMError`; provider error object →
  `ProviderError` without retry). Replace `test_malformed_body_on_the_no_tools_fallback_is_retried_too`
  with: a 404 on a call with tools is retried twice with tools (backoff patched), succeeds if a later
  attempt succeeds, else raises `LLMError` naming tool support and the `--model` resume hint; a call
  without tools is never made.
- Model construction table (3.7), including prefix stripping and `/v1` handling.
- `thread_id` / `policy_hash` stability (pattern order does not matter; any pattern change does).
- `test_architecture.py`: new modules may not import chat-model classes, `httpx`, readers or the
  checkpointer directly.
- Existing `test_stage2.py`, `test_review.py`, `test_tools.py` etc. pass with the LangChain fake
  (adapt fixtures, not assertions).

### 5.4 Integration (after deploy)

- I1: one real run of the graph against the deployed DynamoDB/S3 with a fake model: start, kill,
  resume, then delete the thread (tests clean up after themselves).
- CDK assertions test (`infra/cdk/test`): table keys, TTL, bucket encryption and public-access block,
  SSM parameters (incl. ttl-days), and no IAM resource that grants access to the table or bucket:
  no `AWS::IAM::ManagedPolicy`, no `AWS::IAM::User`, and no role or policy whose statements name the
  table or bucket. The one allowed exception, in dev only, is the execution role of CDK's
  `Custom::S3AutoDeleteObjects` Lambda (created by `autoDeleteObjects: true`).

---

## 6. Delivery plan (one sub-ticket, branch and PR each)

Each sub-ticket follows the workflow: tests first (red for the stated reason) → implement → green →
commit → deploy if infra → push → `mesh review-pull-request <PR>` loop (stop when no HIGH/MEDIUM
findings in the ticket's changes; 2 passes) → DONE with a Jira comment.

| Sub-ticket | Scope | Done when |
|---|---|---|
| **PMAT-264 (237a) — Checkpoint infra (CDK)** | `infra/cdk`, `infra/scripts`, `scripts/deploy-dev-from-ec2.sh`, CDK assertion tests, README section | `deploy-dev-from-ec2.sh` deploys; table, bucket and SSM params exist; write probe (3.10) passes with `dev-EC2`; a manual `DynamoDBSaver` put/get works from EC2 |
| **PMAT-265 (237b) — LangGraph + LangChain models, no behaviour change** | 3.7–3.9, 3.11, 3.12, graph with `InMemorySaver`, Reviewer Agent subgraph, ported unit and canary tests (C1, C4), `CLAUDE_LANGGRAPH.md` | All existing tests pass; a real review of a small PR gives the same report shape as before |
| **PMAT-266 (237c) — Durable checkpoints and resume** | `checkpoints.py`, thread key, start/resume/skip, incomplete retry, multi-model header, `--fresh`, thread lease (3.6a), A1–A8, A10–A14, A16, A17, A20–A22, C2–C3, I1 | Acceptance tests green; a real review killed mid-run resumes from the right file |
| **PMAT-267 (237d) — Exactly-once post and docs** | Run marker (`report_id`), `has_run_marker` (fail closed), marker kept on truncation, A9, A15, A18, A19, README (resume, `--fresh`, free-model workflow, AWS prerequisites), `.env.example` | A9 green; README matches the CLI |

237a and 237b can run in parallel; 237c needs both; 237d needs 237c.

---

## 7. User-visible changes

- A rerun of the same command after a failure resumes, and says so.
- `--fresh` flag.
- Report header lists every model used (`**Models:**` instead of `**Model:**` when more than one),
  and each file's summary names the model that reviewed it, e.g.
  `` `src/x.py` (model: `z-ai/glm-5.3-flash`) — summary… ``. The overview names its model too.
- Hidden run marker in the PR comment.
- AWS credentials (DynamoDB/S3) are required for non-dry runs.

---

## 8. Prerequisites

- AWS credentials on the EC2 instance for account 609382076869, eu-west-1 (CDK deploy and runs).
- `pnpm` (v11) and Node for CDK; `uv` and Python 3.12 for the agent.
- `gh auth login` as today.

---

## 9. Out of scope

- Per-call deadline and retry classification (PMAT-236).
- Parallel file reviews (PMAT-235). The sequential `review_next_file` loop can later become a LangGraph
  `Send` fan-out; LangGraph keeps completed sibling writes when one task in a step fails.
- Reusing results for files unchanged between two head SHAs.
- Automatic restart loops; the user reruns the command.
- Per-call timing and token telemetry (PMAT-262, which this ticket unblocks).

---

## 10. Decisions (owner, 2026-10-14)

1. **404 with tools:** no fallback without tools; retry the same call twice with backoff, then fail with an `LLMError` that says to resume with a tool-capable `--model` (3.9).
2. **TTL:** configurable, default 2 days (3.10).
3. **Models in the report:** header lists every model; each file names its model (section 7).
4. **Access:** same as the PMATFE-mfe-* projects; no IAM access grants (CDK's auto-delete role in dev excepted), rely on the SSO `dev-EC2`
   permission set; 237a proves write access first (3.10).
