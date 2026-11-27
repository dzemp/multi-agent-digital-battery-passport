# Security and responsible-AI threat model

**Status:** Recommended controls and residual risks for the synthetic battery passport MVP. This is an engineering design document, not legal advice, a production security assessment or a compliance certification. The deployment uses synthetic data; controls marked for production are not claims about the deployed MVP.

The model reflects the [architecture](architecture.md), [app roles and interfaces](../app/README.md), [agent workflow](agents-and-process.md) and [EU AI Act analysis](eu-ai-act.md). Review it whenever the architecture, data types, identity model, agent capabilities or intended use changes.

## 1. Scope and protected assets

| Item | Contents |
|---|---|
| **In scope** | Container Apps passport app with Easy Auth; Azure SQL ledger and outbox; application and digest Blob storage; Microsoft Foundry (two hosted and three prompt agents), including the Content Safety Prompt Shields API of its account; Durable Task Scheduler and Microsoft Agent Framework workflow; mock connector; Fabric reporting; Azure Container Registry (ACR); Log Analytics and Application Insights; developer VM; VNet/private endpoints; two-stage deployment |
| **Assets** | Passport versions and snapshots; restricted field groups; synthetic supplier evidence and lot claims; holds, compliance cases and second-life decisions; ledger digests; party registry and public keys; agent instructions, model deployments and AI provenance records; Entra identities and app roles; telemetry |
| **Threat actors** | Anonymous internet user; signed-in user with an inappropriate role; malicious or compromised supplier or partner; poisoned document content (indirect prompt injection); compromised agent or model output; insider or privileged operator; software supply-chain attacker; operator unable to continue service |
| **Not provided by this MVP** | Real supplier integration; verifiable credentials; Union registry integration; high availability, disaster recovery or multi-region operation; organization-level agent isolation; customer-managed keys; Microsoft Purview deployment; locked digest immutability |

## 2. Trust boundaries, isolation and invariants

### 2.1 Trust boundaries

| Boundary | Crossing | Enforcement point |
|---|---|---|
| B1 Internet → app | Browser/API requests | Container Apps HTTPS ingress, Easy Auth, app roles and field policy |
| B2 Partner → connector | Signed claims and lifecycle events | Ed25519 verification against the party registry; allowed lots/event types and revocation |
| B3 Package/Fabric → app | Package imports and reporting | Schema and content checks; Operator-only reporting endpoint |
| B4a Supplier evidence → agent | Third-party document text before the Supplier agent reads it | Prompt Shields `shieldPrompt` call per document from the app's managed identity over the private endpoint; a detection or a service error stops the run and routes to a person |
| B4 Agent → app tools | `read_section` calls and decision JSON | Tool/action scope, budgets, grounding and stale-input checks |
| B5 App → data | SQL and Blob access | Managed identities, private endpoints and per-table SQL grants |
| B6 App → orchestration | Durable Task Scheduler | Private endpoint with public network access disabled; managed-identity authentication |
| B7 Operator → Azure | ARM, developer VM and ACR operations | Entra RBAC and two-stage deployment |

### 2.2 Isolation

