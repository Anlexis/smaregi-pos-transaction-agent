# PB (server boot): importing src.api.server must not raise (CMN-C2-285).
#
# The standalone entry point constructs the agent, compiles the graph, and
# provisions the secret provider AT IMPORT TIME - a boot regression (bad
# import, ctor arg on a no-arg node, manifest drift) therefore surfaces here
# before any deployment runs. The agent is also constructed + compiled via
# the supported path (compile(), never private internals) independent of the
# FastAPI import so the graph-build contract is asserted on its own.
# server.py imports `Graph` (the module-level alias `Graph =
# SmaregiPOSTransactionAgent` in src/graph/graph.py) - the alias is asserted
# here so a rename can never break the entry point silently.

import importlib

import pytest

try:
    import fastapi  # noqa: F401 - dependency of the real framework wheel

    _FASTAPI_ERROR = None
except Exception as exc:  # pragma: no cover - only in stripped-down stub envs
    _FASTAPI_ERROR = exc


class TestServerBoot:
    def test_agent_constructs_and_compiles(self):
        """Supported construction path: no-arg agent + compile()."""
        from src.graph.graph import SmaregiPOSTransactionAgent

        agent = SmaregiPOSTransactionAgent()
        agent.compile()
        assert agent._compiled is not None
        assert agent.name == "cmn_c2_285"

    def test_graph_alias_points_at_agent_class(self):
        """src/api/server.py imports `Graph` from src.graph.graph - the alias
        must stay bound to SmaregiPOSTransactionAgent."""
        from src.graph.graph import Graph, SmaregiPOSTransactionAgent

        assert Graph is SmaregiPOSTransactionAgent

    @pytest.mark.skipif(_FASTAPI_ERROR is not None, reason=f"fastapi unavailable: {_FASTAPI_ERROR}")
    def test_server_module_imports_and_boots(self):
        """Importing src.api.server performs the full boot (construct + compile
        + provision_secrets) and must not raise."""
        server = importlib.import_module("src.api.server")
        assert server.app is not None
        assert server.agent._compiled is not None

    @pytest.mark.skipif(_FASTAPI_ERROR is not None, reason=f"fastapi unavailable: {_FASTAPI_ERROR}")
    def test_server_exposes_invoke_and_health_routes(self):
        server = importlib.import_module("src.api.server")
        paths = {route.path for route in server.app.routes}
        assert "/invoke" in paths
        assert "/health" in paths

    @pytest.mark.skipif(_FASTAPI_ERROR is not None, reason=f"fastapi unavailable: {_FASTAPI_ERROR}")
    def test_health_endpoint_reports_agent(self):
        server = importlib.import_module("src.api.server")
        assert server.health() == {"status": "ok", "agent": "SmaregiPOSTransactionAgent"}
