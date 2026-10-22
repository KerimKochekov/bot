"""Check Prenotami's Ashgabat Schengen service and notify a private Telegram chat."""
import argparse
import fcntl
import json
import logging
import math
import os
import re
import signal
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from visa_runtime import load_environment, telegram

ROOT = Path(os.environ.get('VISA_DATA_DIR', Path(__file__).resolve().parent)).expanduser().resolve()
BASE = 'https://prenotami.esteri.it'
INTERVAL = 15 * 60
LOGIN_TIMEOUT = 60
BOOKING_TIMEOUT = 40
TIMEZONE = ZoneInfo('Asia/Ashgabat')
STATE_FILE = ROOT / 'visa-state.json'
STATUS_FILE = ROOT / 'visa-status.json'
LOCK_FILE = ROOT / 'visa-monitor.lock'
SERVICE = 'Schengen visa to Italy and France'
LOG = logging.getLogger('visa')
BOOKED = re.compile(
    r'all appointments for this service are currently booked|'
    r'al momento non ci sono date disponibili|'
    r'tutti gli appuntamenti per questo servizio sono (?:al momento )?prenotati', re.I)
HUMAN_CHECK = re.compile(
    r'verify (?:that )?you are human|confirm you are human|'
    r'complete the (?:captcha|security check)|checking your browser|'
    r'access denied|accesso negato|unusual traffic|too many requests|'
    r'one.time (?:code|password)|verification code|codice di verifica', re.I)


class CheckFailed(RuntimeError):
    """A safe, user-facing explanation without URLs, tokens, or form contents."""


class AuthenticationRejected(CheckFailed):
    pass


def now():
    return datetime.now(TIMEZONE)


def write_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def read_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def set_status(phase, **fields):
    write_json(STATUS_FILE, {'phase': phase, 'updated_at': now().isoformat(),
                             'pid': os.getpid(), **fields})


def acquire_lock():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = LOCK_FILE.open('a+')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise CheckFailed('A visa checker is already running.') from None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()) + '\n')
    handle.flush()
    return handle


def notify(message):
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not chat_id or not os.environ.get('TELEGRAM_BOT_TOKEN'):
        raise CheckFailed('Telegram token and private chat ID must be configured in .env.')
    return telegram('sendMessage', {'chat_id': chat_id, 'text': message,
                                    'link_preview_options': {'is_disabled': True}})


def verify_telegram():
    if not os.environ.get('TELEGRAM_CHAT_ID') or not os.environ.get('TELEGRAM_BOT_TOKEN'):
        raise CheckFailed('Telegram token and private chat ID must be configured.')
    chat = telegram('getChat', {'chat_id': os.environ['TELEGRAM_CHAT_ID']})
    if chat.get('type') != 'private':
        raise CheckFailed('Visa notifications require a private Telegram chat.')


def classify(text, *, calendar=False, human_check=False, booking_flow=False):
    """Only explicit booked text or a visible booking calendar is conclusive."""
    if human_check or HUMAN_CHECK.search(text):
        return 'action_required'
    if BOOKED.search(text):
        return 'booked'
    if calendar and booking_flow:
        return 'calendar_open'
    return 'unknown'


# Read rendered calendar widgets, not hidden HTML or application state.
# A widget must show at least seven numbered dates and weekday/month labels.
CALENDAR_SCRIPT = r'''() => {
    const visible = el => !!(el.getClientRects().length &&
        getComputedStyle(el).visibility !== 'hidden');
    const labels = /monday|tuesday|wednesday|thursday|friday|saturday|sunday|\bmo\b|\btu\b|\bwe\b|\bth\b|\bfr\b|\bsa\b|\bsu\b|luned|marted|mercoled|gioved|venerd|sabato|domenica|january|february|march|april|may|june|july|august|september|october|november|december|gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre/i;
    const widgets = document.querySelectorAll(
        '#calendar, #BookingCalendar, .calendar, .ui-datepicker, .fc, '
        + '.datepicker, .bootstrap-datetimepicker-widget, [role="grid"], table');
    return [...widgets].some(el => {
        if (!visible(el)) return false;
        const text = el.innerText || '';
        const dates = text.match(/\b(?:[1-9]|[12][0-9]|3[01])\b/g) || [];
        return dates.length >= 7 && labels.test(text);
    });
}'''


def body_text(page):
    return page.locator('body').inner_text(timeout=10000)


def human_verification(page, text):
    host = urlparse(page.url).hostname or ''
    if host not in {'prenotami.esteri.it', 'iam.esteri.it'}:
        return True
    if HUMAN_CHECK.search(text):
        return True
    return page.locator(
        'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], '
        'input[autocomplete="one-time-code"], input[name*="captcha" i]'
    ).filter(visible=True).count() > 0


def safe_failure(error):
    return str(error) if isinstance(error, CheckFailed) else (
        'The website or browser did not complete the check. Availability is unknown. '
        'Inspect the checker browser and visa-status.json.')


