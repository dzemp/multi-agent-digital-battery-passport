"""Reproducible, entirely synthetic finance inputs and incremental, pretax cash flows.

No Azure prices, customer contracts, app exports or financial actuals are read. Money is in EUR.
Protected contract revenue and cash delayed by a hold are never counted as investment benefits.
"""

from decimal import Decimal, ROUND_HALF_UP


VERSION = "SYN-FIN-2026-11"
LABEL = "Synthetic assumption, not actual or approved"
D = Decimal
CENT = D("0.01")
DISCOUNT = D("0.10")
MANUAL_UNIT_COST = D("18")
BUILD_BUDGET = D("1900000")
RAMPS = (D("0.55"), D("0.85"), D("1"))

CONTRACTS = (
    ("SE", "Sweden", "Nordic OEM", 720000000, 36000, "0.96", 15, "0.25", "0.18"),
    ("SE", "Sweden", "Transit OEM", 480000000, 24000, "0.94", 20, "0.30", "0.16"),
    ("DE", "Germany", "Central OEM", 630000000, 31500, "0.88", 30, "0.45", "0.20"),
    ("DE", "Germany", "Fleet OEM", 420000000, 21000, "0.90", 25, "0.40", "0.17"),
    ("PL", "Poland", "Eastern OEM", 450000000, 22500, "0.80", 45, "0.60", "0.15"),
    ("PL", "Poland", "Storage OEM", 300000000, 15000, "0.84", 40, "0.55", "0.14"),
)

# Component, annual fixed expense, variable expense per passport, one-time build expense.
COSTS = (
    ("App, storage and monitoring", 150000, "0.35", 420000),
    ("Fabric reporting", 45000, "0", 120000),
    ("Model inference", 0, "0.08", 60000),
    ("Human review and assurance", 100000, "2.40", 360000),
    ("Connector and partner support", 120000, "0.50", 600000),
    ("Security and operations", 95000, "0.15", 240000),
)

SCENARIOS = (
    {"scenario": "Cautious", "order": 1, "volume_factor": "0.75", "coverage": "0.96",
     "verified_share": "0.60", "premium_bps": "0", "returned_units": 1200, "recovery_yield": "0.55",
     "resale_unit_eur": "600", "recovery_cost_eur": "420", "investment_factor": "0.90"},
    {"scenario": "Base", "order": 2, "volume_factor": "1", "coverage": "0.99",
     "verified_share": "0.80", "premium_bps": "5", "returned_units": 2400, "recovery_yield": "0.65",
     "resale_unit_eur": "700", "recovery_cost_eur": "430", "investment_factor": "1"},
    {"scenario": "Accelerated", "order": 3, "volume_factor": "1.40", "coverage": "1",
     "verified_share": "0.90", "premium_bps": "10", "returned_units": 4200, "recovery_yield": "0.75",
     "resale_unit_eur": "800", "recovery_cost_eur": "450", "investment_factor": "1.15"},
)


def money(value):
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def payback_months(cashflows):
    """Simple, undiscounted payback; None when the modeled horizon never repays the investment."""
    cumulative = D(0)
    for year, amount in enumerate(cashflows):
        amount = D(amount)
        if not year and amount >= 0:
            return D(0)
        if year and cumulative < 0 and cumulative + amount >= 0:
            return money(((year - 1) + (-cumulative / amount)) * 12)
        cumulative += amount
    return None


