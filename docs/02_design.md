# Template Design Specification — CMN-C2-285 Smaregi POS Transaction Agent

## Position in the AgentCore Architecture

- **Agent Class**: `SmaregiPOSTransactionAgent` (`src/graph/graph.py`)
- **Category**: Cat 2 (multi-step domain workflow). The outer graph is the fixed
  5-node backbone; the domain pipeline is encapsulated in a `GraphNode` (`main`
  slot) wrapping an inner graph (`src/graph/domain_workflow_graph.py`).
- **Agent type**: tool-calling — classify intent -> extract POS-transaction fields
  -> build a Smaregi POS REST API request -> call the tool -> format the
  confirmation. No retrieval, no autonomous loop.
- **Three-Layer Separation**:
  - State: flat TypedDict `State(AgentState)` (no Pydantic — not msgpack-safe)
  - Node: `execute(self, state) -> dict` override only
  - Graph: composition (`register_nodes()` + `super().register_nodes()`;
    `add_edges()` not overridden on the outer graph)

| Layer | Class |
|---|---|
| L1 Base (framework base class) | AgentBaseGraph — direct framework inheritance |

## Architecture Overview

### Outer graph — node configuration (`src/graph/graph.py`)

| Node | Responsibility | Input State | Output State | Trust | Inherits/Overrides |
|------|---------------|-------------|--------------|-------|-------------------|
| initialize | framework setup (schema, session, trust) | user_input | session/trust fields | framework default | InitializeNode (default) |
| pre_process | validate the caller contract; screen both caller text channels; serialize the request into `validated_input` | user_input, input_context | validated_input, store_hint, caller_fields | **VERIFIED_EXTERNAL** (the single external gate) | PreProcessNode (FunctionNode) |
| main | run the inner Smaregi workflow subgraph | validated_input, caller_fields | result, intent, store_id, transaction_id, product_code, record_id, record_ref, record_label, confirmation, smaregi_payload | GraphNode (caller ctx forwarded unchanged) | SmaregiWorkflowGraphNode (GraphNode) |
| post_process | shape `formatted_output`; enforce the external output boundary | inner-result fields | formatted_output | ANONYMOUS | PostProcessNode (FunctionNode) |
| finalize | framework finalize (metadata, timing) | — | response_metadata | framework default | FinalizeNode (default) |

### Inner workflow — node configuration (`src/graph/domain_workflow_graph.py`)

The inner graph inherits the plain base graph (fully custom linear topology). The
5 pipeline steps map 1:1 to inner nodes. **Every inner domain node declares
`required_trust_level = TrustLevel.ANONYMOUS`** — the caller's
`InvocationContext` is forwarded into the subgraph unchanged, so the single
external trust gate stays on the backbone `pre_process`.

| Inner node | Step | Responsibility | Output | Trust |
|------|------|---------------|--------|-------|
| validate_input | 1 ValidateInput | empty/non-request guard; instruction-override refusal; flag-and-redact of email/token-like strings before logging | validated_input, store_hint, redaction_flags | ANONYMOUS |
| classify_intent | 2 ClassifyIntent | deterministic keyword classification -> lookup_transaction / summarize_daily_sales / check_product_sales; low confidence -> lookup_transaction (the narrowest single-record read) | intent | ANONYMOUS |
| infer_smaregi_fields | 3 InferSmaregiFields | extract store id / transaction id / product code / ISO date; assemble the request parameters per intent; an unresolved id is left empty, never invented | store_id, transaction_id, product_code, target_date, smaregi_payload | ANONYMOUS |
| call_smaregi_api | 4 CallSmaregiApi | report on the caller's own POS record when one was supplied, otherwise call the client (lookup / daily-sales / product-sales); token via ctx.secrets; 4xx/5xx -> status=error | record_id, record_ref, record_label, store_id | ANONYMOUS |
| confirm | 5 Confirm | format intent + record reference into a human-readable confirmation | confirmation, result | ANONYMOUS |

### Data Flow

```
Outer:  START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
                                              | (RETRY, max 3) ^
Inner (inside main / SmaregiWorkflowGraphNode):
        START -> validate_input -> classify_intent -> infer_smaregi_fields
              -> call_smaregi_api -> confirm -> END
```

