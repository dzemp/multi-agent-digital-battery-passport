"""Build native PBIR reports and the embedded, offline synthetic finance model.

Run from the MVP folder: python fabric/powerbi/build_reports.py
The optional --preview writes an HTML design proof, not a Power BI rendering.
"""

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.finance import BUILD_BUDGET, VERSION, build_tables  # noqa: E402


HERE = Path(__file__).resolve().parent
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report"
DEFINITION = f"{SCHEMA}/definition"
# Match the passport app's base.html tokens, including semantic status colors.
INK, MUTED = "#201F1E", "#605E5C"
BLUE, BLUE_INK, BLUE_TINT = "#0078D4", "#005A9E", "#F1F7FD"
TEAL, PURPLE, CORAL, GOLD = "#007F82", "#332288", "#C50F1F", "#8A6100"
PAPER, WHITE, LINE = "#FAF9F8", "#FFFFFF", "#E1DFDD"
PALETTE = (TEAL, CORAL, BLUE, GOLD, PURPLE, "#484644")
SM, MP, PS, LC, SC = ("success_measures", "measures_by_plant", "passport_status_current",
                      "lot_claim_status_current", "supplier_completeness")
FS, FC, FF, FCO, FB, FA, FSE = ("finance_scenarios", "finance_contracts", "finance_cashflow",
                               "finance_costs", "finance_budget", "finance_assumptions", "finance_sensitivity")
MONEY = '"EUR "#,0;("EUR "#,0);"EUR "0'


def literal(value):
    if isinstance(value, bool):
        value = str(value).lower()
    elif isinstance(value, (int, float)):
        value = f"{value}D"
    else:
        value = "'" + value.replace("'", "''") + "'"
    return {"expr": {"Literal": {"Value": value}}}


def color(value):
    return {"solid": {"color": literal(value)}}


def properties(**values):
    return [{"properties": values}]


def instance(**values):
    return [{"properties": values, "selector": {"id": "default"}}]


def binding(table, name, kind="Measure"):
    return {"field": {kind: {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}},
            "queryRef": f"{table}.{name}", "nativeQueryRef": name}


def column(table, name):
    return binding(table, name, "Column")


def measure(table, name):
    return binding(table, name)


def container(background=None, title=None, subtitle=None, accent=None):
    result = {
        "background": properties(show=literal(background is not None), color=color(background or WHITE),
                                 transparency=literal(0)),
        "border": properties(show=literal(background == WHITE), color=color(LINE), radius=literal(8)),
        "padding": properties(**{side: literal(14) for side in ("top", "bottom", "left", "right")}),
        "visualHeader": properties(show=literal(False)),
    }
    if title:
        result["title"] = properties(show=literal(True), text=literal(title), fontSize=literal(11),
                                     fontColor=color(accent or INK), fontFamily=literal("Segoe UI Semibold"),
                                     titleWrap=literal(True), alignment=literal("left"))
    if subtitle:
        result["subTitle"] = properties(show=literal(True), text=literal(subtitle), fontSize=literal(9),
                                        fontColor=color(MUTED), titleWrap=literal(True))
    return result