def find_book_navigation(page):
    return page.get_by_role('link', name=re.compile(r'^(Book|Prenota)$', re.I))


def ensure_login(page):
    page.goto(BASE + '/Services', wait_until='domcontentloaded', timeout=60000)
    deadline = time.monotonic() + LOGIN_TIMEOUT
    submitted = False
    verification_pending = False
    while time.monotonic() < deadline:
        text = body_text(page)
        verification_pending = human_verification(page, text)
        if verification_pending:
            # Give normal browser scripts/redirects time to finish. Never click a
            # challenge, fill a verification code, or enter credentials here.
            page.wait_for_timeout(500)
            continue
        if page.locator('#dataTableServices').is_visible():
            # The service name is language-independent in this embassy's listing.
            return
        if re.search(r'invalid username or password|credenziali non valide', text, re.I):
            raise AuthenticationRejected('Prenotami rejected the login. Monitoring has stopped '
                                         'to avoid repeated failed logins. Update .env and restart.')
        password = page.locator('input[type="password"]').filter(visible=True)
        if password.count() and not submitted:
            username = page.get_by_role('textbox', name=re.compile(
                r'user name|username|email|e-mail', re.I))
            if username.count() != 1:
                raise CheckFailed('The sign-in form changed. Inspect the checker browser.')
            if not os.environ.get('PRENOTAMI_USERNAME') or not os.environ.get('PRENOTAMI_PASSWORD'):
                raise CheckFailed('Set PRENOTAMI_USERNAME and PRENOTAMI_PASSWORD in .env.')
            # Credentials are entered only on the official service and identity provider.
            if urlparse(page.url).hostname not in {'prenotami.esteri.it', 'iam.esteri.it'}:
                raise CheckFailed('Unexpected sign-in destination; login was not submitted.')
            username.fill(os.environ['PRENOTAMI_USERNAME'])
            password.fill(os.environ['PRENOTAMI_PASSWORD'])
            page.get_by_role('button', name=re.compile(
                r'^(Next|Sign in|Log in|Login|Accedi|Avanti)$', re.I)).click(timeout=15000)
            submitted = True
        elif page.locator('#pingid-button').is_visible():
            page.locator('#pingid-button').click(timeout=15000)
        elif urlparse(page.url).hostname == 'prenotami.esteri.it':
            navigation = find_book_navigation(page)
            if navigation.count() == 1 and navigation.is_visible():
                navigation.click(timeout=15000)
        page.wait_for_timeout(500)
    if verification_pending:
        raise CheckFailed('Prenotami requires a security check or verification code. '
                          'Complete it in the checker browser; the next check is in 15 minutes.')
    raise CheckFailed('Login or the service list did not finish loading. Availability is unknown.')


def check(page):
    ensure_login(page)
    rows = page.locator('#dataTableServices tbody tr').filter(has_text=SERVICE)
    rows.first.wait_for(state='visible', timeout=30000)
    if rows.count() != 1:
        raise CheckFailed('The Schengen visa service is missing or ambiguous.')
    book = rows.get_by_role('link', name=re.compile(r'^(Book|Prenota)$', re.I))
    if book.count() != 1:
        raise CheckFailed('The Schengen visa booking link is missing or ambiguous.')
    booking_path = urlparse(book.get_attribute('href') or '').path
    if not re.fullmatch(r'/Services/Booking/\d+', booking_path, re.I):
        raise CheckFailed('The booking link changed. Inspect the checker browser.')
    dialogs = []

    def handle_dialog(dialog):
        dialogs.append(dialog.message)
        dialog.dismiss()  # Never accept a confirmation or submit an appointment.

    page.on('dialog', handle_dialog)
    try:
        book.click(timeout=30000)
        deadline = time.monotonic() + BOOKING_TIMEOUT
        verification_pending = False
        while time.monotonic() < deadline:
            text = body_text(page) + '\n' + '\n'.join(dialogs)
            path = urlparse(page.url).path
            booking_flow = (urlparse(page.url).hostname == 'prenotami.esteri.it'
                            and bool(re.search(r'/Services/.*(?:Booking|Calendar)', path, re.I)))
            status = classify(text, human_check=human_verification(page, text),
                              calendar=page.evaluate(CALENDAR_SCRIPT), booking_flow=booking_flow)
            verification_pending = status == 'action_required'
            if status in {'booked', 'calendar_open'}:
                return status
            page.wait_for_timeout(500)
        if verification_pending:
            raise CheckFailed('Prenotami requires a security check or verification code. '
                              'Complete it in the checker browser; the next check is in 15 minutes.')
        raise CheckFailed('The booking button did not show a recognized calendar or fully-booked '
                          'message. Inspect the checker browser. Availability is unknown.')
    finally:
        page.remove_listener('dialog', handle_dialog)


def capture(page):
    directory = ROOT / 'visa-captures'
    directory.mkdir(mode=0o700, exist_ok=True)
    try:
        page.screenshot(path=str(directory / 'latest.png'), full_page=True, timeout=10000)
    except Exception:
        LOG.warning('Screenshot unavailable.')