| Layer | Isolated in the MVP | Enforcement | Not isolated / limitation |
|---|---|---|---|
| **Network** | SQL, both storage accounts, Foundry and Durable Task Scheduler | Public access disabled; private endpoints and private DNS in the `endpoints` subnet; hosted agents run in the `agents` subnet | App ingress, ACR, monitoring ingestion/query and Fabric are public. RDP is limited to `client_ip` or Defender JIT, depending on configuration. |
| **Identity** | Human users, app, hosted agents and deployment | Separate Entra identities; hosted agents have only their specific app role and are on the sign-in allowlist; managed identities for service access, including Cognitive Services User on the Foundry account for Prompt Shields (granted by the template in `bootstrap`, not by `deploy`); Entra-only SQL; no SQL password or storage key | Easy Auth uses a client secret stored as a Container Apps secret and renewed by deployment. |
| **Authorization** | App roles and field groups | Server-side field policy derives roles from the principal; `?role=` and headers do not grant roles; View-as selects only roles assigned to the account | Field groups are an MVP policy, not a legal classification of Battery Regulation data. |
| **Partner** | Partner signing keys, lots and event types | Party registry associates keys with parties and allowed lots/event types; revoked or unknown keys are refused | No separate database or runtime trust boundary per supplier or organization. |
| **Agent** | Agent tool access and decision rights | Role-scoped task sections and actions; hosted callbacks require a registered task/attempt pair; per-attempt read budgets; application gates enforce decisions | No separate runtime or data boundary per organization. Attempt registration does not provide organization-level isolation. |
| **Data** | App database and object access | App SQL identity has per-table grants, no `DELETE` or DDL and no `report` schema access; digest storage is separate and app access is read-only | Digest immutability is unlocked. No customer-managed key is configured. |
| **Environment** | Each deployment | Separate resource group, VNet, Foundry project and SQL database per deployment | A deployment is one trust domain; the MVP is not a multi-tenant service. |

### 2.3 Security and decision invariants

- Agents do not hold human decision rights: resolving reviews, deciding compliance cases, approving second life or activating Watch guidance. The app's gates, not agent instructions, enforce those decisions.
- No supplier evidence reaches a model unscreened: Prompt Shields screens every document first, and a detection or a screening error stops the Supplier run instead of skipping the check.
- A committed passport version is not overwritten; corrections create new versions.
- Persisted agent text fields have `AiContentProvenance` records and visible AI marks on supported API and screen surfaces. See the [EU AI Act guide](eu-ai-act.md) for marking scope and known exceptions.
- `DBP_AGENT_MODE=off` stops agent routes without disabling human workflows. It also enables deterministic publication of eligible batteries without agent checks, so it is not a publication stop switch.

## 3. Threat register

**Status key:** **MVP** means implemented in code or infrastructure. *Off by default*, *optional* and *platform default* identify qualifications. **Production** means not built in the MVP and to address before real-data or production use.

