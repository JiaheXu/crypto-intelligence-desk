import tempfile
import unittest
from pathlib import Path

import proxy


class FinanceCalendarTests(unittest.TestCase):
    def test_loads_company_finance_reports_from_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "finance_calendar.yaml"
            path.write_text(
                """company_finance_reports:
  - symbol: AAPL
    name: Apple 财报
    time_utc: 2026-08-01T20:00:00Z
  - TSLA
  - symbol: MSFT
    time_utc:
""",
                encoding="utf-8",
            )

            self.assertEqual(
                proxy.load_finance_calendar_events(path),
                [{"n": "Apple 财报", "t": 1785614400000}],
            )


if __name__ == "__main__":
    unittest.main()