class Page:
    def __init__(self, key, label, title, description, insight, finance=False, base=False):
        self.key, self.label = key, label
        self.title, self.description, self.insight = title, description, insight
        self.finance, self.base, self.visuals = finance, base, []

    def add(self, name, box, kind, objects=None, roles=None, vco=None, sample=None):
        x, y, width, height = box
        index = len(self.visuals)
        document = {"$schema": f"{DEFINITION}/visualContainer/2.0.0/schema.json", "name": name,
                    "position": {"x": x, "y": y, "width": width, "height": height,
                                 "z": index + 10, "tabOrder": index}}
        document["visual"] = {"visualType": kind, "visualContainerObjects": vco or container(),
                              "drillFilterOtherVisuals": True}
        if objects:
            document["visual"]["objects"] = objects
        if roles:
            document["visual"]["query"] = {"queryState": {role: {"projections": values}
                                                        for role, values in roles.items()}}
        self.visuals.append({"document": document, "sample": sample})
        return document

    def text(self, name, box, text, size=14, foreground=INK, background=None, bold=False):
        paragraphs = [{"textRuns": [{"value": paragraph,
                                     "textStyle": {"fontFamily": "Segoe UI Semibold" if bold else "Segoe UI",
                                                   "fontSize": f"{size * 0.75:g}pt", "color": foreground}}],
                       "horizontalTextAlignment": "left"} for paragraph in text.split("\n")]
        vco = container(background)
        if background is None:
            vco["padding"] = properties(**{side: literal(0) for side in ("top", "bottom", "left", "right")})
        self.add(name, box, "textbox", {"general": properties(paragraphs=paragraphs)},
                 vco=vco, sample={"text": text})

    def shape(self, name, box, fill):
        vco = container()
        vco["padding"] = properties(**{side: literal(0) for side in ("top", "bottom", "left", "right")})
        self.add(name, box, "shape", {
            "shape": properties(tileShape=literal("rectangle")),
            "fill": instance(fillColor=color(fill), transparency=literal(0)),
            "outline": instance(show=literal(False))}, vco=vco)

    def kpi(self, slot, table, name, title, detail, sample, accent=TEAL):
        x = 256 + slot * 332
        self.add(f"kpi_{slot}", (x, 184, 316, 166), "cardVisual", {
            "value": instance(fontSize=literal(29), fontColor=color(accent), bold=literal(True),
                              fontFamily=literal("Segoe UI Semibold")),
            "label": instance(show=literal(False)),
            "outline": instance(show=literal(False)),
            "padding": instance(paddingUniform=literal(4)),
            "layout": instance(paddingUniform=literal(0)),
            "accentBar": instance(show=literal(True), position=literal("Left"), width=literal(4), color=color(accent)),
        }, {"Data": [measure(table, name)]},
            container(WHITE, title + " | synthetic", detail), {"value": sample, "title": title, "detail": detail,
                                                              "accent": accent})

    def chart(self, name, box, title, table, category, values, sample, kind="clusteredBarChart", detail=None,
              sort_value=None):
        objects = {
            "legend": properties(show=literal(len(values) > 1), position=literal("Top"),
                                 fontSize=literal(10), labelColor=color(MUTED), showTitle=literal(False)),
            "categoryAxis": properties(show=literal(True), showAxisTitle=literal(False),
                                       fontSize=literal(11), labelColor=color(MUTED)),
            "valueAxis": properties(show=literal(True), showAxisTitle=literal(False),
                                    fontSize=literal(10), labelColor=color(MUTED),
                                    gridlineShow=literal(True), gridlineColor=color(LINE)),
            "labels": properties(show=literal(kind != "lineChart"), fontSize=literal(10),
                                 color=color(INK), labelPrecision=literal(1)),
            "dataPoint": [{"properties": {"fill": color(PALETTE[index % len(PALETTE)])},
                           "selector": {"metadata": f"{table}.{value}"}}
                          for index, value in enumerate(values)],
        }
        if kind == "lineChart":
            objects["lineStyles"] = properties(strokeWidth=literal(3))
            objects["markers"] = properties(show=literal(True), markerSize=literal(6))
        doc = self.add(name, box, kind, objects, {"Category": [column(table, category)],
                                          "Y": [measure(table, value) for value in values]},
                 container(WHITE, title + " | synthetic", detail),
                 {"rows": sample, "series": values, "title": title, "detail": detail})
        doc["visual"]["query"]["sortDefinition"] = {
            "sort": [{"field": (measure(table, sort_value) if sort_value else column(table, category))["field"],
                      "direction": "Ascending"}], "isDefaultSort": False}

    def table(self, name, box, title, fields, sample, headings, detail=None, descending=None):
        for field, heading in zip(fields, headings, strict=True):
            field["displayName"] = heading
        doc = self.add(name, box, "tableEx", {
            "grid": properties(gridVertical=literal(False), gridHorizontal=literal(True),
                               gridHorizontalColor=color(LINE), rowPadding=literal(9)),
            "columnHeaders": properties(fontColor=color(MUTED), backColor=color(PAPER), fontSize=literal(10),
                                         bold=literal(True), wordWrap=literal(True)),
            "values": properties(fontColorPrimary=color(INK), backColorPrimary=color(WHITE), fontSize=literal(11),
                                 wordWrap=literal(True)),
            "total": properties(totals=literal(False)),
        }, {"Values": fields}, container(WHITE, title + " | synthetic", detail),
            {"rows": sample, "headings": headings, "title": title, "detail": detail})
        doc["visual"]["query"]["sortDefinition"] = {
            "sort": [{"field": fields[descending if descending is not None else 0]["field"],
                      "direction": "Descending" if descending is not None else "Ascending"}], "isDefaultSort": False}

    def finish(self, pages):
        # Backgrounds are behind every foreground visual, including the navigation.
        foreground = self.visuals
        self.visuals = []
        self.shape("header_background", (0, 0, 1600, 164), WHITE)
        self.shape("navigation_background", (0, 0, 220, 900), WHITE)
        self.shape("header_border", (220, 163, 1380, 1), LINE)
        self.shape("navigation_border", (219, 4, 1, 896), LINE)
        self.shape("brand_accent_blue", (0, 0, 960, 4), BLUE)
        self.shape("brand_accent_teal", (960, 0, 640, 4), TEAL)
        self.shape("brand_mark", (24, 28, 7, 62), BLUE)
        self.text("brand", (34, 20, 178, 90), "BATTERY\nPASSPORT", 22, INK, bold=True)
        self.text("audience", (14, 122, 200, 46), "FINANCE / CFO" if self.finance else "SUSTAINABILITY / CSO",
                  11, BLUE_INK, bold=True)
        for index, page in enumerate(pages):
            selected = page.key == self.key
            doc = self.add(f"nav_{page.key}", (16, 212 + index * 58, 188, 48), "actionButton", {
                "text": [*properties(show=literal(True)),
                         *instance(text=literal(f"{index + 1:02}  {page.label}"), fontSize=literal(10),
                                   fontColor=color(BLUE_INK if selected else INK),
                                   horizontalAlignment=literal("left"))],
                "icon": [*properties(show=literal(False)), *instance(placement=literal("left"))],
                "fill": [*properties(show=literal(True)), *instance(fillColor=color(BLUE_TINT if selected else WHITE))],
                "outline": properties(show=literal(False)),
                "shape": properties(tileShape=literal("rectangleRounded"),
                                    roundEdge={"expr": {"Literal": {"Value": "6L"}}}),
            }, vco=container())
            doc["visual"]["visualContainerObjects"]["visualLink"] = properties(
                show=literal(True), type=literal("PageNavigation"), navigationSection=literal(page.key),
                tooltip=literal("Open " + page.label))
            doc["visual"]["visualContainerObjects"]["padding"] = properties(
                **{side: literal(0) for side in ("top", "bottom", "left", "right")})
            if selected:
                self.shape("navigation_selection", (16, 212 + index * 58, 4, 48), BLUE)
        self.text("deadline", (14, 666, 198, 104), "18 FEB 2027\nScoped passport milestone\nNot a compliance finding",
                  12, MUTED)
        self.text("sidebar_note", (14, 800, 198, 74),
                  "ASSUMPTIONS, NOT ACTUALS" if self.finance else "LATEST REPORTING EXPORT", 10, GOLD, bold=True)
        self.text("eyebrow", (242, 12, 1040, 36),
                  "DECISION MODEL  /  FICTIONAL FY2027 PORTFOLIO" if self.finance
                  else "READINESS  /  EVIDENCE  /  CIRCULARITY",
                  12, BLUE_INK, bold=True)
        self.text("synthetic_badge", (1282, 14, 296, 30),
                  "SYNTHETIC FINANCE  |  NO ACTUALS" if self.finance else "SYNTHETIC  |  ILLUSTRATIVE", 12, GOLD,
                  bold=True)
        self.text("headline", (242, 49, 1330, 58), self.title, 33, INK, bold=True)
        self.text("subtitle", (242, 108, 1330, 49), self.description, 15, MUTED)
        self.text("decision_ribbon", (256, 770, 1312, 76), self.insight, 16, BLUE_INK, BLUE_TINT, bold=True)
        self.text("footnote", (242, 852, 1330, 40),
                  ("BASE CASE | " if self.base else "") + ("Synthetic inputs. No approved budget or realized returns. "
                  f"Model {VERSION}." if self.finance else
                  "App export, not live telemetry. Blank means not measured. Decisions stay in the passport app."),
                  11, MUTED)
        for entry in foreground:
            index = len(self.visuals)
            entry["document"]["position"].update(z=index + 10, tabOrder=index)
            self.visuals.append(entry)