def report_result(state, result):
    if result == 'calendar_open' and not state.get('calendar_alerted'):
        notify('Prenotami alert: the appointment calendar is now open for '
               + SERVICE + ' at the Italian Embassy in Ashgabat. '
               'Open Book → Visas → Book now: ' + BASE + '/Services\n'
               'Calendar access does not guarantee a reservable slot. Checked ' + now().strftime('%d %b %H:%M')
               + ' Asia/Ashgabat.')
        state['calendar_alerted'] = True
    if not state.get('first_result_sent'):
        summary = ('All appointments are currently booked.' if result == 'booked'
                   else 'The appointment calendar is open; see the alert above.')
        runtime = os.environ.get('VISA_RUNTIME_LABEL', 'this Mac')
        notify('Visa checker first live result: ' + summary + '\n'
               'Automatic checks run every 15 minutes on ' + runtime + '.')
        state['first_result_sent'] = True
    if result == 'booked':
        state['calendar_alerted'] = False
    if state.get('error_notified'):
        notify('Visa checker recovered and completed a check. '
               + ('Appointments are currently fully booked.' if result == 'booked'
                  else 'The appointment calendar is open.'))
    state.update(last_result=result, last_checked_at=now().isoformat(), error_notified=False,
                 last_error=None)
    write_json(STATE_FILE, state)


def report_failure(state, error):
    message = safe_failure(error)
    LOG.warning('%s [%s]', message, type(error).__name__)
    if state.get('last_error') != message or not state.get('error_notified'):
        try:
            notify('Visa checker needs attention: ' + message)
            state['last_error'] = message
            state['error_notified'] = True
        except RuntimeError:
            LOG.warning('Telegram delivery failed; notification will be retried next check.')
    state['last_failed_at'] = now().isoformat()
    write_json(STATE_FILE, state)
    return message


def next_delay(started, finished):
    """Fixed 15-minute cadence; skip missed ticks rather than hammering the site."""
    return max(0, started + max(1, math.floor((finished - started) / INTERVAL) + 1)
               * INTERVAL - finished)


def main():
    os.umask(0o077)
    load_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='Complete one check and exit.')
    parser.add_argument('--test-telegram', action='store_true', help='Send two test messages and exit.')
    parser.add_argument('--headless', action='store_true', help='Hide the dedicated checker browser.')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    verify_telegram()
    if args.test_telegram:
        for number in (1, 2):
            runtime = os.environ.get('VISA_RUNTIME_LABEL', 'this Mac')
            notify(f'Visa checker Telegram test {number}/2 from {runtime}: your bot is awake. '
                   'Monitoring target: ' + SERVICE + ', Prenotami Ashgabat.')
        return 0
    lock = acquire_lock()
    keeper = None
    context = None
    state = read_state()
    exit_code = 0

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        if Path('/usr/bin/caffeinate').exists() and not args.once:
            keeper = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())])
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            try:
                while True:
                    started = time.monotonic()
                    checked_at = now().isoformat()
                    set_status('checking', check_started_at=checked_at)
                    try:
                        if context is None:
                            profile = ROOT / '.visa-session'
                            profile.mkdir(mode=0o700, exist_ok=True)
                            context = playwright.chromium.launch_persistent_context(
                                str(profile), headless=args.headless,
                                viewport={'width': 1280, 'height': 900})
                            context.set_default_timeout(15000)
                        pages = [p for p in context.pages if not p.is_closed()]
                        page = pages[0] if pages else context.new_page()
                        result = check(page)
                        capture(page)
                        report_result(state, result)
                        LOG.info('Availability result: %s', result)
                        last = {'last_result': result, 'last_checked_at': now().isoformat()}
                    except Exception as error:
                        if context and context.pages:
                            capture(context.pages[0])
                        message = report_failure(state, error)
                        last = {'last_result': 'unknown', 'last_error': message,
                                'last_checked_at': now().isoformat()}
                        if isinstance(error, AuthenticationRejected):
                            set_status('authentication_rejected', **last)
                            exit_code = 2
                            break
                        if args.once:
                            exit_code = 1
                        # Reopen only a disconnected browser, preserving an interactive challenge.
                        if context and (not context.pages or all(p.is_closed() for p in context.pages)):
                            try:
                                context.close()
                            except Exception:
                                pass
                            context = None
                    if args.once:
                        set_status('stopped', **last)
                        break
                    delay = next_delay(started, time.monotonic())
                    set_status('waiting_for_next_check', **last,
                               next_check_at=(now() + timedelta(seconds=delay)).isoformat(),
                               interval_seconds=INTERVAL)
                    time.sleep(delay)
            finally:
                if context:
                    context.close()
    except KeyboardInterrupt:
        LOG.info('Visa checker stopped.')
        set_status('stopped')
    finally:
        if keeper:
            keeper.terminate()
        lock.close()
    return exit_code


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (CheckFailed, RuntimeError) as error:
        LOG.error('%s', safe_failure(error))
        raise SystemExit(1) from None
