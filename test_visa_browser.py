"""Offline rendered fixtures; opt in with RUN_VISA_BROWSER_TESTS=1."""
import os
import unittest

from visa_watcher import BASE, CALENDAR_SCRIPT, SERVICE, check, load_environment


@unittest.skipUnless(os.environ.get('RUN_VISA_BROWSER_TESTS') == '1', 'opt-in browser fixtures')
class RenderedCalendarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_environment()
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()

    def tearDown(self):
        self.page.close()

    def calendar(self, style=''):
        return ('<div id="calendar" style="' + style + '"><h2>October 2026</h2>'
                '<div>Monday Tuesday Wednesday Thursday Friday Saturday Sunday</div>'
                '<div>1 2 3 4 5 6 7 8 9 10 11 12 13 14</div></div>')

    def test_visible_calendar_and_hidden_calendar(self):
        self.page.set_content(self.calendar())
        self.assertTrue(self.page.evaluate(CALENDAR_SCRIPT))
        self.page.set_content(self.calendar('display:none'))
        self.assertFalse(self.page.evaluate(CALENDAR_SCRIPT))

    def test_service_table_and_birthday_input_are_not_calendars(self):
        self.page.set_content('<table><tr><td>VISAS</td><td>Schengen</td>'
                              '<td>BOOK</td></tr></table><input type="date" name="birthday">')
        self.assertFalse(self.page.evaluate(CALENDAR_SCRIPT))

    def test_booking_flow_handles_calendar_and_booked_dialog(self):
        services = ('<table id="dataTableServices"><tbody><tr><td>' + SERVICE
                    + '</td><td><a href="/Services/Booking/4814">BOOK</a>'
                    '</td></tr></tbody></table>')
        for expected, booking in [
            ('calendar_open', self.calendar()),
            ('booked', '<script>alert("All appointments for this service are currently booked.")</script>'),
        ]:
            with self.subTest(result=expected):
                self.page.unroute_all()
                def respond(route, request, html=booking):
                    route.fulfill(content_type='text/html', body=(
                        services if request.url == BASE + '/Services' else html))
                self.page.route('**/*', respond)
                self.assertEqual(check(self.page), expected)


if __name__ == '__main__':
    unittest.main()
