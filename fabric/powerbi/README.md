# Executive Power BI reports

Two six-page Power BI reports support different executive decisions. The chief sustainability officer (CSO) report uses the app's reporting export for passport readiness, supplier assurance, and circularity. The chief financial officer (CFO) report uses a separate, entirely fictional financial model. Both use the passport app's visual style.

| Report | Source | Open or update |
|---|---|---|
| CSO, `passport-measures-report` | Existing Direct Lake model, minimized app reporting export | On the configured VM, run `python -m dbp_mvp report publish`. Use `python -m dbp_mvp powerbi` to publish the model and report separately. Open `passport-measures` in the Fabric workspace after resuming its capacity. |
| CFO, `finance-synthetic-report` | Separate import model with entirely fictional, embedded inputs | Open [finance-synthetic.pbip](finance-synthetic.pbip) in current Power BI Desktop and choose **Refresh**. No deployment, finance tables, credentials or Fabric capacity is needed locally. |

Use Power BI Desktop with Power BI Project (PBIP) and Tabular Model Definition Language (TMDL) support. If an older release requests preview-feature settings, enable the project/TMDL options or update Desktop. Keep each project beside its model folder. In Desktop authoring mode, **Ctrl+click** the page-navigation buttons. Desktop's local `.pbi` caches are ignored by Git.

The CFO pages are **Revenue at stake**, **Unit economics**, **Investment case**, **Cash and funding**, **Circular value**, and **Assumptions and risk**. Scalar finance cards and cost/cash/budget views use **Base**, while comparison charts show all three scenarios. There is no scenario slicer. Cross-visual filtering is intentionally disabled so static explanations and independent table grains stay truthful; navigation, tooltips and table scrolling remain available.

To share the CFO report, refresh it in Desktop, then use **Publish** to the intended workspace with appropriate Power BI permissions and licenses. Check its model refresh and report rendering there before distributing it. The CLI's `powerbi` command remains the CSO route and does not publish or overwrite the CFO project.

## Authoring

From the component root:

```powershell
python fabric\powerbi\build_reports.py --preview
```

This rebuilds both native Power BI enhanced report format (PBIR) definitions, the shared theme, and the CFO import model. Edit layout and financial measure definitions in [build_reports.py](build_reports.py), not generated visual JSON. Edit financial assumptions and cash calculations in [data/finance.py](../../data/finance.py), then regenerate. CSO measures remain in `passport-measures/definition/tables/`.

The optional `output\executive-reports\index.html` is an offline **HTML design proof**, not a Power BI rendering. Its CFO samples come from the financial generator. Its CSO samples are illustrative layout values, not the current app export. It does not evaluate DAX or Power Query.

All CFO figures are fictional, sourced from [`data/finance.py`](../../data/finance.py), and are not actual contracts, service prices, approved budget or realized benefit. Native Power BI rendering, data refresh and tenant publishing have not been verified. See the [deployment guide](../../docs/deployment.md#66-publish-and-open-power-bi-reports) for the open and publish steps.
