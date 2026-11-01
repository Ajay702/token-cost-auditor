import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import auditor  # noqa: E402

PRICES = {
    "big":     {"input": 10.0, "output": 50.0, "cache_write": 12.5, "cache_read": 1.0},
    "small":   {"input": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.1},
    "default": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.3},
}
DOWNGRADE = {"big": "small"}


def rec(model="big", template="t", i=0, o=0, cr=0, cw=0):
    return {"model": model, "template": template, "input_tokens": i, "output_tokens": o,
            "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}


class TemplateKeyTests(unittest.TestCase):
    def test_variable_parts_collapse_to_same_key(self):
        a = auditor.template_key({"prompt": "Ticket #123 from bob@x.com: order 555 late"})
        b = auditor.template_key({"prompt": "Ticket #98 from amy@y.org: order 1 late"})
        self.assertEqual(a, b)

    def test_explicit_template_id_wins(self):
        self.assertEqual(auditor.template_key({"template_id": "faq", "prompt": "x"}), "faq")

    def test_different_templates_differ(self):
        self.assertNotEqual(auditor.template_key({"prompt": "Summarise this"}),
                            auditor.template_key({"prompt": "Translate this"}))


class CostTests(unittest.TestCase):
    def test_call_cost(self):
        r = rec(i=1_000_000, o=1_000_000, cr=1_000_000, cw=1_000_000)
        self.assertAlmostEqual(auditor.call_cost(r, PRICES["big"]), 10 + 50 + 1 + 12.5)

    def test_unknown_and_dated_models(self):
        self.assertIs(auditor.price_for("mystery", PRICES), PRICES["default"])
        self.assertIs(auditor.price_for("big-20260101", PRICES), PRICES["big"])

    def test_parse_record_accepts_both_shapes(self):
        nested = auditor.parse_record({"model": "big", "usage": {"input_tokens": 5, "output_tokens": 2}})
        flat = auditor.parse_record({"model": "big", "input_tokens": 5, "output_tokens": 2})
        self.assertEqual(nested, flat)

    def test_aggregate_groups_and_cache_rate(self):
        rows = auditor.aggregate([rec(i=100, cr=300), rec(i=100, cr=500), rec(template="u", i=10)], PRICES)
        t = next(r for r in rows if r["template"] == "t")
        self.assertEqual(t["calls"], 2)
        self.assertAlmostEqual(t["cache_hit_rate"], 800 / 1000)
        self.assertEqual(rows[0]["template"], "t")  # sorted by cost


class SuggestionTests(unittest.TestCase):
    def fixes(self, records):
        row = auditor.aggregate(records, PRICES)[0]
        return [f for f, _ in auditor.suggest(row, PRICES, DOWNGRADE)]

    def test_uncached_big_prompt_suggests_caching(self):
        fixes = self.fixes([rec(i=5000, o=500) for _ in range(5)])
        self.assertTrue(any("prompt caching" in f for f in fixes))

    def test_well_cached_prompt_is_quiet_on_caching(self):
        fixes = self.fixes([rec(model="small", i=200, cr=8000, o=300) for _ in range(5)])
        self.assertFalse(any("caching" in f or "Cache" in f for f in fixes))

    def test_cache_writes_without_reads(self):
        fixes = self.fixes([rec(i=100, cw=4000, o=500) for _ in range(5)])
        self.assertTrue(any("written more than it is read" in f for f in fixes))

    def test_small_jobs_on_big_model_suggest_downgrade_with_savings(self):
        row = auditor.aggregate([rec(i=200, o=10) for _ in range(10)], PRICES)[0]
        tips = auditor.suggest(row, PRICES, DOWNGRADE)
        fix, saving = next(t for t in tips if "try small" in t[0])
        self.assertAlmostEqual(saving, row["cost"] * 0.9)

    def test_long_output_flagged(self):
        fixes = self.fixes([rec(model="small", i=100, o=5000)])
        self.assertTrue(any("max_tokens" in f for f in fixes))


class EndToEndTests(unittest.TestCase):
    def test_demo_report_renders(self):
        prices, downgrade = auditor.load_prices(auditor.DEFAULT_PRICES)
        report = auditor.build_report(auditor.load_logs(auditor.DEMO_LOGS), prices, downgrade)
        md = auditor.render_markdown(report)
        self.assertEqual(report["calls"], 163)
        self.assertIn("Suggested fixes", md)
        self.assertGreater(len(report["findings"]), 3)


if __name__ == "__main__":
    unittest.main()