The request text travels as a JSON string: `pre_process` serializes
`{"text", "store_hint"}` into `validated_input`,
`SmaregiWorkflowGraphNode.extract_input()` hands that JSON to the subgraph, and
`validate_input` parses it back.

**The validated caller contract does NOT travel that way.** The framework masks
`validated_input` at every node boundary, so a caller-supplied value can be
rewritten between hops — a store id of the wrong digit shape comes out
`[MASKED]`. The contract therefore crosses on the caller-context bridge
(`src/graph/context_bridge.py`): `extract_input()` stashes it on a ContextVar
immediately before the subgraph invoke, and the inner graph's
`_extra_initial_state()` seeds it back into the inner state. That hook is also
what supplies `input_context` at all inside a subgraph — `GraphNode.execute()`
invokes the inner graph without forwarding it, so an inner read would otherwise
always see `{}`.

### State Definition (`src/schemas/state.py`)

All domain fields are declared `NotRequired[...]` — fields are absent until their
producer node writes them. Dict/list payloads are stored as JSON strings
(`Optional[str]`) via the module helpers `to_json` / `from_json`.

| Field | Type | Purpose | Producer |
|-------|------|---------|----------|
| store_hint | NotRequired[str] | caller-supplied store id/code hint; never inferred | pre_process / validate_input |
| caller_fields | NotRequired[Optional[str]] | JSON — the VALIDATED caller contract (bounded ids, ISO dates, finite amounts and counts); carried across the graph boundary by the bridge | pre_process |
| store_id | NotRequired[str] | resolved store id (pass-through when the hint or text already carries one) | infer_smaregi_fields |
| transaction_id | NotRequired[str] | explicit transaction (receipt) id from the request text; never invented | infer_smaregi_fields |
| product_code | NotRequired[str] | explicit product code for sales-count checks; never invented | infer_smaregi_fields |
| target_date | NotRequired[str] | explicit ISO date (YYYY-MM-DD); empty -> latest business day | infer_smaregi_fields |
| redaction_flags | NotRequired[Optional[str]] | JSON list of the categories redacted before logging | validate_input |
| record_label | NotRequired[str] | human-readable label of the affected record | call_smaregi_api |
| smaregi_payload | NotRequired[Optional[str]] | JSON — the assembled request parameters | infer_smaregi_fields |
| smaregi_config | NotRequired[Optional[str]] | JSON — the runtime `smaregi:` section injected by the inner graph | inner graph |
| record_id | NotRequired[str] | transaction head id / summary id | call_smaregi_api |
| record_ref | NotRequired[str] | record reference (`smaregi://transactions/<id>`, `smaregi://stores/<id>/daily-sales/<date>`) | call_smaregi_api |
| confirmation | NotRequired[str] | human-readable confirmation | confirm |

`intent`, `result`, `validated_input`, `formatted_output` and `input_context` are
inherited from `AgentState` and are **not** re-declared.

**State constraints (mandatory, satisfied):**
- Flat TypedDict only (primitives + JSON-serializable) — no Pydantic/dataclass.
- No credentials in State — the access token is read via `ctx.secrets`.
- `InvocationContext` read via `InvocationContext.from_state(state)`, never stored.

## Configuration

Two files, two jobs. `config/agent.yaml` is the STATIC manifest the registry reads
at ROOT level (there is no `agent:` block). `config/config.yaml` carries the
RUNTIME parameters and is passed to the graph as `config=`.

| File | Keys | Read by |
|---|---|---|
| config/agent.yaml | id, name, namespace, version, enabled, category, generation_mode, industry, base_type, class, required_trust_level, requires | the registry, at discovery time |
| config/config.yaml | max_retry, timeout_s, smaregi.base_url | the graph constructor |

Nodes take **no constructor arguments**. `SmaregiWorkflowGraphNode._parent_config()`
reads `config/config.yaml` and forwards the `smaregi` section plus the runtime
values (as an `agent` section) to the inner graph under `config["configurable"]`;
the inner graph's `_extra_initial_state()` injects the `smaregi` section into
State as a JSON string, where `CallSmaregiApiNode` reads it.

