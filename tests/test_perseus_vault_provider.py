import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from agent.memory_provider import MemoryProvider
except ModuleNotFoundError:
    agent_module = types.ModuleType("agent")
    memory_provider_module = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        pass

    memory_provider_module.MemoryProvider = MemoryProvider
    sys.modules["agent"] = agent_module
    sys.modules["agent.memory_provider"] = memory_provider_module

from hermes_mimir import MimirProvider, PerseusVaultProvider


class PerseusVaultProviderTests(unittest.TestCase):
    def test_uses_perseus_vault_as_canonical_provider_name(self):
        self.assertEqual(MimirProvider().name, "perseus-vault")

    def test_prefers_canonical_tool_aliases_over_legacy_names(self):
        provider = MimirProvider()
        schemas = provider._normalize_schemas(
            [
                {"name": "mimir_remember", "description": "legacy", "inputSchema": {"type": "object"}},
                {"name": "perseus_vault_remember", "description": "canonical", "inputSchema": {"type": "object"}},
                {"name": "mimir_recall", "description": "legacy", "inputSchema": {"type": "object"}},
            ]
        )

        self.assertEqual(
            [schema["name"] for schema in schemas],
            ["perseus_vault_remember", "perseus_vault_recall"],
        )
        self.assertEqual(schemas[0]["description"], "canonical")

    def test_accepts_canonical_only_tools(self):
        provider = MimirProvider()
        schemas = provider._normalize_schemas(
            [{"name": "perseus_vault_recall", "description": "canonical", "inputSchema": {"type": "object"}}]
        )

        self.assertEqual([schema["name"] for schema in schemas], ["perseus_vault_recall"])
        self.assertEqual(provider._tool_name_aliases, {"perseus_vault_recall": "perseus_vault_recall"})

    def test_routes_canonical_tool_calls_to_legacy_binary_aliases(self):
        class Client:
            def __init__(self):
                self.calls = []

            def is_running(self):
                return True

            def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                return "ok"

        provider = MimirProvider()
        client = Client()
        provider._client = client
        provider._tool_name_aliases = {"perseus_vault_recall": "mimir_recall"}

        self.assertEqual(provider.handle_tool_call("perseus_vault_recall", {"query": "test"}), "ok")
        self.assertEqual(client.calls, [("mimir_recall", {"query": "test"})])

    def test_uses_legacy_alias_when_discovery_has_not_populated_aliases(self):
        class Client:
            def __init__(self):
                self.calls = []

            def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                return "ok"

        provider = MimirProvider()
        client = Client()
        provider._client = client

        self.assertEqual(provider._call_vault_tool("perseus_vault_remember", {"key": "x"}), "ok")
        self.assertEqual(client.calls, [("mimir_remember", {"key": "x"})])

    def test_does_not_inject_failed_recall_as_context(self):
        class Client:
            def is_running(self):
                return True

            def call_tool(self, name, arguments):
                return '{"error":"unavailable"}'

        provider = MimirProvider()
        provider._client = Client()
        provider._tool_name_aliases = {"perseus_vault_recall": "perseus_vault_recall"}

        self.assertEqual(provider.prefetch("test"), "")

    def test_uses_canonical_binary_and_database_defaults(self):
        provider = MimirProvider()
        provider._hermes_home = r"C:\temp\hermes-profile"

        with patch.object(provider, "_settings", return_value={}):
            with patch("hermes_mimir.shutil.which", side_effect=lambda name: "/bin/perseus-vault" if name == "perseus-vault" else None):
                self.assertEqual(provider._resolve_binary(), "/bin/perseus-vault")

            self.assertEqual(
                provider._resolve_db_path(),
                os.path.join(r"C:\temp\hermes-profile", "perseus-vault.db"),
            )

    def test_declares_minimal_setup_schema(self):
        fields = MimirProvider().get_config_schema()
        by_key = {field["key"]: field for field in fields}

        self.assertEqual(set(by_key), {"binary", "db_path"})
        self.assertFalse(by_key["binary"].get("secret", False))
        self.assertFalse(by_key["db_path"].get("secret", False))

    def test_exposes_canonical_provider_class(self):
        self.assertIsInstance(PerseusVaultProvider(), MimirProvider)
        self.assertEqual(PerseusVaultProvider.__name__, "PerseusVaultProvider")


if __name__ == "__main__":
    unittest.main()
