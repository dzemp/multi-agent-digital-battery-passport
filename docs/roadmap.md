# Roadmap and future investment

This draft lists enhancements, additional integrations, and configuration options on the Microsoft platform that could extend the battery passport MVP. The MVP already runs end to end on Azure with synthetic data. The items below build on that deployment and on gaps that the other guides name.

> **Draft, not a commitment.** Nothing here is scheduled, funded, or approved. Product names, availability, and licensing change often; confirm the current status of each service in your tenant and region before planning work.

**Out of scope:** regulatory and partner integrations, such as EU registry submission, physical battery identifiers, dataspace connectors, and verifiable credentials, as well as legal reviews and real data. They belong to a later, separate initiative.

## 1. How to read this roadmap

Each item has a priority:

| Priority | Meaning |
|---|---|
| **Next** | Configuration or hardening of services the MVP already uses. |
| **Later** | New Microsoft services or integrations that extend the deployment. |
| **Explore** | Candidates that the current guides do not yet assess. Evaluate them before committing. |

The **Source** column links to the guide that names the gap, where one exists.

## 2. Investment themes at a glance

| # | Theme | Main services | Why invest |
|---|---|---|---|
| 1 | [Agent governance with Microsoft Agent 365](#3-agent-governance-with-microsoft-agent-365) | Agent 365, Microsoft Entra Agent ID, Defender, Purview | For organizations with the separate required licenses, five agents could use a central inventory, identity, and security view beyond this app. |
| 2 | [Foundry guardrails](#4-foundry-guardrails) | Microsoft Foundry guardrails, Azure AI Content Safety | The model deployments use the default content filter, and no control scans the document text that agents read. |
| 3 | [Agent evaluation and optimization](#5-agent-evaluation-and-optimization) | Foundry evaluations, AI Red Teaming Agent, Agent Optimizer, GitHub Actions | Agent quality is checked by fixed cases on demand, not measured in a release gate or in operation. |
| 4 | [Responsible AI and model lifecycle](#6-responsible-ai-and-model-lifecycle) | Microsoft Foundry, Key Vault, Azure Monitor | Some provenance and telemetry gaps remain, and model changes need a repeatable check. |
| 5 | [Microsoft Defender for AI and cloud workloads](#7-microsoft-defender-for-ai-and-cloud-workloads) | Defender for Cloud, Defender XDR, Microsoft Sentinel | No runtime threat detection or AI security posture view covers the agents and their resources. |
| 6 | [Security and network hardening](#8-security-and-network-hardening) | Private Link, Front Door, Bastion, Entra | Several surfaces are public by design, and some controls are off by default. |
| 7 | [Data security with Microsoft Purview](#9-data-security-with-microsoft-purview) | Purview, Azure Policy | Agent inputs and outputs need classification and data security posture. |
| 8 | [Data and analytics](#10-data-and-analytics) | Fabric IQ, Foundry IQ (Azure AI Search), Content Understanding, Power BI | Analytics are limited to fixed reports, and agents read raw evidence documents. |
| 9 | [AI operations and observability](#11-ai-operations-and-observability) | Foundry Control Plane, Azure Monitor, API Management | Agent traces and cost are not yet managed across a fleet, and model traffic has no AI gateway. |
| 10 | [Reliability, scale, and operations](#12-reliability-scale-and-operations) | Azure SQL, Container Apps, Cost Management, GitHub | No zone redundancy or disaster recovery, and some operations are manual. |
| 11 | [App enhancements](#13-app-enhancements) | Container Apps, Foundry, Microsoft Teams | The app covers one production batch and a simple genealogy. |

## 3. Agent governance with Microsoft Agent 365

[Microsoft Agent 365](https://learn.microsoft.com/office365/servicedescriptions/microsoft-agent-365/microsoft-agent-365) is a Microsoft 365 service that provides a central control plane to discover, manage, govern, and secure AI agents, including agents built outside Microsoft 365. Microsoft announced its general availability on May 1, 2026. It brings together Microsoft Entra Agent ID, an agent registry, and Microsoft Defender, Intune, and Purview capabilities. **Agent 365 is separately licensed, either as a standalone per-user subscription or through Microsoft 365 E7, and may not be available in every tenant.** Feature availability depends on the licenses held, and some features are in preview. Treat the recommendations below as conditional on the organization owning the required licenses; otherwise, continue with the Microsoft platform controls described in the other roadmap sections.

Today, the MVP governs its agents inside the app: hosted agents have their own Entra identities with one app role each, prompt agents have no identity of their own, and the app's gates decide every action. Agent 365 would add an organization-wide view; it would not replace the app's gates or human decision rights.

| Item | Priority | Notes |
|---|---|---|
| Register the five agents (Supplier, Carbon, Issuance, Lifecycle, and Watch) in the Agent 365 registry with owner, purpose, autonomy level, model version, and permissions. | Next, if licensed | Gives an operational inventory alongside the model register in the [EU AI Act guide](eu-ai-act.md#5-model-register-and-change-review). |
| Adopt Microsoft Entra Agent ID for the hosted agents, and assess whether prompt agents should get their own identities. | Next, if licensed | Keep the invariant that no agent identity holds a human role ([architecture](architecture.md#4-identities-and-permissions)). |
| Add a risk classification for each agent based on its autonomy level and the potential impact of its actions. | Next | Follow the Cloud Adoption Framework's risk-based governance approach; use the classification to determine approval, monitoring, and access controls. |
| Apply agent lifecycle governance: sponsorship, access reviews, and retirement of old agent versions created by each `deploy`. | Later, if licensed | Each instruction change creates a new agent version. |
| Apply Conditional Access and Entra ID Protection to agent identities where Entra Agent ID supports them. | Later, if licensed | Confirm support and licensing for the target tenant before relying on these controls. |
| Use the Defender capabilities in Agent 365 for agent exposure, suspicious activity alerts, and tool invocation blocking where licensed. | Later, if licensed | Pairs with the Azure-side protection in [section 7](#7-microsoft-defender-for-ai-and-cloud-workloads). |
| Export Agent 365 analytics to Fabric or Power BI for agent usage and cost reporting. | Explore, if licensed | Preview capability; compare with the existing Agents workbook. |

## 4. Foundry guardrails

[Foundry guardrails](https://learn.microsoft.com/azure/foundry/guardrails/guardrails-overview) replace the earlier content filter configuration. A guardrail is a set of controls. Each control names a risk, the intervention points where it is scanned (user input, tool call, tool response, and output), and the action when it is detected (annotate, or annotate and block). A guardrail can be assigned to a model deployment or to an agent; for an agent, the agent's guardrail overrides the model's. Agent guardrails, the tool call and tool response intervention points, and several risks (task adherence, personally identifiable information, groundedness, and spotlighting) are in preview.

Today, all three model deployments use the default guardrail. Injection resistance relies on the role instructions, the typed `read_section` scopes, read budgets, and the app's gates ([threat model](threat-model.md#3-threat-register), T03). The text that `read_section` returns, such as supplier claims and audit reports, is exactly where an indirect prompt attack would arrive, and the default guardrail does not scan tool responses. The app now screens supplier evidence with the Prompt Shields API before the Supplier agent runs and routes detections to human review ([threat model](threat-model.md#3-threat-register), T03). On the deployed endpoint it detected 4 of 8 attack probes and flagged none of 9 clean ones (26 November 2026); paraphrased, role-play and field-name attacks got through. A project guardrail would add a second layer on the model calls themselves.

| Item | Priority | Notes |
|---|---|---|
| Define a project guardrail for the MVP with core harm categories, user prompt attacks, and indirect attacks, and assign it to the three model deployments. | Next | Configure Prompt Shields sensitivity separately; harm severity levels do not change it. |
| Assign an agent guardrail to the prompt agents that scans tool responses for indirect attacks and task adherence, because `read_section` returns supplier document text. | Next | Preview intervention points. Confirm how guardrails apply to the hosted Supplier and Lifecycle agents, which reach the model through Agent Framework. |
| Start each new control in annotate mode on a test agent version, measure false positives on the synthetic evidence, then switch to annotate and block. | Next | Assigning a guardrail takes effect immediately. Adverse and audit reports may legitimately mention harmful topics. |
| Handle a blocked request as an agent failure that defers to people, and record the detected risk and intervention point in telemetry. | Next | Extends the failure handling in [budgets, replays, and failures](agents-and-process.md#34-budgets-replays-and-failures); a block must never count as `no_issue`. |
| Version guardrails as code: model guardrails in Bicep and agent assignments in `deploy`, with an infrastructure test that keeps them aligned. | Next | Today nothing in `infra/` sets a guardrail, so any change made in the portal would drift. |
| Add personally identifiable information detection on output, so personal data in agent text is caught before it is stored. | Later | Preview. The data is synthetic, but the control prepares for documents that name auditors or contacts. |
| Test Spotlighting as a complementary indirect-injection control on representative supplier evidence, including paraphrased, role-play, instruction-like field-name and embedded-report cases. | Next | Preview and model-level support only; verify actual coverage and false positives. Keep the app's deterministic checks and Prompt Shields gate authoritative. |
| Validate supplier evidence against an explicit field allowlist and route unknown fields, including instruction-like field names, to human review before model use. | Next | Proposed mitigation for the field-name attack missed by Prompt Shields; this is a roadmap item only and is not implemented. Define compatibility handling for legitimate schema extensions. |
| Add a second check before accepting Supplier `no_issue` for unattended publication. Compare an independent verifier run with human review, and route disagreement or uncertainty to a person. | Next | Measure false `no_issue` and disagreement rates on a representative evaluation set. Keep publication held until the check succeeds; neither a second model nor sampling alone proves the result is safe. |
| Compare groundedness detection on the model deployments with the app's own citation and full reading checks. | Explore | Preview and available for models only. The app's `422 citation_required` and `422 grounding_required` checks stay authoritative. |

## 5. Agent evaluation and optimization

Today, `scenario evaluation` runs fixed, labeled cases from `agents/evaluation/cases.py`. A local run checks the scoring code; with `--apply`, it runs every case through temporary prompt agents and compares three model assignments ([scenario walkthrough](scenario-walkthrough.md#8-optional-evaluating-the-agent-instructions)). The score checks the action, the exact set of cited evidence, and that every role section was read. Results stay in a local JSON report, are not run on a schedule, and do not gate a release. The false Supplier `no_issue` rate is not measured ([EU AI Act known gaps](eu-ai-act.md#7-known-gaps)).

[Foundry evaluations](https://learn.microsoft.com/azure/foundry/concepts/evaluation-overview) can score test datasets, generated responses, or production traces, with built-in agent evaluators such as task adherence, task completion, intent resolution, tool call accuracy, tool selection, tool input accuracy, and tool output utilization, plus groundedness, safety, and custom evaluators.

| Item | Priority | Notes |
|---|---|---|
| Run `scenario evaluation --apply` with repeats and record pass rates, sample size, and the false Supplier `no_issue` rate with the model register. | Next | Closes the measurement gap named in the [EU AI Act guide](eu-ai-act.md#7-known-gaps). |
| Register the cases as a Foundry dataset, port the scorer (action, citation set, and full reading) as code-based custom evaluators, and add the built-in task adherence, tool call accuracy, tool selection, tool input accuracy, and groundedness evaluators. | Next | Results become visible and comparable in the Foundry project. Keep the existing local scorer as the offline check. |
| Set release thresholds per role, for example zero false `no_issue` results on escalation cases, and fail the release if they are missed. | Next | Required by the [threat model](threat-model.md#3-threat-register) (T05, T15). |
| Run the evaluation in CI with the [Foundry evaluation GitHub Action](https://learn.microsoft.com/azure/foundry/how-to/evaluation-github-action) when instructions, tools, or models change, and compare agent versions with its confidence intervals. | Next | The action is in beta. Depends on moving the CI workflow into `.github/workflows/` ([section 12](#12-reliability-scale-and-operations)). |
| Add more cases per role: evidence conflicts, missing sections, injected instructions in documents, and lifecycle events at state boundaries. | Next | The current cases are held out from the story data but are not production coverage. |
| Run the [AI Red Teaming Agent](https://learn.microsoft.com/azure/foundry/concepts/ai-red-teaming-agent) against each agent for indirect prompt injection, prohibited actions (with the agent's allowed actions as policy), sensitive data leakage, and task adherence, and track the attack success rate per release. | Next | Use the results to tune the guardrails in [section 4](#4-foundry-guardrails). Review findings before acting; false positives are possible. |
| Add Foundry risk and safety evaluators, including `builtin.indirect_attack`, to the release gate alongside quality and task evaluators. | Next | Set thresholds per role and test representative synthetic evidence; retain the app's deterministic checks as authoritative. |
| Turn on [continuous evaluation](https://learn.microsoft.com/azure/foundry/observability/how-to/continuous-scheduled-evaluation) for agent responses with an hourly run cap, and a scheduled dataset evaluation that replays the fixed cases daily. | Later | Billable model calls. Show scores next to the Agents workbook, and alert when a score drops below its threshold. |
| Build evaluation datasets from traces of synthetic runs, including human overrides of agent proposals, so new cases reflect observed behavior. | Later | Traces already flow to Application Insights. |
| Try the [Agent Optimizer](https://learn.microsoft.com/azure/foundry/agents/concepts/agent-optimizer-overview) to propose better instructions and tool descriptions, and use its model search to replace the three-assignment comparison. | Explore | Apply a candidate only after review: it must keep the role contract, the 1,600 character instruction limit, and the human decision rights. Each applied candidate is a model register change. |

## 6. Responsible AI and model lifecycle

| Item | Priority | Source |
|---|---|---|
| Remove input values from stored validation errors so rejected model output is never stored unmarked. | Next | [EU AI Act known gaps](eu-ai-act.md#7-known-gaps) |
| Add a check that prevents per-person oversight metrics in telemetry. | Next | [EU AI Act known gaps](eu-ai-act.md#7-known-gaps) |
| Set `versionUpgradeOption` to `NoAutoUpgrade` for each model deployment, and notify an owner about model retirement dates. | Next | The deployment stops working at retirement, so evaluate and explicitly deploy a replacement before that date. |
| Review and configure Azure OpenAI data, privacy, and abuse-monitoring settings for the deployed models. | Later | Document the selected settings and their effect on data handling before any real data is considered. |
| Complete Microsoft's Responsible AI Impact Assessment for the five agents and revisit it when their autonomy, data, or intended use changes. | Later | Treat the assessment as a product governance artifact, not a legal review. See Microsoft's [AI Impact Assessment resources](https://www.microsoft.com/en-us/ai/tools-practices). |
| Review the app's AI marks, override controls, and escalation screens against Microsoft's Human-AI Interaction (HAX) guidance. | Explore | Use the review to identify improvements to user understanding, feedback, and human control. |
| Move from the preview `agent-framework-foundry-hosting` package to a stable release when available. | Later | [Agents and process](agents-and-process.md#21-what-runs-where) |
| Sign AI provenance records with a Key Vault key so they are verifiable outside the database. | Later | [EU AI Act](eu-ai-act.md#45-limits-of-the-application-layer) |
| Revalidate GPT-6.2 Sol, or later models, through the Foundry project endpoint and the evaluation gate before any model change. | Later | [Model selection](agents-and-process.md#36-model-selection-and-request-capacity) |
| Evaluate decision models, such as Microsoft-Decision-1.1, for bounded classification and routing roles. | Explore | [Model selection](agents-and-process.md#36-model-selection-and-request-capacity) |

## 7. Microsoft Defender for AI and cloud workloads

[AI threat protection in Microsoft Defender for Cloud](https://learn.microsoft.com/azure/defender-for-cloud/ai-threat-protection) (the Defender for AI Services plan) detects runtime threats to generative AI workloads, such as jailbreak attempts, data leakage, data poisoning, and credential theft. It uses Prompt Shields signals and Microsoft threat intelligence, and sends alerts to Defender XDR. Since February 2026, it also covers agents built with Foundry. Microsoft states that protection for Foundry agents is currently free of charge, while model traffic is billed per scanned token after a 30-day trial. [AI security posture management](https://learn.microsoft.com/azure/defender-for-cloud/ai-security-posture), part of the Defender CSPM plan, discovers an AI bill of materials and shows recommendations and attack paths; its agent coverage is in preview.

Today, no Defender plan is enabled, and the alerts that exist are three log alert rules, off by default ([threat model](threat-model.md#3-threat-register), T14).

| Item | Priority | Notes |
|---|---|---|
| Enable the Defender for AI Services plan on the subscription that holds the Foundry resource, and route its alerts to an owner. | Next | Confirm current pricing; complements the guardrails in [section 4](#4-foundry-guardrails), which supply the Prompt Shields signals. |
| Decide whether to capture suspicious prompt evidence in alerts, and pass end user context with agent requests where the app knows the signed-in person. | Next | Evidence capture is a privacy setting; decide it before real documents are used. Agent tasks triggered by the app carry no end user. |
| Enable Defender CSPM with AI security posture management to inventory the models, agents, and connected data, and to review attack paths and externally reachable endpoints. | Later | Agent coverage is in preview. Use it to verify the network items in [section 8](#8-security-and-network-hardening). |
| Map the threat register to MITRE ATLAS and the OWASP Top 10 for LLM and Agentic Applications. | Later | Use the Cloud Adoption Framework's secure AI guidance to check that threat scenarios, mitigations, and evidence cover AI-specific attack paths. |
| Enable Defender for Containers (registry image scanning and runtime protection), Defender for Storage with malware scanning of uploaded evidence, Defender for Azure SQL, Defender for Key Vault, and Defender for Resource Manager. | Later | [Threat model](threat-model.md#3-threat-register) (T03, T11) |
| Connect Defender XDR and the Log Analytics workspace to Microsoft Sentinel, and correlate AI alerts with app, identity, and resource events. | Later | [Threat model](threat-model.md#3-threat-register) (T14) |
| Write response playbooks for AI alerts, starting with the agent stop switch `DBP_AGENT_MODE=off`. | Later | Uses an existing control; see the [threat model](threat-model.md#3-threat-register) (T03). |

## 8. Security and network hardening

| Item | Priority | Source |
|---|---|---|
| Enable the three log alert rules with an action group, and add diagnostic settings on the remaining resources (SQL audit and the Foundry account are covered). | Next | [Threat model](threat-model.md#3-threat-register) (T14) |
| Enable SQL auditing to Log Analytics. | Next | [Threat model](threat-model.md#7-residual-risk-and-production-gates) |
| Add a private endpoint for Durable Task Scheduler and remove the `0.0.0.0/0` allowlist. | Next | [Agents and process](agents-and-process.md#9-limits-and-later-work) |
| Replace the Easy Auth client secret with a federated credential or a Key Vault managed approach. | Next | [Threat model](threat-model.md#7-residual-risk-and-production-gates) |
| Add private endpoints for Azure Container Registry and an Azure Monitor Private Link Scope. | Later | [Threat model](threat-model.md#3-threat-register) (T10) |
| Assign Azure Policy built-ins for Foundry and AI resources, and use Azure Resource Graph to maintain an inventory of AI resources and agent-related dependencies. | Next | Align policy scope with the risk classification in [section 3](#3-agent-governance-with-microsoft-agent-365). |
| Route the Foundry agents subnet's outbound traffic through Azure Firewall with an explicit host-name allowlist. | Later | The subnet currently uses default outbound access or an optional NAT gateway; preserve only the egress needed for agent builds and runtime dependencies. |
| Place Azure Front Door with Web Application Firewall in front of the app, add rate limiting, and consider DDoS Protection. | Later | [Threat model](threat-model.md#3-threat-register) (T10, T12) |
| Replace developer VM RDP with Azure Bastion. | Later | [Threat model](threat-model.md#3-threat-register) (T10) |
| Apply Conditional Access, Entra ID Protection, Privileged Identity Management, access reviews, and per-person separation of duties for decision roles. | Later | [Threat model](threat-model.md#3-threat-register) (T01, T08, T11) |
| Lock the digest immutability policy with an approved retention period, and add customer-managed keys. | Later | [Architecture](architecture.md#6-what-this-architecture-does-not-cover) |

## 9. Data security with Microsoft Purview

| Item | Priority | Source |
|---|---|---|
| Deploy Microsoft Purview, including Data Security Posture Management for AI. | Later | [Threat model](threat-model.md#3-threat-register) (T06) |
| Apply sensitivity labels to evidence, passport snapshots, and Fabric reporting items. | Later | [Threat model](threat-model.md#3-threat-register) (T06) |
| Configure retention and deletion policies for Blob storage, Log Analytics, and backups. | Later | [Threat model](threat-model.md#4-grounding-injection-and-leakage-controls) |
| Enforce configuration with Azure Policy and resource locks. | Later | [Threat model](threat-model.md#3-threat-register) (T09) |
| Configure Microsoft Purview Audit, Data Loss Prevention for AI interactions, and Communication Compliance for Foundry. | Later | Select the workloads and data sources deliberately; prompt capture may conflict with the MVP's telemetry rule that prompts are never recorded. |
| Assess Purview Compliance Manager AI assessment templates as a way to track control evidence. | Explore | The templates support governance tracking and do not provide legal advice or establish compliance. |

## 10. Data and analytics

| Item | Priority | Source |
|---|---|---|
| Add telemetry for the age of pending second-life events, and show it in the workbooks. | Next | [Architecture](architecture.md#35-monitoring) |
| Publish the CFO report to the Fabric workspace from the command-line tool, alongside the CSO report. | Later | [Power BI reports](../fabric/powerbi/README.md) |
| Offer natural-language analytics through Fabric IQ, with known SQL and DAX answers as acceptance tests. Fabric IQ is in preview and replaces the deprecated Foundry Fabric data agent tool. | Explore | Not yet in the MVP |
| Evaluate Foundry IQ (Azure AI Search) for retrieval over larger evidence collections and regulatory sources for the Supplier and Watch agents. | Explore | Not yet in the MVP |
| Evaluate Azure Content Understanding to extract structured data from supplier documents before agents read them. | Explore | Not yet in the MVP |

## 11. AI operations and observability

[Foundry Control Plane](https://learn.microsoft.com/azure/foundry/control-plane/monitoring-across-fleet) provides a fleet view for agent health, usage, token consumption, alerts, and policies. The MVP already connects its Foundry project to Application Insights, but should verify that both hosted and prompt agents emit OpenTelemetry traces using the generative AI semantic conventions needed for cross-agent monitoring.

| Item | Priority | Source |
|---|---|---|
| Assess Foundry Control Plane for fleet health, token and cost tracking, anomaly detection, and operational alerts. | Next | Confirm feature availability, licensing, and the required OpenTelemetry instrumentation in the target tenant. |
| Verify that hosted and prompt agents emit OpenTelemetry generative AI traces, and that traces reach the connected Application Insights resource with useful agent and operation attributes. | Next | Preserve the telemetry allowlist: do not record prompts, evidence, names, emails, or tokens. |
| Put Azure API Management's AI gateway in front of model deployments to apply per-agent token limits, collect token metrics by consumer, and balance or fail over model traffic. | Later | The AI gateway capabilities vary by tier and feature status; confirm availability and cost before adopting it. |
| Assess semantic caching and content-safety policies in the AI gateway for suitable requests. | Explore | Validate that caching cannot return another user's or agent's context and does not bypass the Foundry guardrails in [section 4](#4-foundry-guardrails). |
| Create separate development, test, and production environments, and promote agent instructions, evaluation datasets, and agent versions together. | Later | Keep environment-specific identity, network, and data configuration separate. |
| Assess the Foundry Agent Service standard setup with customer-managed storage if agents later need server-side state. | Explore | The current workflow is intentionally bounded and does not require persistent agent state. |

## 12. Reliability, scale, and operations

| Item | Priority | Source |
|---|---|---|
| Set Azure Cost Management budgets and alerts, and run a load test. | Next | [Threat model](threat-model.md#3-threat-register) (T12) |
| Automate ledger digest anchoring instead of the manual `ledger anchor` command. | Next | [Ledger verification](../app/README.md#51-ledger-verification) |
| Move the CI workflow into `.github/workflows/` and enable Dependabot, secret scanning, and code scanning. | Next | [Threat model](threat-model.md#3-threat-register) (T11) |
| Add automatic retry or release for stuck agent work, beyond detection. | Later | [Agents and process](agents-and-process.md#34-budgets-replays-and-failures) |
| Enable zone redundancy for Container Apps, Azure SQL, and storage; add geo-replication and tested restores. | Later | [Threat model](threat-model.md#3-threat-register) (T13) |
| Replace the single application lock with per-battery or per-lot locking, and test concurrency with multiple replicas. | Later | [App design choices](../app/README.md#5-why-it-is-built-this-way) |
| Design organization-level isolation with separate tenants or row-level security. | Explore | [Architecture](architecture.md#6-what-this-architecture-does-not-cover) |

## 13. App enhancements

| Item | Priority | Source |
|---|---|---|
| Add a correction workflow that creates new passport versions for incorrect decisions. | Later | [Threat model](threat-model.md#7-residual-risk-and-production-gates) |
| Add human sampling of clean `no_issue` results, informed by the measured false `no_issue` rate. | Later | [Threat model](threat-model.md#7-residual-risk-and-production-gates) |
| Support more than one production batch view and genealogy beyond one predecessor and successor link. | Later | [App known limits](../app/README.md#9-known-limits) |
| Notify reviewers and compliance officers of new holds and cases in Microsoft Teams. | Explore | Not yet in the MVP |
| Assess a conversational agent for staff, with AI disclosure under Art. 50(1). | Explore | [EU AI Act](eu-ai-act.md#1-key-considerations-at-a-glance) |

## 14. Open decisions

- Does the organization own the separate Agent 365 licenses required for these capabilities, and which features are included and generally available in the target tenant?
- Which autonomy and impact criteria should determine an agent's risk classification and required controls?
- Which preview guardrail features (tool call and tool response scanning, task adherence, personally identifiable information) are acceptable before they are generally available?
- What release thresholds should each agent meet, and who approves a release that misses one?
- Which Defender plans justify their cost for an MVP with synthetic data, and who responds to their alerts?
- Should Purview capture prompts or agent responses when the current telemetry policy prohibits storing prompts?
- Does the AI gateway's added token controls, observability, and resilience justify its cost and tier requirements?
- Which items are needed together, for example Durable Task Scheduler private endpoint and Front Door, to change the network posture?
- Which explore items justify their added cost and preview risk?

## 15. Sources

Read on November 24 and 26, 2026.

- [Microsoft Agent 365 service description](https://learn.microsoft.com/office365/servicedescriptions/microsoft-agent-365/microsoft-agent-365)
- [Microsoft Agent 365 licensing FAQ](https://www.microsoft.com/licensing/faqs/122)
- [Microsoft Agent 365, now generally available](https://www.microsoft.com/en-us/security/blog/2026/05/01/microsoft-agent-365-now-generally-available-expands-capabilities-and-integrations/)
- [Guardrails and controls overview in Microsoft Foundry](https://learn.microsoft.com/azure/foundry/guardrails/guardrails-overview)
- [How to configure guardrails and controls in Microsoft Foundry](https://learn.microsoft.com/azure/foundry/guardrails/how-to-create-guardrails)
- [Evaluation in Microsoft Foundry](https://learn.microsoft.com/azure/foundry/concepts/evaluation-overview)
- [Agent evaluators](https://learn.microsoft.com/azure/foundry/concepts/evaluation-evaluators/agent-evaluators)
- [Continuous and scheduled evaluation for agents](https://learn.microsoft.com/azure/foundry/observability/how-to/continuous-scheduled-evaluation)
- [Run an evaluation in a GitHub Action](https://learn.microsoft.com/azure/foundry/how-to/evaluation-github-action)
- [AI Red Teaming Agent](https://learn.microsoft.com/azure/foundry/concepts/ai-red-teaming-agent)
- [Risk and safety evaluators in Microsoft Foundry](https://learn.microsoft.com/azure/foundry/concepts/evaluation-evaluators/risk-safety-evaluators)
- [Agent optimizer overview](https://learn.microsoft.com/azure/foundry/agents/concepts/agent-optimizer-overview)
- [Well-Architected Framework for AI workloads](https://learn.microsoft.com/azure/well-architected/ai/get-started)
- [AI workload architecture patterns](https://learn.microsoft.com/azure/well-architected/ai/architecture-pattern)
- [Responsible AI in Microsoft Foundry](https://learn.microsoft.com/azure/foundry/responsible-use-of-ai-overview)
- [Responsible AI for AI workloads](https://learn.microsoft.com/azure/well-architected/ai/responsible-ai)
- [Governance for AI agents in the Cloud Adoption Framework](https://learn.microsoft.com/azure/cloud-adoption-framework/ai-agents/architecture-governance)
- [Secure AI in the Cloud Adoption Framework](https://learn.microsoft.com/azure/cloud-adoption-framework/ai/secure)
- [Foundry Control Plane fleet monitoring](https://learn.microsoft.com/azure/foundry/control-plane/monitoring-across-fleet)
- [Microsoft Purview for Azure AI Foundry](https://learn.microsoft.com/purview/ai-azure-foundry)
- [Generative AI gateway capabilities in Azure API Management](https://learn.microsoft.com/azure/api-management/genai-gateway-capabilities)
- [AI gateway overview in Azure API Management](https://learn.microsoft.com/azure/api-management/ai-gateway-overview)
- [Human-AI Experience (HAX) Toolkit](https://www.microsoft.com/en-us/haxtoolkit/)
- [Microsoft Responsible AI Transparency Report](https://www.microsoft.com/en-us/ai/responsible-ai-transparency-report)
- [Model retirements and upgrades in Microsoft Foundry](https://learn.microsoft.com/azure/foundry/openai/concepts/model-retirements)
- [AI threat protection in Microsoft Defender for Cloud](https://learn.microsoft.com/azure/defender-for-cloud/ai-threat-protection)
- [Extending Defender's AI threat protection to Microsoft Foundry agents](https://techcommunity.microsoft.com/blog/microsoftdefendercloudblog/extending-defender%E2%80%99s-ai-threat-protection-to-microsoft-foundry-agents/4491927)
- [AI security posture management in Defender for Cloud](https://learn.microsoft.com/azure/defender-for-cloud/ai-security-posture)
- [Fabric IQ tool for Foundry (preview)](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/fabric-iq)

The other items come from this folder's guides and are linked in each table.
