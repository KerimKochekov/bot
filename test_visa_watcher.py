import unittest
from unittest.mock import patch

from visa_watcher import CheckFailed, classify, next_delay, report_failure, report_result


class DetectionTests(unittest.TestCase):
    def test_booked_requires_explicit_message(self):
        self.assertEqual(classify('All appointments for this service are currently booked.'), 'booked')
        for text in ('', 'Loading...', '503 Service unavailable', 'Visas BOOK', 'Something went wrong'):
            self.assertEqual(classify(text), 'unknown')

    def test_calendar_requires_correct_flow_and_no_blocker(self):
        self.assertEqual(classify('October 2026', calendar=True, booking_flow=True), 'calendar_open')
        self.assertEqual(classify('October 2026', calendar=True), 'unknown')
        self.assertEqual(classify('All appointments for this service are currently booked.',
                                  calendar=True, booking_flow=True), 'booked')
        self.assertEqual(classify('Verify you are human', calendar=True, booking_flow=True),
                         'action_required')
        self.assertEqual(classify('Calendar', human_check=True, calendar=True, booking_flow=True),
                         'action_required')

    def test_fixed_cadence_skips_missed_ticks(self):
        self.assertEqual(next_delay(100, 160), 840)
        self.assertEqual(next_delay(100, 1000), 900)
        self.assertEqual(next_delay(100, 1950), 850)


class NotificationTests(unittest.TestCase):
    @patch('visa_watcher.write_json')
    @patch('visa_watcher.notify')
    def test_alert_deduplication_and_reopening(self, notify, write):
        state = {'first_result_sent': True}
        report_result(state, 'calendar_open')
        report_result(state, 'calendar_open')
        self.assertEqual(notify.call_count, 1)
        report_result(state, 'booked')
        report_result(state, 'calendar_open')
        self.assertEqual(notify.call_count, 2)

    @patch('visa_watcher.write_json')
    @patch('visa_watcher.notify', side_effect=RuntimeError('network down'))
    def test_failed_telegram_delivery_does_not_mark_alert_sent(self, notify, write):
        state = {'first_result_sent': True}
        with self.assertRaises(RuntimeError):
            report_result(state, 'calendar_open')
        self.assertFalse(state.get('calendar_alerted'))
        self.assertFalse(write.called)

    @patch('visa_watcher.write_json')
    @patch('visa_watcher.notify')
    def test_error_is_unknown_not_booked_and_does_not_clear_positive(self, notify, write):
        state = {'last_result': 'calendar_open', 'calendar_alerted': True}
        error = CheckFailed('Security check required')
        report_failure(state, error)
        report_failure(state, error)
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(state['last_result'], 'calendar_open')
        self.assertTrue(state['calendar_alerted'])


if __name__ == '__main__':
    unittest.main()