| ID | Threat (STRIDE) | MVP (implemented) | Production (to address) | Verification evidence | Proposed owner |
|---|---|---|---|---|---|
| T01 | Identity spoofing (S) | Entra ID with Easy Auth; roles taken only from the token; forged `?role=`/header ignored; View-as limited to assigned roles and forged selections fail closed; hosted-agent identities on sign-in allowlist with one agent role each; managed identities to SQL, Blob, Foundry, ACR and Durable Task Scheduler; Entra-only SQL; storage shared-key access disabled; Foundry local authentication disabled | Conditional Access (MFA, phishing-resistant methods for staff roles, compliant devices); Entra ID Protection risk policies; replace the 30-day Easy Auth client secret with a federated credential or Key Vault-managed approach | Token without role → `403`; forged role parameter/header ignored; identity not on allowlist refused | Identity/IAM |
| T02 | Cross-organization or cross-object access (I, E) | Server-side field policy; minimal public view; held passport shows anonymous users only “Not yet issued”; restricted endpoints require roles; OEM warranty/recall access requires one fleet role and imported pack membership; party registry allowlists lots and event types | Organization-level isolation (separate tenant or row-level security); per-object authorization beyond the scoped aftersales views; access reviews and PIM for decision-role groups; field-group mapping against Battery Regulation Annex XIII | Role × field-group matrix using independent accounts; fleet-role and pack-membership checks; supplier key posting for another lot → `403 lot_not_allowed` | Application security |
| T03 | Prompt injection and tool misuse (T, E) | One read-only `read_section` tool with sections typed as `Literal` per role; registered task and per-attempt IDs; eight reads per attempt; `403 tool_scope`/`action_scope`; `429 tool_budget`; strict decision schema; instruction guard treats document text as data; Prompt Shields screens every supplier evidence document before any model reads it, and a detection opens a sourcing review for a person instead of running the agent (fails closed); agents have no human decision rights; Foundry default content filter; `supplier-injection` evaluation case; `DBP_AGENT_MODE=off` stop switch | Add a strict supplier-evidence field allowlist and route unknown fields for review (proposed, not implemented); add an independent second check before accepting Supplier `no_issue` for unattended publication, with unresolved or failed checks routed to a person; add a second guardrail layer for attacks Prompt Shields misses (measured 26 November 2026: 4 of 8 attack probes detected, 0 of 9 clean probes flagged); test Spotlighting as an indirect-injection control; repeat measurement on real supplier formats; extend screening to other roles if they read third-party text; Foundry AI red-teaming; Defender for Cloud AI threat protection | Unknown supplier fields are rejected or held; false Supplier `no_issue` rate and second-check disagreement rate with sample size; Spotlighting and guardrail false-positive/attack-detection results; unknown attempt and ninth-read refusals; red-team attack success rate | AI platform and application security |
| T04 | Forged, stale or false partner claims (S, T) | Ed25519 signature over canonical payload; signing key must match issuer; audience check; expired messages refused; messages issued more than five minutes in the future refused; revoked key, disallowed lot/event type or absent agreement refused; payload capped at 8,000 bytes; message-ID replay handling (**no nonce**): same ID and payload returns stored result, same ID with different payload returns `409 message_conflict`, refused messages do not claim the ID; coded quarantine and human review | Verifiable credentials and a standards-aligned partner connector; partner keys in HSM-backed Key Vault with rotation; party registry rooted in an independently governed trust source | `scenario credential_negatives`: forged, misaddressed, expired, revoked, unknown-key and replayed messages | Partner integration |
| T05 | Fabricated citations and incorrect calculations (T) | `422 grounding_required` until every role section is read; Supplier `evidence_ids` must exist in the lot's evidence (`422 citation_required`); app calculates footprints from versioned factor sets; illustrative labels; `409 stale_task`; evaluation cases `supplier-conflict` and `supplier-cobalt-escalation` | Foundry groundedness/task-adherence evaluations with release thresholds; carbon method aligned to applicable delegated act; sampling of clean publications | False Supplier `no_issue` rate with sample size (**not measured**); citation-existence test; calculation regression test | Data and calculation method |
| T06 | Sensitive-data leakage (I) | Field-policy projection; agents read only their role's sections (agent records capped at 8,000 encoded JSON bytes); telemetry allowlist excludes evidence, free text, prompts, names, emails and tokens; sensitive-data recording and Live Metrics disabled; connector log excludes payloads; provenance verification never returns text; DataZoneStandard model deployments; private endpoints for data/model planes | Purview DSPM for AI and sensitivity labels; Defender for Cloud AI threat protection; customer-managed keys where required; DPIA before real personal data | Leakage matrix by role and channel; canary strings absent from telemetry and other roles' views | Data protection and application |
| T07 | Replay, stale approval or concurrent update (T) | Command-ID idempotency checked against payload hash; `expected_row_version` → `409 version_conflict`; `409 stale_task` and `409 request_not_pending`; transactional outbox; committed versions not overwritten | Re-test concurrency with multiple replicas and distributed workers | Stale-form and concurrent-approval tests; scenario rerun creates no duplicate events | Application and data platform |
| T08 | Hold or separation-of-duties bypass (E) | Gated agent path Supplier → Carbon → Issuance; `409 review_open`/`compliance_case_open`; only people resolve reviews, decide cases, approve second life and activate guidance; role separation for Reviewer, Compliance and PolicyApprover; `422 reason_required`; human decisions retain the real user identity | Enforce separation of duties per person where an account has multiple roles; PIM and access reviews for decision roles; name and brief role holders | Agent cannot publish a held battery; Lifecycle agent receives `403` for second-life decision | Compliance and operational release |
| T09 | Evidence or ledger tampering (T, R) | SQL ledger tables; digests in separate storage account where app access is read-only; app SQL identity has no `DELETE` or DDL; evidence SHA-256 rechecked after upload, blob version ID recorded, overwrite disabled; blob versioning and seven-day soft delete; digest immutability policy is one day and **unlocked**; SQL server auditing of sign-ins and permission, principal, role and schema changes to Log Analytics (no statement auditing) | Lock immutability with an approved retention period; review audit retention and statement auditing; export Activity Logs; resource locks and Azure Policy | `ledger.verified` succeeds; digest verification detects tampering; a test sign-in appears in `SQLSecurityAuditEvents` | Security and audit |
| T10 | Public endpoint or network compromise (I, D) | Private endpoints/private DNS for SQL, both storage accounts, Foundry and Durable Task Scheduler, with public access disabled; storage TLS 1.2 minimum; developer VM RDP limited to `client_ip` or Defender JIT (optional); NAT gateway for agents subnet (optional). Public by design: app ingress, ACR, monitoring ingestion/query and Fabric | Front Door with WAF; Azure Bastion instead of RDP; ACR private endpoint; Azure Monitor Private Link Scope; DDoS Protection and applicable Defender for Cloud plans | External scan shows only intended endpoints; data planes unreachable from internet | Cloud platform and network security |
| T11 | Privileged-account or supply-chain compromise (E, T) | Least-privilege data roles; ACR admin disabled and app pulls with managed identity; no SQL passwords or storage keys; Easy Auth client secret stored as a Container Apps secret; app image built from a digest-pinned base image with hash-locked Python packages and without pip; developer VM installers pinned by version and SHA-256 and signature-checked; Python dependencies pinned; models pinned in `agent_contracts.py`; preflight checks operator roles (PIM activation may be a tenant prerequisite) | PIM with eligible, time-bound Azure and Entra roles; break-glass accounts; Defender for Containers image scanning; image signing; Dependabot, secret scanning and code scanning | Role inventory; scan results; pinned-version check | Cloud security and release engineering |
| T12 | Denial of service and runaway inference cost (D) | Eight reads per attempt; 2,500 output tokens per model call; 120-second app invocation timeout and 115-second hosted deadline; prompt activations capped at 24,000 reported tokens; model capacity defaults to 200 (verify effective RPM/TPM); passport Container App capped at 3 replicas of 1 vCPU and 2 GiB each; serverless SQL capacity cap; `DBP_AGENT_MODE=off` stops model calls | Azure Cost Management budgets and alerts; edge rate limiting (Front Door/WAF or API Management); load test | Ninth read → `429`; token/time-budget refusal; load test; budget alert fires | Service operations and FinOps |
| T13 | Data loss and operator/provider exit (D) | Azure SQL automated backups and point-in-time restore (platform default); blob versioning and seven-day soft delete; snapshot documents with SHA-256; reporting export to Fabric | Zone redundancy; geo-replication/geo-backup and disaster recovery; tested restores; exit/re-hosting plan (Battery Regulation Arts. 77(8), 78(e)); standard data-model export | Restore test; export and re-hosting test | Business continuity and data platform |
| T14 | Telemetry misuse and monitoring blind spots (R, I) | Allowlisted business events with coded dimensions; Log Analytics with 30-day retention; Foundry connected to Application Insights; four persona workbooks; three alert rules (created **off by default**, enabled by `enable_alerts`); SQL security audit events in Log Analytics; Foundry account request logs (including Prompt Shields calls) and metrics in the same workspace. Accepted risk: Application Insights local authentication stays enabled, so the connection string alone can send (not read) telemetry | Enable alerts with action group; Microsoft Sentinel; diagnostic settings on the remaining resources; Log Analytics table-level RBAC; retention policy; Entra-only Application Insights ingestion | Alert-fire test; telemetry content scan | Security monitoring and observability |
| T15 | Responsible-AI failure: over-reliance, unmarked AI text, autonomy creep or silent reclassification (R, E) | `AiContentProvenance` for persisted agent text, visible marks and `POST /provenance/verify`; human-only decision rights; L2 to L4 autonomy constrained by code; agent stop switch; pinned models; evaluation cases; decision trail separates AI, person and rule actors; Art. 4 briefings defined but **not delivered** | Deliver Art. 4 briefings; name role holders; release gate with evaluations/red teaming; review classification on each trigger | Provenance/mark checks for each persisted agent text field; no agent identity has a human role; evaluation report per release | AI governance (responsible-AI lead) |

