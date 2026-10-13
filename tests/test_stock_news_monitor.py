import tempfile
import unittest
import logging
from io import StringIO
from datetime import date
from pathlib import Path
from unittest.mock import patch

import stock_news_monitor as monitor


RSS = b"""<rss><channel><item><title>News</title><description>Details</description></item></channel></rss>"""


class CombinedAdviceTests(unittest.TestCase):
    @patch("stock_news_monitor.fetch", return_value=RSS)
    @patch("stock_news_monitor.call_llm", side_effect=["First summary", "Second summary", "Combined advice"])
    def test_one_advice_uses_all_successful_channels(self, mocked_llm, _mocked_fetch):
        with tempfile.TemporaryDirectory() as directory:
            config = {
                "data_dir": directory,
                "rss_channels": [
                    {"name": "Channel One", "url": "https://one.test/rss"},
                    {"name": "Channel Two", "url": "https://two.test/rss"},
                ],
                "stocks": ["NASDAQ:TEST"],
                "llm": {"base_url": "https://llm.test", "model_name": "test"},
            }
            result = monitor.run(config, date(2026, 8, 16), force=False)

            self.assertEqual(result, 0)
            self.assertEqual(mocked_llm.call_count, 3)
            advice_prompt = mocked_llm.call_args_list[2].args[2]
            self.assertIn("Channel One", advice_prompt)
            self.assertIn("First summary", advice_prompt)
            self.assertIn("Channel Two", advice_prompt)
            self.assertIn("Second summary", advice_prompt)
            advice_path = Path(directory) / "2026-08-16" / "advice.txt"
            self.assertEqual(advice_path.read_text(encoding="utf-8"), "Combined advice\n")
            self.assertTrue((Path(directory) / "2026-08-16" / "channel-one" / "summary.txt").exists())
            self.assertTrue((Path(directory) / "2026-08-16" / "channel-two" / "summary.txt").exists())
            self.assertEqual(len(list(Path(directory).rglob("advice.txt"))), 1)

    def test_history_reads_legacy_feed_first_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            legacy = data_dir / "channel-one" / "2026-08-15" / "summary.txt"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("Legacy summary", encoding="utf-8")
            history = monitor.history_text(
                data_dir,
                [monitor.Feed("Channel One", "https://one.test/rss", "channel-one")],
                2,
                date(2026, 8, 16),
                5000,
            )
            self.assertIn("Legacy summary", history)

    def test_history_reads_former_centralized_advice_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            former = data_dir / "advices" / "2026-08-15.txt"
            former.parent.mkdir(parents=True)
            former.write_text("Earlier combined advice", encoding="utf-8")
            history = monitor.history_text(
                data_dir, [], 2, date(2026, 8, 16), 5000
            )
            self.assertIn("Earlier combined advice", history)

    @patch("stock_news_monitor.sys.stderr", new_callable=StringIO)
    @patch("stock_news_monitor.sys.stdout", new_callable=StringIO)
    def test_logging_splits_output_and_errors(self, stdout, stderr):
        monitor.configure_logging(verbose=False)
        logger = logging.getLogger("split-test")
        logger.info("normal progress")
        logger.error("something failed")

        self.assertIn("normal progress", stdout.getvalue())
        self.assertNotIn("something failed", stdout.getvalue())
        self.assertIn("something failed", stderr.getvalue())
        self.assertNotIn("normal progress", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
