# LangGraph conventions

For agents in this repo built on LangGraph (first: `agents/review-pull-request/src/review_pull_request/graph.py`).
Taken from the reference project `~/environment/learn/langgraph/checkpoints`.

- **Python 3.12.** Pin it (`requires-python = "~=3.12.0"`, `.python-version`); newer versions are
  problematic with the LangGraph modules. Use `uv` for everything (`uv sync`, `uv run pytest`).
- **Typed state.** Declare the graph state as a `TypedDict` (a Pydantic `BaseModel` only when it needs
  validation). Keep it to what must survive a crash: it is what a checkpointer saves.
- **Nodes return only the keys they update.** Treat the state as immutable inside a node; build new
  values (`{"results": {**state["results"], path: result}}`) rather than mutating the old ones.
- **Node signature:** `def node_name(state: State, runtime: Runtime[Context]) -> dict`.
- **Resources through `Runtime` context, not globals.** Models, sessions, clients and callbacks go in
  a context dataclass passed at invoke time (`graph.invoke(input, config, context=ctx)`) and read via
  `runtime.context`. The context is never checkpointed, so nothing in it can leak into the store.
- **Conditional edges return node names.** A routing function returns the literal name of the next
  node; list the possible targets in `add_conditional_edges(..., path_map)`.
- **Idempotent nodes.** On resume a node may run again. Make a node safe to repeat (no appending to
  outside lists, no double side effects); where that is impossible (e.g. posting a comment), say so
  next to the node and in the README.
- **Start / resume / skip.** Before invoking, read `graph.get_state(config)`: no values → start with
  the input; `next` non-empty → resume with `graph.stream(None, config)`; reached the end → skip.
- **One place builds a checkpointer.** Tests may use `InMemorySaver` or `SqliteSaver`; production code
  creates its saver in one module only.
