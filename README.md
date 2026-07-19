# Hermes Perseus Vault provider

A local-first [Perseus Vault](https://github.com/Perseus-Computing-LLC/perseus-vault) memory provider for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

Perseus Vault runs as a local subprocess backed by encrypted SQLite. Hermes receives its memory lifecycle through the standard `MemoryProvider` contract: pre-turn recall, asynchronous completed-turn persistence, and explicit tool calls.

## Status

This repository is the standalone implementation and compatibility migration path. The canonical Hermes provider name is **`perseus-vault`**. `mimir` is accepted only as a legacy binary/config fallback so existing installations retain their data.

## Install

Install the Vault binary first:

```bash
cargo install perseus-vault
# Or download a release from:
# https://github.com/Perseus-Computing-LLC/perseus-vault/releases
```

Install this provider package:

```bash
pip install git+https://github.com/Perseus-Computing-LLC/hermes-mimir.git
```

Until the provider is bundled by Hermes, copy the package entrypoint and manifest into your active profile:

```bash
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_HOME/plugins/perseus-vault"
python - <<'PY'
import hermes_mimir
import os
import shutil

home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
target = os.path.join(home, "plugins", "perseus-vault")
source = os.path.dirname(hermes_mimir.__file__)
shutil.copy(os.path.join(source, "__init__.py"), os.path.join(target, "__init__.py"))
shutil.copy(os.path.join(os.path.dirname(source), "plugin.yaml"), os.path.join(target, "plugin.yaml"))
PY
```

Then run:

```bash
hermes memory setup
```

Select **perseus-vault**, configure its optional binary/database paths, and restart Hermes or start a new session.

## Configuration

The setup wizard persists non-secret settings in the active Hermes profile at:

```text
$HERMES_HOME/perseus-vault.json
```

```json
{
  "binary": "/usr/local/bin/perseus-vault",
  "db_path": "/home/me/.hermes/perseus-vault.db"
}
```

Both settings are optional:

- `binary` defaults to `perseus-vault` on `PATH`, falling back to legacy `mimir` locations for migration.
- `db_path` defaults to `$HERMES_HOME/perseus-vault.db`. When an existing `$HERMES_HOME/mimir.db` exists, that database is retained automatically.

Legacy `memory.mimir.binary` and `memory.mimir.db_path` configuration remains a fallback. Canonical profile JSON settings take precedence.

## Tool naming and migration

Perseus Vault advertises canonical `perseus_vault_*` tools. The provider suppresses duplicate legacy aliases and exposes canonical names to Hermes. If connected to an older Vault binary that only advertises `mimir_*`, the provider maps those tool schemas to the canonical names while retaining compatibility with the binary's tool endpoint.

## Data and privacy

- The provider launches a local `perseus-vault` subprocess and communicates through MCP JSON-RPC over stdio.
- Automatic turn capture stores the user and assistant text for completed turns in the configured local Vault database.
- No cloud endpoint, telemetry, or API key is required by this provider.
- Vault encryption is configured by the Vault runtime; operators should configure its key management before storing sensitive data.

## Development

The suite uses the Python standard library so it can run without a separate test dependency:

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
python -m compileall -q hermes_mimir tests
```

## License

MIT.
