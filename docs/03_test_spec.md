# Test Specification - CMN-C2-285 Smaregi POS Transaction Agent

## Test Strategy

- Test types: Unit (per node + service + config + inner graph) / Proof-of-Boundary
  (full outer-graph invoke, end-to-end invoke through the ASGI entry point, import
  isolation, state safety, server boot, HITL stub).
- Location: `tests/unit/`, `tests/proof_of_boundary/` (`tests/integration/` is an
  empty package; end-to-end coverage lives in the boundary suite, which drives the
  real compiled graph).
- The Smaregi call is exercised through the deterministic, network-free default
  transport, through caller-supplied POS records, and through monkeypatched fake
  clients; no live Smaregi call is ever made.
- **Trust-gate routing canon**: every per-node unit test invokes the node as
  `node(state)` - `BaseNode.__call__` routes the full security pipeline (trust gate
  -> input gate -> `execute()` -> output gate) - never bare `node.execute(state)`.
  State builders set `caller_trust_level = TrustLevel.VERIFIED_EXTERNAL.value` for
  PreProcessNode (the single external gate) and `TrustLevel.ANONYMOUS.value` for
  every other node. The trust-rejection test asserts on the RETURNED error dict
  (`status == AgentStatus.ERROR.value`, "trust gate denied" in `error_log`,
  execute-only keys absent) - `__call__` never raises for a trust denial.
- **Where a test calls `execute()` directly, and why.** Three cases, all
  deliberate: (a) the screening tests, because a guarantee that only holds when
  the framework gate happens to be present and configured on is not the
  template's guarantee - removing the wrapper is the point; (b) the output-gate
  error-envelope test, because `__call__` short-circuits an incoming errored
  state before `execute()` runs; (c) the `execute(state, config=...)` override
  test, because `__call__` cannot forward a second argument.
- Assertion contract: the invoke surface is `result["output"]` / `status` /
  `trace_id` / `correlation_id` / `node_history` (never `formatted_output` at the
  invoke surface); status is compared to `AgentStatus.SUCCESS`/`.value` (lowercase
  `success`/`error`); the outer graph is called as `invoke(user_input=..., ctx=...,
  input_context=...)`; identifiers may be masked (`[MASKED]`) so record evidence is
  asserted by presence, not by raw repr; audit spies assert on `call.args[1]` (the
  event payload), never the whole-call repr.
- Framework behaviours the suite encodes: `__call__` short-circuits on an incoming
  errored state (`execute()` is skipped; error status/error_log pass through); the
  framework PII mask rewrites Title-Case bigrams (across newlines), emails and
  digit groups in `user_input`/`validated_input` to `[MASKED]` before `execute()`
  sees the text - positive payloads are PII-free (short ids like `t-1001` / `s-01`
  / `p-123`), intentional-PII tests assert the `[MASKED]` path. **That masking is
  also why the caller contract does not ride inside `validated_input`**: caller
  values could be rewritten between hops, so they travel on the caller-context
  bridge instead.
- Domain audit events are muted per module via an autouse fixture patching
  `src.nodes.<mod>.emit_trace_event` (never a `sys.modules` stub of `shared.*`).

## Unit Tests (`tests/unit/`)

