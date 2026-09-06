"""The Coexistence onboarding page.

Connecting a number that is already live in the WhatsApp Business app goes
through Meta's Embedded Signup, a JavaScript flow rather than a dashboard
button. It must be served over HTTPS from a domain registered on the
Facebook Login for Business configuration -- which the webhook's own tunnel
already provides, so it is served from here rather than hosted separately.
"""

from __future__ import annotations

from .config import Settings

#: Meta returns the onboarding result by posting a message to the window that
#: launched the flow, so the page has to stay open to receive it.
_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Connect WhatsApp Business</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif;
         max-width: 40rem; margin: 3rem auto; padding: 0 1.5rem;
         line-height: 1.6; color: #1a1a1a; }}
  button {{ background: #1877f2; color: #fff; border: 0; border-radius: 6px;
           padding: 0.9rem 1.6rem; font-size: 1rem; cursor: pointer; }}
  button:disabled {{ background: #999; cursor: not-allowed; }}
  pre {{ background: #f4f4f5; padding: 1rem; border-radius: 6px;
        overflow-x: auto; font-size: 0.85rem; }}
  .result {{ border-left: 4px solid #16a34a; padding-left: 1rem; }}
  .error {{ border-left: 4px solid #dc2626; padding-left: 1rem; }}
  code {{ background: #f4f4f5; padding: 0.1rem 0.35rem; border-radius: 3px; }}
</style>
</head>
<body>
<h1>Connect your WhatsApp Business number</h1>
<p>This keeps the number working in the WhatsApp Business app on your phone
while also connecting it to the Cloud API. You will be asked to scan a QR
code from the app.</p>
<p><strong>Before you start:</strong> subscribe the <code>messages</code>,
<code>history</code> and <code>smb_message_echoes</code> webhook fields.
History is sent once, shortly after onboarding succeeds.</p>

<p><button id="go">Start onboarding</button></p>
<div id="out"></div>

<script>
  const APP_ID = "{app_id}";
  const CONFIG_ID = "{config_id}";
  const out = document.getElementById("out");

  function show(html, cls) {{
    out.innerHTML = '<div class="' + cls + '">' + html + '</div>';
  }}

  window.fbAsyncInit = function () {{
    FB.init({{ appId: APP_ID, autoLogAppEvents: true,
              xfbml: true, version: "v23.0" }});
    document.getElementById("go").disabled = false;
  }};

  // Meta posts the identifiers back to this window; without this listener
  // the flow completes but the IDs are never surfaced.
  window.addEventListener("message", function (event) {{
    if (!event.origin.endsWith("facebook.com")) return;
    let payload;
    try {{ payload = JSON.parse(event.data); }} catch (e) {{ return; }}
    if (payload.type !== "WA_EMBEDDED_SIGNUP") return;

    if (payload.event === "FINISH" || payload.event === "FINISH_ONLY_WABA") {{
      const d = payload.data || {{}};
      show("<h2>Connected</h2><p>Put these in your <code>.env</code>:</p>"
        + "<pre>WHATSAPP_PHONE_NUMBER_ID=" + (d.phone_number_id || "?")
        + "\\nWABA_ID=" + (d.waba_id || "?") + "</pre>"
        + "<p>Then restart the webhook. History should arrive within a few "
        + "minutes.</p>", "result");
    }} else if (payload.event === "CANCEL") {{
      show("<p>Cancelled at step: <code>"
        + (payload.data || {{}}).current_step + "</code></p>", "error");
    }} else if (payload.event === "ERROR") {{
      show("<p>Error: " + ((payload.data || {{}}).error_message || "unknown")
        + "</p>", "error");
    }}
  }});

  document.getElementById("go").onclick = function () {{
    FB.login(function (response) {{
      if (!response.authResponse) {{
        show("<p>No response from Meta -- the flow was closed early.</p>",
             "error");
      }}
    }}, {{
      config_id: CONFIG_ID,
      response_type: "code",
      override_default_response_type: true,
      extras: {{
        setup: {{}},
        // This is what selects Coexistence rather than a fresh number.
        featureType: "whatsapp_business_app_onboarding",
        sessionInfoVersion: "3"
      }}
    }});
  }};
</script>
<script async defer crossorigin="anonymous"
        src="https://connect.facebook.net/en_US/sdk.js"></script>
</body>
</html>
"""

_NOT_CONFIGURED = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Onboarding not configured</title></head>
<body style="font-family: sans-serif; max-width: 40rem; margin: 3rem auto;">
<h1>Onboarding is not configured</h1>
<p>Set <code>META_APP_ID</code> and <code>META_CONFIG_ID</code> in
<code>.env</code> and restart.</p>
<p>The configuration ID comes from your app's
<em>Facebook Login for Business &rarr; Configurations</em>, created with the
Embedded Signup variation and the <code>whatsapp_business_management</code>
and <code>whatsapp_business_messaging</code> permissions.</p>
</body></html>
"""


def onboarding_page(settings: Settings) -> tuple[str, int]:
    """Return the onboarding HTML and its status code."""
    if not settings.can_onboard:
        return _NOT_CONFIGURED, 503
    return (
        _PAGE.format(app_id=settings.app_id, config_id=settings.config_id),
        200,
    )