def eur(value, scale=1000000, unit="m"):
    return f"EUR {value / Decimal(scale):,.2f}{unit}"


def grouped(rows, key, *columns):
    result = {}
    for row in rows:
        totals = result.setdefault(row[key], [Decimal(0)] * len(columns))
        for index, name in enumerate(columns):
            totals[index] += Decimal(row[name])
    return [[key, *[float(value) for value in values]] for key, values in result.items()]


def cfo_pages(tables):
    contracts, scenarios = tables[FC], tables[FS]
    base = next(s for s in scenarios if s["scenario"] == "Base")
    total = sum(r["contracted_eur"] for r in contracts)
    exposed = sum(r["exposed_eur"] for r in contracts)
    weighted = sum(r["weighted_working_capital_eur"] for r in contracts)
    pages = []
    p = Page("mandate", "Revenue at stake", "Protect the right to sell, then earn the upside.",
             "EUR 3bn is the fictional contract base, not a benefit. Prioritize unready contracts by cash and margin "
             "exposure.",
             "DECISION  |  Confirm the highest-exposure contracts and owners before committing a readiness date.",
             finance=True)
    p.kpi(0, FC, "Portfolio display", "Contracted portfolio", "Fictional annual revenue base",
          eur(Decimal(total), 10**9, "bn"))
    p.kpi(1, FC, "Exposure display", "Readiness exposure", "Unready share of contract revenue, not loss", eur(exposed),
          CORAL)
    p.kpi(2, FC, "Liquidity display", "Weighted liquidity exposure", "Annual exposure x hold days x likelihood / 365",
          eur(weighted), GOLD)
    p.kpi(3, FC, "Margin exposure display", "Contribution exposed", "If every unready contract were lost",
          eur(sum(r["margin_exposed_eur"] for r in contracts)), CORAL)
    p.chart("revenue_by_plant", (256, 360, 780, 386), "Revenue readiness by plant", FC, "country",
            ["Ready contract revenue", "Exposed contract revenue"],
            grouped(contracts, "country", "ready_eur", "exposed_eur"),
            kind="barChart", detail="EUR, annual portfolio. Ready does not mean legal approval.")
    p.table("contract_priorities", (1056, 360, 512, 386), "Where commercial attention goes first",
            [column(FC, "customer"), measure(FC, "Exposed contract revenue"), measure(FC, "Assumed delay days")],
            [[r["customer"], eur(r["exposed_eur"]), str(r["delay_days"]) + "d"]
             for r in sorted(contracts, key=lambda r: r["exposed_eur"], reverse=True)],
            ["Fictional OEM", "Exposed", "Delay"], "Synthetic contract assumptions, not an order book.", descending=1)
    pages.append(p)
    p = Page("economics", "Unit economics", "Scale the passport, not the manual workload.",
             "Base case, steady-state Y3. Bottom-up operating expenses include people and platform, not only "
             "inference.",
             "DECISION  |  Validate human-review demand and the EUR 18 manual baseline before treating efficiency as "
             "savings.",
             finance=True, base=True)
    p.kpi(0, FS, "Unit cost display", "Operating cost / passport", "All passport operating components, Y3",
          eur(base["cost_per_passport_eur"], 1, ""))
    p.kpi(1, FS, "Run cost display", "Annual operating cost", "Y3, excludes the one-time build",
          eur(base["operating_eur"]))
    p.kpi(2, FS, "Investment display", "One-time investment", "Before the first operating year",
          eur(base["investment_eur"]), GOLD)
    p.kpi(3, FB, "Headroom display", "Build-budget headroom", "Unapproved budget less modeled build",
          eur(BUILD_BUDGET - base["investment_eur"]), TEAL)
    p.chart("component_cost", (256, 360, 780, 386), "The real cost stack", FCO, "component", ["Base operating cost"],
            [[r["component"], float(r["operating_eur"])] for r in tables[FCO] if r["scenario"] == "Base"],
            detail="EUR / year; all prices are fictional, not Azure quotes.")
    p.chart("build_budget", (1056, 360, 512, 386), "Build plan versus budget", FB, "period",
            ["Base build forecast", "Base build budget"],
            [[r["period"], float(r["forecast_eur"]), float(r["budget_eur"])] for r in tables[FB]
             if r["scenario"] == "Base"],
            kind="lineChart", detail="EUR / month. Both lines are synthetic plans, not actuals.")
    pages.append(p)
    p = Page("returns", "Investment case", "Do not book protected revenue as project return.",
             "Three operating years at a 10% discount rate. Compare incremental value with and without uncontracted "
             "upside.",
             "DECISION  |  The cautious case has a negative NPV. Release funding in gates, not against the upside "
             "forecast.",
             finance=True)
    p.kpi(0, FS, "Base NPV display", "Base incremental NPV", "Includes assumed premium and circularity",
          eur(base["npv_eur"]))
    p.kpi(1, FS, "Efficiency NPV display", "Efficiency-only NPV", "No premium or circular contribution",
          eur(base["efficiency_npv_eur"]), TEAL)
    p.kpi(2, FS, "Payback display", "Base simple payback", "Undiscounted, within the modeled horizon",
          f"{base['payback_months']:.1f} mo", GOLD)
    p.kpi(3, FS, "ROI display", "Base discounted ROI", "(PV benefits - PV costs) / PV costs", f"{base['roi']:.1%}")
    p.chart("scenario_npv", (256, 360, 780, 386), "What changes if the assumptions do?", FS, "scenario",
            ["Scenario NPV", "Efficiency-only NPV"],
            [[r["scenario"], float(r["npv_eur"]), float(r["efficiency_npv_eur"])] for r in scenarios],
            detail="EUR, incremental pretax NPV. No EUR 3bn revenue benefit is included.")
    p.table("scenario_inputs", (1056, 360, 512, 386), "Three explicitly hypothetical outcomes",
            [column(FS, "scenario"), measure(FS, "Scenario volume"), measure(FS, "Premium basis points"),
             measure(FS, "Scenario payback")],
            [[r["scenario"], f"{r['published_passports']:,}", f"{r['premium_bps']} bps",
              f"{r['payback_months']:.1f} mo" if r["payback_months"] is not None else "Not reached"]
             for r in scenarios],
            ["Scenario", "Y3 passports", "Premium", "Payback"])
    pages.append(p)
    p = Page("cash", "Cash and funding", "Make the funding curve as visible as the return.",
             "Base case cash is incremental and pretax. Y0 funds the build; the rollout reaches 55%, 85% and 100% in "
             "Y1 to Y3.",
             "DECISION  |  Match funding gates to cash need. Delayed customer receipts are a separate timing risk, not "
             "ROI.",
             finance=True, base=True)
    flows = [r for r in tables[FF] if r["scenario"] == "Base"]
    p.kpi(0, FS, "Funding display", "Peak cash requirement", "Maximum cumulative modeled deficit",
          eur(base["peak_funding_eur"]), GOLD)
    p.kpi(1, FS, "Year three cash display", "Year 3 net cash", "After recurring and recovery expenses",
          eur(base["year3_cash_eur"]))
    p.kpi(2, FS, "Payback display", "Simple payback", "Fractional months, not discounted",
          f"{base['payback_months']:.1f} mo")
    p.kpi(3, FS, "Investment display", "Upfront cash", "Y0 investment, not annual expense", eur(base["investment_eur"]),
          GOLD)
    p.chart("funding_curve", (256, 360, 780, 386), "From funding to cumulative cash generation", FF, "period",
            ["Base cumulative cash", "Base annual cash"],
            [[r["period"], float(r["cumulative_cash_eur"]), float(r["net_cash_eur"])] for r in flows],
            kind="lineChart", detail="EUR. Negative values are funding needs, not missing data.")
    p.table("cash_bridge", (1056, 360, 512, 386), "Cash-flow bridge",
            [column(FF, "period"), measure(FF, "Base efficiency cash"), measure(FF, "Base upside cash"),
             measure(FF, "Base annual cash")],
            [[r["period"], eur(r["manual_avoided_eur"] - r["operating_eur"] - r["investment_eur"]),
              eur(r["premium_eur"] + r["circular_eur"]), eur(r["net_cash_eur"])] for r in flows],
            ["Year", "Efficiency", "Upside", "Net cash"], "Efficiency includes build and operating expense.")
    pages.append(p)
    p = Page("circularity", "Circular value", "Separate second-life revenue from second-life profit.",
             "Base case, steady-state Y3. Yield is applied to resale units; recovery expense is charged on every "
             "returned battery.",
             "DECISION  |  Confirm resale routes, state-of-health thresholds and processing cost before recognizing "
             "value.",
             finance=True, base=True)
    p.kpi(0, FS, "Returns display", "Returned batteries", "Hypothetical annual supply to recovery",
          f"{base['returned_units']:,}", GOLD)
    p.kpi(1, FS, "Recovered display", "Saleable second-life units", "65% assumed recovery yield",
          f"{base['recovered_units']:,}")
    p.kpi(2, FS, "Circular display", "Net circular contribution", "Resale less cost on all returns",
          eur(base["circular_eur"]), TEAL)
    p.kpi(3, FS, "Premium display", "Potential low-carbon premium", "5 bps on verified, ready revenue only",
          eur(base["premium_eur"]), GOLD)
    p.chart("circular_bridge", (256, 360, 780, 386), "Revenue is not the same as contribution", FS, "scenario",
            ["Second-life resale", "Recovery expense", "Circular contribution"],
            [[r["scenario"], float(r["resale_eur"]), float(r["recovery_total_eur"]), float(r["circular_eur"])]
             for r in scenarios], detail="All scenarios shown; recovery can destroy value.")
    p.text("value_controls", (1056, 360, 512, 386),
           "VALUE RECOGNITION GATES\n\nReturn is not recovery.\n"
           "Recover only when state of health and safety evidence support it.\n\n"
           "Premium is not contracted.\nThe model assumes an OEM pays for verified low-carbon product.\n\n"
           "Circular value is not booked revenue.\nNo market price, realized sale or live telemetry is represented.",
           19, INK, WHITE)
    pages.append(p)
    p = Page("assumptions", "Assumptions and risk", "Know which assumption could reverse the decision.",
             "These are deliberately fictional financial inputs. No CFO system, cloud billing feed or customer data is "
             "connected.",
             "FUNDING GATE  |  CFO validates economics, CSO validates readiness, Commercial validates contracts. No "
             "approval is implied.",
             finance=True)
    p.chart("sensitivity", (256, 194, 620, 552), "Base NPV sensitivity, one input at a time", FSE, "driver",
            ["Lower-input NPV", "Higher-input NPV"],
            [[r["driver"], float(r["lower_input_npv_eur"]), float(r["higher_input_npv_eur"])] for r in tables[FSE]],
            detail="Inputs -/+20%; discount rate 8% / 12%. No probability or correlation implied.")
    p.table("assumption_register", (896, 194, 672, 552), "The assumption register",
            [column(FA, "assumption"), column(FA, "value"), column(FA, "basis"), column(FA, "owner_role")],
            [[r["assumption"], r["value"], r["basis"], r["owner_role"]] for r in tables[FA]],
            ["Input", "Synthetic value", "Validation boundary", "Owner role"],
            "All inputs are unvalidated. Scroll for the full register.")
    pages.append(p)
    for page in pages:
        page.finish(pages)
    return pages


