import logging
import smtplib
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urljoin

from jinja2 import DictLoader, Environment, select_autoescape

from alerta.models.customer import Customer
from alerta.models.group import Group, GroupUsers
from alerta.models.user import User
from alerta.plugins import PluginBase

LOG = logging.getLogger('alerta.plugins.customer_mailer')

_TEMPLATES = {
    'email.subject.j2':
        'New Alert! [{{ alert.customer }}] {{ alert.severity|upper }}: '
        '{{ alert.event }} on {{ alert.resource }}\n',

    'email.text.j2': """\
A new alert has been raised for customer {{ alert.customer }}.

  Resource    : {{ alert.resource }}
  Event       : {{ alert.event }}
  Environment : {{ alert.environment }}
  Severity    : {{ alert.severity }}
  Status      : {{ alert.status }}
  Service     : {{ alert.service|join(', ') }}
  Group       : {{ alert.group }}
  Value       : {{ alert.value }}
  Text        : {{ alert.text }}
  Tags        : {{ alert.tags|join(', ') }}
  Origin      : {{ alert.origin }}
  Created     : {{ alert.create_time }}

{% if alert_url %}View: {{ alert_url }}{% endif %}
""",

    'email.html.j2': """\
<!doctype html>
<html>
  <body style="font-family: -apple-system, Segoe UI, Roboto, sans-serif; color: #222;">
    <h2 style="margin:0 0 12px 0;">
      [{{ alert.customer }}] {{ alert.severity|upper }}: {{ alert.event }}
    </h2>
    <p>A new alert has been raised for customer <strong>{{ alert.customer }}</strong>.</p>
    <table cellpadding="4" cellspacing="0" border="0" style="border-collapse:collapse;">
      <tr><td><strong>Resource</strong></td><td>{{ alert.resource }}</td></tr>
      <tr><td><strong>Event</strong></td><td>{{ alert.event }}</td></tr>
      <tr><td><strong>Environment</strong></td><td>{{ alert.environment }}</td></tr>
      <tr><td><strong>Severity</strong></td><td>{{ alert.severity }}</td></tr>
      <tr><td><strong>Status</strong></td><td>{{ alert.status }}</td></tr>
      <tr><td><strong>Service</strong></td><td>{{ alert.service|join(', ') }}</td></tr>
      <tr><td><strong>Group</strong></td><td>{{ alert.group }}</td></tr>
      <tr><td><strong>Value</strong></td><td>{{ alert.value }}</td></tr>
      <tr><td><strong>Text</strong></td><td>{{ alert.text }}</td></tr>
      <tr><td><strong>Tags</strong></td><td>{{ alert.tags|join(', ') }}</td></tr>
      <tr><td><strong>Origin</strong></td><td>{{ alert.origin }}</td></tr>
      <tr><td><strong>Created</strong></td><td>{{ alert.create_time }}</td></tr>
    </table>
    {% if alert_url %}
    <p style="margin-top:16px;">
      <a href="{{ alert_url }}">View alert in dashboard</a>
    </p>
    {% endif %}
  </body>
</html>
""",
}

_jinja = Environment(
    loader=DictLoader(_TEMPLATES),
    autoescape=select_autoescape(['html', 'xml']),
)


def _resolve_base_url():
    """Pick the public base URL the same way alerta does.

    Order: alerta's BASE_URL config → request.url_root (the URL the
    alert was POSTed to, available because post_receive runs inside
    the request context). This mirrors alerta.utils.response.absolute_url.
    """
    try:
        from flask import current_app
        configured = (current_app.config.get('BASE_URL') or '').strip()
        if configured:
            return configured
    except Exception:
        pass

    try:
        from flask import request
        return request.url_root
    except Exception:
        return ''


def _build_alert_url(base_url, alert_id):
    """Build a click-through URL to the alert detail page.

    Mirrors alerta's own link helper (alerta/auth/utils.py:link): a
    base URL ending with `/` is treated as a hashbang-mode SPA root
    (`<base>/#/alert/<id>`), otherwise html5-mode (`<base>/alert/<id>`).
    """
    if not base_url or not alert_id:
        return ''
    if base_url.endswith('/'):
        return urljoin(base_url, '/'.join(('#', 'alert', alert_id)))
    return urljoin(base_url, '/'.join(('alert', alert_id)))


