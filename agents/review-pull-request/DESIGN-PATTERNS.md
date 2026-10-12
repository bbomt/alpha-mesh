# Design patterns in review-pull-request

## The one rule

> **Content of an excluded file never reaches the model, by any path.**

"Excluded" means it matches `.pr-review-ignore`, `[exclusions]` in `review_config.toml`, or the built-in sensitive patterns (`**/.env`, `**/.env.*`, `**/.envrc`, `**/.claude/*`, `**/.mesh/*`, `**/config/active.json`).

File content can reach the model three ways:

1. the **overview prompt** (the whole diff)
2. the **per-file prompts** (one file's diff)
3. the **`read_file` tool** (the model asks to open a file)

Each of these once had its own copy of the exclusion check, so a fix in one place left a hole in another. The design below means the rule is enforced in one place, and code cannot get round it by accident.

```
                 .pr-review-ignore + review_config.toml + built-in rules
                                     │
                                     ▼
                              ┌─────────────┐
                              │ PathPolicy  │  "is this path excluded?"
                              └─────────────┘
              ┌──────────────────────┼───────────────────────┐
              ▼                      ▼                       ▼
     ┌─────────────────┐   ┌───────────────────┐    ┌────────────────┐
     │    SafeDiff     │   │ PolicyFileReader  │    │   GuardedLLM   │
     │ (diff without   │   │ (read_file refuses│    │ (redacts known │
     │  excluded files)│   │  excluded paths)  │    │  tokens)       │
     └─────────────────┘   └───────────────────┘    └────────────────┘
              └──────────────────────┼───────────────────────┘
                                     ▼
                              ┌─────────────┐
                              │ReviewSession│  hands only these to the stages
                              └─────────────┘
                                     ▼
                      stage1 (overview)   stage2 (per-file)
```

## The patterns, in plain terms

### 1. Specification: `PathPolicy` (`policy.py`)

**What it is:** one small object that answers one question: *is this path excluded?*

**Why:** checking a path is harder than it looks. `.env`, `.ENV`, `./.env`, `/.env` and `svc/../.env` are all the same file. Previously this logic was copied into the chunker, the read tool and the review loop, and each copy handled a different subset of these cases. Now the path is cleaned up (normalised) and compared case-insensitively in one place, and nothing else compares paths. A test fails if `fnmatch` shows up in any other module.

Policies combine with `|` (`PathPolicy(a) | PathPolicy(b)`), which is the composite part of the pattern.

### 2. Value object, "parse, don't validate": `SafeDiff` (`safe_diff.py`)

**What it is:** a diff that has already been through the policy. It can only be built with `SafeDiff.from_raw(raw_diff, policy)`, and it can't be changed afterwards (frozen).

**Why:** the stages accept a `SafeDiff`, not a string. If someone passes the raw diff by mistake, the code doesn't fit; there's no `.text` on a plain string. That is exactly the bug the overview pass once had: it was handed the raw diff while the per-file reviews got the filtered one. `SafeDiff` also records which files were excluded, so the report can list them.

### 3. Decorator: `PolicyFileReader` (`readers.py`)

**What it is:** a wrapper with the same `read_file(path, max_lines)` method as the real readers (`GitHubFileReader` for the API, `LocalFileReader` for a checkout). It checks the policy first and refuses an excluded path before anything is read. With a local checkout it also checks where a symlink really points.

**Why:** the read tool no longer needs an `exclusions=` argument threaded through every call. Whoever holds the wrapped reader simply cannot read an excluded file.

### 4. Decorator, used as a proxy: `GuardedLLM` (`guard.py`)

**What it is:** a wrapper with the same `chat(messages, tools)` method as the real model client (`HttpLLM`). Before passing messages on, it redacts known secrets: the API token, the GitHub token, and token-shaped strings such as `sk-…`, `ghp_…` and `AKIA…`.

**Why:** defence in depth. The policy keeps excluded *files* out; this catches a credential that someone pasted into an ordinary file.

### 5. Facade: `ReviewSession` (`session.py`)

**What it is:** one object built once per run. It creates the policy and hands out only the protected pieces: `review_llm`, `reader` and `safe_diff()`.

**Why:** the stages receive the session, not the raw tools. `stage1.py` and `stage2.py` don't import the HTTP client or the file readers at all, and a test enforces that. So the only way for a stage to talk to the model or read a file is through the protections.

## How we know it works

- **`tests/test_canary.py`:** the end-to-end check.
  - It plants a unique secret in every path in: the diff, case variants, nested `.env.*` files, the incremental diff, and `read_file` requests (including `../` tricks).
  - It runs the real pipeline against a fake model that records everything it is shown, and asserts the secret never appears.
  - It also runs once with protection switched off and checks that the secret *does* appear, so the test can't pass by accident.
- **`tests/test_architecture.py`:** checks that the stages can't bypass the session, that only `policy.py` compares paths, and that only `safe_diff.py` builds a `SafeDiff`.
- Unit tests for each piece: `test_policy.py`, `test_safe_diff.py`, `test_policy_reader.py` and `test_guard.py`.

## Adding something new

- **A new way content reaches the model:** add it to the canary test first, then route it through the session.
- **A new kind of exclusion:** add patterns to the policy. Don't add a path check anywhere else.
- **Files that are noise rather than secrets** (for example, letting the model *read* `README.md` without *reviewing* it): add a second `PathPolicy` to the session with its own rules. The design already allows this; we have chosen not to do it yet.
