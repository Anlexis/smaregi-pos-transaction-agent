# CMN-C2-285 - Unit tests: manifest + runtime-config sanity.
#
# Two files, two jobs: config/agent.yaml is the STATIC manifest the registry
# reads at ROOT level (no `agent:` block), config/config.yaml carries the
# RUNTIME parameters passed to the graph as `config=`. A value declared in the
# wrong one is silently dead, so both are asserted here.

import pathlib

import pytest

try:
    import yaml  # pyyaml (transitive dep of the framework wheel)

    _YAML_ERROR = None
except Exception as exc:  # pragma: no cover
    yaml = None
    _YAML_ERROR = exc

_MANIFEST_PATH = pathlib.Path(__file__).parents[2] / "config" / "agent.yaml"
_RUNTIME_PATH = pathlib.Path(__file__).parents[2] / "config" / "config.yaml"

pytestmark = pytest.mark.skipif(_YAML_ERROR is not None, reason=f"pyyaml unavailable: {_YAML_ERROR}")


def _manifest():
    return yaml.safe_load(_MANIFEST_PATH.read_text())


def _runtime():
    return yaml.safe_load(_RUNTIME_PATH.read_text())


def test_manifest_identity():
    data = _manifest()
    assert data["id"] == "CMN-C2-285"
    assert data["name"] == "SmaregiPOSTransactionAgent"
    assert data["namespace"] == "cmn"
    assert data["category"] == "Cat 2"
    assert data["industry"] == "CMN"
    assert data["base_type"] == "ToolCallingAgent"
    assert data["enabled"] is True
    assert data["generation_mode"] == "deterministic"


def test_manifest_is_flat():
    """Every key sits at ROOT level - a nested `agent:` block is never read."""
    assert "agent" not in _manifest()


def test_manifest_entry_point():
    assert _manifest()["class"] == "src.graph.graph.SmaregiPOSTransactionAgent"


def test_manifest_security():
    data = _manifest()
    # Agent-level entry trust, enforced by the outer backbone pre_process gate
    # (VERIFIED_EXTERNAL); inner domain nodes stay ANONYMOUS.
    assert data["required_trust_level"] == "VERIFIED_EXTERNAL"
    # The Smaregi token is read OPTIONALLY (ctx.secrets.get) because the default
    # network-free transport needs no credential; declaring it as required would
    # make the agent fail to compile wherever it is not provisioned.
    assert data["requires"]["secrets"] == []
    assert data["requires"]["extras"] == []


def test_runtime_config_carries_the_integration_section():
    runtime = _runtime()
    # Forwarded to the inner graph by SmaregiWorkflowGraphNode._parent_config().
    assert runtime["smaregi"]["base_url"] == "https://api.smaregi.jp/pos"


def test_runtime_config_backbone_values():
    runtime = _runtime()
    assert isinstance(runtime["max_retry"], int)
    assert isinstance(runtime["timeout_s"], int)