def cso_pages():
    pages = []
    p = Page("overview", "Executive overview", "The right to sell starts with trusted evidence.",
             "Four outcomes, one decision: close the readiness gap without bypassing sourcing, footprint or human "
             "controls.",
             "EXECUTIVE LENS  |  Coverage protects market readiness. Verified evidence and lifecycle value make it "
             "defensible.")
    p.kpi(0, SM, "Coverage display", "Passport coverage", "Published / produced, successors excluded", "40.0%")
    p.kpi(1, SM, "Footprint display", "Verified footprint / kWh", "Illustrative kg CO2e, published batteries", "38.95",
          GOLD)
    p.kpi(2, SM, "Completeness display", "Supplier completeness", "Complete fields and a verified claim", "71.4%")
    p.kpi(3, SM, "Second life display", "Second-life value", "Illustrative value, not realized revenue", "EUR 0", GOLD)
    p.chart("plant_coverage", (256, 360, 780, 386), "Which plant is furthest from ready?", MP, "plant_id",
            ["Plant coverage"], [["PL", 0], ["DE", 0.5], ["SE", 0.5]],
            detail="Published / produced, not a legal-compliance score.",
            sort_value="Plant coverage")
    p.text("board_context", (1056, 360, 512, 386),
           "THE BUSINESS QUESTION\n\nCan every in-scope battery carry a defensible passport before it reaches the "
           "market?\n\n"
           "Close evidence gaps first.\nKeep risk acceptance with Compliance.\nVerify footprint before making a "
           "claim.\n"
           "Track second life without counting a successor as new production.", 21, INK, WHITE)
    pages.append(p)
    p = Page("readiness", "Market readiness", "Remove the hold, not the control.",
             "Separate publication coverage from held and blocked work. Use the source app for evidence, ownership and "
             "decisions.",
             "DECISION  |  Assign an evidence owner to each hold. A published passport is not a legal-compliance "
             "certification.")
    p.kpi(0, PS, "Held display", "Held, unpublished", "Human review or evidence is still needed", "3", CORAL)
    p.kpi(1, PS, "Blocked display", "Blocked, unpublished", "Cannot advance under current rules", "0", CORAL)
    p.kpi(2, SM, "Published display", "Published batteries", "Original production only", "2")
    p.kpi(3, SM, "Unpublished display", "Publication gap", "Produced less published", "3", GOLD)
    p.chart("readiness_by_plant", (256, 360, 650, 386), "Publication gap by plant", MP, "plant_id",
            ["Plant batteries without passport"], [["SE", 1], ["DE", 1], ["PL", 1]],
            detail="Counts, not estimates of contract loss.")
    p.table("held_reasons", (926, 360, 642, 386), "What is preventing the next passport?",
            [column(PS, "eligibility"), column(PS, "hold_reason"), measure(PS, "Unpublished batteries")],
            [["Held", "Conflicting supplier evidence", "2"], ["Held", "Open compliance case", "1"]],
            ["Status", "Reason", "Batteries"], "Case age and decision ownership are not in the reporting export.")
    pages.append(p)
    p = Page("suppliers", "Supplier assurance", "Completeness alone does not establish trust.",
             "Pair data completeness with claim verification and explicit risk acceptance. An accepted case is not "
             "verified evidence.",
             "DECISION  |  Resolve missing or conflicting claims; Compliance decides risk acceptance in the app, not "
             "in this report.")
    p.kpi(0, LC, "Open cases display", "Lots with an open case", "One current state per lot", "1", CORAL)
    p.kpi(1, LC, "Unverified display", "Unverified lot claims", "Not verified, including missing or revoked", "4", GOLD)
    p.kpi(2, LC, "Accepted cases display", "Lots with accepted risk", "Human acceptance, not evidence clearance", "0",
          GOLD)
    p.kpi(3, SM, "Completeness display", "Supplier completeness", "Verified claim and every required field", "71.4%")
    p.chart("claim_status", (256, 360, 650, 386), "Where is evidence still uncertain?", LC, "claim_status",
            ["Lots in export"], [["Verified", 10], ["Missing", 2], ["Invalid", 1], ["Revoked", 1]])
    p.table("supplier_material", (926, 360, 642, 386), "Supplier and material assurance",
            [column(SC, "supplier_id"), column(SC, "material"), measure(SC, "Lot completeness")],
            [["SUP-1", "Lithium", "50%"], ["SUP-1", "Nickel", "100%"], ["SUP-2", "Cobalt", "67%"],
             ["SUP-2", "Graphite", "100%"]],
            ["Supplier", "Material", "Complete"], "Averages do not replace due diligence on critical lots.")
    pages.append(p)
    p = Page("footprint", "Footprint and content", "Make the carbon claim traceable to the battery.",
             "Published, verified footprint and all three recycled-material shares are illustrative, not certified "
             "product claims.",
             "INTERPRETATION  |  Recycled shares are unweighted battery averages, not mass-weighted compliance against "
             "a legal threshold.")
    p.kpi(0, SM, "Footprint display", "Verified footprint / kWh", "Illustrative kg CO2e per kWh", "38.95", GOLD)
    p.kpi(1, SM, "Verified display", "Verified footprint batteries", "Published with verified critical claims", "2")
    p.kpi(2, PS, "Cobalt display", "Recycled cobalt share", "Average %, published original batteries", "16.0%")
    p.kpi(3, PS, "Lithium display", "Recycled lithium share", "Average %, published original batteries", "8.0%")
    p.chart("plant_footprint", (256, 360, 650, 386), "How does the verified footprint compare?", MP, "plant_id",
            ["Plant average footprint"], [["SE", 31.8], ["DE", 46.1]],
            detail="kg CO2e / kWh. No approved carbon threshold is supplied.")
    p.chart("recycled_shares", (926, 360, 642, 386), "Recycled shares by plant", PS, "plant_id",
            ["Average recycled cobalt share", "Average recycled lithium share", "Average recycled nickel share"],
            [["SE", 16, 8, 12], ["DE", 16, 8, 12]],
            detail="Percent, unweighted published-battery averages. No legal threshold is implied.")
    pages.append(p)
    p = Page("finance", "Circular value", "Value depends on a safe next life, not a new record.",
             "Keep second-life counts and value separate from publication coverage. The standalone CFO report models "
             "financial hypotheses.",
             "DECISION  |  Verify state of health, safety and partner evidence before a second-life decision. Values "
             "are not booked revenue.")
    p.kpi(0, SM, "Second life count display", "Batteries in second life", "Originals with a successor passport", "0",
          TEAL)
    p.kpi(1, SM, "Second life display", "Illustrative second-life value", "From the app export, not the CFO model",
          "EUR 0", GOLD)
    p.kpi(2, PS, "Nickel display", "Recycled nickel share", "Average %, published originals", "12.0%")
    p.kpi(3, SM, "Coverage display", "Passport coverage", "Successors never inflate production", "40.0%")
    p.chart("lifecycle_status", (256, 360, 650, 386), "Where are the original batteries now?", PS, "lifecycle_status",
            ["Produced batteries"], [["Active", 5]],
            detail="Original batteries only. Successor passports are excluded.")
    p.text("lifecycle_controls", (926, 360, 642, 386),
           "CONTROLLED LIFECYCLE\n\nState-of-health and ownership updates remain in the passport app.\n\n"
           "OEMs, recyclers and authorities receive role-controlled views, not unrestricted evidence.\n\n"
           "The CFO model is separate.\n"
           "Its contracts, costs, cash flows and scenarios are entirely fictional and never blended with this export.",
           21, INK, WHITE)
    pages.append(p)
    p = Page("trust", "Trust and gaps", "Know what the report can prove.",
             "Trace figures to the export, preserve human decision boundaries, and keep unavailable evidence visibly "
             "unavailable.",
             "ASSURANCE  |  The report never approves, clears a hold or issues a passport. No legal approval or real "
             "customer evidence is implied.")
    p.table("source_identity", (256, 194, 1312, 156), "The reporting snapshot",
            [measure(SM, "Export ID"), measure(SM, "Generated UTC"), measure(SM, "Batteries not in export")],
            [["Illustrative layout sample only", "See actual export in Power BI", "0"]],
            ["Export ID", "Generated UTC", "Missing batteries"], "The latest loaded app export, not live telemetry.")
    p.table("reconciliation", (256, 360, 650, 386), "Coverage reconciliation",
            [measure(PS, "Published batteries (status rows)"), measure(SM, "Published batteries (success measures)")],
            [["2", "2"]], ["Status rows", "Pipeline measures"],
            "Compare the same export; successors are excluded from both.")
    p.text("gaps", (926, 360, 642, 386),
           "VISIBLE LIMITS\n\nNot measured: case age, series trends, service latency, model quality and full operating "
           "cost.\n\n"
           "No regulatory verdict or approved carbon threshold.\nIndependent table grains are not cross-filtered.\n"
           "No raw evidence, prompts or people are exported.\n\nThe CFO assumptions are a separate model, not actual "
           "financial records.",
           20, INK, WHITE)
    pages.append(p)
    for page in pages:
        page.finish(pages)
    return pages