**The standalone entry point loads the runtime file itself.** The platform
registry constructs the agent with that config; `src/api/server.py` must do the
same, or every declared value is silently absent and the agent runs on defaults
it never declared.

## Caller-data contract

`POST /invoke` accepts `input_context` alongside the request text. The adapter
enforces a serialized size cap (256 KB); `PreProcessNode` owns the contract and
validates every field.

| Field | Shape | Bound |
|---|---|---|
| store_id / store_hint / store_code | POS identifier | inert `[A-Za-z0-9][A-Za-z0-9_-]{0,31}` |
| transaction | `{transaction_id, store_id, total}` | ids as above; `total` finite, 0 .. 1e12 |
| daily_sales | `{store_id, sum_date, sales_total, transaction_count}` | ISO date; amount finite and in range; count a whole number 0 .. 9,999 |
| product_sales | `{product_code, store_id, sum_date, sales_count}` | as above |

Rules that hold for all of them:

- **Type before value.** A number, boolean, mapping or list where a string is
  required is refused outright, never coerced: `str(float("nan"))` is `"nan"`,
  which passes an identifier shape check, so coercion would let a non-finite
  value name the store of a lookup.
- **Every number is finite and bounded.** NaN and ±Infinity survive `float()` and
  arrive intact through raw JSON, and every comparison against NaN is False — a
  silent fail-OPEN on exactly the figures this agent reports. They are refused,
  as are out-of-range magnitudes and fractional counts.
- **Counts are capped below five digits.** A count renders verbatim; the cap is
  what keeps it structurally distinguishable from an unmarked monetary run at the
  output boundary.
- **Every rendered string is an inert identifier.** Free text there would be
  caller-controlled output.
- **Refusals name the field, never the value**, and an unrecognised field NAME is
  masked rather than echoed.
- **Absent fields are simply absent**: the workflow falls back to what it can read
  out of the request text and to the built-in transport.
- **A credential-shaped string on this channel is refused at the adapter (HTTP
  422), naming the field.** The framework scans every value of every node result
  for credential patterns, and the backbone's first node returns
  `input_context` verbatim in its own result — so a credential anywhere in that
  channel fails the run at node one, before any template code executes, and the
  caller gets an error status with no explanation. The request cannot succeed
  either way, so the entry point refuses it with something actionable. Every
  field this agent declares is an inert identifier, which makes the channel
  structurally unable to carry a legitimate credential.

## Security Design