def scenario_tables(spec):
    name = spec["scenario"]
    factor = D(spec["volume_factor"])
    volume = int(D(sum(c[4] for c in CONTRACTS)) * factor * D(spec["coverage"]))
    investment = sum(money(D(c[3]) * D(spec["investment_factor"])) for c in COSTS)
    recovered = int(D(spec["returned_units"]) * D(spec["recovery_yield"]))
    resale = money(recovered * D(spec["resale_unit_eur"]))
    recovery = money(D(spec["returned_units"]) * D(spec["recovery_cost_eur"]))
    circular = resale - recovery
    premium = money(sum(c[3] for c in CONTRACTS) * factor * D(spec["coverage"])
                    * D(spec["verified_share"]) * D(spec["premium_bps"]) / 10000)
    costs = [{"scenario": name, "component": component, "fixed_eur": money(fixed),
              "variable_per_passport_eur": D(variable),
              "operating_eur": money(D(fixed) + D(variable) * volume),
              "investment_eur": money(D(build) * D(spec["investment_factor"]))}
             for component, fixed, variable, build in COSTS]
    flows = [{"scenario": name, "year": 0, "period": "Y0", "passports": 0,
              "manual_avoided_eur": D(0), "premium_eur": D(0), "circular_eur": D(0),
              "resale_eur": D(0), "recovery_eur": D(0),
              "operating_eur": D(0), "investment_eur": investment,
              "net_cash_eur": -investment, "discounted_cash_eur": -investment,
              "discounted_benefit_eur": D(0), "discounted_cost_eur": investment,
              "cumulative_cash_eur": -investment}]
    for year, ramp in enumerate(RAMPS, 1):
        units = int(volume * ramp)
        operating = sum(money(D(fixed) + D(variable) * units) for _, fixed, variable, _ in COSTS)
        manual = money(units * MANUAL_UNIT_COST)
        year_premium = money(premium * ramp)
        year_resale, year_recovery = money(resale * ramp), money(recovery * ramp)
        year_circular = year_resale - year_recovery
        net = manual + year_premium + year_circular - operating
        pv_benefit = money((manual + year_premium + year_resale) / (1 + DISCOUNT) ** year)
        pv_cost = money((operating + year_recovery) / (1 + DISCOUNT) ** year)
        flows.append({"scenario": name, "year": year, "period": f"Y{year}", "passports": units,
                      "manual_avoided_eur": manual, "premium_eur": year_premium, "circular_eur": year_circular,
                      "resale_eur": year_resale, "recovery_eur": year_recovery,
                      "operating_eur": operating, "investment_eur": D(0), "net_cash_eur": net,
                      "discounted_benefit_eur": pv_benefit, "discounted_cost_eur": pv_cost,
                      "discounted_cash_eur": pv_benefit - pv_cost,
                      "cumulative_cash_eur": flows[-1]["cumulative_cash_eur"] + net})
    pv_cost = sum(f["discounted_cost_eur"] for f in flows)
    pv_benefit = sum(f["discounted_benefit_eur"] for f in flows)
    npv = sum(f["discounted_cash_eur"] for f in flows)
    efficiency_npv = -investment + sum(money((f["manual_avoided_eur"] - f["operating_eur"])
                                            / (1 + DISCOUNT) ** f["year"]) for f in flows[1:])
    row = {**{k: D(v) if isinstance(v, str) and k != "scenario" else v for k, v in spec.items()},
           "published_passports": volume, "investment_eur": investment,
           "operating_eur": sum(c["operating_eur"] for c in costs),
           "cost_per_passport_eur": money(sum(c["operating_eur"] for c in costs) / volume),
           "premium_eur": premium, "recovered_units": recovered, "resale_eur": resale,
           "recovery_total_eur": recovery, "circular_eur": circular,
           "npv_eur": npv, "efficiency_npv_eur": efficiency_npv,
           "roi": (pv_benefit - pv_cost) / pv_cost, "payback_months": payback_months(f["net_cash_eur"] for f in flows),
           "peak_funding_eur": -min(D(0), *(f["cumulative_cash_eur"] for f in flows)),
           "year3_cash_eur": flows[-1]["net_cash_eur"], "discount_rate": DISCOUNT, "horizon_years": 3}
    return row, costs, flows


def sensitivity(flows):
    """One input at a time; no probability or correlation is implied."""
    result = []
    initial = flows[0]["investment_eur"]
    for driver in ("Manual cost per passport", "Recurring operating cost", "Premium revenue",
                   "Circular contribution", "Discount rate"):
        values = []
        for change in (D("-0.20"), D("0.20")):
            rate = DISCOUNT + change / 10 if driver == "Discount rate" else DISCOUNT
            total = -initial
            for f in flows[1:]:
                net = f["net_cash_eur"]
                key = {"Manual cost per passport": "manual_avoided_eur", "Recurring operating cost": "operating_eur",
                       "Premium revenue": "premium_eur", "Circular contribution": "circular_eur"}.get(driver)
                if key:
                    net += f[key] * change * (-1 if key == "operating_eur" else 1)
                total += money(net / (1 + rate) ** f["year"])
            values.append(total)
        result.append({"driver": driver, "lower_input_npv_eur": values[0], "higher_input_npv_eur": values[1],
                       "change": "-/+2 percentage points" if driver == "Discount rate" else "-/+20 percent"})
    return result


