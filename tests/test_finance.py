"""Financial identities and the standalone, entirely synthetic executive report."""

import importlib.util
import json
import re
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import finance  # noqa: E402


class FinanceTests(unittest.TestCase):
    def setUp(self):
        self.tables = finance.build_tables()

    def test_contract_exposure_and_liquidity_are_not_claimed_losses(self):
        contracts = self.tables["finance_contracts"]
        self.assertEqual(sum(r["contracted_eur"] for r in contracts), 3_000_000_000)
        self.assertEqual(sum(r["exposed_eur"] for r in contracts), Decimal("313200000"))
        for row in contracts:
            self.assertEqual(row["ready_eur"] + row["exposed_eur"], row["contracted_eur"])
            self.assertEqual(row["margin_exposed_eur"], finance.money(row["exposed_eur"] * row["margin_share"]))
            self.assertEqual(row["weighted_working_capital_eur"],
                             finance.money(row["exposed_eur"] * row["delay_days"] / 365 * row["delay_probability"]))

    def test_incremental_cash_discounted_return_and_unit_economics_reconcile(self):
        for scenario in self.tables["finance_scenarios"]:
            name = scenario["scenario"]
            costs = [r for r in self.tables["finance_costs"] if r["scenario"] == name]
            flows = [r for r in self.tables["finance_cashflow"] if r["scenario"] == name]
            with self.subTest(scenario=name):
                self.assertEqual(sum(r["investment_eur"] for r in costs), scenario["investment_eur"])
                self.assertEqual(sum(r["operating_eur"] for r in costs), scenario["operating_eur"])
                self.assertEqual(scenario["cost_per_passport_eur"],
                                 finance.money(scenario["operating_eur"] / scenario["published_passports"]))
                cumulative = Decimal(0)
                for flow in flows:
                    expected = (flow["passports"] * finance.MANUAL_UNIT_COST + flow["premium_eur"]
                                + flow["resale_eur"] - flow["recovery_eur"]
                                - flow["operating_eur"] - flow["investment_eur"])
                    self.assertEqual(flow["net_cash_eur"], expected)
                    cumulative += expected
                    self.assertEqual(flow["cumulative_cash_eur"], cumulative)
                    self.assertAlmostEqual(flow["discounted_cash_eur"],
                                           expected / (1 + finance.DISCOUNT) ** flow["year"], delta=Decimal(".02"))
                pv_cost = sum(r["discounted_cost_eur"] for r in flows)
                pv_benefit = sum(r["discounted_benefit_eur"] for r in flows)
                self.assertEqual(scenario["npv_eur"], pv_benefit - pv_cost)
                self.assertEqual(scenario["roi"], (pv_benefit - pv_cost) / pv_cost)
                self.assertEqual(scenario["peak_funding_eur"], -min(r["cumulative_cash_eur"] for r in flows))
                efficiency = -scenario["investment_eur"] + sum(
                    finance.money((r["manual_avoided_eur"] - r["operating_eur"])
                                  / (1 + finance.DISCOUNT) ** r["year"]) for r in flows[1:])
                self.assertEqual(scenario["efficiency_npv_eur"], efficiency)

    def test_failed_recoveries_still_cost_money_and_downside_stays_negative(self):
        scenarios = {r["scenario"]: r for r in self.tables["finance_scenarios"]}
        base, cautious = scenarios["Base"], scenarios["Cautious"]
        self.assertEqual((base["recovered_units"], base["recovery_total_eur"], base["circular_eur"]),
                         (1560, Decimal("1032000"), Decimal("60000")))
        self.assertEqual(cautious["circular_eur"], Decimal("-108000"))
        self.assertLess(cautious["npv_eur"], 0)
        self.assertGreater(cautious["efficiency_npv_eur"], 0)
        for row in scenarios.values():
            self.assertEqual(row["recovery_total_eur"], row["returned_units"] * row["recovery_cost_eur"])

    def test_simple_payback_is_fractional_and_unrecovered_funding_is_not_zero(self):
        self.assertEqual(finance.payback_months([-100, 40, 120]), Decimal("18"))
        self.assertEqual(finance.payback_months([-100, 100]), Decimal("12"))
        self.assertIsNone(finance.payback_months([-100, -20, 50]))
        self.assertEqual(finance.payback_months([0, 100]), Decimal(0))

    def test_sensitivity_respects_cost_and_discount_direction(self):
        rows = {r["driver"]: r for r in self.tables["finance_sensitivity"]}
        for name in ("Manual cost per passport", "Premium revenue", "Circular contribution"):
            self.assertLess(rows[name]["lower_input_npv_eur"], rows[name]["higher_input_npv_eur"])
        for name in ("Recurring operating cost", "Discount rate"):
            self.assertGreater(rows[name]["lower_input_npv_eur"], rows[name]["higher_input_npv_eur"])

    def test_budget_phasing_reconciles_and_every_row_is_labeled(self):
        for scenario in self.tables["finance_scenarios"]:
            rows = [r for r in self.tables["finance_budget"] if r["scenario"] == scenario["scenario"]]
            self.assertEqual(sum(r["forecast_eur"] for r in rows), scenario["investment_eur"])
            self.assertEqual(sum(r["budget_eur"] for r in rows), finance.BUILD_BUDGET)
        for rows in self.tables.values():
            for row in rows:
                self.assertEqual((row["data_label"], row["model_version"]), (finance.LABEL, finance.VERSION))


class ExecutiveReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("executive_reports",
                                                      ROOT / "fabric" / "powerbi" / "build_reports.py")
        cls.builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.builder)
        cls.folder = cls.builder.HERE

    def test_report_colors_match_the_passport_app(self):
        stylesheet = (ROOT / "app" / "passport" / "templates" / "base.html").read_text(encoding="utf-8")
        tokens = dict(re.findall(r"--([\w-]+):\s*(#[0-9A-Fa-f]{6})", stylesheet))
        theme = self.builder.theme()
        self.assertEqual(theme["background"], tokens["page"])
        self.assertEqual(theme["foreground"], tokens["ink"])
        self.assertEqual(theme["dataColors"],
                         [tokens[k] for k in ("teal", "red", "blue", "amber", "purple", "charcoal")])
        self.assertEqual([theme[k] for k in ("good", "neutral", "bad")],
                         [tokens[k] for k in ("green", "amber", "red")])
        for color in (self.builder.BLUE_INK, self.builder.BLUE_TINT, self.builder.MUTED, self.builder.LINE):
            self.assertIn(color, tokens.values())

    def test_checked_in_reports_and_embedded_finance_are_reproducible(self):
        b = self.builder
        tables = finance.build_tables()
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(b, "HERE", Path(temporary)):
            b.write_finance_model(tables)
            b.write_report(Path(temporary) / "passport-measures-report", b.cso_pages())
            b.write_report(Path(temporary) / "finance-synthetic-report", b.cfo_pages(tables), finance=True)
            for path in Path(temporary).rglob("*"):
                if path.is_file():
                    relative = path.relative_to(temporary)
                    with self.subTest(path=str(relative)):
                        self.assertEqual(path.read_text(encoding="utf-8"),
                                         (self.folder / relative).read_text(encoding="utf-8"))

    def test_finance_opens_on_an_embedded_import_model_not_the_app_or_fabric(self):
        project = json.loads((self.folder / "finance-synthetic.pbip").read_text(encoding="utf-8"))
        report = self.folder / project["artifacts"][0]["report"]["path"]
        binding = json.loads((report / "definition.pbir").read_text(encoding="utf-8"))
        self.assertEqual(binding["datasetReference"], {"byPath": {"path": "../finance-synthetic"}})
        model = self.folder / "finance-synthetic" / "definition"
        self.assertFalse((model / "dataSources.tmdl").exists())
        for table in (model / "tables").glob("*.tmdl"):
            text = table.read_text(encoding="utf-8")
            self.assertIn("mode: import", text)
            self.assertIn("#table(", text)
            self.assertNotRegex(text, r"(?i)Sql\.Database|Web\.Contents|File\.Contents|AzureStorage|{{")

    def test_queries_and_measure_references_resolve_and_layouts_fit(self):
        for report_name, model_name in (("passport-measures-report", "passport-measures"),
                                        ("finance-synthetic-report", "finance-synthetic")):
            model = {}
            for path in (self.folder / model_name / "definition" / "tables").glob("*.tmdl"):
                text = path.read_text(encoding="utf-8")
                model[path.stem] = set(re.findall(r"^\tcolumn (\w+)$|^\tmeasure '([^']+)' =", text, re.MULTILINE))
                model[path.stem] = {a or b for a, b in model[path.stem]}
            known = set().union(*model.values())
            for path in (self.folder / model_name / "definition" / "tables").glob("*.tmdl"):
                for expression in re.findall(r"^\tmeasure '[^']+' = (.*)$", path.read_text(encoding="utf-8"),
                                             re.MULTILINE):
                    self.assertFalse(set(re.findall(r"\[([^\]]+)\]", expression)) - known, expression)
            report = self.folder / report_name / "definition"
            order = json.loads((report / "pages" / "pages.json").read_text(encoding="utf-8"))["pageOrder"]
            for page in order:
                layout = json.loads((report / "pages" / page / "page.json").read_text(encoding="utf-8"))
                boxes, data = [], []
                for path in (report / "pages" / page / "visuals").glob("*/visual.json"):
                    visual = json.loads(path.read_text(encoding="utf-8"))
                    content, p = visual["visual"], visual["position"]
                    self.assertTrue(0 <= p["x"] < p["x"] + p["width"] <= 1600, path)
                    self.assertTrue(0 <= p["y"] < p["y"] + p["height"] <= 900, path)
                    if content["visualType"] != "shape":
                        box = p["x"], p["y"], p["x"] + p["width"], p["y"] + p["height"]
                        self.assertFalse(any(box[0] < r and left < box[2] and box[1] < bottom and top < box[3]
                                             for left, top, r, bottom in boxes), path)
                        boxes.append(box)
                    if content["visualType"] == "cardVisual":
                        self.assertEqual(set(content["query"]["queryState"]), {"Data"})
                    if "query" not in content:
                        continue
                    data.append(visual["name"])
                    for role in content["query"]["queryState"].values():
                        for projection in role["projections"]:
                            field = next(iter(projection["field"].values()))
                            self.assertIn(field["Property"], model[field["Expression"]["SourceRef"]["Entity"]], path)
                self.assertLessEqual(len(data), 6)
                self.assertEqual({(i["source"], i["target"], i["type"]) for i in layout["visualInteractions"]},
                                 {(source, target, "NoFilter") for source in data for target in data
                                  if source != target})


if __name__ == "__main__":
    unittest.main()