## 4. Grounding, injection and leakage controls

**Status key:** **MVP** is implemented; **MVP, not measured** means a case/control exists but results are not recorded; **Production** is not built and remains a gate.

### 4.1 Grounding

| Control | Mechanism | Metric or target | Status |
|---|---|---|---|
| Read before deciding | Agent must read every section for its role; otherwise `422 grounding_required` | 100% of accepted decisions read all required sections | MVP |
| Citations | Supplier `evidence_ids` must exist in lot evidence; otherwise `422 citation_required` | Zero accepted decisions with unknown citations | MVP |
| Bounded context | Eight reads per attempt; 8,000 encoded JSON bytes per agent record; 2,500 output tokens per model call; 120-second app timeout and 115-second hosted deadline | Ninth read refused (`429`); oversized record refused (`413`); timed-out activation fails | MVP |
| Staleness | Changed inputs invalidate the answer (`409 stale_task`) | Zero decisions on stale inputs | MVP |
| Calculation | App calculates from versioned factor sets; agent does not supply figures | Reproducible from factor-set version | MVP; values illustrative |
| Evaluation cases | `supplier-conflict`, `supplier-cobalt-escalation` | False `no_issue` rate with sample size | MVP, not measured |
| Foundry evaluators | Groundedness and task adherence | Defined release thresholds | Production |