class CustomerMailer(PluginBase):
    """Email every user assigned to alert.customer on first creation only."""

    def pre_receive(self, alert, **kwargs):
        return alert

    def post_receive(self, alert, **kwargs):
        if not alert.customer:
            return
        if alert.repeat or (alert.duplicate_count or 0) > 0:
            return
        self._maybe_notify(alert, kwargs)

    def status_change(self, alert, status, text, **kwargs):
        # Re-notify when an alert leaves `expired`. To the customer, an
        # expired-then-re-firing alert is a fresh incident. `alert.status`
        # is the previous status at this hook; `status` is the new one.
        if alert.status == 'expired' and status != 'expired':
            self._maybe_notify(alert, kwargs)

    def _maybe_notify(self, alert, kwargs):
        if not alert.customer:
            return

        allowed_envs = self.get_config(
            'CUSTOMER_MAILER_ENVIRONMENTS', default=None, type=list, **kwargs)
        if allowed_envs:
            allowed = {e.strip() for e in allowed_envs if e and e.strip()}
            if allowed and alert.environment not in allowed:
                LOG.debug(
                    'customer_mailer: skipping alert %s — environment %r not in %s',
                    alert.id, alert.environment, sorted(allowed))
                return

        recipients = self._recipients_for(alert.customer)
        if not recipients:
            LOG.debug('customer_mailer: no recipients for customer %s', alert.customer)
            return

        try:
            self._send(alert, recipients, kwargs)
        except Exception:
            LOG.exception('customer_mailer: failed to send for alert %s', alert.id)

    @staticmethod
    def _recipients_for(customer_name):
        emails = set()
        matches = [c.match for c in Customer.find_all()
                   if c.customer == customer_name and c.match and c.match != '*']
        if not matches:
            return []

        groups_by_name = None
        all_users = None

        for m in matches:
            u = User.find_by_username(m) or User.find_by_email(m)
            if u and u.email:
                emails.add(u.email)
                continue

            if groups_by_name is None:
                groups_by_name = {g.name: g for g in Group.find_all()}
            group = groups_by_name.get(m)
            if group:
                for gu in GroupUsers.find_by_id(group.id):
                    full = User.find_by_id(gu.id)
                    if full and full.email:
                        emails.add(full.email)
                continue

            if all_users is None:
                all_users = User.find_all()
            m_lower = m.lower()
            for user in all_users:
                if user.email and user.domain and user.domain.lower() == m_lower:
                    emails.add(user.email)

        return sorted(emails)

    def _send(self, alert, recipients, kwargs):
        # Build the message and send via SMTP. Logic mirrors the canonical
        # alerta-contrib mailer integration's _send_email_message:
        # always MIMEMultipart('alternative') with utf-8 text part and an
        # optional html part; explicit ehlo/starttls/login/sendmail/close.
        host = self.get_config('SMTP_HOST', default='localhost', **kwargs)
        port = self.get_config('SMTP_PORT', default=25, type=int, **kwargs)
        starttls = self.get_config('SMTP_STARTTLS', default=False, type=bool, **kwargs)
        use_ssl = self.get_config('SMTP_USE_SSL', default=False, type=bool, **kwargs)
        username = self.get_config('SMTP_USERNAME', **kwargs)
        password = self.get_config('SMTP_PASSWORD', **kwargs)
        ssl_key_file = self.get_config('SSL_KEY_FILE', **kwargs)
        ssl_cert_file = self.get_config('SSL_CERT_FILE', **kwargs)
        mail_from = self.get_config('MAIL_FROM', default='alerta@localhost', **kwargs)
        local_hostname = self.get_config('MAIL_LOCALHOST', **kwargs)
        email_type = self.get_config('EMAIL_TYPE', default='text', **kwargs)
        debug = self.get_config('SMTP_DEBUG', default=False, type=bool, **kwargs)

        base_url = _resolve_base_url()
        alert_url = _build_alert_url(base_url, alert.id)
        ctx = {
            'alert': alert,
            'base_url': base_url,
            'alert_url': alert_url,
        }
        subject = _jinja.get_template('email.subject.j2').render(**ctx).strip()
        text_body = _jinja.get_template('email.text.j2').render(**ctx)
        html_body = (_jinja.get_template('email.html.j2').render(**ctx)
                     if email_type == 'html' else None)

        msg = MIMEMultipart('alternative')
        msg['Subject'] = Header(subject, 'utf-8').encode()
        msg['From'] = mail_from
        msg['To'] = ', '.join(recipients)
        msg.preamble = msg['Subject']

        msg.attach(MIMEText(text_body, 'plain', 'utf-8'))
        if html_body:
            msg.attach(MIMEText(html_body, 'html', 'utf-8'))

        if use_ssl:
            mx = smtplib.SMTP_SSL(
                host, port,
                local_hostname=local_hostname,
                keyfile=ssl_key_file,
                certfile=ssl_cert_file,
            )
        else:
            mx = smtplib.SMTP(host, port, local_hostname=local_hostname)

        try:
            if debug:
                mx.set_debuglevel(True)
            mx.ehlo()
            if starttls and not use_ssl:
                mx.starttls()
                mx.ehlo()
            if password:
                mx.login(username or mail_from, password)
            mx.sendmail(mail_from, recipients, msg.as_string())
        finally:
            mx.close()

        LOG.info('customer_mailer: sent alert %s to %d recipients (customer=%s)',
                 alert.id, len(recipients), alert.customer)