- **Trust gate** — the single external gate is
  `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; every inner
  domain node — **including the tool-calling `CallSmaregiApiNode`** — declares
  `TrustLevel.ANONYMOUS`. `GraphNode.execute()` forwards the caller's
  `InvocationContext` into the subgraph **unchanged** (no elevation), and
  `VERIFIED_EXTERNAL < INTERNAL`, so declaring an inner node `INTERNAL` would deny
  a legitimate external caller before the call runs — the boundary is enforced
  exactly once, at `pre_process`. The agent-level default trust is declared in
  `config/agent.yaml`. `src/api/server.py` is the entry-point auth boundary
  (`INVOKE_AUTH_TOKEN` -> VERIFIED_EXTERNAL elevation).
- **Instruction-override screen (template-owned)** — `src/services/security.py`
  refuses directives aimed at the model: chat-template control tokens screened as
  a CLASS (`<|…|>`, `[INST]`, `<<SYS>>`), instruction-override and exfiltration
  phrasing, and model-role reassignment. It runs on the request text AND on every
  decoded string in `input_context`, keys included, at any depth, on the PARSED
  mapping (so JSON escaping cannot smuggle a phrase past it). Two things make it
  the template's own guarantee rather than the platform's: it is enforced inside
  `execute()`, so calling the node directly still refuses; and it screens the text
  BOTH raw and post-strip — `<|im_start|>` matches the markup pattern, so a screen
  that only ran on sanitized text would delete the token and forward the directive
  residue as ordinary text, turning a detectable attack into an undetectable one.
  The reverse direction is enforced too: every alternative is anchored on a full
  directive phrase, so ordinary retail prose ("please ignore the voided line on
  the previous receipt", "acting as the store manager") is not refused.
- **Input flag-and-redact** — `ValidateInputNode.execute()` runs a deterministic
  (regex, not model-based) scan for email addresses and access-token-like strings
  and redacts them before any logging. A POS request legitimately names stores and
  products, so this is flag-and-redact, not a hard reject. The framework PII mask
  additionally masks emails/phones/names in `user_input`/`validated_input`.
- **Credentials** — the integration token is read via
  `ctx.secrets.get("SMAREGI_TOKEN")`, never `os.environ`, never stored in State.
  It is read OPTIONALLY, and `config/agent.yaml requires.secrets` is therefore
  empty: the default transport sends no request and needs no credential, and
  declaring a secret that is not provisioned makes the agent fail to compile.
  Wiring in a live transport is what makes the secret required — and with a live
  transport a missing token is a hard `status=error`, so a real API is never
  called unauthenticated.
- **Audit** — every node's `execute()` emits a positional
  `emit_trace_event("<event>", {small non-PII payload}, state)` on its success
  path (intent / presence signals only — never request text, customer-attributable
  sales content, or credentials), plus refusal events on the screening paths.
  `__call__()` is never overridden and framework lifecycle events are not
  re-emitted.

  | Node | Event |
  |------|-------|
  | pre_process | `pre_process_complete`, `pre_process_validation_failed` |
  | validate_input | `validate_input_complete`, `validate_input_refused` |
  | classify_intent | `classify_intent_complete` |
  | infer_smaregi_fields | `infer_smaregi_fields_complete` |
  | call_smaregi_api | `call_smaregi_api_complete` |
  | confirm | `confirm_complete` |
  | post_process | `post_process_complete`, `post_process_blocked` |

## External output schema

The response is the agent's product, so its shape is a contract, not a detail.

**Aggregates only, on a 1,000 grid.** Monetary figures are reported as aggregates
rounded to the nearest 1,000, rendered with an explicit `JPY` marker, and the
response carries a `schema_note` saying so. A single receipt's exact total is a
raw line item — customer-attributable financial data — and is **never rendered**:
a transaction lookup reports the record's identity and store, not its amount.
Counts (transaction counts, unit counts) are structural and render verbatim.

**Identifiers never render bare.** Every identifier is emitted behind a `#` or
inside a `smaregi://` reference. At an output boundary a standalone digit run is
indistinguishable from an unmarked amount, and POS identifiers are frequently
purely numeric — rendering one bare invites the boundary to rewrite it and hand
the caller a different record.

`PostProcessNode` enforces all of this in three ordered layers:

1. **Credential scan**, walking the WHOLE output structure — nested mappings and
   lists included, because `smaregi_payload` is a mapping and `error` is a list,
   and a scan of top-level strings would step straight past a credential one level
   down. A finding names the LOCATION and never the matched value. (That is not
   only good hygiene: the framework runs its own credential scan over whatever
   this node returns, so a violation message that quoted the match would make that
   scan raise — and the containment step below would never be applied.)
2. **Precision grid**, applied to the narrative fields (`record_label`,
   `confirmation`). Monetary tokens are identified by FORM (comma-grouped, or runs
   of 5+ digits) and by CURRENCY CONTEXT (a 3-letter code or a symbol, before or
   after the value, attached or separated by any horizontal whitespace run,
   signed), never by magnitude, and every off-grid token is snapped with an audit
   count. Scope is a field list rather than the whole output on purpose: the
   grammar's identifier guards protect a value from its NEIGHBOURS, so they cannot
   protect an identifier that IS the whole string — a grid applied to
   `record_id = "1234567"` would rewrite it to `1,235,000`. Every non-narrative
   field is an identifier, a reference, an ISO date or a label, all validated to
   an inert alphabet upstream, so none of them can carry an amount.
3. **Re-scan**, so no rewrite performed inside the gate can produce an unscanned
   surface.

**Every error return clears the output.** `AgentBaseGraph.get_output()` projects
`formatted_output or result` with **no status check**, so an error return that
merely set a status would still ship the un-gated inner answer — credentials
included — inside the error envelope. Both error returns (a gate violation, and a
pre-existing inner-workflow error arriving from the subgraph) therefore overwrite
every output-bearing field: `result`, `confirmation`, `record_label`,
`smaregi_payload`, `intent`, and the resolved POS identifiers `record_id`,
`record_ref`, `store_id`, `transaction_id`, `product_code`.