### 4.2 Prompt injection

| Control | Mechanism | Metric or target | Status |
|---|---|---|---|
| Least privilege | One read-only tool; role sections typed as `Literal`; registered attempt ID with an eight-read budget per attempt; one action from each role's list | Out-of-scope calls and unknown attempts refused (`403`); ninth read refused (`429`) | MVP |
| No decision rights | Human-only decisions and application gates | Zero agent-caused publications of held batteries | MVP |
| Instruction guard | Treat document text as data and require reporting embedded instructions | `supplier-injection` passes | MVP, not measured |
| Content filter | Foundry default filter (`Microsoft.DefaultV2`) on every model deployment | Screens the prompt as a whole; no per-document result, review routing or measurement | MVP |
| Prompt Shields | The app calls the Content Safety `shieldPrompt` API (2024-09-01) for every supplier evidence document before the Supplier agent runs. Long documents are split so that no text goes unscreened. A detection opens a sourcing review (`system:prompt-shield`) naming the flagged evidence, records `prompt_attack_detected`, and stops the run with `409`; no model reads the document. A person who resolves that review lets the same evidence through. A service error stops the run | `scenario evaluation --apply --shield`: 8 attack probes (paraphrased, role-play, field name, German, French, after long text, and two supplier cases) detected, and 9 clean probes (including benign "ignore the old label" notes) not flagged | MVP. Deployed endpoint, 26 November 2026, one run: 4 of 8 attacks detected (blunt English, German, French, after long text), 0 of 9 clean probes flagged, 0 errors. Missed: paraphrased, role-play, field-name, and a short injected note in the cobalt adverse report. The gate is one layer; the other T03 controls carry the misses |
| Supplier evidence field allowlist | Validate supplier evidence against fields the app explicitly accepts; route unknown fields, including instruction-like names, for human review before model use | Unknown fields cannot silently reach the Supplier agent | Proposed; not implemented |
| Second check before automatic pass | Independently verify Supplier `no_issue` against the evidence before unattended publication; define whether this is a separate verifier run or human review, and route disagreement or uncertainty to a person | False `no_issue` rate and second-check disagreement rate with sample size | Proposed; not implemented |
| Spotlighting | Mark untrusted document content so the model can distinguish it from instructions; test as a complementary indirect-injection control | Attack detection and false-positive results on representative supplier evidence; does not replace deterministic app gates | Proposed; not implemented; model support and preview limits require validation |
| Red teaming | Foundry AI red-teaming agent | Attack success rate; zero critical failures | Production |

