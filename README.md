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

## Coexistence: keep using the WhatsApp Business app

Registering a number with the Cloud API normally takes it over -- the app
stops working on it. **Coexistence** is Meta's alternative: the same number
stays live in the WhatsApp Business app *and* on the Cloud API, with messages
mirrored both ways. It rolled out from May 2025 and is now available
worldwide.

That is usually what you want when the number is already in daily use. You
carry on replying from your phone, and the bridge sees everything.

Under Coexistence two extra webhook fields carry the app's activity, and both
must be subscribed alongside `messages`:

| Field | Carries |
|---|---|
| `smb_message_echoes` | messages you send from the phone app |
| `history` | the backfill of conversations that predate onboarding |

Both are parsed into the same conversation store as ordinary messages, with
the right direction, so a thread reads as a conversation rather than only the
customer's half. History arrives in chunks that may be delivered out of
order; ingestion is keyed on message ID, so repeats and reordering are safe.

Known limits of Coexistence, which are Meta's rather than this bridge's:

- Throughput is capped at 5 messages per second.
- Official Business Account status (the blue badge) is not available.
- Messages sent from some companion clients, such as WhatsApp for Windows,
  are not echoed and so cannot be mirrored.

### Connecting a number that is already in the app

Coexistence onboarding runs through Meta's Embedded Signup, a JavaScript
flow rather than a dashboard button, and it must be served over HTTPS from a
domain registered on the app's Facebook Login for Business configuration.
The tunnel fronting the webhook is already such a domain, so the flow is
served from the webhook itself at `/onboard`.

Set `META_APP_ID` and `META_CONFIG_ID`, then open
`https://<your-tunnel>/onboard` and follow the flow. It asks you to scan a
QR code from the WhatsApp Business app, and returns the phone number ID and
WABA ID to paste into `.env`.

Subscribe `history` and `smb_message_echoes` **before** onboarding. History
is sent once, in the minutes after it succeeds.

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

`run_mcp.sh` is the entry point for both clients below. It anchors the
working directory to the repository before starting the server, because
Claude launches MCP servers from an arbitrary directory while `.env`
discovery and the default relative `WHATSAPP_DB_PATH` resolve against the
current one. Registering the bare interpreter path instead either fails to
import the package or silently reads an empty database.

Claude Code:

```bash
claude mcp add whatsapp -- /absolute/path/to/ProjectTest/run_mcp.sh
```

Claude Desktop — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "/absolute/path/to/ProjectTest/run_mcp.sh"
    }
  }
}
```

The path must be absolute. The script handles the working directory, so no
`cwd` or `env` block is needed -- credentials come from the `.env` beside it.
On macOS the config file lives at
`~/Library/Application Support/Claude/claude_desktop_config.json`, and Claude
must be fully quit and reopened for a change to take effect.

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

98 tests, no network access required — the Cloud API is mocked with `respx`.
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
