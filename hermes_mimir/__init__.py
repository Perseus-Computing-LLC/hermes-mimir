"""Perseus Vault memory provider for Hermes Agent.

Bridges Hermes's MemoryProvider ABC to the Perseus Vault persistent memory
engine via MCP JSON-RPC 2.0 over stdio.  Provides encrypted,
local-first memory with hybrid search (FTS5 + embeddings + RRF).

Requires the Perseus Vault binary.  Install with:
    cargo install perseus-vault
or download from https://github.com/Perseus-Computing-LLC/perseus-vault/releases

Configuration (in config.yaml):
    memory:
      provider: perseus-vault
      perseus_vault:
        binary: /usr/local/bin/perseus-vault   # optional, auto-detected
        db_path: ~/.hermes/perseus-vault.db    # optional
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSON-RPC 2.0 client for Mimir over stdio
# ---------------------------------------------------------------------------

class _MimirClient:
    """Lightweight JSON-RPC 2.0 client for a Mimir stdio subprocess."""

    def __init__(self, binary: str, db_path: str, timeout: float = 30.0):
        self._binary = binary
        self._db_path = db_path
        self._timeout = timeout
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._request_id = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Launch the Mimir binary and perform MCP handshake."""
        if self._proc is not None:
            return True

        try:
            self._proc = subprocess.Popen(
                [self._binary, "--db", self._db_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            logger.error("Mimir binary not found: %s", self._binary)
            return False
        except Exception as e:
            logger.error("Failed to start Mimir: %s", e)
            return False

        # MCP initialize handshake
        try:
            result = self._call("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "hermes-perseus-vault", "version": "0.2.0"},
            })
            if result is None:
                logger.error("Mimir initialize handshake failed")
                self.stop()
                return False
        except Exception as e:
            logger.error("Mimir initialize error: %s", e)
            self.stop()
            return False

        return True

    def stop(self) -> None:
        """Terminate the Mimir subprocess."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.stdin.close()
            proc.stdout.close()
            proc.stderr.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass

    def is_running(self) -> bool:
        """Check if the Mimir subprocess is alive."""
        return self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------------------------
    # JSON-RPC
    # ------------------------------------------------------------------

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Call a Mimir MCP tool and return the text result.

        Uses the MCP tools/call method.
        """
        result = self._call("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        if result is None:
            return json.dumps({"error": "Mimir MCP call failed"})

        # Extract text content from MCP result
        content = result.get("content", [])
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(item.get("text", ""))
        return "\n".join(text_parts) if text_parts else json.dumps(result)

    def list_tools(self) -> List[Dict[str, Any]]:
        """Return the list of Mimir MCP tools."""
        result = self._call("tools/list", {})
        if result is None:
            return []
        return result.get("tools", [])

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _call(self, method: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Send a JSON-RPC request and return the result."""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return None

            self._request_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params,
            }

            try:
                req_str = json.dumps(request) + "\n"
                self._proc.stdin.write(req_str)
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                logger.warning("Mimir write failed: %s", e)
                return None

            try:
                line = self._proc.stdout.readline()
                if not line:
                    return None
                response = json.loads(line)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Mimir read failed: %s", e)
                return None

            if "error" in response:
                logger.warning("Mimir RPC error: %s", response["error"])
                return None

            return response.get("result")


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

def register(ctx):
    """Plugin entry point — called by Hermes plugin loader."""
    ctx.register_memory_provider(PerseusVaultProvider())


class PerseusVaultProvider(MemoryProvider):
    """Perseus Vault persistent memory provider for Hermes Agent.

    Provides 27 MCP tools for full memory lifecycle: remember, recall,
    search, forget, decay, vault export, summarize, embed, prune, and more.
    Features AES-256-GCM encryption, hybrid search (FTS5 + embeddings + RRF),
    and confidence decay — all in a single Rust binary with embedded SQLite.
    """

    def __init__(self):
        self._client: Optional[_MimirClient] = None
        self._session_id: str = ""
        self._hermes_home: str = ""
        self._tool_schemas: List[Dict[str, Any]] = []
        self._tool_name_aliases: Dict[str, str] = {}
        self._initialized = False

    # ------------------------------------------------------------------
    # MemoryProvider ABC
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "perseus-vault"

    def is_available(self) -> bool:
        """Check if the Perseus Vault binary is findable without I/O."""
        binary = self._resolve_binary()
        return binary is not None

    def get_config_schema(self) -> List[Dict[str, Any]]:
        """Declare the two non-secret settings required by the setup wizard."""
        return [
            {
                "key": "binary",
                "description": "Perseus Vault binary path (leave blank to use PATH)",
                "default": "",
            },
            {
                "key": "db_path",
                "description": "Vault database path (leave blank for the profile default)",
                "default": "",
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Persist wizard configuration in the active Hermes profile."""
        path = Path(hermes_home) / "perseus-vault.json"
        path.write_text(json.dumps({
            key: value for key, value in values.items()
            if key in {"binary", "db_path"} and value
        }, indent=2) + "\n", encoding="utf-8")

    def initialize(self, session_id: str, **kwargs) -> None:
        """Start the Perseus Vault subprocess and perform MCP handshake."""
        self._session_id = session_id
        self._hermes_home = kwargs.get("hermes_home", os.path.expanduser("~/.hermes"))
        agent_context = kwargs.get("agent_context", "primary")

        # Skip for non-primary contexts to avoid polluting cron/subagent memory
        if agent_context not in ("primary", "flush"):
            return

        if self._initialized:
            return

        binary = self._resolve_binary()
        if not binary:
            logger.warning("Perseus Vault binary not found — memory provider unavailable")
            return

        db_path = self._resolve_db_path()
        self._client = _MimirClient(binary, db_path)

        if not self._client.start():
            logger.warning("Perseus Vault failed to start — memory provider unavailable")
            self._client = None
            return

        # Discover available MCP tools
        try:
            self._tool_schemas = self._client.list_tools()
            # Convert MCP tool schemas to OpenAI function-calling format
            self._tool_schemas = self._normalize_schemas(self._tool_schemas)
        except Exception as e:
            logger.warning("Failed to discover Perseus Vault tools: %s", e)
            self._tool_schemas = []

        self._initialized = True
        logger.info(
            "Perseus Vault memory provider ready — %d tools, db=%s",
            len(self._tool_schemas), db_path,
        )

    def system_prompt_block(self) -> str:
        """Include Perseus Vault availability in the system prompt."""
        if not self._initialized or not self._client or not self._client.is_running():
            return ""
        return (
            "You have access to Perseus Vault persistent memory. "
            "Use perseus_vault_remember to store important information, "
            "perseus_vault_recall to retrieve context, and perseus_vault_semantic_search "
            "for semantic search. Memories persist across sessions with "
            "AES-256-GCM encryption and confidence decay."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant memories for the current turn."""
        if not self._client or not self._client.is_running():
            return ""

        try:
            result = self._call_vault_tool("perseus_vault_recall", {
                "query": query,
                "limit": 5,
            })
            if result and result.strip() and result.strip() != "null":
                try:
                    payload = json.loads(result)
                    if isinstance(payload, dict) and "error" in payload:
                        return ""
                except json.JSONDecodeError:
                    pass
                return f"[Perseus Vault recall]\n{result}"
        except Exception as e:
            logger.debug("Perseus Vault prefetch failed: %s", e)
        return ""

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Store the completed turn in Perseus Vault."""
        if not self._client or not self._client.is_running():
            return

        # Store in background to avoid blocking the agent loop
        def _store():
            try:
                import json as _json
                sid = session_id or self._session_id
                key = f"turn-{sid}-{int(time.time())}"
                body = _json.dumps({
                    "user": user_content[:2000],
                    "assistant": assistant_content[:2000],
                    "timestamp": int(time.time()),
                })
                self._call_vault_tool("perseus_vault_remember", {
                    "key": key,
                    "category": "conversation",
                    "body_json": body,
                    "status": "active",
                })
            except Exception as e:
                logger.debug("Perseus Vault sync_turn failed: %s", e)

        t = threading.Thread(target=_store, daemon=True, name="mimir-sync")
        t.start()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return Perseus Vault tool schemas in OpenAI function-calling format."""
        return self._tool_schemas

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """Forward a tool call to Perseus Vault via MCP."""
        if not self._client or not self._client.is_running():
            return json.dumps({"error": "Perseus Vault is not running"})

        try:
            return self._call_vault_tool(tool_name, args)
        except Exception as e:
            logger.warning("Perseus Vault tool call '%s' failed: %s", tool_name, e)
            return json.dumps({"error": str(e)})

    def shutdown(self) -> None:
        """Clean shutdown — terminate the Perseus Vault subprocess."""
        if self._client:
            self._client.stop()
        self._initialized = False

    def _call_vault_tool(self, canonical_name: str, args: Dict[str, Any]) -> str:
        """Call a canonical tool through the name advertised by the binary."""
        if not self._client:
            return json.dumps({"error": "Perseus Vault is not running"})
        binary_name = self._tool_name_aliases.get(canonical_name)
        if binary_name is None and canonical_name.startswith("perseus_vault_"):
            binary_name = f"mimir_{canonical_name.removeprefix('perseus_vault_')}"
        return self._client.call_tool(binary_name or canonical_name, args)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_binary(self) -> Optional[str]:
        """Find the canonical binary, retaining the legacy name as fallback."""
        explicit = self._settings().get("binary", "")
        if explicit and os.path.isfile(explicit):
            return explicit

        # Check PATH
        for name in ("perseus-vault", "mimir"):
            which = shutil.which(name)
            if which:
                return which

        # Check common locations
        for candidate in [
            os.path.expanduser("~/.cargo/bin/perseus-vault"),
            "/usr/local/bin/perseus-vault",
            "/opt/perseus-vault/perseus-vault",
            os.path.expanduser("~/.cargo/bin/mimir"),
            "/usr/local/bin/mimir",
        ]:
            if os.path.isfile(candidate):
                return candidate

        return None

    def _resolve_db_path(self) -> str:
        """Determine a profile-scoped database path without losing legacy data."""
        explicit = self._settings().get("db_path", "")
        if explicit:
            return os.path.expanduser(explicit)
        legacy_path = os.path.join(self._hermes_home, "mimir.db")
        if os.path.exists(legacy_path):
            return legacy_path
        return os.path.join(self._hermes_home, "perseus-vault.db")

    def _settings(self) -> Dict[str, Any]:
        """Read canonical profile settings, with old config as a fallback."""
        settings: Dict[str, Any] = {}
        home = self._hermes_home or os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
        config_path = Path(home) / "perseus-vault.json"
        try:
            settings.update(json.loads(config_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
        try:
            from hermes_cli.config import load_config
            memory = (load_config() or {}).get("memory", {})
            settings = {**memory.get("mimir", {}), **memory.get("perseus_vault", {}), **settings}
        except Exception:
            pass
        return settings

    def _normalize_schemas(
        self, mcp_tools: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Convert MCP tool schemas to OpenAI function-calling format.

        MCP format:
            {"name": "mimir_remember", "description": "...", "inputSchema": {...}}

        OpenAI format:
            {"name": "mimir_remember", "description": "...", "parameters": {...}}
        """
        canonical: Dict[str, Dict[str, Any]] = {}
        legacy: Dict[str, Dict[str, Any]] = {}
        advertised_names: Dict[str, str] = {}
        order: List[str] = []
        for tool in mcp_tools:
            name = tool.get("name", "")
            if name.startswith("perseus_vault_"):
                suffix = name.removeprefix("perseus_vault_")
                target = canonical
                canonical_name = name
            elif name.startswith("mimir_"):
                suffix = name.removeprefix("mimir_")
                target = legacy
                canonical_name = f"perseus_vault_{suffix}"
            else:
                continue
            if suffix not in canonical and suffix not in legacy:
                order.append(suffix)
            if name.startswith("perseus_vault_") or canonical_name not in advertised_names:
                advertised_names[canonical_name] = name
            target[suffix] = {
                "name": canonical_name,
                "description": tool.get("description", ""),
                "parameters": tool.get("inputSchema", {
                    "type": "object",
                    "properties": {},
                }),
            }
        result = [canonical[suffix] if suffix in canonical else legacy[suffix] for suffix in order]
        self._tool_name_aliases = {
            schema["name"]: advertised_names[schema["name"]]
            for schema in result
        }
        return result


# Backward-compatible import for existing standalone integrations.
MimirProvider = PerseusVaultProvider