def display(raw, scale=1, suffix="", prefix=""):
    return (f'IF(ISBLANK([{raw}]), "Not measured", "{prefix}" & '
            f'FORMAT(DIVIDE([{raw}], {scale}), "0.00") & "{suffix}")')


def finance_measures():
    result = {
        FC: [
            ("Contracted revenue", f"SUM({FC}[contracted_eur])", MONEY),
            ("Ready contract revenue", f"SUM({FC}[ready_eur])", MONEY),
            ("Exposed contract revenue", f"SUM({FC}[exposed_eur])", MONEY),
            ("Weighted liquidity exposure", f"SUM({FC}[weighted_working_capital_eur])", MONEY),
            ("Exposed contribution", f"SUM({FC}[margin_exposed_eur])", MONEY),
            ("Assumed delay days", f"MAX({FC}[delay_days])", "0"),
            ("Portfolio display", display("Contracted revenue", 10**9, "bn", "EUR "), None),
            ("Exposure display", display("Exposed contract revenue", 10**6, "m", "EUR "), None),
            ("Liquidity display", display("Weighted liquidity exposure", 10**6, "m", "EUR "), None),
            ("Margin exposure display", display("Exposed contribution", 10**6, "m", "EUR "), None),
        ],
        FS: [],
        FCO: [("Base operating cost",
               f'CALCULATE(SUM({FCO}[operating_eur]), REMOVEFILTERS({FS}), {FCO}[scenario] = "Base")', MONEY)],
        FF: [],
        FB: [],
        FA: [],
        FSE: [("Lower-input NPV", f"SUM({FSE}[lower_input_npv_eur])", MONEY),
              ("Higher-input NPV", f"SUM({FSE}[higher_input_npv_eur])", MONEY)],
    }
    for name, key, fmt in (("Scenario NPV", "npv_eur", MONEY), ("Efficiency-only NPV", "efficiency_npv_eur", MONEY),
                           ("Scenario volume", "published_passports", "#,0"),
                           ("Premium basis points", "premium_bps", "0"),
                           ("Scenario payback", "payback_months", '0.0" months"'),
                           ("Second-life resale", "resale_eur", MONEY),
                           ("Recovery expense", "recovery_total_eur", MONEY),
                           ("Circular contribution", "circular_eur", MONEY)):
        result[FS].append((name, f"IF(HASONEVALUE({FS}[scenario]), MAX({FS}[{key}]))", fmt))
    for name, key in (("Unit cost", "cost_per_passport_eur"), ("Run cost", "operating_eur"),
                      ("Investment", "investment_eur"), ("Base NPV", "npv_eur"),
                      ("Efficiency NPV", "efficiency_npv_eur"), ("Payback", "payback_months"), ("ROI", "roi"),
                      ("Funding", "peak_funding_eur"), ("Year three cash", "year3_cash_eur"),
                      ("Returns", "returned_units"), ("Recovered", "recovered_units"),
                      ("Circular", "circular_eur"), ("Premium", "premium_eur")):
        result[FS].append((name, f'CALCULATE(MAX({FS}[{key}]), REMOVEFILTERS({FS}), {FS}[scenario] = "Base")', None))
        if name == "Payback":
            expr = 'IF(ISBLANK([Payback]), "Not reached", FORMAT([Payback], "0.0") & " mo")'
        elif name == "ROI":
            expr = 'FORMAT([ROI], "0.0%")'
        elif name in ("Returns", "Recovered"):
            expr = f'FORMAT([{name}], "#,0")'
        else:
            expr = display(name, 1 if name == "Unit cost" else 10**6, "" if name == "Unit cost" else "m", "EUR ")
        result[FS].append((name + " display", expr, None))
    for name, expr in (("Base annual cash", f"SUM({FF}[net_cash_eur])"),
                       ("Base cumulative cash", f"MAX({FF}[cumulative_cash_eur])"),
                       ("Base efficiency cash",
                        f"SUM({FF}[manual_avoided_eur]) - SUM({FF}[operating_eur]) - SUM({FF}[investment_eur])"),
                       ("Base upside cash", f"SUM({FF}[premium_eur]) + SUM({FF}[circular_eur])")):
        result[FF].append((name, f'CALCULATE({expr}, REMOVEFILTERS({FS}), {FF}[scenario] = "Base")', MONEY))
    for name, key in (("Base build forecast", "forecast_eur"), ("Base build budget", "budget_eur")):
        result[FB].append((name, f'CALCULATE(SUM({FB}[{key}]), REMOVEFILTERS({FS}), {FB}[scenario] = "Base")', MONEY))
    result[FB] += [("Budget headroom", "[Base build budget] - [Base build forecast]", MONEY),
                   ("Headroom display", display("Budget headroom", 10**6, "m", "EUR "), None)]
    return result


