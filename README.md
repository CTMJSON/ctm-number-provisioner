# ctm-number-provisioner

A small, standalone [MCP](https://modelcontextprotocol.io/) server that lets an
AI assistant **search for, buy, and configure CallTrackingMetrics (CTM) tracking
numbers** using your own CTM API credentials.

It exposes six tools: identify the account, search available numbers, buy
numbers (exact numbers or "any N in this area code"), list routing targets,
apply a name / tracking source / call route to one or many numbers, and release
numbers.

It is a plain Python package with no internal dependencies. It talks only to the
public CTM API at `https://api.calltrackingmetrics.com/api/v1`.

---

## Contents

- [Safety model](#safety-model)
- [Requirements](#requirements)
- [Install](#install)
- [Configure credentials](#configure-credentials)
- [Run the server](#run-the-server)
- [Use with Claude Code / Claude Desktop](#use-with-claude-code--claude-desktop)
- [Use with Codex CLI](#use-with-codex-cli)
- [Use with a local LLM](#use-with-a-local-llm)
- [Tools](#tools)
- [Recommended workflow](#recommended-workflow)
- [CTM API endpoints used](#ctm-api-endpoints-used)
- [How the wrong-account risk is prevented](#how-the-wrong-account-risk-is-prevented)
- [Development](#development)
- [License](#license)

---

## Safety model

Buying phone numbers costs money and cannot be undone except by releasing the
number. This server is built so an assistant cannot spend your money or touch
the wrong account by accident:

- **`buy_numbers` defaults to `dry_run=True`.** It returns a plan and makes
  **zero** write calls. You must see the plan and explicitly confirm before the
  assistant calls it again with `dry_run=False`.
- **`test=True` buys free test numbers.** Use it for all evaluation and
  development. The README and the tool docstrings both default you toward
  `test=True` until you are ready for real numbers.
- **`whoami` is the first tool you should call.** It shows exactly which CTM
  account the credentials resolve to, so you never buy on an unexpected account.
  See [How the wrong-account risk is prevented](#how-the-wrong-account-risk-is-prevented).
- **`release_numbers` refuses to run unless `confirm=True`.** It is destructive
  and irreversible.
- **Every successful purchase is appended to `purchases.log`** (JSON lines: UTC
  timestamp, account id, TPN id, number, test flag) as a local audit trail. This
  file is git-ignored.
- **Secrets are never printed.** The token is read into memory, used as an HTTP
  `Authorization` header, and reported only as its *source* (`env` or
  `env.txt:<name>`), never its value.

---

## Requirements

- Python **3.10+**
- A CTM API basic-auth token (base64 `access:secret`) for the account you want
  to work in.

---

## Install

```bash
git clone https://github.com/<your-org>/ctm-number-provisioner.git
cd ctm-number-provisioner

python3.12 -m venv .venv          # any Python >= 3.10
.venv/bin/pip install -e '.[dev]' # omit [dev] if you don't want test tools
```

This installs a console script at `.venv/bin/ctm-number-provisioner`. Use that
absolute path in the MCP client configuration below.

---

## Configure credentials

The server resolves credentials in this order:

1. **`CTM_BASIC_AUTH`** — the raw base64 `access:secret` token. Use this for a
   single account.
2. **`CTM_ENV_FILE`** (default `~/.ctm/env`) plus **`CTM_TOKEN_NAME`** — a named
   line in a `name:token` credentials file. Use this to keep several accounts
   side by side and switch per tool call.

The CTM account id comes from **`CTM_ACCOUNT_ID`**. Every tool also accepts an
optional `account_id` override, and an optional `token_name` override to select
a different line from the credentials file.

### Option A: single account, environment variable

```bash
export CTM_BASIC_AUTH="$(printf '%s' 'ACCESS:SECRET' | base64)"
export CTM_ACCOUNT_ID="12345"
```

### Option B: multiple accounts, credentials file

Create `~/.ctm/env` (chmod it `600`) with one line per account:

```
acme_main:<base64-access-secret>
acme_test:<base64-access-secret>
```

Then select a line:

```bash
export CTM_ENV_FILE="$HOME/.ctm/env"
export CTM_TOKEN_NAME="acme_main"
export CTM_ACCOUNT_ID="12345"
```

Lines starting with `#` and blank lines are ignored. Only the first `:` on a
line splits the name from the token, so tokens containing `:` are preserved.

> **Never commit your token.** `.gitignore` already excludes `.env`, `*.env`,
> `.ctm/`, and `credentials.json`.

---

## Run the server

The server speaks MCP over **stdio** (stdout is reserved for the protocol):

```bash
CTM_BASIC_AUTH="…" CTM_ACCOUNT_ID="12345" .venv/bin/ctm-number-provisioner
```

On startup it prints the resolved account to **stderr** only, for example:

```
[ctm-number-provisioner] account 12345 (Acme Tracking) via env
```

Most users never run this by hand — the MCP client launches it. The sections
below wire it into each client.

---

## Use with Claude Code / Claude Desktop

### Claude Code (CLI)

Register the server once at user scope:

```bash
claude mcp add ctm-numbers -s user \
  -e CTM_BASIC_AUTH="<base64-access:secret>" \
  -e CTM_ACCOUNT_ID="12345" \
  -- "$PWD/.venv/bin/ctm-number-provisioner"
```

Verify it connected and lists the six tools:

```bash
claude mcp list
```

Then, in a session, try:

> Call `ctm-numbers` `whoami`, then search for available numbers in area code 443.

**Multiple accounts:** register one server per account with different names and
`CTM_TOKEN_NAME` values, e.g. `ctm-numbers-acme` and `ctm-numbers-acme-test`.

### Claude Desktop

Edit `claude_desktop_config.json`
(`~/Library/Application Support/Claude/` on macOS,
`%APPDATA%\Claude\` on Windows):

```json
{
  "mcpServers": {
    "ctm-numbers": {
      "command": "/absolute/path/to/ctm-number-provisioner/.venv/bin/ctm-number-provisioner",
      "env": {
        "CTM_BASIC_AUTH": "<base64-access:secret>",
        "CTM_ACCOUNT_ID": "12345"
      }
    }
  }
}
```

Restart Claude Desktop. The `ctm-numbers` tools appear in the tool picker.

### Bundled skill (optional)

If you use Claude Code, copy [`skills/ctm-buy-numbers/SKILL.md`](skills/ctm-buy-numbers/SKILL.md)
into `~/.claude/skills/ctm-buy-numbers/SKILL.md`. It teaches the assistant the
safe order of operations (whoami → search → dry-run → confirm → buy → configure)
so users don't have to prompt step by step.

---

## Use with Codex CLI

Codex supports MCP servers over stdio. Add a block to `~/.codex/config.toml`:

```toml
[mcp_servers.ctm-numbers]
command = "/absolute/path/to/ctm-number-provisioner/.venv/bin/ctm-number-provisioner"
env = { CTM_BASIC_AUTH = "<base64-access:secret>", CTM_ACCOUNT_ID = "12345" }
```

Then start Codex and ask it to use the `ctm-numbers` tools:

> Use the ctm-numbers MCP server: call whoami, then search area code 443.

Prefer `CTM_ENV_FILE` + `CTM_TOKEN_NAME` in the `env` table if you'd rather not
put the token literal in `config.toml`.

---

## Use with a local LLM

Any MCP-capable local client works, because this server is just a stdio process.

**Generic stdio MCP config** (Open WebUI, LibreChat, Continue, Cline, custom
scripts, etc.):

```json
{
  "mcpServers": {
    "ctm-numbers": {
      "command": "/absolute/path/to/ctm-number-provisioner/.venv/bin/ctm-number-provisioner",
      "env": { "CTM_BASIC_AUTH": "…", "CTM_ACCOUNT_ID": "12345" }
    }
  }
}
```

**Local model quality note.** Number search returns large lists and the tools
return JSON. A capable tool-calling model (e.g. a 30B+ instruct model with
function calling, such as Qwen or Llama derivatives) handles this well. Smaller
models often forget to call `whoami` first or skip the dry-run confirmation —
the bundled skill file is worth including in the system prompt for those.

**No MCP support?** Drive the tools directly from Python. Each tool is an
ordinary async function:

```python
import asyncio, json
from ctm_numbers.server import whoami, search_available_numbers

async def main():
    print(json.dumps(await whoami(), indent=2))
    found = await search_available_numbers(area_code="443")
    print(found["count"], "numbers available")

asyncio.run(main())
```

Set `CTM_BASIC_AUTH` and `CTM_ACCOUNT_ID` in the environment first.

---

## Tools

| Tool | Writes? | What it does |
|---|---|---|
| `whoami` | no | Resolves and reports the account id + name and the token source. |
| `search_available_numbers` | no | Finds available numbers by area code, ZIP/address, prefix, toll-free, or international pattern. |
| `buy_numbers` | **yes** | Buys numbers. `dry_run=True` by default; `test=True` buys free test numbers. |
| `list_routing_targets` | no | Lists tracking sources, receiving numbers, queues, voice menus, and users. |
| `configure_numbers` | **yes** | Applies a name, tracking source, and one call route to one or many numbers. |
| `release_numbers` | **yes** | Releases numbers. Requires `confirm=True`. |

### `buy_numbers` arguments

- `phone_numbers` — exact numbers from `search_available_numbers`, **or**
- `area_code` + `quantity` — let CTM pick N numbers in that area code
- `test` — buy free test numbers (default `False`)
- `dry_run` — plan only, no writes (default `True`)

### `configure_numbers` routes

Pick **at most one**:

- `receiving_number_ids` — forward calls to one or more RPN ids
- `queue_id` — a call queue (`CQU...`)
- `voice_menu_id` — a voice menu / IVR (`VOM...`)
- `user_id` — ring an agent (`USR...`), with `user_no_answer_seconds` and
  `user_default_action`
- `route_override` — a raw `{"virtual_phone_number": {…}}` dial-routes body

`name` accepts `{n}` (1-based index) and `{number}` placeholders, e.g.
`"Google Ads {n}"`.

---

## Recommended workflow

1. **`whoami`** — confirm the account. Tell the user which account you'll act on.
2. **`search_available_numbers`** — show the results and let the user pick.
3. **`buy_numbers(dry_run=True)`** — show the plan.
4. **Get explicit confirmation**, then `buy_numbers(dry_run=False)` with
   `test=True` unless the user says it's for real.
5. **`list_routing_targets`** — present the options; let the user choose the
   tracking source and route target.
6. **`configure_numbers`** — apply the name, source, and exactly one route.
7. **`release_numbers(…, confirm=True)`** — only if the user explicitly asks,
   typically to clean up test numbers.

The skill at [`skills/ctm-buy-numbers/SKILL.md`](skills/ctm-buy-numbers/SKILL.md)
encodes this flow.

---

## CTM API endpoints used

Base URL: `https://api.calltrackingmetrics.com/api/v1` (override with
`CTM_BASE_URL`).

All requests send `Authorization: Basic <token>`, `Accept: application/json`,
and `Content-Type: application/json`. Query parameters with `None` values are
dropped. Non-2xx responses raise `CTMError(status, body, method, path)`; tools
catch it per item so one failure can't abort a batch.

| Purpose | Method + path |
|---|---|
| Identify account (`whoami`, startup check) | `GET /accounts/{aid}` |
| Search available numbers | `GET /accounts/{aid}/numbers/search.json` |
| Buy an exact number | `POST /accounts/{aid}/numbers` |
| Buy in an area code | `POST /accounts/{aid}/numbers/areacode` |
| Read a number | `GET /accounts/{aid}/numbers/{TPN}` |
| Rename / set custom fields | `POST /accounts/{aid}/numbers/{TPN}/update_number` |
| Attach a tracking source | `POST /accounts/{aid}/sources/{TSO}/numbers/{TPN}/add` |
| Add a receiving number | `POST /accounts/{aid}/numbers/{TPN}/receiving_numbers/{RPN}/add` |
| Set the call route | `PUT /accounts/{aid}/numbers/{TPN}/dial_routes` |
| Release a number | `DELETE /accounts/{aid}/numbers/{TPN}` |
| List tracking sources | `GET /accounts/{aid}/sources` |
| List receiving numbers | `GET /accounts/{aid}/receiving_numbers` |
| List queues | `GET /accounts/{aid}/queues.json` |
| List voice menus | `GET /accounts/{aid}/voice_menus` |
| List users | `GET /accounts/{aid}/users` |

### Search parameters

US/Canada (`country=US`, the default). `searchby` is inferred when omitted:

| Intent | Params |
|---|---|
| Area code | `searchby=area&areacode=443` |
| ZIP / street address | `searchby=address&address=21201` |
| Area code + prefix | `searchby=number&number=917563` |
| Toll-free | `searchby=tollfree` |
| International | `country=GB&pattern=430[&operator=start_with\|includes]` |

Search returns up to ~50 numbers per call and includes overlay area codes.

### Call-route bodies

```jsonc
// queue
{"virtual_phone_number": {"dial_route": "call_queue", "call_queue_id": "CQU..."}}

// voice menu
{"virtual_phone_number": {"dial_route": "voice_menu", "voice_menu_id": "VOM..."}}

// agent
{"virtual_phone_number": {
  "dial_route": "call_agent",
  "user_id": "USR...",
  "user_default_action_label": "voicemail",
  "user_no_answer_seconds": 25
}}
```

Verified against the live API:

- The queue body above sets `route_to.type == "call_queue"` with
  `route_to.dial.id` equal to the `CQU...` id.
- The tracking-source add works with the `TSO...` id from `GET /sources` (the
  numeric `filter_id` is not required).
- `GET /numbers/{TPN}` returns the number object directly (not wrapped in a
  `number` key). Route changes are confirmed by re-reading `route_to`.

### Rate limits

CTM allows roughly 10 requests/second. The server uses a global concurrency
limit of 4, retries transport errors twice, and retries `429`/`5xx` with
exponential backoff (1s, 2s, 4s). Paginated list endpoints fetch page 1, then
all remaining pages **concurrently**.

---

## How the wrong-account risk is prevented

A CTM token does not guarantee the account you expect: the same token can map to
a different sub-account than its label suggests, and some CTM clients silently
fall back to a default account. This server defends against that in several
layers:

1. **`whoami` resolves the account from the API, not from configuration.** It
   calls `GET /accounts/{CTM_ACCOUNT_ID}` and returns the **name CTM reports for
   that id**, alongside the id and the token *source*. If the returned name is
   not the account you intended, you stop before buying anything.
2. **The server prints the resolved account name on startup** (to stderr), so a
   misconfigured client is visible immediately in the logs.
3. **Every tool takes an optional `account_id` override** that is resolved
   per call, so a single server can safely target different accounts without
   relying on a stale global default.
4. **`buy_numbers` dry-runs by default and includes the resolved account id and
   name in the plan**, so the last thing a human sees before approving a
   purchase is exactly which account it will hit.
5. **`buy_numbers` never proceeds on ambiguity**: it requires exactly one of
   `phone_numbers` or `area_code`, and validates the quantity range up front.

The recommended assistant behavior (encoded in the bundled skill) is: **call
`whoami` first, state the account name to the user, and refuse to buy until the
user confirms that name.**

---

## Development

```bash
.venv/bin/pytest -q          # 20 tests, all offline (respx, no live calls)
.venv/bin/ruff check src tests
```

Project layout:

```
src/ctm_numbers/
  __main__.py   console entry point + stderr startup check
  auth.py       credential resolution (env var or named line in a file)
  client.py     async httpx wrapper: retries, backoff, concurrent pagination
  server.py     FastMCP instance and the six tools
tests/test_tools.py
```

---

## License

[MIT](LICENSE)