**No error envelope names a record.** `record_id` / `record_ref` are this agent's
lookup evidence — the gate above *refuses* a SUCCESS that lacks them — so
returning them under an ERROR status would tell a caller being informed of failure
that a POS record was nonetheless resolved, and which receipt, store or product it
was. Every non-success return goes through one module-level `_contain()` helper,
and the envelope it publishes is a **closed set**:

```
{"reason": "<closed-set code>"}
```

`reason` is one of the module's `ERROR_REASONS` — `smaregi_workflow_failed` (the
inner workflow reported an error) / `output_withheld_by_gate` (the output gate
refused the response) — and is a constant, which keeps the mapping **truthy** — a
falsy `formatted_output` would re-open the `or result` projection this containment
exists to prevent. Nothing else is published: never `error_log`, never the gate's
violation entries, never any node-authored text. Those lines can embed an upstream
API error body, identifiers or caller-derived fragments, and truncating or
redacting them is not a closed set. `error_log` stays the **internal** channel —
the state reducer appends to it and the audit trail needs it — and is not
projected to the caller; the inner entries are not re-emitted by `post_process`
(the reducer would duplicate every line). A gate violation is written to
`error_log` naming the offending PATH only (fixed keys and indices, never the
value; a credential-shaped mapping key is withheld from the label rather than
quoted), and the `post_process_blocked` / `post_process_error_contained` audit
events carry the reason code and a **count** only.

**Error reasons are closed-set labels.** `error_log` is operator-side, but it is
the audit trail, so a reason that interpolates upstream text stores the same
disclosure server-side for every failed run. `call_smaregi_api` reports the **HTTP
status** for an API error (a live tenant's error body is unbounded third-party text
that can quote the record it refused) and the **exception type** for a transport
failure (a transport error string can carry the request URL and the record id).
`pre_process` refusals name the contract field only (a constant), never the value.

### The precision grammar, and what it must not break

Three properties are load-bearing and each is pinned by tests in both directions:

- **The delimiter stays inside one line** (`[ \t]*(?:\n[ \t]*)?`). A plain `\s*`
  spans blank lines, so a 3-letter uppercase word ending a line would bind to the
  number opening the next block and rewrite it — the gate would edit document
  structure.
- **A decimal is absorbed whole** (`(?:\.\d+|(?!\.\d))`). Without absorption the
  fraction of `9999.99999` is a standalone 5+-digit run in its own right and gets
  rewritten into a number the output never contained. The `(?!\.\d)` arm is what
  makes absorption stick: a plain `(?:\.\d+)?` lets the engine backtrack out of
  the fraction and re-match the integer alone whenever the text after it fails the
  trailing guard.
- **Identifier guards over this template's OWN render alphabet.** The guards are
  single-character assertions over `[A-Za-z0-9_\-/:#]` (the leading guard also
  carries `.`, so no alternative can enter a number part-way through a fraction;
  `.` is deliberately absent from the trailing guard, or an amount ending a
  sentence would escape the grid). The class is read off the renderer — `/` and
  `:` because a reference is `smaregi://stores/<id>/…`, `#` because every bare
  identifier renders behind one, `_` because caller identifiers may contain it.
- **`ABC-1234` is an identifier, `JPY-9999` is a negative amount.** The two are
  lexically identical, so no amount of guard-widening separates them; something
  has to know which three-letter words are currencies. The gate consults the ISO
  4217 alphabetic codes for that ONE decision. Everywhere else it still treats any
  standalone three-letter uppercase word as a marker, because there a false snap
  fails safe — here it does not, since this agent renders caller-supplied ids of
  exactly that shape and rewriting one names a different record.

## Deterministic classification and extraction

Intent classification (`ClassifyIntentNode`) uses a keyword heuristic and field
inference (`InferSmaregiFieldsNode`) uses regex / line-structure extraction, so
the template runs and is testable without a language model. **No model client is
constructed anywhere** and no system prompt is read — there is no dead config.

## Transport

