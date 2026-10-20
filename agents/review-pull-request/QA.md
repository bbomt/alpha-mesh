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
