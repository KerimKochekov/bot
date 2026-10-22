"""One Prenotami check on a hosted runner, with encrypted cross-run state."""
import hashlib
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

import visa_watcher as watcher
from visa_runtime import CREDENTIAL_KEYS, load_environment

LOG = logging.getLogger('visa.actions')


def credential_fingerprint():
    values = [os.environ.get(key, '') for key in CREDENTIAL_KEYS[:2]]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def fresh_state():
    return {'version': 1, 'notifications': {}, 'browser': None,
            'login_blocked_for': None, 'telegram_tests_sent': 0}


def load_checkpoint(path, key):
    try:
        cipher = Fernet(key.encode())
        if not path.exists():
            return fresh_state()
        state = json.loads(cipher.decrypt(path.read_bytes()))
        if (not isinstance(state, dict) or state.get('version') != 1
                or not isinstance(state.get('notifications'), dict)
                or state.get('browser') is not None and not isinstance(state['browser'], dict)
                or state.get('login_blocked_for') is not None
                and not isinstance(state['login_blocked_for'], str)
                or type(state.get('telegram_tests_sent')) is not int
                or not 0 <= state['telegram_tests_sent'] <= 2):
            raise ValueError
        return state
    except (OSError, ValueError, TypeError, InvalidToken):
        raise watcher.CheckFailed(
            'GitHub Actions cannot decrypt its saved state. No login was attempted. '
            'Check VISA_STATE_KEY and clear the visa-state cache if the key changed.') from None


def save_checkpoint(path, key, state):
    payload = Fernet(key.encode()).encrypt(json.dumps(state).encode())
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(payload)
    temporary.chmod(0o600)
    temporary.replace(path)


def official_browser_state(state):
    """Retain session data only for the official service and identity provider."""
    hosts = {'prenotami.esteri.it', 'iam.esteri.it'}
    return {
        'cookies': [cookie for cookie in state.get('cookies', [])
                    if cookie.get('domain', '').lstrip('.') in hosts | {'esteri.it'}],
        'origins': [origin for origin in state.get('origins', [])
                    if urlparse(origin.get('origin', '')).hostname in hosts],
    }


def hosted_failure(error):
    # Playwright exceptions can contain URLs, query strings and form contents.
    if isinstance(error, watcher.AuthenticationRejected):
        return watcher.CheckFailed(
            'Prenotami rejected the GitHub Actions login. Further logins are blocked '
            'until the credential secrets change or reset_login_block is selected.')
    if isinstance(error, watcher.CheckFailed):
        text = str(error)
        if 'security check' in text or 'verification code' in text:
            return watcher.CheckFailed(
                'Prenotami requires a security check or verification code on GitHub Actions. '
                'Availability is unknown; this hosted browser cannot complete the check unattended.')
        if 'login' in text.lower() or 'sign-in' in text.lower():
            return watcher.CheckFailed(
                'GitHub Actions could not finish Prenotami sign-in. Availability is unknown.')
    return watcher.CheckFailed(
        'GitHub Actions could not verify the Schengen calendar or fully-booked message. '
        'Availability is unknown. Check the workflow status.')


def check_browser(state):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        try:
            context = browser.new_context(storage_state=state['browser'],
                                          viewport={'width': 1280, 'height': 900})
            page = context.new_page()
            try:
                return watcher.check(page)
            finally:
                try:
                    state['browser'] = official_browser_state(context.storage_state(indexed_db=True))
                except Exception:
                    LOG.warning('Browser session could not be saved.')
        finally:
            try:
                browser.close()
            except Exception:
                LOG.warning('Browser cleanup did not finish.')


def perform_check(state, *, reset_login_block=False):
    fingerprint = credential_fingerprint()
    if reset_login_block or state['login_blocked_for'] != fingerprint:
        state['login_blocked_for'] = None
    if state['login_blocked_for']:
        raise watcher.AuthenticationRejected('Login remains blocked after rejected credentials.')
    try:
        return check_browser(state)
    except watcher.AuthenticationRejected:
        state['login_blocked_for'] = fingerprint
        state['browser'] = None
        raise


def output(**values):
    destination = os.environ.get('GITHUB_OUTPUT')
    if destination:
        with Path(destination).open('a') as handle:
            for name, value in values.items():
                handle.write(name + '=' + value + '\n')


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    load_environment()
    os.environ['VISA_RUNTIME_LABEL'] = 'GitHub Actions (independent of your Mac)'
    path = Path(os.environ.get('VISA_ENCRYPTED_STATE', '.visa-ci/state.enc'))
    key = os.environ.get('VISA_STATE_KEY', '')
    state = None
    result = 'unknown'
    try:
        if any(not os.environ.get(name) for name in (*CREDENTIAL_KEYS, 'VISA_STATE_KEY')):
            raise watcher.CheckFailed('Required GitHub Actions secrets are missing.')
        state = load_checkpoint(path, key)
        watcher.verify_telegram()
        if os.environ.get('VISA_SEND_TEST_MESSAGES') == 'true':
            while state['telegram_tests_sent'] < 2:
                number = state['telegram_tests_sent'] + 1
                watcher.notify(f'Visa checker hosted test {number}/2: Telegram is awake. '
                               'This message was sent by GitHub Actions without your Mac.')
                state['telegram_tests_sent'] = number
                save_checkpoint(path, key, state)
        result = perform_check(state, reset_login_block=(
            os.environ.get('VISA_RESET_LOGIN_BLOCK') == 'true'))
        watcher.report_result(state['notifications'], result)
    except Exception as error:
        result = 'unknown'
        safe = error if state is None and isinstance(error, watcher.CheckFailed) else hosted_failure(error)
        if state is not None:
            watcher.report_failure(state['notifications'], safe)
        else:
            LOG.error('%s', watcher.safe_failure(safe))
    finally:
        if state is not None:
            try:
                save_checkpoint(path, key, state)
                output(state_saved='true')
            except Exception:
                LOG.error('Encrypted state could not be saved; inspect the workflow status.')
                result = 'unknown'
        output(availability=result)
    LOG.info('Verified availability: %s', result)
    return 0 if result in {'booked', 'calendar_open'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
