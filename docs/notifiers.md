# Notifiers

The notifier layer is a fan-out of channels, each delivering a single
`NotifyEvent` (`src/notifiers/base.py`) through a different transport.
`MultiplexNotifier` (`src/notifiers/multiplex.py`) is the entry point
the bot pushes events through.

## Channel matrix

| Channel | Source | Transport | Retries | Best-effort? | Validation |
| --- | --- | --- | --- | --- | --- |
| `desktop` | `src/notifiers/desktop.py` | `plyer` OS toast + terminal bell | none | yes (logged on failure) | `io.desktop-notify-called` |
| `webhook` | `src/notifiers/webhook.py` | `httpx.AsyncClient.post(...)` JSON | 3 attempts, exp backoff | no — raises on terminal error | `io.webhook-real-post`, `io.webhook-payload-shape` |
| `discord` | `src/notifiers/discord.py` | Discord incoming webhook POST | 3 attempts, exp backoff | no | `io.discord-real-post`, `io.discord-payload-shape` |
| `telegram` | `src/notifiers/telegram.py` | Telegram Bot API `sendMessage` POST | 3 attempts, exp backoff | no | `io.telegram-real-post`, `io.telegram-payload-shape` |
| `slack` | `src/notifiers/slack.py` | Slack incoming webhook POST + body check | 3 attempts, exp backoff (transport only); body errors are terminal | no | `io.slack-deferred` |
| `multiplex` | `src/notifiers/multiplex.py` | Fan-out across the above | n/a (each child carries its own) | aggregate is best-effort: never raises | `io.multiplex-fanout-routing`, `io.multiplex-isolated-failures` |

`Notifier` is the ABC in `src/notifiers/base.py`. Every concrete class
above subclasses it directly or via `WebhookNotifier`, which carries
the shared retry loop.

## NotifyEvent payload

The dataclass in `src/notifiers/base.py` carries five fields:

- `event_type` (string) — short logical name. Routing keys off this.
  Examples: `cart_success`, `checkout_failure`, `sold_out`,
  `not_on_sale`.
- `title` — human-readable headline.
- `message` — body text.
- `severity` — `"info"`, `"warning"`, or `"error"`. Channels map this
  to colours / icons / log levels.
- `metadata` — arbitrary structured context (account name, section,
  price, …). HTTP channels usually serialise this under a `metadata`
  key on the outbound payload.

## Channel payload shapes

### Webhook (`src/notifiers/webhook.py`)

`WebhookNotifier.build_payload` emits a stable JSON envelope. Any
generic consumer can react to this shape:

```json
{
  "event": "cart_success",
  "title": "Tickets in cart",
  "message": "2x section 100",
  "severity": "info",
  "timestamp_iso": "2025-01-01T12:34:56.789Z",
  "metadata": {
    "account": "alice",
    "section": "100"
  }
}
```

The `Content-Type` header is forced to `application/json` even if the
caller passes a different value in `headers=...`.

### Discord (`src/notifiers/discord.py`)

`DiscordNotifier.build_payload` extends the webhook shape into
Discord's documented format:

```json
{
  "content": "Tickets in cart: 2x section 100",
  "embeds": [
    {
      "title": "Tickets in cart",
      "description": "2x section 100",
      "color": 65280,
      "timestamp": "2025-01-01T12:34:56.789012+00:00",
      "fields": [
        {"name": "account", "value": "alice", "inline": true}
      ]
    }
  ]
}
```

Severity → embed colour map (constants in
`src/notifiers/discord.py`): `info` → cyan, `success` → green,
`warning` → amber, `error` → red, anything else → Discord's neutral
grey. The `username` and `avatar_url` constructor kwargs are forwarded
verbatim when set.

### Telegram (`src/notifiers/telegram.py`)

`TelegramNotifier.build_payload` posts to
`https://api.telegram.org/bot<token>/sendMessage`:

```json
{
  "chat_id": "-1001234567890",
  "text": "*Tickets in cart*\n\n2x section 100",
  "parse_mode": "MarkdownV2"
}
```

Every Telegram MarkdownV2 reserved character (see
`MARKDOWN_V2_SPECIAL_CHARS` in `src/notifiers/telegram.py`) is
backslash-escaped by `escape_markdown_v2` before substitution so user
content cannot break the surrounding bold formatting.