| TC-ID | Test file | Focus | Expected |
|-------|-----------|-------|----------|
| U-01 | test_trust_gate.py | trust boundary: ANONYMOUS caller on the VERIFIED_EXTERNAL pre_process gate; every inner node declares ANONYMOUS | denial RETURNS an error dict ("trust gate denied" in error_log, execute-only keys absent); VERIFIED_EXTERNAL passes; trust-posture declarations asserted per class |
| U-02 | test_pre_process_node.py | the caller-data contract: request serialization + store-hint priority (store_id > store_hint > store_code); markup strip; the instruction-override screen on BOTH channels; per-field type/shape/length/range bounds; the non-finite matrix per numeric field | hint resolved by priority; `<script>` stripped; control tokens / directive phrases refused while ordinary POS prose passes; NaN / ±Infinity / over-magnitude / fractional counts refused fail-CLOSED; refusals name the field and never echo the value; unrecognised field NAMES masked |
| U-03 | test_validate_input_node.py | empty/short guard; JSON-shaped input; framework `[MASKED]` path for emails; node-level token flag-and-redact | email -> `[MASKED]` before execute; token -> `[REDACTED]` + `redaction_flags=["token"]` (JSON string); empty/short -> error; the audit payload carries flags only |
| U-04 | test_classify_intent_node.py | intent = lookup_transaction / summarize_daily_sales / check_product_sales (keyword incl. JA, most-specific-first priority product > daily > lookup, read-only default) | correct intent per keyword; no-signal defaults to lookup_transaction with a non-fatal note; empty -> error; audit emits the intent label only |
| U-05 | test_infer_smaregi_fields_node.py | store/transaction/product/date resolution (text > id-shaped hint > `Key: value` lines; ids must contain a digit, so "store transactions" never false-matches; never invented); request parameters per intent (JSON string) | lookup `{store_id, transaction_id}`; daily `{store_id, sum_date}` (no date -> `""` = latest); product `{store_id, product_code, sum_date}`; unresolved ids left `""`; empty input -> error; audit emits presence signals only |
| U-06 | test_call_smaregi_api_node.py | lookup/daily/product through the network-free transport (incl. latest-date default + all-stores product scope); `smaregi_config` state field + `execute(state, config=...)` override; API error / unresolved ids / unknown intent / missing payload; credential posture (live transport refuses to run unauthenticated; token read via `ctx.secrets`, never env/state) | record_id/record_ref per intent; 403 surfaces in error_log; live+no-secret -> error "unauthenticated"; live+bound secret -> token passed to the client; a receipt's exact total is never rendered; audit emits presence signals |
| U-07 | test_confirm_node.py | human-readable confirmation per intent verb; reference formatting; label fallback | "Retrieved POS transaction / Summarized daily sales / Checked product sales count ... ref=..."; every identifier rendered behind `#` or inside a `smaregi://` reference, never bare; missing evidence -> error |
| U-08 | test_post_process_node.py | `formatted_output` shaping (payload round-trip); output gate (record evidence, credential scan walking NESTED structures, mapping keys included, a credential-shaped key withheld from the label); containment on violation; **closed-set error envelope** (parameterised over every non-success path — inner error, inner error with the answer still in `result`, inner error with a credential in `error_log`, missing record evidence, credential in a top-level value, nested in the payload, credential-shaped mapping key, post-grid re-scan: every envelope value is one of the module's `ERROR_REASONS`; `error_log` seeded with a sentinel upstream line that must appear nowhere in the returned mapping, keys and values walked; inner entries not re-emitted; gate violations travel in `error_log` only; audit events carry a count and no text); the precision grid and its grammar, both directions | success shape with parsed request parameters and the schema note; SUCCESS without record evidence blocked; credentials found at any depth, location named and value never echoed; a violation CLEARS every output-bearing field; the error envelope is `{"reason": <constant>}` only; off-grid aggregates snap, identifiers/counts/dates/decimals byte-identical |
| U-09 | test_smaregi_client.py | Smaregi POS REST API client: get_transaction / get_daily_sales / get_product_sales; `Authorization: Bearer` header; `SmaregiApiError` on non-2xx (`errors` join + `message` fallback); default-transport shapes; `uses_stub_transport` | correct URLs/headers/params; 404/500 raise; deterministic shapes; latest-date default |
| U-10 | test_config.py | `config/agent.yaml` manifest + `config/config.yaml` runtime parameters | manifest is FLAT (no `agent:` block), id/name/namespace/category/industry/base_type/generation_mode, dotted entry-point class, VERIFIED_EXTERNAL, `requires.secrets == []` with the reason; runtime file carries max_retry / timeout_s / smaregi.base_url |
| U-11 | test_domain_workflow_graph.py | inner `SmaregiWorkflowGraph`: identity, `_extra_initial_state()` config injection + bridge seeding, `route()` error short-circuit, `get_output` contract, compile, direct inner invoke | name/state_schema correct; config forwarded as a JSON string; the bridge key is always seeded; error -> END; inner invoke runs validate -> classify -> infer -> call -> confirm to SUCCESS with record evidence |
| U-12 | test_security_service.py | the template-owned screens: instruction-override detection (control tokens as a class, directive phrases, spliced markup) and its false-positive direction; markup strip; flag-and-redact | attacks detected in both raw and stripped form; this template's own corpus sentences pass; length capped; emails/token shapes flagged and redacted, ordinary text untouched |
| U-13 | test_context_bridge.py | the caller-context hand-off across the graph boundary | unset reads as `{}`; values round-trip; the stash is copied, not aliased; concurrent tasks never see each other's contract |
| U-14 | test_framework_compliance_tc06_tc07.py | the framework's final gate methods | TC-06 / TC-07: overriding the default input/output gate raises at class-definition time |

## Proof-of-Boundary Tests (`tests/proof_of_boundary/`)

| PB-ID | Boundary | Test | Expected |
|-------|----------|------|----------|
| PB-4 | Import isolation | test_import_isolation.py | AST scan of `src/`: no platform-internal imports |
| PB-2/PB-5 | State serialization | test_state_safety.py | `state.py`: no Pydantic, no credential fields |
| PB-6 | Backbone invoke-order + external trust | test_pb_invoke_order.py | `_VALID_PAYLOAD` byte-equal to `deploy/invoke_payload.json` "input" (asserted); VERIFIED_EXTERNAL caller yields `status=success` with `node_history == [InitializeNode, PreProcessNode, SmaregiWorkflowGraphNode, PostProcessNode, FinalizeNode]` and record evidence + confirmation in `result["output"]`; ANONYMOUS caller denied at pre_process (error, no post_process, no output); blank input -> error, not crash |
| PB-6E | The PUBLIC path, end to end through the ASGI entry point | test_pb_invoke_e2e.py | Bearer auth enforced (401 generic body, oversized structured channel 413, credential-shaped value on the structured channel 422 with the field named and unrecognised names masked, plus the clean control); a caller-supplied aggregate REACHES the inner graph and is reported on the grid; counts survive verbatim; purely numeric POS identifiers survive the output boundary; refusals (under-trusted caller, invalid caller field, instruction override, blank input) return an error with NO output; a blocked response ships the truthy withheld notice and no released text, no record evidence, no traceback and no source paths |
| PB-6C | Error-path containment (output boundary) | test_error_envelope_no_record_evidence.py | The existing-ERROR branch of `post_process` returns a **truthy** record-free envelope (`reason` only) — a falsy value would re-open the framework's `formatted_output or result` projection — carrying no `record_id`, `record_ref`, `smaregi://` reference, store/product id or rendered label; the delta CLEARS every output-bearing field (`result`, `confirmation`, `record_label`, `smaregi_payload`, `intent`, `record_id`, `record_ref`, `store_id`, `transaction_id`, `product_code`); the error reasons `call_smaregi_api` writes carry closed-set labels only (HTTP status, exception type — never the upstream body or the request URL); a success-path control proves the containment did not empty the clean path |
| PB (containment) | Caller-facing envelope through `get_output()` and the ASGI `/invoke` | test_output_envelope_containment.py | post_process driven through its real call path (execute() for an errored state — `__call__` short-circuits on it and the backbone routes an error to finalize), partial state merged the way the reducer merges it, then projected through `SmaregiPOSTransactionAgent.get_output()`: on every non-success path (credential nested in the payload, credential in a top-level value, missing record evidence, inner workflow error with the answer still in `result` and a sentinel upstream line in `error_log`) `output == {"reason": <one of ERROR_REASONS>}` and stays truthy; no refused text, record evidence, sentinel fragment or gate violation appears anywhere in the envelope, keys and values walked; the violation label sits in `error_log` only; the inner entry is present once (not re-emitted); the same properties through the real ASGI `POST /invoke` with the sentinel seeded by an inner node and a violation forced at the boundary, and on a live inner error (no output, no `error_log` key, sentinel nowhere in the body); clean-path control ships the answer with no reason code |
| PB-7 | HITL interrupt propagation *(conditional)* | test_pb7_hitl_interrupt_propagation.py | **Auto-waived - non-HITL** (no `hitl.enabled: true`): module-level skipif; the stub bodies are real AssertionErrors, so enabling HITL without implementing PB-7 fails loudly |
| PB (boot) | Server entry point | test_server_boot.py | importing `src.api.server` does not raise (construct + compile + provision_secrets at import); the agent constructs + compiles via the supported path; the `Graph` alias stays bound to `SmaregiPOSTransactionAgent`; `/invoke` + `/health` routes exposed |

> PB-1 (audit emission) is covered inside the unit suite via the emit-spy tests
> (validate / classify / infer / call / post_process assert on the event payload,
> `call.args[1]`). PB-3 (a live external service) is not exercised in this suite -
> the default transport is network-free by design.

## Test Execution Summary

- Runner: `python -m pytest tests/ -v` against the published `agenticstar-agentcore`
  wheel (the version the pipeline installs).
- Total tests: 297
- Pass: 295 / Fail: 0 / Skip: 2 (PB-7 A/B - auto-waived, non-HITL)
