"""Input screening helpers for the Smaregi POS transaction agent.

Pure, stateless domain helpers (NOT framework gate methods), shared by the
outer backbone pre_process and the inner validate step so that BOTH caller
channels get the same treatment.

That sharing is the point. The framework's own input scan covers the request
text but not the structured caller channel, so a screen wired only into the
request path would leave caller-supplied fields unscreened.
"""

from __future__ import annotations

import re

# Markup / control sequences that may ride in a pasted POS request. The
# angle-bracket strip covers HTML-ish markup AND the chat-template control
# tokens that share its shape (``<|im_start|>``) - which is exactly why
# ``contains_instruction_override`` screens the text BEFORE this strip runs.
_MARKUP_CONTROL_RE = re.compile(r"<[^>]{1,500}>")

# Email addresses and bearer/JWT/API-token-like strings that might appear in a
# pasted request. These are flagged and redacted before anything is logged or
# forwarded.
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_TOKEN_RE = re.compile(r"\b(?:eyJ[A-Za-z0-9_-]{6,}|secret_[A-Za-z0-9]{6,}|sk-[A-Za-z0-9]{6,})\b")
_REDACTION = "[REDACTED]"

DEFAULT_MAX_LENGTH = 4000

# ── Template-owned instruction-override screen ────────────────────────────────
# A POS request is ordinary retail prose: "show the previous receipt", "ignore
# the voided line", "check the transaction the manager flagged". A substring
# screen on directive verbs would refuse real work, so every alternative here is
# anchored on a full directive PHRASE aimed at the MODEL (an override verb plus
# an instruction noun, or a model role), or on a chat-template control token,
# which has no legitimate reading in a POS request.
#
# This screen is the template's own guarantee, not the platform's: the
# framework input gate covers only the request text (never the structured
# caller channel) and rejects only high-confidence findings, so wherever it is
# absent or configured off the template would fail OPEN. It is therefore
# enforced inside the node's own execute() path.
_INSTRUCTION_OVERRIDE_RE = re.compile(
    # Chat-template control tokens: <|im_start|>, <|system|>, [INST], <<SYS>>.
    # Screened as a CLASS, not as a list of known token names.
    r"<\|[a-z_]{2,32}\|>"
    r"|\[/?INST\]"
    r"|<</?SYS>>"
    r"|<\s*/?(?:system|assistant)\s*>"
    # "ignore/disregard/forget ... previous/... instructions/prompt/rules".
    # The noun class is instruction-nouns ONLY - never "receipt"/"line"/
    # "transaction" - so ordinary retail prose passes.
    r"|\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+|the\s+|your\s+|my\s+)*"
    r"(?:previous|prior|above|earlier|preceding|original|system)\s+"
    r"(?:instruction|instructions|prompt|prompts|rule|rules|direction|directions)\b"
    # bare "ignore all rules/instructions" (no temporal qualifier)
    r"|\b(?:ignore|disregard)\s+all\s+(?:rules|instructions)\b"
    # exfiltration: "reveal/print your system prompt / the hidden instructions"
    r"|\b(?:reveal|show|print|repeat|output|disclose|display|dump)\s+(?:me\s+)?"
    r"(?:your\s+(?:system\s+|initial\s+|hidden\s+)?(?:prompt|prompts|instructions)"
    r"|the\s+(?:system|initial|hidden)\s+(?:prompt|prompts|instructions|message))\b"
    # role reassignment: requires a MODEL role, so "you are now the store
    # manager" (a person's role) passes.
    r"|\byou\s+are\s+now\s+(?:a\s+|an\s+)?(?:different\s+|unrestricted\s+|new\s+|jailbroken\s+)?"
    r"(?:assistant|ai|chatbot|language\s+model|llm|dan)\b"
    # privileged-mode role-play: developer/admin/root + "mode". "acting as the
    # store manager" never matches - "acting" is not the whole word "act".
    r"|\bact\s+as\s+(?:if\s+you\s+(?:are|were)\s+)?(?:a\s+|an\s+)?"
    r"(?:developer|admin|administrator|root|jailbroken|unrestricted)\s+mode\b"
    # rule-override: "override your/the instructions/safety/guardrails" -
    # "override approved by the manager" has no instruction noun and passes.
    r"|\boverride\s+(?:your|the)\s+"
    r"(?:instruction|instructions|rule|rules|safety|guardrail|guardrails|restriction|restrictions)\b"
    # "new system prompt:" header form
    r"|\b(?:new|updated)\s+system\s+(?:prompt|instructions)\s*[:=]",
    re.IGNORECASE,
)


