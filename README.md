# ProjectTest

Test Project created for ibm online study.

Also contains a **WhatsApp Business bridge** (`whatsapp/`) — a direct
integration with Meta's WhatsApp Business Cloud API that lets Claude read and
send messages on a business number. See below.

---

# WhatsApp Business ↔ Claude bridge

Two processes over one SQLite database:

```
  Meta Cloud API                         Claude
        |                                   |
        | POST /webhook                     | MCP (stdio)
        v                                   v
  whatsapp.webhook  ──►  whatsapp.db  ◄──  whatsapp.mcp_server
   (FastAPI, always on)   (conversations)   (8 tools)
```

The webhook must run continuously — it is the only way inbound messages are
captured. The MCP server is spawned by Claude on demand, reads the same
database, and calls the Cloud API to send.

## What Claude can do

| Tool | Purpose |
|---|---|
| `list_threads` | Recent conversations, with 24-hour window state |
| `read_thread` | Full history of one conversation |
| `search_messages` | Text search across everything stored |
| `send_message` | Freeform text (inside the 24-hour window) |
| `send_template` | Approved template — works outside the window |
| `send_media` | Image, video, audio, document or sticker by URL |
| `download_media` | Fetch an attachment a contact sent |
| `mark_read` | Blue ticks, optionally with a typing indicator |

## The 24-hour window

Meta only permits freeform messages within 24 hours of the contact's most
recent message. Outside it, sends fail with error `131047` and the only way
through is a **pre-approved template**.

The bridge surfaces this everywhere rather than letting you discover it as a
failed send: `list_threads` and `read_thread` show whether the window is open,
and `send_message` refuses locally — without spending an API call — when it
knows the window has closed. A contact the store has never seen is always
attempted, since the local database may simply predate them.

## Read-only mode

To only *read* messages, two values are enough: `WHATSAPP_APP_SECRET` and
`WHATSAPP_VERIFY_TOKEN`. Message content arrives inside the webhook payload
itself, so no access token is involved in receiving. Leave
`WHATSAPP_ACCESS_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID` unset and the bridge
starts in read-only mode: `list_threads`, `read_thread` and `search_messages`
work; the send tools and `download_media` explain that they are disabled.

This does not reduce the Meta-side setup. A phone number must still be
connected to the Cloud API and the webhook subscribed to the `messages`
field, because the webhook is the only mechanism by which messages arrive.

**Connecting a number to the Cloud API takes it over.** That number can no
longer be used in the WhatsApp Business phone app -- the Cloud API becomes
its only client. Use Meta's free test number if you are not ready for that.

## Setup

### 1. Meta side

You need a Meta app with WhatsApp added, a WhatsApp Business Account, and a
phone number registered to it (Meta's test number works for development).

From the App Dashboard collect four values into `.env`:

```bash
cp .env.example .env
```

Only the last two are needed for read-only use; the first two enable sending.

- `WHATSAPP_ACCESS_TOKEN` — *WhatsApp → API Setup*. The temporary token
  expires in 24 hours; for real use create a System User in Business Settings
  and issue a permanent token with the `whatsapp_business_messaging` and
  `whatsapp_business_management` scopes.
- `WHATSAPP_PHONE_NUMBER_ID` — *WhatsApp → API Setup*. Not the phone number,
  and not the WhatsApp Business Account ID.
- `WHATSAPP_APP_SECRET` — *App Settings → Basic*. Verifies webhook signatures.
- `WHATSAPP_VERIFY_TOKEN` — a random string you invent; you will paste the
  same value into the dashboard in step 3. Generate one with
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

Then confirm the values actually work before going any further:

```bash
.venv/bin/python -m whatsapp.doctor
```

It calls the Graph API with what you pasted and names the specific mistake if
something is off — the App ID copied in place of the App Secret, the WhatsApp
Business Account ID in place of the phone number ID, or a temporary token that
has passed its 24-hour expiry.

### 2. Install and run the webhook

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn whatsapp.webhook:app --factory --host 0.0.0.0 --port 8000
```

Meta requires a public HTTPS URL with a valid certificate. For local
development, tunnel it:

```bash
ngrok http 8000     # or: cloudflared tunnel --url http://localhost:8000
```

### 3. Subscribe the webhook

In *App Dashboard → WhatsApp → Configuration → Webhooks*:

- **Callback URL**: `https://<your-public-host>/webhook`
- **Verify token**: the `WHATSAPP_VERIFY_TOKEN` from your `.env`

Click *Verify and save* — Meta calls `GET /webhook` and the server echoes the
challenge. Then **subscribe to the `messages` field**. This is the step most
often missed: without it the webhook is registered but no events are ever
delivered.

### 4. Connect it to Claude

Claude Code:

```bash
claude mcp add whatsapp -- /bin/sh -c 'cd "$HOME/ProjectTest" && exec .venv/bin/python -m whatsapp.mcp_server'
```

The `cd` matters. Claude starts the server from whatever directory it happens
to be in, and both `.env` discovery and the default relative `WHATSAPP_DB_PATH`
resolve against the working directory -- without it the server either fails to
import or silently reads an empty database.

Claude Desktop — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "whatsapp.mcp_server"],
      "cwd": "/absolute/path/to/ProjectTest",
      "env": {
        "WHATSAPP_ACCESS_TOKEN": "...",
        "WHATSAPP_PHONE_NUMBER_ID": "...",
        "WHATSAPP_APP_SECRET": "...",
        "WHATSAPP_VERIFY_TOKEN": "..."
      }
    }
  }
}
```

Paths must be absolute. `cwd` matters: `WHATSAPP_DB_PATH` defaults to a
relative path, so the MCP server must start in the same directory as the
webhook or the two will use different databases.

Then ask Claude: *"What WhatsApp messages came in today?"*

## Design notes

**Signatures are checked against raw bytes.** `X-Hub-Signature-256` is an
HMAC over the exact body received. Re-serialising parsed JSON changes key
order and whitespace and will never match, so `verify_signature` runs before
anything parses the payload. Unsigned requests are rejected with 403.

**Webhook processing failures still answer 200.** Meta redelivers on any
non-200 and disables a subscription that keeps failing, so a malformed
payload is logged and swallowed rather than returned as a 5xx.

**Delivery is idempotent.** Redelivery is normal; message IDs are the primary
key and re-inserting is a no-op.

**Phone numbers are normalised to digits.** `+1 (555) 000-1111` and the
`15550001111` a webhook reports are the same thread.

**Status updates for unknown messages create placeholder rows,** so messages
a colleague sent from the WhatsApp Manager UI still appear in the thread.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

91 tests, no network access required — the Cloud API is mocked with `respx`.
Coverage includes signature forgery and tampering, the verification
handshake, webhook idempotency, every inbound message type, the 24-hour
window in both states, and the two-step media download.

## Limitations

- **History starts when the webhook does.** The Cloud API has no endpoint for
  fetching past conversations; Meta only pushes messages as they arrive.
  Nothing sent before this bridge was running can be recovered.
- **Single phone number** per deployment.
- **SQLite** suits one process writing and occasional reads. High volume
  wants Postgres — `ConversationStore` is the only class that would change.
- Sends are **not queued or retried**; a failed send surfaces its error to
  the caller.
