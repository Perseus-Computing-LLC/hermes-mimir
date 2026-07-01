# hermes-mimir

Perseus Vault (formerly "Mimir") persistent memory provider for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

Perseus Vault is an encrypted, local-first memory engine for AI agents — 27 MCP tools,
single Rust binary (~8 MB), embedded SQLite + FTS5 + vector search. Zero cloud
dependencies.

## Features

- **Encrypted at rest** — AES-256-GCM encryption for production deployments
- **Hybrid search** — BM25 (FTS5) + dense embeddings + Reciprocal Rank Fusion
- **Confidence decay** — memories naturally decay unless reinforced
- **27 MCP tools** — full memory lifecycle: remember, recall, search, forget,
  summarize, decay, vault export, embed, prune
- **Web dashboard** — browse and manage memories at `http://localhost:8080`
- **Single binary** — no Docker, Postgres, or cloud required

## Installation

```bash
# From GitHub (until published to PyPI)
pip install git+https://github.com/Perseus-Computing-LLC/hermes-mimir.git

# Or from PyPI (coming soon)
# pip install hermes-mimir
```

Then install the Perseus Vault binary (still named `mimir` for back-compat, `perseus-vault` is the new primary name):

```bash
# Via cargo
cargo install perseus-vault

# Or download from GitHub Releases
# https://github.com/Perseus-Computing-LLC/perseus-vault/releases
```

### Hermes Plugin Setup

Hermes discovers memory providers in `$HERMES_HOME/plugins/<name>/`.
After installing the package, link it into your Hermes plugins directory:

```bash
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_HOME/plugins/mimir"

# Copy the provider module and metadata
python3 -c "
import hermes_mimir, shutil, os
src = os.path.dirname(hermes_mimir.__file__)
shutil.copy(os.path.join(src, '__init__.py'), '$HERMES_HOME/plugins/mimir/__init__.py')
shutil.copy(os.path.join(os.path.dirname(src), 'plugin.yaml'), '$HERMES_HOME/plugins/mimir/plugin.yaml')
print('Plugin installed to $HERMES_HOME/plugins/mimir/')
"
```

Verify the plugin is discovered:

```bash
hermes memory setup   # should show mimir as an available provider
```

## Configuration

Add to `~/.hermes/config.yaml`:

```yaml
memory:
  provider: mimir
  mimir:
    binary: /usr/local/bin/mimir     # optional — auto-detected from PATH
    db_path: ~/.hermes/mimir.db      # optional — defaults to ~/.hermes/mimir.db
```

Then restart Hermes:

```bash
hermes gateway restart
# or exit and re-launch the CLI
```

## How It Works

`hermes-mimir` implements Hermes's `MemoryProvider` ABC. On startup, it:

1. Launches the Perseus Vault binary as a subprocess
2. Performs an MCP JSON-RPC 2.0 handshake (`initialize`)
3. Discovers available MCP tools via `tools/list`
4. Exposes those tools to the Hermes agent as callable functions
5. Handles `prefetch` (recall before each turn) and `sync_turn` (persist after each turn)

The agent sees `mimir_remember`, `mimir_recall`, `mimir_search_memories`, and
24 other tools — all backed by the Perseus Vault binary running alongside Hermes.

## Comparison

| Feature | Built-in memory | Perseus Vault |
|---|---|---|
| Storage | Flat text files | Encrypted SQLite |
| Search | Substring grep | FTS5 + embeddings + RRF |
| Encryption | None | AES-256-GCM |
| Cross-session | Yes (same files) | Yes (structured entities) |
| Decay | No | Ebbinghaus decay with configurable half-life |
| Dashboard | No | Built-in web UI |
| Tools | 1 (`memory`) | 27 |
| Dependencies | None | Single Rust binary |

## Development

```bash
git clone https://github.com/Perseus-Computing-LLC/hermes-mimir.git
cd hermes-mimir
pip install -e ".[dev]"
pytest
```

## License

MIT — same as Hermes Agent and Perseus Vault.

## Related

- [Perseus Vault](https://github.com/Perseus-Computing-LLC/perseus-vault) — the memory engine (formerly "Mimir"/"Mneme")
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — the agent framework
- [Perseus](https://github.com/Perseus-Computing-LLC/perseus) — live context engine for AI agents
