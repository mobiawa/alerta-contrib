import contextlib
import os
import sys
import types
import unittest
from unittest import mock


# ---------------------------------------------------------------------------
# Stub the alerta.* imports so the plugin can be imported without a running
# Alerta server. The plugin only uses these names as references — every test
# patches them on the plugin module directly.
# ---------------------------------------------------------------------------

def _install_alerta_stubs():
    if 'alerta' in sys.modules:
        return

    alerta = types.ModuleType('alerta')
    alerta_models = types.ModuleType('alerta.models')
    alerta_plugins = types.ModuleType('alerta.plugins')

    customer_mod = types.ModuleType('alerta.models.customer')
    user_mod = types.ModuleType('alerta.models.user')
    group_mod = types.ModuleType('alerta.models.group')

    class _Customer:
        @staticmethod
        def find_all():
            return []

    class _User:
        @staticmethod
        def find_by_username(_):
            return None

        @staticmethod
        def find_by_email(_):
            return None

        @staticmethod
        def find_by_id(_):
            return None

        @staticmethod
        def find_all():
            return []

    class _Group:
        @staticmethod
        def find_all():
            return []

    class _GroupUsers:
        @staticmethod
        def find_by_id(_):
            return []

    class _PluginBase:
        def __init__(self, name=None):
            self.name = name or self.__module__

        @staticmethod
        def get_config(key, default=None, type=None, **kwargs):
            if key in os.environ:
                rv = os.environ[key]
                if type == bool:
                    return rv.lower() in ['yes', 'on', 'true', 't', '1']
                if type == list:
                    return rv.split(',')
                if type is not None:
                    try:
                        rv = type(rv)
                    except ValueError:
                        rv = default
                return rv
            try:
                return kwargs['config'].get(key, default)
            except KeyError:
                return default

    customer_mod.Customer = _Customer
    user_mod.User = _User
    group_mod.Group = _Group
    group_mod.GroupUsers = _GroupUsers
    alerta_plugins.PluginBase = _PluginBase

    sys.modules['alerta'] = alerta
    sys.modules['alerta.models'] = alerta_models
    sys.modules['alerta.models.customer'] = customer_mod
    sys.modules['alerta.models.user'] = user_mod
    sys.modules['alerta.models.group'] = group_mod
    sys.modules['alerta.plugins'] = alerta_plugins


_install_alerta_stubs()

import alerta_customer_mailer as mod  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def mod_env(**update):
    env = os.environ
    saved = {k: env.get(k) for k in update}
    env.update({k: v for k, v in update.items() if v is not None})
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v