### 4.3 Leakage

| Channel | MVP control | Test or production action | Status |
|---|---|---|---|
| HTML/API | Field-policy projection per role; held passport shows anonymous users only “Not yet issued” | Role × field-group matrix with independent accounts | MVP; test evidence required |
| Agent output | Agents read role-scoped sections; outputs are provenance-marked and exposed only on authorized staff surfaces | Seeded canary values must not appear in other roles' views | MVP; test evidence required |
| Telemetry | Allowlisted events; no evidence, free-text reasons, prompts, names, emails or tokens; sensitive-data recording disabled | Scan Application Insights for canary strings and personal-data patterns | MVP; test evidence required |
| Connector/registry | Message log omits payloads; party registry stores public keys | Review responses and exports for prohibited fields | MVP |
| Model provider/region | DataZoneStandard deployments; private endpoints for the Foundry data plane | Verify deployment region, service terms and data-use commitments before real data | MVP infrastructure; production review |
| Logs/backups | Log Analytics retention is 30 days; teardown is not secure erasure | Define retention, deletion and backup handling before real data | MVP retention setting; production policy |

## 5. Compliance mappings

These are engineering control mappings for a synthetic MVP, not findings of legal compliance. Applicability, legal basis, controller/processor roles, retention, access entitlements and implementation dates require review for the actual deployment and use.

### 5.1 GDPR (Regulation (EU) 2016/679)

| Article | Relevance | MVP control | Threats | Gap before real personal data |
|---|---|---|---|---|
| Art. 5(1)(c), data minimization | Staff identity in decisions and operational records | Telemetry excludes names, emails and free text; synthetic passport records use fictional data | T06, T14 | Minimize and pseudonymize actor identifiers in shared views |
| Art. 5(1)(e), storage limitation; Art. 17, erasure | Append-only ledger and operational copies | Keep personal data out of passport content; Log Analytics retention is 30 days | T09, T14 | Define retention/erasure schedule; avoid personal data in ledger rows; account for backups and exports |
| Art. 6, lawful basis | Staff and partner-contact processing | Synthetic passport content; real staff identities may appear in sign-in and decision records | T06 | Identify and document a lawful basis; Battery Regulation access “legitimate interest” is not the GDPR lawful-basis test |
| Arts. 13 to 14, transparency | Staff and partner contacts | No production privacy notice supplied | T06 | Prepare notices for applicable data subjects |
| Art. 22, automated decisions | Agent-assisted workflow | Agents do not make decisions about natural persons; such use is excluded | T15 | Reassess if purpose or decisions change |
| Art. 25, data protection by design/default | App and data surfaces | Field policy, least privilege and limited public projection | T02, T06 | Confirm defaults against real data and user roles |
| Art. 28, processor | Microsoft cloud services | Microsoft Products and Services Data Protection Addendum may apply | T06 | Confirm roles, contract, services and subprocessors |
| Art. 30, records of processing | Processing by deployer | Not supplied by the MVP | T06 | Create and maintain the applicable record |
| Art. 32, security | Identity, data and service protection | Entra, managed identities, private endpoints, TLS, encryption at rest and SQL ledger | T01 to T14 | Risk-assess controls; consider customer-managed keys where needed |
| Arts. 33 to 34, breach notification | Security incident response | Alert rules exist but are disabled by default | T14 | Enable monitoring and create a breach assessment/notification runbook |
| Art. 35, DPIA | Potentially high-risk processing of personal data | No DPIA supplied | T06 | Assess whether a DPIA is required and complete it before applicable processing |
| Arts. 44 to 49, transfers | Cloud service processing regions | DataZoneStandard model deployments and configured Azure regions | T06 | Verify regions and transfer arrangements for every service, including identity and monitoring |

