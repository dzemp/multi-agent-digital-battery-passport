# AI and the EU AI Act

This guide explains the MVP's agent limits, human decisions, application-level AI text marking, and predeployment governance checks. Section 1 summarizes the design; later sections explain its controls and gaps.

The legal discussion retains the source review dated November 13, 2026, covering Regulation (EU) 2024/1689 (the AI Act) and the cited Regulation (EU) 2026/1744. This editorial revision does not reverify those sources or their application dates.

> **Not legal advice.** This is an engineering interpretation for a synthetic MVP, not a conformity assessment. Counsel has not reviewed it. Confirm the cited amendments, application dates, classification, and your duties before real use. Local tests do not establish end-to-end behavior in an Azure tenant.

## 1. Key considerations at a glance

| Topic | What applies | What you must do |
|---|---|---|
| **AI system** | The five agents, which run on Azure OpenAI general-purpose AI (GPAI) models in Foundry. Gates, the state machine, the carbon calculator and verifier, the connector and Fabric are deterministic code, not AI. | Keep binding decisions in code. |
| **Your role** | The design treats the organization operating an instance as its **provider and deployer** (Art. 3(3), 3(4)), not as the provider of the underlying GPAI model. Model modification needs a separate assessment. | Confirm your legal role and name an AI system owner ([3.1](#31-your-role)). |
| **Risk class** | The engineering assessment identifies no prohibited practice or high-risk use for the intended purpose (no Annex I safety component or Annex III area). | Obtain legal review and reassess on each classification trigger ([3.3](#33-classification-and-reclassification-triggers)). |
| **Autonomy** | The Act sets no levels; the MVP caps each agent. Watch **L2** recommends; Supplier **L3** acts and escalates risk; Carbon, Issuance and Lifecycle **L4** finish their step only through code-owned gates. No agent can override a gate, clear a concern or take a human decision. | Never widen an agent's actions without reassessing ([2.2](#22-autonomy-levels-of-the-five-agents)). |
| **Human in the loop** | Mandatory human oversight (Art. 14, 26) applies only to high-risk systems. By design, people alone resolve sourcing reviews, decide compliance cases, approve second life and activate Watch guidance. A battery with no flagged lot publishes without a person. | Assign named, briefed people to these roles; never give one to an agent identity ([2.3](#23-decisions-only-a-person-can-take)). |
| **Monitor and stop** | The Operator can inspect runs and refusals. `DBP_AGENT_MODE=off` stops agent execution but enables deterministic publication of eligible batteries without agent checks. | Review both effects before changing the setting ([2.5](#25-monitoring-and-stopping-the-agents)). |
| **AI text marking** (Art. 50(2), application date recorded as August 2, 2026) | The app records and labels accepted agent text. `POST /provenance/verify` matches normalized copies; rejected-output fragments can remain unmarked. These controls do not establish legal compliance. | Confirm applicability and keep marking enabled ([4](#4-how-ai-text-is-marked-and-detected-art-502)). |
| **Talking to people** (Art. 50(1)) | No agent chats with or writes to a natural person. | Disclose AI before adding one that does. |
| **AI literacy** (Art. 4) | Briefings defined, not delivered. | Brief every role holder ([6](#6-ai-literacy-art-4)). |
| **Research exclusion** (Art. 2(8)) | A private, synthetic deployment that only explores the design is most likely outside the Act. | It ends when the system is put into service or tested in real-world conditions; treat real data or real use as that point ([3.1](#31-your-role)). |
| **Open gaps** | No counsel review, no tenant evaluation, no detection of prompt attacks hidden in documents. | Close before real use ([7](#7-known-gaps)). |

## 2. Autonomy and human oversight

### 2.1 What the law requires

- **No autonomy levels and no general duty for a human in the loop.** The Act only says that AI systems operate "with varying levels of autonomy" (Art. 3(1)).
- **Human oversight is a high-risk duty.** Art. 14 requires high-risk systems to be effectively overseeable by people, with measures "commensurate with the risks, level of autonomy and context of use" (14(3)). Art. 26(2) requires deployers to assign oversight to people "who have the necessary competence, training and authority". Both apply only to high-risk systems, from 2 December 2027 (Annex III) or 2 August 2028 (Annex I).
- **The MVP's classification depends on its intended purpose.** The assessment assumes that people decide concerns, compliance cases, second life, and guidance. Human controls alone do not determine the legal classification. Removing a decision or letting an agent decide about safety or people requires reassessment.
- **Adjacent law.** GDPR Art. 22 protects people from decisions based solely on automated processing that significantly affect them. The agents decide nothing about natural persons; doing so is an excluded use.

If the system ever became high-risk, overseers would need to understand its limits, resist automation bias, interpret and override outputs and halt the system (Art. 14(4)). The labels, separate human reasons, decision trail and agent-mode switch cover part of this. Risk management, technical documentation, conformity assessment and registration are not designed for.

### 2.2 Autonomy levels of the five agents

The levels are the MVP's own scale, shown in the Operator console; they are not an EU category.

| Level | Meaning |
|---|---|
| **L2: recommends** | The output has no effect until a person approves it. |
| **L3: acts and escalates risk** | Takes a bounded action and sends concerns to people; cannot clear a concern. |
| **L4: autonomous within code gates** | Completes its step without a person, but only through code-owned checks it cannot override; a failed check stops it. |

No agent goes beyond L4: none sets its own goals, gains tools, picks the next step, messages another agent, keeps memory between tasks or decides about a person.

| Agent | Level | May do alone | Can never do | Enforced by the app |
|---|---|---|---|---|
| Supplier | L3 | Open a sourcing review or compliance case; record `no_issue` for an unflagged lot, citing its evidence | Clear a lot with an open review or case; decide a case | `409 review_open`, `409 compliance_case_open`, `422 citation_required`; no decide action |
| Carbon | L4 | Ask the app to calculate; a separate verifier checks | Supply figures; calculate with missing measurements | App calculates; `409 inputs_incomplete` |
| Issuance | L4 | Ask the app to publish a passport version | Publish while any gate fails | App rechecks every gate; `409 publication_blocked` |
| Lifecycle | L4 | Apply the signed partner event the connector verified and routed | Approve a second life; apply another event | State machine; `422 action_mismatch`; `403` on the second-life decision |
| Watch | L2 | Store a pending, source-cited guidance candidate | Activate guidance or change a gate | `422 source_citations`; only `Passport.PolicyApprover` activates |

For every agent the app, not the prompt, also enforces:

- **Scope:** only its role's task sections (`403 tool_scope`). A hosted callback must supply a registered task/attempt pair (`403 unknown_attempt`). Each attempt allows at most eight reads (`429 tool_budget`), and all required sections must be read during that attempt (`422 grounding_required`). The app accepts only actions in the role's list (`403 action_scope`); changed inputs invalidate the answer (`409 stale_task`).
- **Budget:** 120 seconds per activation and 2,500 output tokens per model call; prompt agents also 24,000 tokens per activation.
- **No side channels:** the workflow order (Supplier, Carbon, Issuance) is fixed in code. Evidence is untrusted data, never instructions. Errors and refusals are stored, never replaced with a permissive answer. Hosted agent identities can only read task sections.

See [`agent_contracts.py`](../app/passport/agent_contracts.py), [`agent_service.py`](../app/passport/agent_service.py) and [agents and process](agents-and-process.md#32-the-agents-in-the-process).

### 2.3 Decisions only a person can take

| Decision | Role | The person must give | Why human |
|---|---|---|---|
| Resolve a sourcing review | `Passport.Reviewer` | New evidence, own reason, `expected_row_version` | Clearing a concern is a due diligence judgment |
| Decide a compliance case | `Passport.Compliance` | `keep_hold`, or `accept_with_action_plan` with owner, due date and evidence | Accepting mitigated risk is a business and legal decision |
| Approve or reject a second life | `Passport.Reviewer` | Decision, reason, `expected_row_version`; the request must cite a safety assessment | Keeps agents out of battery safety decisions |
| Activate or reject Watch guidance | `Passport.PolicyApprover` | Reason (5-400 characters), `expected_row_version` | Adopting AI-drafted guidance is an organizational decision; it becomes the active guidance on `/policy` and Watch's baseline. No gate or other agent reads it. |

Every human decision is a ledger row with actor and reason. To keep oversight meaningful:

- the agent's reason is labeled and kept apart from the person's own reason and evidence;
- a Reviewer can open a review on any lot at any time (`POST /reviews`);
- telemetry reports by role, never per person, because measuring decision speed or override rates would be staff monitoring;
- `Passport.PolicyApprover` is declared but not assigned by deployment.

Screens: [agents and process](agents-and-process.md#4-human-in-the-loop).

### 2.4 What runs without a person, and its main risk

A Supplier `no_issue` on an unflagged lot, the Carbon calculation, Issuance publication and every Lifecycle event except the second-life decision run without a person. So a battery whose critical lots raise no concern is published by agents and gates alone: people handle exceptions, not every battery.

**Main residual risk: a false Supplier `no_issue`.** A missed concern publishes without any person seeing it. Mitigations: mandatory citation and full reading, Prompt Shields screening of every supplier document before the agent runs (a detection goes to a person, not to the model), an injection guard in the instructions, the evaluation cases `supplier-conflict`, `supplier-injection` and `supplier-cobalt-escalation` ([`cases.py`](../agents/evaluation/cases.py)), and Reviewers' own reviews. The miss rate with the deployed models is **not measured**. Prompt Shields itself detected 4 of 8 synthetic attack probes on the deployed endpoint (26 November 2026), so it does not replace the other mitigations. Before real use, run `python -m dbp_mvp scenario evaluation --apply` and decide whether people should sample clean publications.

### 2.5 Monitoring and stopping the agents

- **Monitor.** `GET /agents/runs` and the decision trail (`/trail/{id}`) show every run, refusal and AI text. The `agent.run` event in Application Insights holds role, outcome, model, tokens and latency, but no prompts or evidence.
- **Stop.** Set `DBP_AGENT_MODE=off` on the Container App (a new revision). The app refuses agent runs and agent endpoints (`409`) and starts no agent workflows. The connector still verifies and logs partner events, but none is applied. Eligible batteries are published by the deterministic clean-case rule without an agent check, so Reviewers must open reviews themselves.

## 3. Roles, intended purpose and classification

### 3.1 Your role

| Party | Role under the AI Act |
|---|---|
| The organization operating an instance (in the scenario, the fictional Contoso Battery AB) | Assessed here as **provider and deployer** of the agent system; confirm the role for actual use and model modifications |
| OpenAI; Microsoft | GPAI model provider; supplier of the models and Foundry. Neither carries your transparency duties. |
| Suppliers, OEM partner, recycler, authority users | Affected parties or recipients of outputs |
| Publishers of this source code | Free and open-source components (Art. 2(12)); no model, no running service |

**The AI system owner** maintains the intended purpose, triggers, model register, human role assignments, and literacy record. The owner also keeps application-level marking enabled and answers requests to verify agent text.

**Research exclusion.** The Act does not apply to research, testing or development before a system is placed on the market or put into service (Art. 2(8)). A local run, or a private synthetic deployment that only explores the design, most likely qualifies. Legally, the exclusion ends when the system is placed on the market, put into service or tested in real-world conditions; the Act does not draw the line between synthetic and real data. As a conservative project rule, treat the first real supplier document or partner event, operational use of agent outputs or real work by staff as that point. The open-source exemption does not cover Art. 50.

### 3.2 Intended purpose and excluded uses

**Intended purpose.** Agents help staff maintain digital battery passports from supplier deliveries: they propose sourcing reviews and compliance escalations about lots and companies, request gated footprint calculation and publication, apply partner lifecycle events through a state machine and draft guidance from cited sources. People decide every concern, case, second life and guidance version.

**Excluded uses**, each requiring a fresh classification first: assessing natural persons; monitoring or evaluating staff; using an agent output as a safety component of a battery, vehicle, or product under EU harmonization law; letting an agent message natural persons; publishing agent text to inform the public.

### 3.3 Classification and reclassification triggers

| Agent | Basis for the engineering classification, subject to legal review |
|---|---|
| Supplier | Assesses lots and companies; people clear concerns |
| Carbon | The app calculates; a separate verifier checks |
| Issuance | The app's gates decide publication |
| Lifecycle | The state machine applies signed events; a person decides second life |
| Watch | Guidance stays inactive until a `Passport.PolicyApprover` decides |

**Triggers** for a fresh assessment: any excluded use; removing a human decision in section 2.3; a new action or decision right for an agent, including through a model change; and the Commission's final high-risk classification guidelines (still a draft when read). Models are pinned in [`agent_contracts.py`](../app/passport/agent_contracts.py) and listed in section 5.

## 4. How AI text is marked and detected (Art. 50(2))

Art. 50(2) addresses machine-readable marking and detection of synthetic content, subject to its scope and technical-feasibility provisions. The MVP records provenance in the app, independently of the model. The implementation and limits below are engineering controls, not a determination that the legal requirement is met.

### 4.1 What is marked

Marked: every agent `reason`, and Watch's `policy_title` and `policy_text`. Not marked, because they are not AI text that informs people: actions, evidence IDs, codes, and passport values, versions, carbon figures and reports, which deterministic code produces. Failed activations store an error, not an agent answer, but a validation error can quote up to 400 characters of the rejected model output without a mark (see [section 7](#7-known-gaps)). Public passport pages show no agent text.

### 4.2 Provenance record

`AgentService.finish` writes one row per agent text field to the append-only ledger table `dbp.AiContentProvenance`, in the same transaction as the text ([`provenance.py`](../app/passport/provenance.py)). Each row holds a `provenance_id` (`PV-` and 61 hex characters); the task, role, field, and storage locations; `ai_generated` and the IPTC digital source type (`trainedAlgorithmicMedia` for models, `algorithmicMedia` for the scripted runner, which is not AI); agent, model, version, and hosting; and the SHA-256 and length of the normalized text. It names no people. Reviews, policy candidates, and the active policy link to their text's `provenance_id` and record whether a person or AI wrote it.

### 4.3 Marks on every surface

| Surface | Mark |
|---|---|
| `GET /agents/runs`, `GET /policy/candidates` | `provenance` per text field: `provenance_id`, `ai_generated`, `digital_source_type` |
| `GET /passports/{id}/view` (`decisions`) | `reason_origin` and `reason_provenance`, separate from the person's `resolution_reason` |
| Screens and the scenario run register | Visible "AI-generated", "AI proposal" or "AI-drafted" labels with `data-ai-generated="true"` and `data-provenance-id`; scripted text is marked "(not AI)" |

### 4.4 Detection: `POST /provenance/verify`

`Passport.Authority`, `Passport.Compliance`, and `Passport.Operator` send either `text` (1-20,000 characters) or `provenance_id`. The app normalizes the text (Unicode NFC, collapsed whitespace), compares its SHA-256, and returns `match` with each matching record's role, field, model, version, and time, but never the fingerprint, text, or a person:

```json
{"text": "The cobalt claim names a mine that the transport record does not mention."}
```

Invalid input returns `422` (`unknown_fields`, `text_or_provenance_id`, `bad_provenance_id` or `bad_text`).

### 4.5 Limits of the application layer

- **Normalized copies only.** Matching ignores Unicode-form and whitespace differences. Paraphrased, translated, or otherwise edited text does not match. This is the implementation's limit, not a conclusion about the limits of technical feasibility under the Act.
- **No signature.** Tamper evidence comes from the SQL ledger ([app reference](../app/README.md#51-ledger-verification)); sign with a Key Vault key if records must be verifiable outside your database.
- **Own method.** It is published here, not an industry standard. Local runs keep the records in memory, without a ledger.

## 5. Model register and change review

The default assignments are pinned in [`agent_contracts.py`](../app/passport/agent_contracts.py). Confirm deployed versions and regional availability in the target environment.

| Role | Model | Pinned version |
|---|---|---|
| Supplier (hosted) | `gpt-6.1-sol` | `2026-09-29` |
| Carbon (prompt) | `gpt-5.6-luna` | `2026-07-09` |
| Issuance (prompt) | `gpt-5.6-luna` | `2026-07-09` |
| Lifecycle (hosted) | `gpt-5.6-luna` | `2026-07-09` |
| Watch (prompt) | `gpt-5.6-terra` | `2026-07-09` |

Before changing a model, review its deployment terms, task fit, and effect on the intended purpose and classification. Run `python -m dbp_mvp scenario evaluation --apply` and retain the results with the model register. An evaluation through temporary prompt agents does not establish hosted-agent transport compatibility; validate that path separately. Keep the application gates and human decision rights unchanged unless the design is explicitly reassessed.

## 6. AI literacy (Art. 4)

Providers and deployers must take measures to support the AI literacy of people who operate or use the system for them; no guaranteed level per person is required. Every briefing covers:

1. what each agent may and may not decide, and the human decisions it cannot bypass (section 2);
2. automation bias: an "AI proposal" is a starting point, so check the evidence and give your own reason;
3. prompt injection: instructions inside a supplier document are a warning sign to report; a review opened by "Prompt Shields" means the model never read the flagged evidence, so check it yourself before resolving;
4. the provenance labels and `POST /provenance/verify`;
5. synthetic, illustrative figures carry no assurance;
6. whom to contact about a suspicious answer or misuse, and who may set `DBP_AGENT_MODE=off`.

| Role | Additional focus | Length |
|---|---|---|
| `Passport.Reviewer` | Resolving agent-opened reviews with your own evidence; second life | 30 min |
| `Passport.Compliance` | Deciding escalated cases; never citing AI text as evidence | 30 min |
| `Passport.PolicyApprover` | Checking AI-drafted guidance against its sources before adopting it | 30 min |
| `Passport.Operator` | Runs, refusals, provenance; when to stop agent mode | 45 min |
| Administrators | Role assignment (no human roles for agents), model versions, telemetry without prompts | 45 min |
| Developers | Agent contracts, provenance tests, evaluation before a model change | 60 min |

Record each briefing per person (person, role, briefing version, date, delivered by, confirmed understanding, next review) and repeat it after a model change, a new agent role or a change in autonomy.

## 7. Known gaps

| Gap | Risk | Next step |
|---|---|---|
| No counsel review, no AI system register | The classification is an engineering reading | Counsel confirms role, class and duties |
| No measured model quality established by this guide | Accuracy, false `no_issue` rate, and deployed refusal behavior remain unquantified here | Run `scenario evaluation --apply` and record results and sample size in the target environment |
| A failed activation's error message can quote rejected model output | Unmarked AI text fragments in staff-only run results | Drop input values from stored validation errors |
| Prompt Shields screens supplier evidence, but on the deployed endpoint it detected only 4 of 8 synthetic attack probes (0 of 9 clean probes flagged, 26 November 2026); detection on real supplier formats is not measured; it screens text only and is trained mainly on English | A missed attack reaches the model, where only the instruction guard, scopes and human decisions remain | Keep the report with the model register, add a second guardrail layer, and repeat with real document formats |
| No test prevents per-person oversight metrics | Staff monitoring is an excluded use | Keep telemetry role-level; add a check |
| Briefings defined but not delivered | Role holders may misunderstand agent limits or their own duties | Deliver and record the briefings in section 6 |
| Real data | May change legal applicability and introduce personal-data processing | Reassess applicability, determine whether a data protection impact assessment is required, and pseudonymize actor identities |

## 8. Before you deploy

- [ ] Confirm with counsel that your use matches the intended purpose and avoids the excluded uses.
- [ ] Name the AI system owner; record your provider and deployer role in an AI system register.
- [ ] Assign named, briefed people to `Passport.Reviewer`, `Passport.Compliance`, `Passport.PolicyApprover` and `Passport.Operator`, never to an agent identity, and decide who may stop agent mode.
- [ ] Keep the agents' actions, gates and human decisions in section 2, or reassess first.
- [ ] Run `python -m dbp_mvp scenario evaluation --apply` and keep the results with the model register.
- [ ] Keep `AiContentProvenance`, the labels, and `POST /provenance/verify` with their tests passing; review model changes as described in section 5.
- [ ] Use synthetic data only until the real-data gap in section 7 is closed.

## 9. Sources

Read on 13 November 2026, except where noted.

- Regulation (EU) 2024/1689 (AI Act): <https://publications.europa.eu/resource/celex/32024R1689>. Articles 14 and 26 rechecked on 16 November 2026 in an unofficial reproduction of the original text: <https://artificialintelligenceact.eu/article/14/>, <https://artificialintelligenceact.eu/article/26/>
- Regulation (EU) 2026/1744 (Digital Omnibus on AI): <http://data.europa.eu/eli/reg/2026/1744/oj>
- Commission guidelines on Art. 50 transparency obligations, C(2026) 7702: <https://ec.europa.eu/newsroom/dae/redirection/document/131215>
- Code of Practice on transparency of AI-generated content: <https://digital-strategy.ec.europa.eu/en/policies/code-practice-ai-generated-content>
- Draft Commission guidelines on high-risk classification: <https://digital-strategy.ec.europa.eu/en/policies/guidelines-ai-high-risk-systems>
- Regulation (EU) 2016/679 (GDPR), Art. 22, checked on 16 November 2026 in an unofficial reproduction: <https://gdpr-info.eu/art-22-gdpr/>
- Microsoft Learn, *Content provenance* (ms.date 3 November 2026): <https://learn.microsoft.com/en-us/azure/foundry/responsible-ai/content-safety/provenance-disclosure>
- IPTC digital source type vocabulary: <https://cv.iptc.org/newscodes/digitalsourcetype/>