def make_alert(**overrides):
    base = dict(
        id='abc-123', customer='Acme', repeat=False, duplicate_count=0,
        resource='db1', event='down', environment='Production', severity='critical',
        status='open', service=['db'], group='Misc', value='1', text='it died',
        tags=['x', 'y'], origin='probe', create_time='2026-04-27T10:00:00Z',
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def cust_row(match, customer='Acme'):
    return types.SimpleNamespace(match=match, customer=customer)


def user_obj(login=None, email=None, user_id=None):
    domain = email.split('@')[1] if email and '@' in email else None
    return types.SimpleNamespace(
        id=user_id or login or email,
        login=login,
        email=email,
        domain=domain,
    )


def group_obj(group_id, name):
    return types.SimpleNamespace(id=group_id, name=name)


# ---------------------------------------------------------------------------
# Trigger gating tests
# ---------------------------------------------------------------------------

class TriggerGatingTest(unittest.TestCase):

    def setUp(self):
        self.plugin = mod.CustomerMailer()

    def test_skips_when_no_customer(self):
        with mock.patch.object(self.plugin, '_send') as send:
            self.plugin.post_receive(make_alert(customer=None), config={})
            send.assert_not_called()

    def test_skips_when_repeat(self):
        with mock.patch.object(self.plugin, '_send') as send:
            self.plugin.post_receive(make_alert(repeat=True), config={})
            send.assert_not_called()

    def test_skips_when_duplicate_count_positive(self):
        with mock.patch.object(self.plugin, '_send') as send:
            self.plugin.post_receive(make_alert(duplicate_count=3), config={})
            send.assert_not_called()

    def test_skips_when_environment_not_in_allow_list(self):
        with mock.patch.object(self.plugin, '_send') as send:
            self.plugin.post_receive(
                make_alert(environment='Development'),
                config={'CUSTOMER_MAILER_ENVIRONMENTS': ['Production', 'Staging']},
            )
            send.assert_not_called()

    def test_sends_when_environment_in_allow_list(self):
        with mock.patch.object(mod.Customer, 'find_all',
                               return_value=[cust_row('alice@acme.com')]), \
             mock.patch.object(mod.User, 'find_by_username',
                               return_value=user_obj(login='alice@acme.com',
                                                     email='alice@acme.com')), \
             mock.patch.object(self.plugin, '_send') as send:
            self.plugin.post_receive(
                make_alert(environment='Production'),
                config={'CUSTOMER_MAILER_ENVIRONMENTS': ['Production', 'Staging']},
            )
            send.assert_called_once()

    def test_environment_allow_list_via_env_var(self):
        # Env var (comma-separated string) overrides config; only Production allowed.
        with mock.patch.object(self.plugin, '_send') as send, \
             mod_env(CUSTOMER_MAILER_ENVIRONMENTS='Production,Staging'):
            self.plugin.post_receive(make_alert(environment='Development'),
                                     config={})
            send.assert_not_called()

    def test_sends_on_first_creation(self):
        with mock.patch.object(mod.Customer, 'find_all',
                               return_value=[cust_row('alice@acme.com')]), \
             mock.patch.object(mod.User, 'find_by_username',
                               return_value=user_obj(login='alice@acme.com',
                                                     email='alice@acme.com')), \
             mock.patch.object(self.plugin, '_send') as send:
            self.plugin.post_receive(make_alert(), config={})
            send.assert_called_once()
            _, recipients, _ = send.call_args[0]
            self.assertEqual(recipients, ['alice@acme.com'])


# ---------------------------------------------------------------------------
# status_change re-notification tests (expired → open)
# ---------------------------------------------------------------------------

class StatusChangeReopenTest(unittest.TestCase):

    def setUp(self):
        self.plugin = mod.CustomerMailer()

    def test_fires_on_expired_to_open(self):
        with mock.patch.object(self.plugin, '_send') as send, \
             mock.patch.object(mod.Customer, 'find_all',
                               return_value=[cust_row('alice@acme.com')]), \
             mock.patch.object(mod.User, 'find_by_username',
                               return_value=user_obj(login='alice@acme.com',
                                                     email='alice@acme.com')):
            self.plugin.status_change(
                make_alert(status='expired'), status='open', text='re-fired',
                config={},
            )
            send.assert_called_once()

    def test_does_not_fire_on_open_to_ack(self):
        with mock.patch.object(self.plugin, '_send') as send:
            self.plugin.status_change(
                make_alert(status='open'), status='ack', text='ack',
                config={},
            )
            send.assert_not_called()

    def test_does_not_fire_on_expired_to_expired(self):
        # No-op transitions shouldn't notify.
        with mock.patch.object(self.plugin, '_send') as send:
            self.plugin.status_change(
                make_alert(status='expired'), status='expired', text='',
                config={},
            )
            send.assert_not_called()

    def test_does_not_fire_on_closed_to_open(self):
        # Only `expired` → other transitions are treated as a new incident.
        # User-driven re-opens of closed alerts don't notify.
        with mock.patch.object(self.plugin, '_send') as send:
            self.plugin.status_change(
                make_alert(status='closed'), status='open', text='re-opened',
                config={},
            )
            send.assert_not_called()

    def test_expired_to_open_respects_environment_filter(self):
        with mock.patch.object(self.plugin, '_send') as send, \
             mod_env(CUSTOMER_MAILER_ENVIRONMENTS='Production'):
            self.plugin.status_change(
                make_alert(status='expired', environment='Development'),
                status='open', text='re-fired',
                config={},
            )
            send.assert_not_called()

    def test_expired_to_open_skips_when_no_customer(self):
        with mock.patch.object(self.plugin, '_send') as send:
            self.plugin.status_change(
                make_alert(status='expired', customer=None),
                status='open', text='re-fired',
                config={},
            )
            send.assert_not_called()


# ---------------------------------------------------------------------------
# Recipient resolution tests
# ---------------------------------------------------------------------------

class RecipientResolutionTest(unittest.TestCase):

    def test_resolves_login_match(self):
        with mock.patch.object(mod.Customer, 'find_all',
                               return_value=[cust_row('alice@acme.com')]), \
             mock.patch.object(mod.User, 'find_by_username',
                               return_value=user_obj(login='alice@acme.com',
                                                     email='alice@acme.com')), \
             mock.patch.object(mod.User, 'find_by_email', return_value=None):
            self.assertEqual(
                mod.CustomerMailer._recipients_for('Acme'),
                ['alice@acme.com'],
            )

    def test_resolves_group_match(self):
        bob = user_obj(user_id='u-bob', login='bob', email='bob@acme.com')
        carol = user_obj(user_id='u-carol', login='carol', email='carol@acme.com')

        users_by_id = {bob.id: bob, carol.id: carol}

        with mock.patch.object(mod.Customer, 'find_all',
                               return_value=[cust_row('acme-ops')]), \
             mock.patch.object(mod.User, 'find_by_username', return_value=None), \
             mock.patch.object(mod.User, 'find_by_email', return_value=None), \
             mock.patch.object(mod.Group, 'find_all',
                               return_value=[group_obj('g1', 'acme-ops')]), \
             mock.patch.object(mod.GroupUsers, 'find_by_id',
                               return_value=[bob, carol]), \
             mock.patch.object(mod.User, 'find_by_id',
                               side_effect=lambda i: users_by_id.get(i)):
            self.assertEqual(
                mod.CustomerMailer._recipients_for('Acme'),
                ['bob@acme.com', 'carol@acme.com'],
            )

    def test_resolves_domain_match(self):
        users = [
            user_obj(login='bob', email='bob@acme.com'),
            user_obj(login='carol', email='carol@ACME.com'),  # case mismatch
            user_obj(login='dave', email='dave@other.com'),
            user_obj(login='nomail', email=None),
        ]
        with mock.patch.object(mod.Customer, 'find_all',
                               return_value=[cust_row('Acme.COM')]), \
             mock.patch.object(mod.User, 'find_by_username', return_value=None), \
             mock.patch.object(mod.User, 'find_by_email', return_value=None), \
             mock.patch.object(mod.Group, 'find_all', return_value=[]), \
             mock.patch.object(mod.User, 'find_all', return_value=users):
            self.assertEqual(
                mod.CustomerMailer._recipients_for('Acme'),
                ['bob@acme.com', 'carol@ACME.com'],
            )

    def test_skips_wildcard_match(self):
        with mock.patch.object(mod.Customer, 'find_all',
                               return_value=[cust_row('*'), cust_row(None)]):
            self.assertEqual(mod.CustomerMailer._recipients_for('Acme'), [])

    def test_skips_users_without_email(self):
        users = [
            user_obj(user_id='u1', login='bob', email='bob@acme.com'),
            user_obj(user_id='u2', login='ghost', email=None),
        ]
        users_by_id = {u.id: u for u in users}

        with mock.patch.object(mod.Customer, 'find_all',
                               return_value=[cust_row('acme-ops')]), \
             mock.patch.object(mod.User, 'find_by_username', return_value=None), \
             mock.patch.object(mod.User, 'find_by_email', return_value=None), \
             mock.patch.object(mod.Group, 'find_all',
                               return_value=[group_obj('g1', 'acme-ops')]), \
             mock.patch.object(mod.GroupUsers, 'find_by_id', return_value=users), \
             mock.patch.object(mod.User, 'find_by_id',
                               side_effect=lambda i: users_by_id.get(i)):
            self.assertEqual(
                mod.CustomerMailer._recipients_for('Acme'),
                ['bob@acme.com'],
            )

    def test_dedupes_across_match_types(self):
        # alice matches both a login row AND the domain row
        rows = [cust_row('alice@acme.com'), cust_row('acme.com')]
        alice = user_obj(login='alice@acme.com', email='alice@acme.com')
        bob = user_obj(login='bob', email='bob@acme.com')

        # find_by_username only resolves alice's login
        def _by_username(m):
            return alice if m == 'alice@acme.com' else None

        with mock.patch.object(mod.Customer, 'find_all', return_value=rows), \
             mock.patch.object(mod.User, 'find_by_username',
                               side_effect=_by_username), \
             mock.patch.object(mod.User, 'find_by_email', return_value=None), \
             mock.patch.object(mod.Group, 'find_all', return_value=[]), \
             mock.patch.object(mod.User, 'find_all', return_value=[alice, bob]):
            self.assertEqual(
                mod.CustomerMailer._recipients_for('Acme'),
                ['alice@acme.com', 'bob@acme.com'],
            )


# ---------------------------------------------------------------------------
# SMTP sending tests
# ---------------------------------------------------------------------------

class SmtpSendTest(unittest.TestCase):

    def setUp(self):
        self.plugin = mod.CustomerMailer()

    def _patch_recipients(self, recipients):
        return mock.patch.object(
            mod.CustomerMailer, '_recipients_for', return_value=recipients,
        )

    def test_smtp_failure_is_swallowed(self):
        with self._patch_recipients(['alice@acme.com']), \
             mock.patch.object(mod.smtplib, 'SMTP',
                               side_effect=RuntimeError('boom')):
            # Should not raise.
            self.plugin.post_receive(make_alert(), config={})

    def test_smtp_call_args(self):
        with self._patch_recipients(['alice@acme.com', 'bob@acme.com']), \
             mock.patch.object(mod.smtplib, 'SMTP') as smtp_cls:
            server = smtp_cls.return_value
            self.plugin.post_receive(
                make_alert(severity='critical', resource='db1', event='down'),
                config={'MAIL_FROM': 'alerta@example.com'},
            )

            smtp_cls.assert_called_once()
            args, _ = smtp_cls.call_args
            self.assertEqual(args[0], 'localhost')
            self.assertEqual(args[1], 25)

            server.ehlo.assert_called()
            server.sendmail.assert_called_once()
            from_addr, to_addrs, message = server.sendmail.call_args[0]
            self.assertEqual(from_addr, 'alerta@example.com')
            self.assertEqual(to_addrs, ['alice@acme.com', 'bob@acme.com'])
            self.assertIn('From: alerta@example.com', message)
            self.assertIn('To: alice@acme.com, bob@acme.com', message)
            # Subject is q-encoded utf-8; alert tokens appear literally inside.
            self.assertIn('Acme', message)
            self.assertIn('db1', message)
            self.assertIn('down', message)
            self.assertIn('CRITICAL', message)
            self.assertIn('multipart/alternative', message)
            server.close.assert_called_once()

    def test_starttls_and_login_invoked_when_configured(self):
        with self._patch_recipients(['alice@acme.com']), \
             mock.patch.object(mod.smtplib, 'SMTP') as smtp_cls, \
             mod_env(SMTP_STARTTLS='True', SMTP_USERNAME='u', SMTP_PASSWORD='p'):
            server = smtp_cls.return_value
            self.plugin.post_receive(make_alert(), config={})
            server.starttls.assert_called_once()
            server.login.assert_called_once_with('u', 'p')

    def test_login_uses_mail_from_when_username_omitted(self):
        # Mirrors mailer integration: smtp_username defaults to mail_from.
        with self._patch_recipients(['alice@acme.com']), \
             mock.patch.object(mod.smtplib, 'SMTP') as smtp_cls, \
             mod_env(SMTP_PASSWORD='p'):
            server = smtp_cls.return_value
            self.plugin.post_receive(
                make_alert(),
                config={'MAIL_FROM': 'alerta@example.com'},
            )
            server.login.assert_called_once_with('alerta@example.com', 'p')

    def test_no_login_when_password_missing(self):
        with self._patch_recipients(['alice@acme.com']), \
             mock.patch.object(mod.smtplib, 'SMTP') as smtp_cls:
            server = smtp_cls.return_value
            self.plugin.post_receive(make_alert(), config={})
            server.login.assert_not_called()

    def test_smtp_ssl_used_when_configured(self):
        with self._patch_recipients(['alice@acme.com']), \
             mock.patch.object(mod.smtplib, 'SMTP_SSL') as ssl_cls, \
             mock.patch.object(mod.smtplib, 'SMTP') as plain_cls, \
             mod_env(SMTP_USE_SSL='True'):
            self.plugin.post_receive(make_alert(), config={})
            ssl_cls.assert_called_once()
            plain_cls.assert_not_called()
            # SSL_KEY_FILE / SSL_CERT_FILE forwarded as keyword args
            _, ssl_kwargs = ssl_cls.call_args
            self.assertIn('keyfile', ssl_kwargs)
            self.assertIn('certfile', ssl_kwargs)

    def test_html_email_attaches_both_parts(self):
        with self._patch_recipients(['alice@acme.com']), \
             mock.patch.object(mod.smtplib, 'SMTP') as smtp_cls, \
             mod_env(EMAIL_TYPE='html'):
            server = smtp_cls.return_value
            self.plugin.post_receive(make_alert(), config={})
            _, _, message = server.sendmail.call_args[0]
            # multipart/alternative with both text and html sections.
            self.assertIn('multipart/alternative', message)
            self.assertIn('text/plain', message)
            self.assertIn('text/html', message)


# ---------------------------------------------------------------------------
# URL builder tests
# ---------------------------------------------------------------------------

class AlertUrlTest(unittest.TestCase):

    def test_html5_mode_no_trailing_slash(self):
        self.assertEqual(
            mod._build_alert_url('https://alerta.example.com', 'abc-123'),
            'https://alerta.example.com/alert/abc-123',
        )

    def test_hashbang_mode_trailing_slash(self):
        self.assertEqual(
            mod._build_alert_url('https://alerta.example.com/', 'abc-123'),
            'https://alerta.example.com/#/alert/abc-123',
        )

    def test_empty_base_returns_empty(self):
        self.assertEqual(mod._build_alert_url('', 'abc-123'), '')
        self.assertEqual(mod._build_alert_url(None, 'abc-123'), '')

    def test_empty_alert_id_returns_empty(self):
        self.assertEqual(
            mod._build_alert_url('https://alerta.example.com', ''),
            '',
        )

    def test_resolve_base_url_uses_alerta_base_url(self):
        fake_app = mock.MagicMock()
        fake_app.config.get.return_value = 'https://alerta.example.com'
        with mock.patch.dict('sys.modules', {'flask': mock.MagicMock(
                current_app=fake_app, request=mock.MagicMock(url_root='unused/'))}):
            self.assertEqual(
                mod._resolve_base_url(),
                'https://alerta.example.com',
            )

    def test_resolve_base_url_falls_back_to_request_url_root(self):
        fake_app = mock.MagicMock()
        fake_app.config.get.return_value = ''  # alerta BASE_URL not set
        fake_request = mock.MagicMock()
        fake_request.url_root = 'https://api.example.com/'
        with mock.patch.dict('sys.modules', {'flask': mock.MagicMock(
                current_app=fake_app, request=fake_request)}):
            self.assertEqual(
                mod._resolve_base_url(),
                'https://api.example.com/',
            )

    def test_resolve_base_url_returns_empty_when_no_context(self):
        # Outside flask request context, both lookups raise — return empty.
        broken_flask = mock.MagicMock()
        type(broken_flask).current_app = mock.PropertyMock(
            side_effect=RuntimeError('no app'))
        type(broken_flask).request = mock.PropertyMock(
            side_effect=RuntimeError('no request'))
        with mock.patch.dict('sys.modules', {'flask': broken_flask}):
            self.assertEqual(mod._resolve_base_url(), '')

    def test_alert_url_appears_in_message(self):
        plugin = mod.CustomerMailer()
        with mock.patch.object(mod.CustomerMailer, '_recipients_for',
                               return_value=['alice@acme.com']), \
             mock.patch.object(mod, '_resolve_base_url',
                               return_value='https://alerta.example.com/'), \
             mock.patch.object(mod.smtplib, 'SMTP') as smtp_cls:
            server = smtp_cls.return_value
            plugin.post_receive(make_alert(id='abc-123'), config={})
            _, _, message = server.sendmail.call_args[0]
            # html5/hashbang both include 'alert/abc-123'; trailing-slash base
            # produces hashbang form. utf-8 base64-encoded bodies obscure the
            # token, so verify by decoding the multipart payload below.

        # Decode the base64-encoded text part to assert the URL is rendered.
        import base64
        import re
        # Pull each base64 chunk between MIME boundaries.
        chunks = re.findall(
            r'Content-Transfer-Encoding: base64\n\n([A-Za-z0-9+/=\n]+?)\n--',
            message,
        )
        decoded = b''.join(base64.b64decode(c) for c in chunks).decode('utf-8')
        self.assertIn('https://alerta.example.com/#/alert/abc-123', decoded)


if __name__ == '__main__':
    unittest.main()
