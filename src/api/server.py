"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, AgentGateway calls agent.invoke() directly.

import json
import os
import secrets
from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.graph.graph import Graph, load_runtime_config
from src.nodes.pre_process_node import KNOWN_CONTEXT_FIELDS
from src.services.security import find_credential_like

# Serialized size cap for the structured caller channel — enforced at the
# adapter so an oversized payload never reaches the graph.
_MAX_INPUT_CONTEXT_BYTES = 256 * 1024

app = FastAPI(title="Agent")

# The runtime parameters (max_retry, timeout_s, the Smaregi settings) live in
# config/config.yaml. The platform registry loads that file and constructs the
# agent with it; constructing the graph without it here would leave every
# declared value silently absent and run the agent on undeclared defaults.
agent = Graph(config=load_runtime_config())
agent.compile()
# namespace and agent_name match the manifest values in config/agent.yaml
agent.provision_secrets(secrets_factory(namespace="cmn", agent_name="SmaregiPOSTransactionAgent"))


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Structured caller data (store target, and any POS record the caller
    # already holds) — validated field by field in PreProcessNode; the adapter
    # only enforces the size cap.
    input_context: "dict[str, Any]" = Field(default_factory=dict)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> "dict[str, Any]":
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone caller auth: when INVOKE_AUTH_TOKEN is set on the server
    # environment, callers that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a Bearer token and run at
    # VERIFIED_EXTERNAL. Middleware-established trust is never demoted.
    # This adapter is the entry-point auth boundary (the standalone equivalent
    # of the platform auth middleware) — a deployment-level caller credential,
    # not an agent secret, so ctx.secrets does not apply (no InvocationContext
    # exists before auth).
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL

    # Size cap on the structured channel, measured on the serialized form —
    # the graph never sees a payload larger than the documented limit.
    if len(json.dumps(req.input_context, default=str).encode("utf-8")) > _MAX_INPUT_CONTEXT_BYTES:
        raise HTTPException(status_code=413, detail="input_context exceeds the size limit.")

    # Credential screen on the structured channel. The framework scans every
    # value of every node result for credential patterns, and the backbone's
    # first node returns this channel verbatim in its own result — so a
    # credential anywhere in it fails the run at node one, before any template
    # code executes, and the caller gets an error it cannot act on. Refuse here
    # instead, naming the field (never the value; an unrecognised field name is
    # masked too). Every field this agent declares is an inert identifier, so a
    # credential in the channel is always a caller mistake.
    credential_at = find_credential_like(req.input_context, safe_names=KNOWN_CONTEXT_FIELDS)
    if credential_at is not None:
        raise HTTPException(
            status_code=422,
            detail=f"Credential-shaped value in {credential_at}; remove it and retry.",
        )

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return cast("dict[str, Any]", agent.invoke(req.input, ctx=ctx, input_context=req.input_context))


@app.get("/health")
def health() -> "dict[str, str]":
    return {"status": "ok", "agent": "SmaregiPOSTransactionAgent"}