### Slack (`src/notifiers/slack.py`)

`SlackNotifier.build_payload` emits the documented Block Kit shape:

```json
{
  "text": "Tickets in cart: 2x section 100",
  "blocks": [
    {
      "type": "section",
      "text": {"type": "mrkdwn", "text": "*Tickets in cart*\n2x section 100"}
    }
  ]
}
```

The leading `text` field is the push-notification fallback. The three
mrkdwn special characters (`&`, `<`, `>`) are HTML-escaped via
`escape_mrkdwn`. Slack's documented success body is the literal string
`"ok"`; anything else (even on HTTP 200) raises `NotifierError` —
those errors are **terminal**, so they are not retried.

### Desktop (`src/notifiers/desktop.py`)

`DesktopNotifier` logs the title at info level (the
`io.desktop-notify-called` assertion checks this) then dispatches a
`plyer.notification.notify` call inside an `asyncio.to_thread`. Native
backend errors are caught at debug level and swallowed — this is the
only channel documented as best-effort.

## Retry semantics

`WebhookNotifier.notify` is the shared retry loop. Defaults:

- `max_attempts = 3`
- `backoff_base = 1.0` second
- `backoff_max = 30.0` seconds
- Sleep between attempt `n` and `n+1` is
  `min(backoff_base * 2 ** (n - 1), backoff_max)` seconds.

Retried failure classes:

- `httpx.TimeoutException` (connect / read / write / pool).
- `httpx.NetworkError` (connection refused, DNS, etc.).
- 5xx HTTP responses.

Not retried — surfaced as `NotifierError` on the first attempt:

- Every 4xx response (Discord/Slack/Telegram return 4xx for malformed
  payloads; resending will not help).
- 1xx/3xx responses (treated as protocol violations).
- Slack body-error responses (HTTP 2xx + non-`"ok"` body) — terminal.

The validation contract assertions `io.notifier-retry-5xx` and
`io.notifier-no-retry-4xx` pin this behaviour against a real local
FastAPI test server.

## MultiplexNotifier fan-out + routing

`src/notifiers/multiplex.py` dispatches one event across multiple
channels concurrently. Each child runs in its own `asyncio.Task`;
one task's exception is caught inside `_dispatch_one` and logged
without cancelling siblings. The aggregate `notify` therefore never
raises.

Config shape (read by `MultiplexNotifier.from_config`):

```yaml
notifications:
  channels: [desktop_a, discord_main, webhook_zapier]
  routing:
    cart_success:    [discord_main, webhook_zapier]
    checkout_failure: [discord_main]
    sold_out:         [desktop_a]
  channel_configs:
    desktop_a:
      type: desktop
      desktop: true
      sound: true
    discord_main:
      type: discord
      webhook_url: "${DISCORD_WEBHOOK_URL}"
    webhook_zapier:
      type: webhook
      url: "${WEBHOOK_URL}"
```

`channels_for(event_type)` returns the routed list when present, or the
full channel set otherwise. Channels named in `routing` but missing
from `channel_configs` are skipped with a warning (never raise) so a
partially-configured fan-out keeps working.

Construction is forgiving:

- An entry in `channels` with no matching `channel_configs` entry is
  skipped with a warning.
- An entry whose `type` is missing or unknown to
  `src/registry/notifiers.py` is skipped.
- An entry whose constructor raises is skipped (the exception is
  logged via `log.exception`).

## Adding a new channel

1. Subclass `Notifier` from `src/notifiers/base.py`. Override the
   single `async def notify(self, event)` method.
2. If your transport is HTTP, subclass `WebhookNotifier` from
   `src/notifiers/webhook.py` and override `build_payload(event)` to
   emit your service's request body. You inherit the retry loop for
   free.
3. Register the class with `src/registry/notifiers.py` under a stable
   short name (e.g. `pagerduty`). Add it to `MultiplexNotifier`'s
   `channel_configs` block in `config/config.yaml`.
4. Document the channel here with its payload shape and retry caveats.

External packages may register notifiers via the
`ticketmaster_bot.notifiers` entry-point group without touching the
internal registry.