def write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=True) + "\n", encoding="utf-8", newline="\n")


def m_value(value):
    if value is None:
        return "null"
    return '"' + value.replace('"', '""') + '"' if isinstance(value, str) else str(value)


def write_finance_model(tables):
    model = HERE / "finance-synthetic"
    write(model / "definition.pbism", {"$schema": "https://developer.microsoft.com/json-schemas/fabric/item/"
                                                  "semanticModel/definitionProperties/1.0.0/schema.json",
                                       "version": "5.0", "settings": {"qnaEnabled": False}})
    folder = model / "definition"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "database.tmdl").write_text("database finance_synthetic\n\tcompatibilityLevel: 1604\n", encoding="utf-8")
    (folder / "model.tmdl").write_text("model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
                                      "\tdiscourageImplicitMeasures\n\tsourceQueryCulture: en-US\n\n" +
                                      "\n".join(f"ref table {name}" for name in tables) + "\n", encoding="utf-8")
    measures = finance_measures()
    for name, rows in tables.items():
        lines = [f"/// Entirely synthetic. {VERSION}. No external data source or approved financial input.",
                 f"table {name}", ""]
        for label, expr, fmt in measures[name]:
            lines += [f"\tmeasure '{label}' = {expr}"]
            if fmt:
                lines += [f"\t\tformatString: {fmt}"]
            lines += [""]
        types = {}
        for key in rows[0]:
            sample = next((r[key] for r in rows if r[key] is not None), None)
            types[key] = "string" if isinstance(sample, str) else "int64" if isinstance(sample, int) else "double"
            lines += [f"\tcolumn {key}", f"\t\tdataType: {types[key]}", "\t\tsummarizeBy: none",
                      f"\t\tsourceColumn: {key}", ""]
            order_column = {(FS, "scenario"): "order", (FF, "period"): "year", (FB, "period"): "month"}.get((name, key))
            if order_column:
                lines[-1:] = [f"\t\tsortByColumn: {order_column}", ""]
        mtypes = {"string": "text", "int64": "Int64.Type", "double": "nullable number"}
        lines += [f"\tpartition {name} = m", "\t\tmode: import", "\t\tsource =",
                  "\t\t\t#table(",
                  "\t\t\t\ttype table [" + ", ".join(f"{key} = {mtypes[kind]}" for key, kind in types.items()) + "],",
                  "\t\t\t\t{"]
        for index, row in enumerate(rows):
            lines += ["\t\t\t\t\t{" + ", ".join(m_value(value) for value in row.values()) + "}"
                      + ("," if index < len(rows) - 1 else "")]
        lines += ["\t\t\t\t}", "\t\t\t)", ""]
        target = folder / "tables" / f"{name}.tmdl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    write(HERE / "finance-synthetic.pbip", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0", "artifacts": [{"report": {"path": "finance-synthetic-report"}}],
        "settings": {"enableAutoRecovery": True}})


