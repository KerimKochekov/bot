# Prenotami Schengen availability monitor

Checks **Book → Visas → Schengen visa to Italy and France → Book** at the Italian Embassy in Ashgabat and sends alerts to a private Telegram chat. It never submits a reservation or solves a CAPTCHA.

GitHub Actions runs one check at minutes **07, 22, 37 and 52 of each hour (UTC)**, independently of any personal computer. Scheduled runs can be delayed or dropped by GitHub. Public repositories' schedules are automatically disabled after 60 days without repository activity; check Actions periodically. This is a best-effort monitor, not a guaranteed 24/7 service.

## Setup

In **Settings → Secrets and variables → Actions**, add these repository secrets:

- `PRENOTAMI_USERNAME`
- `PRENOTAMI_PASSWORD`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID` (a private chat that has already started the bot)
- `VISA_STATE_KEY` (a Fernet key generated with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)

Run **Actions → Schengen visa availability → Run workflow**, selecting `send_test_messages` for the first hosted test. Two test messages confirm Telegram transport; a separate live result confirms Prenotami was actually checked. A failed run or security challenge means availability is **unknown**.

Only the explicit fully-booked message confirms `booked`. `calendar_open` requires the actual visible appointment calendar in the booking flow. A generic form, missing error message, login wall or network failure does not count as availability.

Calendar alerts, meaningful failures and recoveries are deduplicated across runs. Rejected credentials block subsequent logins until the credential secrets change or the manual `reset_login_block` input is selected.

## Privacy and state

Credentials stay in GitHub Actions secrets. Browser sessions and notification history are encrypted with `VISA_STATE_KEY` before entering the Actions cache. Public caches can be downloaded by others: only encrypted state is cached. No screenshots, private forms or unencrypted sessions are uploaded as artifacts. Do not commit `.env` or state files.

If the encryption key changes, delete the old `visa-state-v1-` caches before the next check. Cache eviction can reset notification history and require sign-in again. Treat people who can edit the workflow as trusted: workflow code can use repository secrets.

Prenotami may block hosted browsers. A Telegram test alone does not establish that unattended availability checks work. Security challenges require another supported access method; the monitor does not bypass them.

The runner uses stable Google Chrome, one of Prenotami's [supported browsers](https://conslondra.esteri.it/en/info-utili/f-a-q/faq-sistema-di-prenotazione/). Automatic page redirects can finish before a verification screen is declared blocked. The checker never clicks challenge controls or fills verification codes. Failure diagnostics contain fixed categories only, without page text, form values or URLs.

## Validation

```
pip install -r requirements-actions.txt
python -m unittest -v test_visa_watcher test_visa_runtime test_visa_actions
```

Optional offline browser fixtures: `playwright install chromium`, then `RUN_VISA_BROWSER_TESTS=1 python -m unittest -v test_visa_browser`.

GitHub's standard hosted runners are free for public repositories. No paid runner or billing configuration is required by this workflow.

References: [scheduled workflow limits](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule), [Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions), [encrypted secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets).