`src/services/smaregi_client.py` is a thin, framework-free wrapper around the
Smaregi POS REST API transaction endpoints (injectable transport,
`SmaregiApiError`, per-call token). Its DEFAULT transport is a deterministic,
**network-free** stub returning the documented response shapes, so the pipeline is
runnable and testable before a live tenant exists; it does not perform a live
call. Going live is a transport injection at construction — the method contracts
and parameter shapes follow the Smaregi POS REST API, so no business logic
changes.

Where the caller already holds the POS record, it can supply it on the structured
channel and the pipeline reports on THOSE figures — real data, validated and
bounded — instead of the built-in baseline. The tool surface is **read-only by
design** (GET only — no create/update/void of transactions).

## Framework Utilization

- [x] `InvocationContext` — read in `CallSmaregiApiNode` via `InvocationContext.from_state(state)` (secrets + trust)
- [x] Trust gate — single external gate on `PreProcessNode`; inner domain nodes declare `TrustLevel.ANONYMOUS`
- [x] Secrets — `ctx.secrets.get("SMAREGI_TOKEN")`; entry-point `bound_secrets` / `secrets_factory` / `provision_secrets` in `src/api/server.py`
- [x] `emit_trace_event()` — domain events per node; framework lifecycle events not re-emitted

### Composition Pattern

- **Pattern**: GraphNode (subgraph) — outer/inner split.
- **Composition target**: inner `SmaregiWorkflowGraph` via `SmaregiWorkflowGraphNode.get_subgraph()`.
- **Config forwarding**: `_parent_config()` reads `config/config.yaml` and forwards `{smaregi, agent}` under `config["configurable"]`.
- **Error propagation**: `propagate` — inner errors re-raised as `SubgraphError`; per-step `status=error` + `error_log` for API/validation failures (no silent pass).

### Conditional routing

`route()` on the inner graph is annotated with **this graph's own `State`**, not
the generic base state. The graph runtime reads a path callable's annotation as
its input schema and projects away every field the annotation does not declare —
a route annotated with the base state is handed a state whose domain fields are
always absent, so the branch that depends on them never runs, while unit tests
calling `route()` directly keep passing because they build a full dict
themselves. The backbone's own conditional edge uses the inherited `route`, whose
parameter carries no annotation and therefore receives the full state.

## Import Isolation

- [x] The template imports `framework/` and `shared/` only; no platform-internal import anywhere
- [x] `src/services/smaregi_client.py` and `src/services/security.py` have no framework imports (pure stdlib)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed multi-step pipeline, not an autonomous loop |
| Composition pattern | flat (single main node) | GraphNode + inner subgraph | GraphNode + inner subgraph | 5 domain steps live in the inner graph |
| Model dependency | model client in the pipeline | deterministic classification/extraction | deterministic | the template runs and tests without a model; no dead prompt/config reads |
| Transport | live HTTP call | injectable transport + network-free default | injectable + network-free default | never fake a live call; state the limitation and make going live a one-line injection |
| Caller data channel | inside the `validated_input` JSON | the caller-context bridge | the bridge | the framework masks `validated_input` at every node boundary, so caller values can be rewritten between hops |
| Node configuration | ctor-arg injection | no-arg nodes + config forwarding via `_parent_config()` | no-arg nodes | nodes are no-arg (ctor args raise at graph build); the config files stay the single source |
| Tool surface | read + write POS operations | read-only lookups/aggregations | read-only | POS transactions are financial records — a wrong write is unrecoverable, a wrong read is not |
| Target resolution | infer store/transaction ids freely | caller-supplied/explicit ids only | explicit only | never query the wrong store/transaction; unresolved -> status=error, not invented |
| Default intent | summarize_daily_sales | lookup_transaction | lookup_transaction | low confidence defaults to the narrowest single-record read, which then surfaces a clean error rather than a broad aggregation nobody asked for |
| Receipt total in the response | render the exact amount | render aggregates only | aggregates only | an individual receipt total is customer-attributable line-item data; rounding it to the nearest 1,000 would be neither the true figure nor useful, so it is not reported at all |
| Precision grid scope | the whole output | the narrative fields | narrative fields | the grammar's guards protect a value from its neighbours and cannot protect an identifier that is the whole string; every other field is an inert identifier by construction |
