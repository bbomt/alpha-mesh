# QA log: review quality seen while testing

Quality issues in the agent's reviews, recorded for later eval work (PMAT-269), not fixed on the spot.
Each entry: date, PR and pass, model, what the review said, why it is wrong or weak.

## 2026-10-20: PR #19 (PMAT-236), pass 1, `nvidia/nemotron-3-ultra-550b-a55b:free`

- **False HIGH, unchecked library behaviour.** "`openai.APIError.code` can be an integer, so
  `e.code == "402"` misclassifies." The SDK stores `str(code)` (`openai/_exceptions.py`). The model
  had `read_file` available but did not read the library before rating it HIGH.
  Eval idea: a HIGH about a third-party API should cite the library code it relied on.
- **Inapplicable MEDIUM.** "`WatchdogTransport` lacks `handle_async_request`." It subclasses
  `httpx.BaseTransport`, the sync interface (async is the separate `AsyncBaseTransport`), and the
  CLI is sync only. A generic completeness checklist was applied without reading the context.
- **Useful MEDIUM, right place, partly wrong reason.** "Error message may become `None`." The real
  failure was a generic SDK message that dropped the provider's error body. Fixed in `786c45b`.

## 2026-10-20: PR #19, pass 2, `nvidia/nemotron-3.5-lightning:free`

- No review produced: the first file's call streamed for more than 160 s twice and hit the per-call
  deadline. Resumed on `nemotron-3-ultra` from the checkpoint; 0 findings. A latency and cost issue,
  not a quality one; tracked on PMAT-236.

## 2026-10-20: PR #20 (PMAT-278), pass 1, `nvidia/nemotron-3-ultra-550b-a55b:free`

- **False HIGH, assumed API semantics.** "`run.tasks` is never consumed, so `on_node` never fires;
  iterate `run` instead." In LangGraph's v3 `GraphRunStream`, iterating any projection is what drives
  the run (its docstring: "the caller's `for` loop is the pump"). The PR's own test asserts the node
  events, which the model could have read.
- **False HIGH, contradicted by the PR's tests.** "Node errors are captured in the task's `error` field
  and swallowed." Node exceptions propagate out of the loop; the existing CLI error tests pass on this path.
  Eval idea: a HIGH that a test in the same PR disproves should count as a false positive in scoring.

## 2026-10-20: PR #20, full review, `nvidia/nemotron-3-ultra-550b-a55b:free`

2 HIGH and 3 MEDIUM; none confirmed.
- **False HIGH, the pass 1 claim again in another form.** "`on_node` is called after the entire graph
  completes, so verbose output prints all node names at once." Iterating `run.tasks` drives the run, so
  each event comes as its node starts.
- **False HIGH, invented library signature.** "`Recorder.process` must take `(event, state)`." The
  LangGraph `StreamTransformer.process(self, event) -> bool` takes one argument, and the canary test
  using it passes. Same class as PR #19 pass 1: a HIGH about a third-party API without reading it.
- **Defensive-coding MEDIUMs on internal data.** A possible `KeyError` on `task["name"]`, and a
  return type of `run.output`. Both are guaranteed by LangGraph's own payloads.
- **MEDIUM restating a documented trade-off.** "`os._exit(130)` skips cleanup." The code comment and
  the README explain that this is deliberate, because a worker thread blocks a normal exit, and that
  finished work is already checkpointed.

## 2026-10-20: PR #20, pass 3, `nvidia/nemotron-3.5-lightning:free`

- **False HIGH, repeated a third time, against the test that disproves it.** "`on_node` callbacks only
  fire after the entire graph finishes." The finding itself names
  `test_node_events_arrive_while_the_graph_runs_not_after_it`, which asserts that each node event comes
  before that node's model call and passes. A second model now makes the same claim, so this is not one
  model's quirk.
  Eval idea: a finding that names a passing test as its evidence should be checked against that test
  before it is posted.

## 2026-10-21: PR #19, pass 3 (incremental), `nvidia/nemotron-3.5-lightning:free`

- 0 findings across 7 files, in 56 minutes. With the 15-minute per-call deadline no call was cut
  off; under the old 160 s deadline this model had failed on the same PR.
- **Overview gets the change backwards.** It says "keep-alive SSE padding resets the idle timer". The
  PR does the opposite: padding is not counted as reply data, so it never resets the timer.
  Eval idea: check the overview's claims about the change against the diff.

## 2026-10-21: PR #19, final full review, `nvidia/nemotron-3-ultra-550b-a55b:free`

2 HIGH, 6 MEDIUM and 5 LOW; none of the HIGH or MEDIUM findings confirmed.
- **False HIGH, half a condition read.** "Streamed errors carry provider codes like
  `rate_limit_exceeded`, so transient errors become permanent." The check is
  `code in statuses or _TRANSIENT_TEXT.search(message)`, and the message match catches them.
- **HIGH for a non-issue.** "`httpx.Client` is never closed." There is one client per one-shot CLI
  run, the same as the SDK's own client before this PR. LOW at most.
- **Speculative MEDIUMs, ×3.** Async sleep, contextvar scope, and no async transport, in a sync-only
  CLI. The async transport is the same claim as pass 1's MEDIUM, already dismissed in the PR.
- **False MEDIUM, a test fixture misread.** "`_Trickle` class state leaks between tests." The
  fixture makes a new `Handler` subclass for each test, and the tests set attributes on that subclass.
- **MEDIUM, intended behaviour.** "Unclassified exceptions are re-raised raw." A programming error is
  not a provider failure, and `main()` has a top-level handler.

## Summary across both PRs (2026-10-20 to 21)

Of 21 HIGH or MEDIUM findings over 7 passes, 2 were real, both MEDIUM, and both were fixed. All 8 HIGH
findings were false or not issues. Incremental passes converged to 0; full reviews never did. The
review-loop stop rule therefore counts only *confirmed* HIGH and MEDIUM findings
(`CLAUDE_ENGINEERING.md`).
