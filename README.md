# CMN-C2-285 — Smaregi POS Transaction Agent

> **Category**: Cat 2 (a fixed multi-step pipeline for one job-to-be-done)
> **Industry**: CMN (industry-agnostic)

## Overview

Answers a plain-language question about a point-of-sale system's transaction records.

Give it a sentence like *"look up transaction t-1001 in store s-01"* or *"summarise yesterday's
sales for store s-01"* and it works out which of three read operations you meant, pulls the store,
receipt or product identifier and the date out of the wording, calls the matching Smaregi POS REST
API endpoint, and hands back a confirmation carrying the record reference.

Three operations are supported, all of them reads: looking up one transaction, summarising a
store's daily sales, and checking how many units of a product sold. The tool surface is read-only
by design — POS transactions are financial records, and a wrong write is unrecoverable where a
wrong read is not. The agent never invents an identifier: an unresolved store or receipt id is
reported as an error rather than guessed at.

Intent classification and field extraction are deterministic (keyword and pattern based), so the
pipeline runs and is testable without a language model. Out of the box it ships with a
network-free transport that returns the documented Smaregi response shapes, which makes the
template runnable end to end before you connect a real tenant; a caller may also pass POS records
it already holds on the request's structured channel, and the pipeline reports on those. Injecting
a live HTTP transport and providing an access token is all that is needed to go live.

Everything the agent reports passes an output boundary that withholds the response if a
credential-shaped string appears anywhere in it, and that rounds every monetary figure to the
nearest 1,000 — the agent reports aggregates, never an individual receipt's exact amount.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. The agent imports its base classes from the framework package at start-up, so without that
package installed and configured, import and graph compile fail outright rather than leaving the
agent running in a partially working state. This is intentional — a half-running agent is worse
than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit and boundary tests
config/       agent manifest (agent.yaml) and runtime parameters (config.yaml)
docs/         design specification and test specification
```

`docs/02_design.md` describes the graph, the state contract and the security design;
`docs/03_test_spec.md` maps every test to the behaviour it pins.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Point `config/config.yaml` at your own POS tenant and inject a live transport in `src/services/smaregi_client.py`.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
