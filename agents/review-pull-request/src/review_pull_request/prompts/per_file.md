## Task
Review this file's diff for real bugs, security issues, performance problems, and architectural concerns. Report every possible issue you can find, including style, formatting, naming, and anything you are unsure about.

## PR Context
{overview}

## Tool use
Before forming a verdict on any issue, if you need to understand a type, interface, base class, imported symbol, or existing pattern that the diff references but does not define, call the read_file tool to inspect the relevant file. You may call it multiple times for different files. Only call it when it would materially change your verdict. You MUST provide a "reason" argument explaining why you need to read the file — e.g. "checking how AuthService is implemented to verify the diff does not break the login flow".
{folder_listing}

## Mandatory checks
You must explicitly audit the diff for ALL of the following. Do not skip a category because it seems unlikely — check each one.

### Architectural & scalability
- Large bundles or modules loaded eagerly that could be code-split
- Synchronous operations blocking the event loop (heavy loops, deep recursion, large serialisations)
- Unbounded data structures or caches that grow without limit
- Missing pagination or streaming where a list endpoint returns all records
- Tight coupling between layers (e.g. business logic embedded directly in UI components)
- Missing error boundaries or graceful degradation

### React-specific bugs
- **Prop spread order:** if library/third-party props are spread AFTER explicit local event handlers (e.g. `<Input onChange={{handleChange}} ...{{{{...props}}}} />`), the spread silently overwrites the handler. Flag as HIGH severity if present; explicit props must come after the spread, or destructure first.
- **State wipe on re-render:** `useEffect` hooks whose dependency array contains an object or array reference that changes on every parent render (e.g. `useEffect(() => {{ setState(x) }}, [props.obj])`) will clobber user-edited local state. Verify object identity, not just shape.
- **Async race conditions in handlers:** form submission and step-navigation handlers (e.g. `handleNextClick`) that call validation or mutation APIs without `await` allow fast-clicking past in-flight operations. Check that promises are awaited or the UI is locked during the request.
- **Stale closures:** missing or incorrect dependency arrays in `useEffect` / `useCallback` / `useMemo`.
- **Derived state lifecycle:** state initialised from props with no mechanism to stay in sync (neither controlled nor properly reset).

### Security
- **Stored XSS via href:** user-controlled data passed directly into an `href` attribute without validation enables `javascript:` protocol injection. Treat any unvalidated variable in `href` as HIGH severity.
- Unescaped user input rendered as HTML (`dangerouslySetInnerHTML` or equivalent).
- Secrets, tokens, or credentials in client-side code or logs.

### Data integrity
- Runtime metadata added to core data models (e.g. a `.platform` tag appended client-side) that may break backend schema validation, database constraints, or API type contracts.
- Missing input validation or missing server-side re-validation of client-supplied values.

## Context
Review all issues in the context of the overall PR changes described above.

## Output format
Respond with valid JSON only, matching this exact schema:
{{
  "summary": "1-2 sentence overview of this file's changes",
  "issues": [
    {{
      "file": "path/to/file.ts",
      "line": 42,
      "severity": "high",
      "comment": "Description of the issue",
      "suggestion": "How to fix it"
    }}
  ]
}}

Rules:
- "line" must be the actual line number shown by the '+' column in the diff, NOT the @@ hunk header number.
- "severity" must be one of: "high", "medium", "low".
- "suggestion" is required — always provide a concrete fix.
- If no issues are found, return: {{"summary": "...", "issues": []}}
- Do not wrap the JSON in markdown code fences.
- Do not include any text before or after the JSON object.

## Diff
{diff_text}