def contains_instruction_override(text: str) -> bool:
    """True when the text carries an instruction-override directive.

    Screens the text BOTH as received and after the markup strip. Both passes
    are needed and neither is redundant:

    * the RAW pass catches chat-template control tokens - ``<|im_start|>``
      matches the markup pattern, so ``sanitize_query`` would delete it and
      forward the bare directive that followed it as ordinary text, turning a
      detectable token attack into an undetectable one;
    * the STRIPPED pass catches a directive spliced with markup
      (``ig<b>nore all rules``) that only re-assembles into a phrase once the
      markup is gone.
    """
    if _INSTRUCTION_OVERRIDE_RE.search(text):
        return True
    stripped = _MARKUP_CONTROL_RE.sub("", text)
    return stripped != text and bool(_INSTRUCTION_OVERRIDE_RE.search(stripped))


# Credential-shaped strings. The framework scans every value of every node
# result for these, and the FIRST node of the backbone returns the request's
# structured channel verbatim in its own result - so a credential anywhere in
# that channel fails the run at node 1, before any template code executes, with
# an error the caller cannot act on. The entry point therefore screens the
# channel itself and refuses with a message naming the field: the request
# cannot succeed either way, and a clear refusal beats an opaque failure.
_CREDENTIAL_LIKE_RE = re.compile(
    r"eyJ[A-Za-z0-9._-]{10,}"
    r"|sk-[A-Za-z0-9]{20,}"
    r"|secret_[A-Za-z0-9]{10,}"
    r"|Bearer\s+[A-Za-z0-9._-]{16,}"
    r"|\bAKIA[A-Z0-9]{16}\b",
    re.IGNORECASE,
)


def find_credential_like(
    value: object, path: str = "input_context", safe_names: "frozenset[str] | None" = None
) -> "str | None":
    """Return the path of the first credential-shaped string, or None.

    Walks mappings and sequences depth-first on the PARSED value, so escaping
    cannot smuggle one past it. Path components outside ``safe_names`` are
    masked, so the returned path is always safe to put in an error message -
    and the matched VALUE is never part of it.
    """
    if isinstance(value, str):
        return path if _CREDENTIAL_LIKE_RE.search(value) else None
    if isinstance(value, dict):
        for key, item in value.items():
            known = isinstance(key, str) and (safe_names is None or key in safe_names)
            found = find_credential_like(item, f"{path}.{key if known else '<field>'}", safe_names)
            if found is not None:
                return found
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = find_credential_like(item, f"{path}[{index}]", safe_names)
            if found is not None:
                return found
    return None


def sanitize_query(query: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Strip markup/control sequences (injection guard) and cap length."""
    cleaned = _MARKUP_CONTROL_RE.sub("", query)
    return cleaned[:max_length]


def redact_sensitive(text: str) -> "tuple[str, list[str]]":
    """Flag-and-redact emails / token-like strings. Returns (redacted, flags).

    Flag-and-redact rather than reject: a POS request legitimately names
    stores, products and receipt ids, so the hard rejections in this template
    are the empty / non-request guard, the caller-contract bounds, and the
    instruction-override screen above. The flags name the CATEGORY found,
    never the value, so they are safe to log.
    """
    flags: list[str] = []
    redacted = text
    if _EMAIL_RE.search(redacted):
        flags.append("email")
        redacted = _EMAIL_RE.sub(_REDACTION, redacted)
    if _TOKEN_RE.search(redacted):
        flags.append("token")
        redacted = _TOKEN_RE.sub(_REDACTION, redacted)
    return redacted, flags