### 5.2 EU AI Act (Regulation (EU) 2024/1689, as amended by Regulation (EU) 2026/1744)

| Article | Relevance | MVP control | Threats | Gap |
|---|---|---|---|---|
| Art. 2(8), research exclusion | Synthetic development/deployment | Private synthetic exploration may qualify before market placement, service or real-world testing | T15 | Confirm legal applicability; reassess before operational or real-data use |
| Art. 3, provider/deployer roles | Organization operating an instance | Current design treats the operator as provider and deployer of the agent system | T15 | Name the accountable owner and confirm legal role |
| Art. 4, AI literacy | People operating or using the system | Role-specific briefings are defined | T03, T15 | Deliver and record the briefings |
| Art. 5, prohibited practices | Intended use | No prohibited practice is intended | T15 | Reassess if intended purpose changes |
| Art. 6 and Annexes I/III, classification | Five agents | Documented as not high-risk for the intended purpose; reclassification triggers identified | T08, T15 | Legal review and reassessment on each trigger |
| Arts. 9 to 15, high-risk requirements (if reclassified) | Potential future classification | Human decision gates, logging and stop switch cover parts of the design | T08, T15 | Risk management, technical documentation and conformity assessment are not designed |
| Arts. 14/26, human oversight | Human decisions in the MVP | People resolve reviews, decide cases, approve second life and activate guidance | T08, T15 | Assign and brief named role holders; mandatory duties depend on classification and applicability |
| Art. 50(1), interaction disclosure | AI interaction with natural persons | No conversational agent interface; staff can read labeled agent proposals | T15 | Confirm applicability to each surface and disclose AI before adding direct interaction |
| Art. 50(2), synthetic text marking | Agent-generated text | `AiContentProvenance`, visible marks and `POST /provenance/verify` | T15 | Check supported surfaces and documented exceptions; confirm legal adequacy |
| Arts. 53 to 55, GPAI model providers | Model-provider duties | Models are pinned; the MVP operator is not treated as the GPAI model provider | T11, T15 | Track provider documentation and deployment changes |

### 5.3 Battery Regulation (Regulation (EU) 2023/1542)

| Article | Requirement | MVP control | Threats | Gap |
|---|---|---|---|---|
| Art. 77(1) | Battery passport for scoped LMT, EV and industrial batteries above 2 kWh | Synthetic per-battery records, versions and coverage measures | T13 | Confirm product scope and applicable dates for an actual operator |
| Art. 77(2), Annex XIII | Differentiated access to public, restricted and individual information | App field policy with public, OEM/Recycler and Authority views | T02, T06 | MVP groups are not a legal Annex XIII classification; confirm current access rules and implementing measures |
| Art. 77(3) | QR code linked to unique identifier (ISO/IEC 15459 series or equivalent) | QR card links to app passport URL | T10 | No compliant physical identifier demonstrated; a URL does not identify a physical battery |
| Art. 77(4) | Economic operator remains accountable for accurate, complete and current information | Human gates; agents cannot take or transfer accountability | T04, T05, T08 | Define accountable operator and data-quality process |
| Art. 77(7) | Repurposing creates a new passport linked to the original | Successor/predecessor links; inherited values labeled | T07, T08 | Validate against actual lifecycle and legal requirements |
| Art. 77(8) | Passport ceases to exist after the battery has been recycled | `recycled` closure | T13 | Distinguish passport closure from evidence and record retention |
| Art. 77(10) | Union registry integration | Passport is labeled `mvp_passport_record_not_registered` | N/A | Registry integration not built |
| Art. 78 | Interoperability, machine readability, access, restricted updates, authenticity, integrity, security, privacy, continuity and authorized hosting | Entra/Easy Auth, app roles, ledger/digests, signed partner events and JSON snapshots | T01 to T13 | Demonstrate required schema/interoperability, entitlements, continuity and export; no conformance is claimed |
| Art. 78(e) | Passport remains available after the responsible economic operator ceases to exist or ceases its activity in the Union | Proposed exit gate | T13 | Fund, test and document continuity, including export and re-hosting |
| Art. 7 | Carbon-footprint declaration under applicable delegated acts | Versioned factors and illustrative labels | T05 | Method is illustrative, not demonstrated against applicable delegated acts |
| Arts. 47 to 52 | Supply-chain due diligence, subject to scope and applicable dates | Supplier claims, holds and compliance cases | T04, T08 | Not a complete due-diligence management system; verify scope and dates |