def theme():
    return {"name": "Battery passport executive", "dataColors": list(PALETTE),
            "background": PAPER, "foreground": INK, "tableAccent": TEAL,
            "good": "#107C10", "neutral": GOLD, "bad": CORAL,
            "textClasses": {"callout": {"fontFace": "Segoe UI Semibold", "fontSize": 29, "color": INK},
                            "title": {"fontFace": "Segoe UI Semibold", "fontSize": 12, "color": INK},
                            "header": {"fontFace": "Segoe UI Semibold", "fontSize": 11, "color": INK},
                            "label": {"fontFace": "Segoe UI", "fontSize": 11, "color": MUTED}}}


def write_report(folder, pages, finance=False):
    definition = folder / "definition"
    wanted = set()
    for page in pages:
        target = definition / "pages" / page.key
        data_visuals = [v["document"]["name"] for v in page.visuals if "query" in v["document"]["visual"]]
        write(target / "page.json", {"$schema": f"{DEFINITION}/page/2.0.0/schema.json", "name": page.key,
                                     "displayName": page.label, "displayOption": "FitToPage", "height": 900,
                                     "width": 1600,
                                     "visualInteractions": [{"source": source, "target": target, "type": "NoFilter"}
                                                            for source in data_visuals for target in data_visuals
                                                            if source != target],
                                     "objects": {"background": properties(color=color(PAPER),
                                                                          transparency=literal(0))}})
        for visual in page.visuals:
            path = target / "visuals" / visual["document"]["name"] / "visual.json"
            wanted.add(path)
            write(path, visual["document"])
    for path in (definition / "pages").glob("*/visuals/*/visual.json"):
        if path not in wanted:
            path.unlink()
            if not any(path.parent.iterdir()):
                path.parent.rmdir()
    write(definition / "pages" / "pages.json", {"$schema": f"{DEFINITION}/pagesMetadata/1.0.0/schema.json",
                                               "pageOrder": [p.key for p in pages], "activePageName": pages[0].key})
    versions = {"visual": "2.0.0", "report": "3.0.0", "page": "2.0.0"}
    write(definition / "report.json", {
        "$schema": f"{DEFINITION}/report/3.0.0/schema.json",
        "themeCollection": {"baseTheme": {"name": "CY24SU10", "reportVersionAtImport": versions,
                                          "type": "SharedResources"},
                            "customTheme": {"name": "ExecutiveTheme", "reportVersionAtImport": versions,
                                            "type": "RegisteredResources"}},
        "resourcePackages": [{"name": "RegisteredResources", "type": "RegisteredResources",
                              "items": [{"name": "ExecutiveTheme", "path": "ExecutiveTheme.json",
                                         "type": "CustomTheme"}]}]})
    write(folder / "StaticResources" / "RegisteredResources" / "ExecutiveTheme.json", theme())
    write(definition / "version.json",
          {"$schema": f"{DEFINITION}/versionMetadata/1.0.0/schema.json", "version": "2.0.0"})
    if finance:
        write(folder / "definition.pbir", {"$schema": f"{SCHEMA}/definitionProperties/2.0.0/schema.json",
                                           "version": "4.0",
                                           "datasetReference": {"byPath": {"path": "../finance-synthetic"}}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", action="store_true", help="also write the offline HTML design proof")
    args = parser.parse_args()
    tables = build_tables()
    cso, cfo = cso_pages(), cfo_pages(tables)
    write_finance_model(tables)
    write_report(HERE / "passport-measures-report", cso)
    write_report(HERE / "finance-synthetic-report", cfo, finance=True)
    if args.preview:
        from preview import write_preview
        write_preview(ROOT / "output" / "executive-reports" / "index.html", cso, cfo,
                      {"ink": INK, "muted": MUTED, "line": LINE, "page": PAPER, "white": WHITE,
                       "dataColors": PALETTE})
    print("Built six CSO pages and six CFO pages. The finance project embeds only synthetic data.")


if __name__ == "__main__":
    main()
