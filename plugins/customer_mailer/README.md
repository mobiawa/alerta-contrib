Customer Mailer Plugin
======================

Alerta plugin that emails every user assigned to an alert's customer
when the alert is created. Subsequent duplicate hits within the alert's
timeout window do not generate further emails.

Install
-------

    pip install git+https://github.com/alerta/alerta-contrib.git#subdirectory=plugins/customer_mailer

Add to `PLUGINS` in `alertad.conf`:

    PLUGINS = ['reject', 'blackout', 'customer_mailer']

Plugin-specific configuration
-----------------------------

| Key                            | Default        | Purpose |
|--------------------------------|----------------|---------|
| `CUSTOMER_MAILER_ENVIRONMENTS` | *(unset = all)* | Comma-separated list of environments that trigger emails (e.g. `Production,Staging`). Alerts in any other environment are silently skipped. Use this to exclude internal/test environments. |

Standard SMTP / mail settings (`SMTP_HOST`, `SMTP_PORT`, `SMTP_STARTTLS`,
`SMTP_USE_SSL`, `SSL_KEY_FILE`, `SSL_CERT_FILE`, `SMTP_USERNAME`,
`SMTP_PASSWORD`, `SMTP_DEBUG`, `MAIL_FROM`, `MAIL_LOCALHOST`,
`EMAIL_TYPE`) follow the same names and semantics as the
[alerta-contrib mailer integration](../../integrations/mailer).
`EMAIL_TYPE` is `text` (text-only) or `html` (multipart text + html).

The click-through link in emails is built from alerta's own `BASE_URL`
config; if that is empty, it falls back to `request.url_root` (the URL
the alert was POSTed to). No plugin-specific URL setting exists.

Recipient resolution
--------------------

Given an alert with `customer="Acme"`, the plugin reads the `customers`
table and resolves every row where `customer = 'Acme'`. The `match`
column is interpreted in three priority tiers (the same precedence
Alerta uses at login):

1. **User login or email** — `match='alice@acme.com'` → user `alice@acme.com`
2. **Group name** — `match='acme-ops'` → every user in the `acme-ops` group
3. **Email domain** — `match='acme.com'` → every user whose email ends `@acme.com`

Wildcard rows (`match='*'`) are skipped — they mean "this user/group
sees all customers", not "everyone is on this customer".

Recipients matched via multiple rows are deduplicated. Users without an
`email` are silently skipped.

When a notification is sent
---------------------------

Two cases trigger an email:

1. **First creation** (`post_receive`):
   `alert.customer` is set, `alert.repeat is False`,
   `alert.duplicate_count == 0`.

2. **Re-fire after expiry** (`status_change`):
   the alert was in status `expired` and is now transitioning to any
   other status. Without this, a re-firing alert would be silently
   de-duplicated against the still-present `expired` row and the
   customer would never hear about the new incident.

Both cases additionally require `alert.environment` to be in
`CUSTOMER_MAILER_ENVIRONMENTS` (when set).

Other transitions (`open → ack`, `ack → closed`, manual re-open of a
`closed` alert, shelve/unshelve, …) do **not** trigger emails.

Templates
---------

Three Jinja2 templates are embedded in
[`alerta_customer_mailer.py`](alerta_customer_mailer.py) (`_TEMPLATES`
dict):

- `email.subject.j2`
- `email.text.j2`
- `email.html.j2`

Templates receive the alert object as `{{ alert }}`, the resolved
`{{ base_url }}`, and a pre-built `{{ alert_url }}` linking to the
alert detail page.

The base URL is resolved using alerta's own settings — plugin-side
behaviour is to use `BASE_URL` if set, otherwise `request.url_root`
(the URL the alert was POSTed to, available because `post_receive`
runs in the request context). This means click-through links work
out of the box on the alerta-web image without any extra config; set
`BASE_URL` in `alertad.conf` only when the public hostname differs
from what alerta sees on the request.

`alert_url` mirrors alerta's
[`link()`](../../../alerta/alerta/auth/utils.py) helper: a base URL
ending with `/` produces hashbang-mode `https://example.com/#/alert/<id>`
(the alerta SPA route), otherwise html5-mode
`https://example.com/alert/<id>`.

To customize, edit the strings in the module and rebuild the image. For
per-customer branding use Jinja conditionals:

    {% if alert.customer == "Acme" %}
      <img src="https://acme.com/logo.png">
    {% endif %}

Caveats
-------

- **Synchronous SMTP.** Email is sent inline in `post_receive`. For
  installs ingesting hundreds of alerts per second, route through the
  [amqp plugin](../amqp) and consume in a sidecar instead.
- **Domain `match` must omit the leading `@`.** Store `acme.com`, not
  `@acme.com`.

Testing
-------

    cd alerta-contrib/plugins/customer_mailer
    python -m unittest test_customer_mailer.py -v

License
-------

MIT.