## 6. Recommended security acceptance

- Test with independent Entra accounts for each role and hosted-agent identities; View-as is not evidence of account isolation.
- Run each negative test at both the agent boundary and API boundary.
- Record pass/fail, test version, sample size and results. Target zero critical failures: cross-role leakage, hold bypass, agent decision bypass and missing AI marks.
- Pair every negative test with a positive case.
- Pin model, instruction and package versions; repeat relevant tests after changes.
- Validate in the target tenant. Template compilation and local/fake tests are not evidence of cloud deployment or service configuration.

## 7. Residual risk and production gates

| Residual risk in the MVP | Production gate | Threat |
|---|---|---|
| A signature proves origin, not truth | Human sampling of clean `no_issue` results; stronger partner credential model | T04, T05 |
| The ledger can preserve an incorrect decision | Correction workflow that creates new versions | T09 |
| A QR code or URL does not identify the physical battery | ISO/IEC 15459 identifier and physical marking | T10 |
| False Supplier `no_issue` rate is not measured | Evaluation run with stated sample size and acceptance threshold | T03, T05 |
| Prompt Shields misses some attacks, and detection on real supplier documents is not measured | Prompt Shields screens supplier evidence in the app (26 November 2026). On the deployed endpoint one run of the 17-probe set detected 4 of 8 synthetic attacks and flagged 0 of 9 clean probes; attacks phrased as ordinary business language (paraphrased, role-play, an instruction-like field name, a short note inside an adverse report) got through. It is trained mainly on English, screens text only (not images or attachments), and does not stop an adaptive attacker on its own; the T03 controls above remain. The `supplier-injection` case passed 9 of 9 model runs across three assignments on 24 and 26 November; repeats of one synthetic case are not an attack-success rate. Add a second guardrail layer, repeat the measurement with real document formats, and red-team before any real supplier document is processed | T03 |
| Multi-role accounts have no per-person separation-of-duties enforcement | Separation-of-duties checks and PIM for eligible role groups | T08 |
| Replica scaling does not guarantee HA/DR or organization isolation | Zone redundancy, disaster recovery and tenant/isolation design | T02, T13 |
| Digest immutability is unlocked; SQL auditing covers security events only, with 30-day retention | Lock policy with approved retention; set audit retention and scope to policy | T09 |
| ACR and monitoring endpoints are public, and Application Insights accepts local authentication (accepted for the MVP) | Consider an ACR private endpoint, Azure Monitor Private Link Scope and Entra-only telemetry ingestion | T10, T14 |
| Easy Auth client secret requires renewal | Adopt a managed rotation or federated-credential approach | T01 |
| Alert rules are disabled by default; no SIEM is configured | Enable alert rules and connect Sentinel if required | T14 |
| No DPIA, processing record, delivered AI literacy briefings or registry integration | Complete applicable privacy/AI governance work and registry integration before relevant operational use | T06, T15 |
| This document is based on a synthetic MVP and does not establish production readiness or legal compliance | Independent security, privacy and legal review before production | All |
