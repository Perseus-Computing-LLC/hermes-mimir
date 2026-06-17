"""Mimir memory provider for Hermes Agent.

Bridges Hermes's MemoryProvider ABC to the Mimir persistent memory
engine via MCP JSON-RPC 2.0 over stdio.  Provides encrypted,
local-first memory with hybrid search (FTS5 + embeddings + RRF).

Requires the Mimir binary.  Install with:
    cargo install mimir
or download from https://github.com/tcconnally/mimir/releases

Configuration (in config.yaml):
    memory:
      provider: mimir
      mimir:
        binary: /usr/local/bin/mimir   # optional, auto-detected
        db_path: ~/.hermes/mimir.db    # optional
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
                "clientInfo": {"name": "hermes-mimir", "version": "0.1.0"},
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
    ctx.register_memory_provider(MimirProvider())


class MimirProvider(MemoryProvider):
    """Mimir persistent memory provider for Hermes Agent.

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
        self._initialized = False

    # ------------------------------------------------------------------
    # MemoryProvider ABC
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "mimir"

    def is_available(self) -> bool:
        """Check if the Mimir binary is findable."""
        binary = self._resolve_binary()
        return binary is not None

    def initialize(self, session_id: str, **kwargs) -> None:
        """Start the Mimir subprocess and perform MCP handshake."""
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
            logger.warning("Mimir binary not found — memory provider unavailable")
            return

        db_path = self._resolve_db_path()
        self._client = _MimirClient(binary, db_path)

        if not self._client.start():
            logger.warning("Mimir failed to start — memory provider unavailable")
            self._client = None
            return

        # Discover available MCP tools
        try:
            self._tool_schemas = self._client.list_tools()
            # Convert MCP tool schemas to OpenAI function-calling format
            self._tool_schemas = self._normalize_schemas(self._tool_schemas)
        except Exception as e:
            logger.warning("Failed to discover Mimir tools: %s", e)
            self._tool_schemas = []

        self._initialized = True
        logger.info(
            "Mimir memory provider ready — %d tools, db=%s",
            len(self._tool_schemas), db_path,
        )

    def system_prompt_block(self) -> str:
        """Include Mimir availability in the system prompt."""
        if not self._initialized or not self._client or not self._client.is_running():
            return ""
        return (
            "You have access to Mimir persistent memory (27 tools). "
            "Use mimir_remember to store important information, "
            "mimir_recall to retrieve context, and mimir_search_memories "
            "for semantic search. Memories persist across sessions with "
            "AES-256-GCM encryption and confidence decay."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant memories for the current turn."""
        if not self._client or not self._client.is_running():
            return ""

        try:
            result = self._client.call_tool("mimir_recall", {
                "query": query,
                "limit": 5,
            })
            if result and result.strip() and result.strip() != "null":
                return f"[Mimir recall]\n{result}"
        except Exception as e:
            logger.debug("Mimir prefetch failed: %s", e)
        return ""

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Store the completed turn in Mimir."""
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
                self._client.call_tool("mimir_remember", {
                    "key": key,
                    "category": "conversation",
                    "body_json": body,
                    "status": "active",
                })
            except Exception as e:
                logger.debug("Mimir sync_turn failed: %s", e)

        t = threading.Thread(target=_store, daemon=True, name="mimir-sync")
        t.start()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return Mimir tool schemas in OpenAI function-calling format."""
        return self._tool_schemas

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """Forward a tool call to Mimir via MCP."""
        if not self._client or not self._client.is_running():
            return json.dumps({"error": "Mimir is not running"})

        try:
            return self._client.call_tool(tool_name, args)
        except Exception as e:
            logger.warning("Mimir tool call '%s' failed: %s", tool_name, e)
            return json.dumps({"error": str(e)})

    def shutdown(self) -> None:
        """Clean shutdown — terminate the Mimir subprocess."""
        if self._client:
            self._client.stop()
        self._initialized = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_binary(self) -> Optional[str]:
        """Find the Mimir binary on this system."""
        # Check explicit config
        try:
            from hermes_cli.config import load_config
            cfg = load_config() or {}
            explicit = cfg.get("memory", {}).get("mimir", {}).get("binary", "")
            if explicit and os.path.isfile(explicit):
                return explicit
        except Exception:
            pass

        # Check PATH
        which = shutil.which("mimir")
        if which:
            return which

        # Check common locations
        for candidate in [
            os.path.expanduser("~/.cargo/bin/mimir"),
            "/usr/local/bin/mimir",
            "/opt/mimir/mimir",
        ]:
            if os.path.isfile(candidate):
                return candidate

        return None

    def _resolve_db_path(self) -> str:
        """Determine where Mimir should store its database."""
        try:
            from hermes_cli.config import load_config
            cfg = load_config() or {}
            explicit = cfg.get("memory", {}).get("mimir", {}).get("db_path", "")
            if explicit:
                return os.path.expanduser(explicit)
        except Exception:
            pass
        return os.path.join(self._hermes_home, "mimir.db")

    def _normalize_schemas(
        self, mcp_tools: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Convert MCP tool schemas to OpenAI function-calling format.

        MCP format:
            {"name": "mimir_remember", "description": "...", "inputSchema": {...}}

        OpenAI format:
            {"name": "mimir_remember", "description": "...", "parameters": {...}}
        """
        normalized = []
        for tool in mcp_tools:
            name = tool.get("name", "")
            # Only expose tools that start with mimir_ to avoid namespace conflicts
            if not name or not name.startswith("mimir_"):
                continue
            normalized.append({
                "name": name,
                "description": tool.get("description", ""),
                "parameters": tool.get("inputSchema", {
                    "type": "object",
                    "properties": {},
                }),
            })
        return normalized