def build_tables():
    contracts = []
    for index, (plant, country, customer, revenue, units, ready, delay, probability, margin) in enumerate(CONTRACTS, 1):
        exposed = money(revenue * (1 - D(ready)))
        contracts.append({"contract": f"SYN-{index:02}", "plant": plant, "country": country, "customer": customer,
                          "contracted_eur": revenue, "planned_batteries": units, "ready_share": D(ready),
                          "ready_eur": money(revenue - exposed), "exposed_eur": exposed, "delay_days": delay,
                          "delay_probability": D(probability), "margin_share": D(margin),
                          "margin_exposed_eur": money(exposed * D(margin)),
                          "working_capital_eur": money(exposed * D(delay) / 365),
                          "weighted_working_capital_eur": money(exposed * D(delay) / 365 * D(probability))})
    tables = {"finance_contracts": contracts, "finance_scenarios": [], "finance_costs": [],
              "finance_cashflow": [], "finance_budget": []}
    for spec in SCENARIOS:
        summary, costs, flows = scenario_tables(spec)
        tables["finance_scenarios"].append(summary)
        tables["finance_costs"].extend(costs)
        tables["finance_cashflow"].extend(flows)
        allocated_forecast = allocated_budget = D(0)
        weights = (4, 6, 8, 10, 12, 14, 14, 10, 8, 6, 4, 4)
        for month, weight in enumerate(weights, 1):
            forecast = (summary["investment_eur"] - allocated_forecast if month == 12 else
                        money(summary["investment_eur"] * weight / 100))
            budget = BUILD_BUDGET - allocated_budget if month == 12 else money(BUILD_BUDGET * weight / 100)
            allocated_forecast += forecast
            allocated_budget += budget
            tables["finance_budget"].append({"scenario": spec["scenario"], "month": month,
                                             "period": f"M{month:02}", "forecast_eur": forecast, "budget_eur": budget})
        if spec["scenario"] == "Base":
            tables["finance_sensitivity"] = sensitivity(flows)
    assumptions = (
        ("Contract revenue", "EUR 3.0bn / FY2027", "Fictional annual portfolio, not realized revenue", "Commercial"),
        ("Volume", "150,000 batteries / year", "A planning volume, not cells or a live forecast", "Operations"),
        ("Readiness", "80 to 96 percent by contract", "Fictional snapshot, not legal market-access approval",
         "Sustainability"),
        ("Delay", "15 to 45 days; 25 to 60 percent likelihood", "Cash timing exposure, not a forecast of lost sales",
         "Treasury"),
        ("Manual baseline", "EUR 18 / passport", "Avoided fully loaded manual process cost", "Finance"),
        ("Discount and horizon", "10 percent / 3 operating years", "Pretax, unlevered; no terminal value", "Finance"),
        ("Rollout", "55 / 85 / 100 percent", "Annual volume ramp; not a legal-readiness forecast", "Program"),
        ("Scenario volumes", "75 / 100 / 140 percent of planning volume",
         "Cautious / Base / Accelerated, not probabilities", "Operations"),
        ("Scenario readiness", "96 / 99 / 100 percent", "Future modeled coverage, separate from the contract snapshot",
         "Sustainability"),
        ("Verified revenue", "60 / 80 / 90 percent", "Modeled verified share of ready revenue, not measured claims",
         "Sustainability"),
        ("Premium", "0 / 5 / 10 basis points", "Only verified, ready revenue; no OEM agreement exists", "Commercial"),
        ("Second life", "1,200 / 2,400 / 4,200 returned batteries", "Yield 55 / 65 / 75 percent; not measured recovery",
         "Circularity"),
        ("Resale price", "EUR 600 / 700 / 800 per saleable unit", "No market price or realized sale is represented",
         "Circularity"),
        ("Recovery expense", "EUR 420 / 430 / 450 per return", "Charged on every return, including failed recovery",
         "Circularity"),
        ("Build variation", "90 / 100 / 115 percent of base build",
         "Scenario investment factors, not confidence intervals", "Program"),
        ("Platform and inference", "Bottom-up fictional EUR expenses",
         "Not Azure prices, invoices or measured token usage", "Technology"),
        ("Build budget", "EUR 1.90m", "Unapproved investment envelope, no actual spend", "Finance"),
        ("Excluded effects", "Tax, FX, financing and terminal value",
         "No fine forecast, CBAM tariff or carbon-credit benefit", "Finance"),
        ("Decision gate", "Validate before funding", "Confirm contracts, cost inputs, risk and benefit ownership",
         "CFO / CSO"),
    )
    tables["finance_assumptions"] = [{"assumption": name, "value": value, "basis": basis, "owner_role": owner,
                                     "status": "Unvalidated synthetic"} for name, value, basis, owner in assumptions]
    for rows in tables.values():
        for row in rows:
            row["data_label"] = LABEL
            row["model_version"] = VERSION
    return tables
