-- Passport app schema for Azure SQL Database.
-- Run as the Microsoft Entra admin. Safe to run again: it only creates what is missing.
-- Ledger tables keep a cryptographic history. Append-only tables accept INSERT only;
-- updatable ledger tables keep every earlier row version in a history table.
-- Limits that shaped this file: no TRUNCATE on ledger tables, bounded column types,
-- and no XML, sql_variant or user-defined types in ledger tables.

-- Schema version 4. Use a fresh database. Other versions are refused without migration or cleanup.

IF OBJECT_ID(N'dbp.SchemaInfo') IS NOT NULL
BEGIN
    IF EXISTS (SELECT 1 FROM dbp.SchemaInfo WHERE component = 'dbp' AND schema_version <> 4)
        THROW 50001, 'Passport schema mismatch: version 4 requires a fresh database.', 1;
END
ELSE IF OBJECT_ID(N'dbp.PassportState') IS NOT NULL
    THROW 50001, 'Unversioned older passport schema: version 4 requires a fresh database.', 1;
GO
IF SCHEMA_ID(N'dbp') IS NULL EXEC (N'CREATE SCHEMA dbp');
GO
IF SCHEMA_ID(N'report') IS NULL EXEC (N'CREATE SCHEMA report');
GO

-- Ordinary table: the schema version this database was created with. The app refuses writes on a mismatch.
IF OBJECT_ID(N'dbp.SchemaInfo') IS NULL
CREATE TABLE dbp.SchemaInfo (
    component       varchar(20)    NOT NULL CONSTRAINT PK_SchemaInfo PRIMARY KEY,
    schema_version  int            NOT NULL,
    applied_utc     datetime2(3)   NOT NULL);
GO
-- Record readiness at the end, after all tables and reporting views exist.

IF OBJECT_ID(N'dbp.AftersalesReceipts') IS NULL
CREATE TABLE dbp.AftersalesReceipts (
    delivery_id varchar(64) NOT NULL PRIMARY KEY,
    series_id varchar(64) NOT NULL,
    as_of_utc datetime2(3) NOT NULL,
    sha256 char(64) NOT NULL,
    imported_utc datetime2(3) NOT NULL,
    imported_by nvarchar(200) NOT NULL,
    CONSTRAINT UQ_AftersalesSeriesDate UNIQUE (series_id, as_of_utc))
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO
IF OBJECT_ID(N'dbp.OEMPhysicalPacks') IS NULL
CREATE TABLE dbp.OEMPhysicalPacks (
    battery_id varchar(64) NOT NULL PRIMARY KEY,
    series_id varchar(64) NOT NULL,
    oem_id varchar(64) NOT NULL,
    batch_id varchar(64) NOT NULL)
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO
IF OBJECT_ID(N'dbp.WarrantyTerms') IS NULL
CREATE TABLE dbp.WarrantyTerms (
    delivery_id varchar(64) NOT NULL,
    battery_id varchar(64) NOT NULL,
    in_service_date char(10) NULL,
    term_version varchar(64) NULL,
    warranty_months int NULL,
    warranty_cycles int NULL,
    CONSTRAINT PK_WarrantyTerms PRIMARY KEY (delivery_id, battery_id))
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO
IF OBJECT_ID(N'dbp.ServiceReadings') IS NULL
CREATE TABLE dbp.ServiceReadings (
    reading_id varchar(64) NOT NULL PRIMARY KEY,
    battery_id varchar(64) NOT NULL,
    delivery_id varchar(64) NOT NULL,
    measured_utc datetime2(3) NOT NULL,
    actual_cycles decimal(18,6) NOT NULL,
    cycle_basis varchar(32) NOT NULL,
    soh_pct decimal(12,6) NULL,
    CONSTRAINT UQ_ServiceReadingsPackTime UNIQUE (battery_id, measured_utc))
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO
IF OBJECT_ID(N'dbp.ServiceCampaigns') IS NULL
CREATE TABLE dbp.ServiceCampaigns (
    delivery_id varchar(64) NOT NULL,
    campaign_id varchar(64) NOT NULL,
    kind varchar(20) NOT NULL,
    status varchar(8) NOT NULL,
    effective_date char(10) NOT NULL,
    CONSTRAINT PK_ServiceCampaigns PRIMARY KEY (delivery_id, campaign_id))
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO
IF OBJECT_ID(N'dbp.CampaignPacks') IS NULL
CREATE TABLE dbp.CampaignPacks (
    delivery_id varchar(64) NOT NULL,
    battery_id varchar(64) NOT NULL,
    campaign_id varchar(64) NOT NULL,
    CONSTRAINT PK_CampaignPacks PRIMARY KEY (delivery_id, battery_id, campaign_id))
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO

-- Ordinary table: one row per command ID, so a retried request returns the first answer.
IF OBJECT_ID(N'dbp.Commands') IS NULL
CREATE TABLE dbp.Commands (
    command_id      varchar(64)    NOT NULL CONSTRAINT PK_Commands PRIMARY KEY,
    command_type    varchar(40)    NOT NULL,
    payload_sha256  char(64)       NOT NULL,
    actor           nvarchar(200)  NOT NULL,
    status_code     int            NOT NULL,
    result_json     nvarchar(max)  NOT NULL,
    recorded_utc    datetime2(3)   NOT NULL);
GO

-- Append-only ledger: each imported Fabric gold package (contract dbp-passport-package-2).
IF OBJECT_ID(N'dbp.Packages') IS NULL
CREATE TABLE dbp.Packages (
    build_id            varchar(50)    NOT NULL CONSTRAINT PK_Packages PRIMARY KEY,
    build_sequence      int            NOT NULL,
    manifest_sha256     char(64)       NOT NULL,
    series_id           varchar(50)    NULL,
    battery_count       int            NOT NULL,
    rules_version       varchar(50)    NULL,
    method_version      varchar(50)    NULL,
    factor_set_version  varchar(50)    NULL,
    intake_folder       varchar(100)   NOT NULL,
    imported_by         nvarchar(200)  NOT NULL,
    imported_utc        datetime2(3)   NOT NULL)
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO

-- Append-only ledger: production batches with their pinned method, factor set and energy reading.
IF OBJECT_ID(N'dbp.Batches') IS NULL
CREATE TABLE dbp.Batches (
    build_id            varchar(50)     NOT NULL,
    batch_id            varchar(50)     NOT NULL,
    plant_id            varchar(50)     NOT NULL,
    version             int             NOT NULL,
    method_version      varchar(50)     NOT NULL,
    factor_set_version  varchar(50)     NOT NULL,
    allocation_rule     varchar(80)     NOT NULL,
    energy_id           varchar(50)     NULL,
    energy_version      int             NULL,
    batch_kwh           decimal(18, 6)  NULL,
    battery_count       int             NOT NULL,
    grid_factor_id      varchar(50)     NOT NULL,
    CONSTRAINT PK_Batches PRIMARY KEY (build_id, batch_id))
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO

-- Append-only ledger: supplier lots as each package delivered them, with the claim reference and field coverage.
IF OBJECT_ID(N'dbp.PackageLots') IS NULL
CREATE TABLE dbp.PackageLots (
    build_id            varchar(50)     NOT NULL,
    lot_id              varchar(50)     NOT NULL,
    version             int             NOT NULL,
    supplier_id         varchar(50)     NULL,
    material            varchar(30)     NOT NULL,
    region              varchar(50)     NULL,
    origin_site         varchar(100)    NULL,
    mass_kg             decimal(18, 6)  NULL,
    recycled_share_pct  decimal(9, 4)   NULL,
    pcf_kgco2e_per_kg   decimal(18, 6)  NULL,
    claim_id            varchar(64)     NULL,
    claim_sha256        char(64)        NULL,
    required_fields     varchar(400)    NOT NULL,
    present_fields      varchar(400)    NOT NULL,
    na_reasons          varchar(1000)   NOT NULL,
    CONSTRAINT PK_PackageLots PRIMARY KEY (build_id, lot_id))
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO

-- Append-only ledger: which lot each battery's materials came from, and how much.
IF OBJECT_ID(N'dbp.PackageComposition') IS NULL
CREATE TABLE dbp.PackageComposition (
    build_id    varchar(50)     NOT NULL,
    battery_id  varchar(50)     NOT NULL,
    material    varchar(30)     NOT NULL,
    lot_id      varchar(50)     NOT NULL,
    mass_kg     decimal(18, 6)  NOT NULL,
    CONSTRAINT PK_PackageComposition PRIMARY KEY (build_id, battery_id, lot_id))
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO

-- Append-only ledger: the battery rows as each package delivered them, plus the app's own footprint calculation.
IF OBJECT_ID(N'dbp.PackageBatteries') IS NULL
CREATE TABLE dbp.PackageBatteries (
    build_id            varchar(50)     NOT NULL,
    battery_id          varchar(50)     NOT NULL,
    series_id           varchar(50)     NULL,
    model_id            varchar(50)     NOT NULL,
    chemistry           varchar(30)     NULL,
    rated_kwh           decimal(18, 6)  NULL,
    rated_capacity_ah   decimal(18, 6)  NULL,
    expected_cycles     int             NULL,
    plant_id            varchar(50)     NULL,
    batch_id            varchar(50)     NULL,
    readiness           varchar(30)     NOT NULL,
    input_hash          char(64)        NOT NULL,
    allocated_kwh       decimal(18, 6)  NULL,
    grid_factor_id      varchar(50)     NULL,
    lot_ids             varchar(400)    NOT NULL,
    reasons             nvarchar(1000)  NOT NULL,
    lifecycle_status    varchar(20)     NOT NULL,
    holder_id           varchar(50)     NOT NULL,
    footprint_per_kwh   decimal(18, 4)  NULL,
    footprint_verified  int             NOT NULL,
    CONSTRAINT PK_PackageBatteries PRIMARY KEY (build_id, battery_id))
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO

-- Updatable ledger: the current state of each passport. Earlier states stay in the history table.
IF OBJECT_ID(N'dbp.PassportState') IS NULL
CREATE TABLE dbp.PassportState (
    battery_id            varchar(50)     NOT NULL CONSTRAINT PK_PassportState PRIMARY KEY,
    model_id              varchar(50)     NOT NULL,
    build_id              varchar(50)     NOT NULL,
    input_hash            char(64)        NOT NULL,
    readiness             varchar(30)     NOT NULL,
    eligibility           varchar(20)     NOT NULL,
    hold_reason           varchar(50)     NULL,
    lot_ids               varchar(400)    NOT NULL,
    chemistry             varchar(30)     NULL,
    rated_kwh             decimal(18, 6)  NULL,
    plant_id              varchar(50)     NULL,
    batch_id              varchar(50)     NULL,
    lifecycle_status      varchar(20)     NOT NULL,
    holder_id             varchar(50)     NOT NULL,
    footprint_per_kwh     decimal(18, 4)  NULL,
    footprint_verified    int             NOT NULL,
    committed_version     int             NOT NULL,
    committed_input_hash  char(64)        NULL,
    row_version           int             NOT NULL,
    updated_utc           datetime2(3)    NOT NULL)
WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = dbp.PassportStateHistory), LEDGER = ON);
GO
-- Append-only ledger: every committed passport version and the SHA-256 of its snapshot file.
IF OBJECT_ID(N'dbp.PassportVersions') IS NULL
CREATE TABLE dbp.PassportVersions (
    battery_id       varchar(50)    NOT NULL,
    version          int            NOT NULL,
    build_id         varchar(50)    NOT NULL,
    input_hash       char(64)       NOT NULL,
    snapshot_sha256  char(64)       NOT NULL,
    snapshot_path    varchar(200)   NOT NULL,
    decision         varchar(30)    NOT NULL,
    review_id        varchar(20)    NULL,
    committed_by     nvarchar(200)  NOT NULL,
    committed_utc    datetime2(3)   NOT NULL,
    CONSTRAINT PK_PassportVersions PRIMARY KEY (battery_id, version))
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO

-- Append-only ledger: proof that the snapshot file was written and read back with the same hash.
IF OBJECT_ID(N'dbp.SnapshotReceipts') IS NULL
CREATE TABLE dbp.SnapshotReceipts (
    battery_id       varchar(50)   NOT NULL,
    version          int           NOT NULL,
    blob_version_id  varchar(100)  NOT NULL,
    verified_sha256  char(64)      NOT NULL,
    written_utc      datetime2(3)  NOT NULL,
    CONSTRAINT PK_SnapshotReceipts PRIMARY KEY (battery_id, version))
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO

-- Append-only ledger: evidence files (the file itself is in Blob; the hash and version ID are here).
IF OBJECT_ID(N'dbp.Evidence') IS NULL
CREATE TABLE dbp.Evidence (
    evidence_id      varchar(20)    NOT NULL CONSTRAINT PK_Evidence PRIMARY KEY,
    kind             varchar(30)    NOT NULL,
    lot_id           varchar(50)    NOT NULL,
    file_name        nvarchar(100)  NOT NULL,
    content_type     varchar(50)    NOT NULL,
    sha256           char(64)       NOT NULL,
    size_bytes       int            NOT NULL,
    blob_path        varchar(200)   NOT NULL,
    blob_version_id  varchar(100)   NOT NULL,
    uploaded_by      nvarchar(200)  NOT NULL,
    uploaded_utc     datetime2(3)   NOT NULL)
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO

-- Updatable ledger: sourcing reviews. row_version stops two people resolving from stale screens.
IF OBJECT_ID(N'dbp.Reviews') IS NULL
CREATE TABLE dbp.Reviews (
    review_id            varchar(20)    NOT NULL CONSTRAINT PK_Reviews PRIMARY KEY,
    lot_id               varchar(50)    NOT NULL,
    status               varchar(20)    NOT NULL,
    reason               nvarchar(400)  NOT NULL,
    opening_evidence     varchar(400)   NOT NULL,
    resolution_reason    nvarchar(400)  NULL,
    resolution_evidence  varchar(400)   NULL,
    opened_by            nvarchar(200)  NOT NULL,
    opened_utc           datetime2(3)   NOT NULL,
    resolved_by          nvarchar(200)  NULL,
    resolved_utc         datetime2(3)   NULL,
    reason_origin        varchar(20)    NOT NULL,
    provenance_id        varchar(64)    NULL,
    row_version          int            NOT NULL)
WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = dbp.ReviewsHistory), LEDGER = ON);
GO

-- Updatable ledger: compliance cases on critical-material lots (open or accepted). History keeps every state.
IF OBJECT_ID(N'dbp.ComplianceCases') IS NULL
CREATE TABLE dbp.ComplianceCases (
    case_id           varchar(20)    NOT NULL CONSTRAINT PK_ComplianceCases PRIMARY KEY,
    lot_id            varchar(50)    NOT NULL,
    status            varchar(20)    NOT NULL,
    reason_code       varchar(50)    NOT NULL,
    opening_evidence  varchar(400)   NOT NULL,
    agent_task_id     varchar(64)    NULL,
    opened_by         nvarchar(200)  NOT NULL,
    opened_utc        datetime2(3)   NOT NULL,
    decision          varchar(30)    NULL,
    action_owner      nvarchar(100)  NULL,
    due_date          varchar(10)    NULL,
    decided_by        nvarchar(200)  NULL,
    decided_utc       datetime2(3)   NULL,
    reopen_count      int            NOT NULL,
    reopened_utc      datetime2(3)   NULL,
    row_version       int            NOT NULL)
WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = dbp.ComplianceCasesHistory), LEDGER = ON);
GO

-- Append-only ledger: every compliance decision (keep_hold or accept_with_action_plan).
IF OBJECT_ID(N'dbp.ComplianceDecisions') IS NULL
CREATE TABLE dbp.ComplianceDecisions (
    decision_id       varchar(20)    NOT NULL CONSTRAINT PK_ComplianceDecisions PRIMARY KEY,
    case_id           varchar(20)    NOT NULL,
    lot_id            varchar(50)    NOT NULL,
    decision          varchar(30)    NOT NULL,
    action_owner      nvarchar(100)  NULL,
    due_date          varchar(10)    NULL,
    evidence_ids      varchar(400)   NOT NULL,
    reason            nvarchar(400)  NOT NULL,
    case_row_version  int            NOT NULL,
    decided_by        nvarchar(200)  NOT NULL,
    decided_utc       datetime2(3)   NOT NULL)
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO

-- Append-only ledger: who did what, including rejected commands.
IF OBJECT_ID(N'dbp.AuditEvents') IS NULL
CREATE TABLE dbp.AuditEvents (
    event_id   bigint IDENTITY(1, 1)  NOT NULL CONSTRAINT PK_AuditEvents PRIMARY KEY,
    event_utc  datetime2(3)           NOT NULL,
    actor      nvarchar(200)          NOT NULL,
    action     varchar(50)            NOT NULL,
    subject    nvarchar(100)          NOT NULL,
    detail     nvarchar(2000)         NOT NULL)
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO

-- Updatable ledger: the party registry (public keys and permissions). History keeps every key change and revocation.
IF OBJECT_ID(N'dbp.Parties') IS NULL
CREATE TABLE dbp.Parties (
    party_id         varchar(50)    NOT NULL CONSTRAINT PK_Parties PRIMARY KEY,
    name             nvarchar(200)  NOT NULL,
    role             varchar(30)    NOT NULL,
    key_id           varchar(100)   NOT NULL,
    public_key       varchar(100)   NOT NULL,
    lot_ids          varchar(1000)  NOT NULL,
    event_types      varchar(400)   NOT NULL,
    revoked          int            NOT NULL,
    registry_sha256  char(64)       NOT NULL,
    updated_utc      datetime2(3)   NOT NULL,
    row_version      int            NOT NULL)
WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = dbp.PartiesHistory), LEDGER = ON);
GO

-- Append-only ledger: mock connector agreements (who may send which message types, and when).
IF OBJECT_ID(N'dbp.ConnectorAgreements') IS NULL
CREATE TABLE dbp.ConnectorAgreements (
    agreement_id   varchar(64)    NOT NULL CONSTRAINT PK_ConnectorAgreements PRIMARY KEY,
    sender         varchar(50)    NOT NULL,
    receiver       varchar(50)    NOT NULL,
    purpose        nvarchar(200)  NOT NULL,
    message_types  varchar(400)   NOT NULL,
    valid_from     datetime2(3)   NOT NULL,
    valid_until    datetime2(3)   NOT NULL,
    recorded_by    nvarchar(200)  NOT NULL,
    recorded_utc   datetime2(3)   NOT NULL)
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO

-- Append-only ledger: every message the mock connector received, accepted or refused, with a coded reason.
-- payload_json holds the verified ASCII payload only for accepted messages.
IF OBJECT_ID(N'dbp.ConnectorMessages') IS NULL
CREATE TABLE dbp.ConnectorMessages (
    receipt_id      varchar(20)    NOT NULL CONSTRAINT PK_ConnectorMessages PRIMARY KEY,
    message_id      varchar(64)    NULL,
    sender          varchar(50)    NULL,
    message_type    varchar(40)    NULL,
    subject         varchar(50)    NULL,
    correlation_id  varchar(64)    NULL,
    payload_sha256  char(64)       NULL,
    payload_json    varchar(8000)  NULL,
    agreement_id    varchar(64)    NULL,
    result          varchar(20)    NOT NULL,
    reason_code     varchar(40)    NOT NULL,
    evidence_id     varchar(20)    NULL,
    received_by     nvarchar(200)  NOT NULL,
    received_utc    datetime2(3)   NOT NULL)
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO

-- Append-only ledger: every lifecycle event the app received, recorded (applied) or quarantined with a coded reason.
IF OBJECT_ID(N'dbp.LifecycleEvents') IS NULL
CREATE TABLE dbp.LifecycleEvents (
    lifecycle_event_id  varchar(20)    NOT NULL CONSTRAINT PK_LifecycleEvents PRIMARY KEY,
    message_id          varchar(64)    NOT NULL,
    battery_id          varchar(50)    NOT NULL,
    event_type          varchar(40)    NOT NULL,
    issuer              varchar(50)    NOT NULL,
    event_utc           datetime2(3)   NULL,
    state_before        varchar(30)    NULL,
    state_after         varchar(30)    NULL,
    result              varchar(20)    NOT NULL,
    reason_code         varchar(40)    NOT NULL,
    passport_version    int            NULL,
    payload_sha256      char(64)       NULL,
    details_json        varchar(2000)  NOT NULL,
    agent_task_id       varchar(64)    NULL,
    recorded_by         nvarchar(200)  NOT NULL,
    recorded_utc        datetime2(3)   NOT NULL)
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO

-- Updatable ledger: second-life requests (pending, approved or rejected). History keeps every state.
IF OBJECT_ID(N'dbp.SecondLifeRequests') IS NULL
CREATE TABLE dbp.SecondLifeRequests (
    request_id              varchar(20)     NOT NULL CONSTRAINT PK_SecondLifeRequests PRIMARY KEY,
    battery_id              varchar(50)     NOT NULL,
    successor_id            varchar(50)     NOT NULL,
    requested_by            varchar(50)     NOT NULL,
    message_id              varchar(64)     NOT NULL,
    safety_assessment_ref   varchar(64)     NOT NULL,
    intended_use            nvarchar(200)   NOT NULL,
    soh_pct                 decimal(9, 2)   NOT NULL,
    illustrative_value_eur  decimal(18, 2)  NOT NULL,
    value_label             varchar(100)    NOT NULL,
    status                  varchar(20)     NOT NULL,
    decision_reason         nvarchar(400)   NULL,
    decided_by              nvarchar(200)   NULL,
    decided_utc             datetime2(3)    NULL,
    requested_utc           datetime2(3)    NOT NULL,
    row_version             int             NOT NULL)
WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = dbp.SecondLifeRequestsHistory), LEDGER = ON);
GO

-- Append-only ledger: links between passports (successor and predecessor), one row per direction.
IF OBJECT_ID(N'dbp.PassportLinks') IS NULL
CREATE TABLE dbp.PassportLinks (
    battery_id   varchar(50)    NOT NULL,
    linked_id    varchar(50)    NOT NULL,
    link_type    varchar(20)    NOT NULL,
    created_by   nvarchar(200)  NOT NULL,
    created_utc  datetime2(3)   NOT NULL,
    CONSTRAINT PK_PassportLinks PRIMARY KEY (battery_id, linked_id))
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO

-- Ordinary table: snapshot files still to be written to Blob (the outbox).
IF OBJECT_ID(N'dbp.Outbox') IS NULL
CREATE TABLE dbp.Outbox (
    outbox_id    bigint IDENTITY(1, 1)  NOT NULL CONSTRAINT PK_Outbox PRIMARY KEY,
    kind         varchar(30)            NOT NULL,
    subject      varchar(100)           NOT NULL,
    payload      nvarchar(max)          NOT NULL,
    status       varchar(20)            NOT NULL,
    attempts     int                    NOT NULL,
    lease_until  datetime2(3)           NULL,
    last_error   nvarchar(1000)         NULL,
    created_utc  datetime2(3)           NOT NULL,
    done_utc     datetime2(3)           NULL);
GO

-- Append-only ledger: agent tasks, results and accepted facts (records stay within varchar(8000)).
IF OBJECT_ID(N'dbp.AgentTasks') IS NULL
CREATE TABLE dbp.AgentTasks (
    task_id varchar(64) NOT NULL CONSTRAINT PK_AgentTasks PRIMARY KEY,
    role varchar(20) NOT NULL,
    subject varchar(64) NOT NULL,
    context_hash char(64) NOT NULL,
    context_json varchar(8000) NOT NULL,
    request_json varchar(8000) NOT NULL,
    actor nvarchar(200) NOT NULL,
    created_utc datetime2(3) NOT NULL)
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO
IF OBJECT_ID(N'dbp.AgentResults') IS NULL
CREATE TABLE dbp.AgentResults (
    task_id varchar(64) NOT NULL CONSTRAINT PK_AgentResults PRIMARY KEY,
    status_code int NOT NULL,
    result_json varchar(8000) NOT NULL,
    recorded_utc datetime2(3) NOT NULL)
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO
IF OBJECT_ID(N'dbp.AgentFacts') IS NULL
CREATE TABLE dbp.AgentFacts (
    fact_id varchar(64) NOT NULL CONSTRAINT PK_AgentFacts PRIMARY KEY,
    kind varchar(20) NOT NULL,
    subject varchar(64) NOT NULL,
    input_hash char(64) NOT NULL,
    data_json varchar(8000) NOT NULL,
    recorded_utc datetime2(3) NOT NULL)
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO

-- Append-only ledger: one machine-readable provenance record per AI-generated text field (EU AI Act Art. 50(2)).
-- No person identifiers; text_sha256 covers the NFC-normalised, whitespace-collapsed text.
IF OBJECT_ID(N'dbp.AiContentProvenance') IS NULL
CREATE TABLE dbp.AiContentProvenance (
    provenance_id varchar(64) NOT NULL CONSTRAINT PK_AiContentProvenance PRIMARY KEY,
    task_id varchar(64) NOT NULL,
    role varchar(20) NOT NULL,
    field varchar(30) NOT NULL,
    stored_in varchar(200) NOT NULL,
    ai_generated int NOT NULL,
    digital_source_type varchar(100) NOT NULL,
    generator varchar(30) NOT NULL,
    agent_name varchar(100) NULL,
    agent_version varchar(50) NULL,
    model varchar(100) NULL,
    model_version varchar(50) NULL,
    hosting varchar(50) NULL,
    text_sha256 char(64) NOT NULL,
    text_chars int NOT NULL,
    generated_utc datetime2(3) NOT NULL,
    human_modified int NOT NULL)
WITH (LEDGER = ON (APPEND_ONLY = ON));
GO

-- Updatable ledger: Watch proposals remain pending until an explicitly assigned Policy Approver decides.
IF OBJECT_ID(N'dbp.PolicyCandidates') IS NULL
CREATE TABLE dbp.PolicyCandidates (
    candidate_id varchar(64) NOT NULL CONSTRAINT PK_PolicyCandidates PRIMARY KEY,
    task_id varchar(64) NOT NULL,
    title nvarchar(120) NOT NULL,
    policy_text nvarchar(1000) NOT NULL,
    source_ids varchar(200) NOT NULL,
    status varchar(20) NOT NULL,
    recommended_by nvarchar(200) NOT NULL,
    requested_by nvarchar(200) NOT NULL,
    model varchar(100) NULL,
    model_version varchar(50) NULL,
    provenance_id varchar(64) NULL,
    recommended_utc datetime2(3) NOT NULL,
    decided_by nvarchar(200) NULL,
    decided_utc datetime2(3) NULL,
    decision_reason nvarchar(400) NULL,
    row_version int NOT NULL)
WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = dbp.PolicyCandidatesHistory), LEDGER = ON);
GO
IF OBJECT_ID(N'dbp.PolicyState') IS NULL
CREATE TABLE dbp.PolicyState (
    policy_slot varchar(20) NOT NULL CONSTRAINT PK_PolicyState PRIMARY KEY,
    version int NOT NULL,
    active_candidate_id varchar(64) NULL,
    title nvarchar(120) NOT NULL,
    policy_text nvarchar(1000) NOT NULL,
    ai_drafted int NOT NULL,
    source_task_id varchar(64) NULL,
    provenance_id varchar(64) NULL,
    activated_by nvarchar(200) NOT NULL,
    activated_utc datetime2(3) NOT NULL,
    row_version int NOT NULL)
WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = dbp.PolicyStateHistory), LEDGER = ON);
GO
IF NOT EXISTS (SELECT 1 FROM dbp.PolicyState WHERE policy_slot = 'active')
    INSERT INTO dbp.PolicyState (policy_slot, version, title, policy_text, ai_drafted, activated_by, activated_utc,
                                 row_version)
    VALUES ('active', 2, N'Baseline policy',
            N'Operational gates remain code-owned. Watch recommendations are advisory until approved by the designated Policy Approver.',
            0, N'system', SYSUTCDATETIME(), 1);
GO

-- Minimized reporting views: no people, files, hashes, agent prompts, model outputs or evidence text.
-- The operator copies them to Fabric. Columns whose source arrives in a later version carry interim values.
CREATE OR ALTER VIEW report.v_passport_status AS
SELECT s.battery_id,
       s.model_id,
       s.plant_id,
       s.batch_id,
       s.chemistry,
       s.rated_kwh,
       s.eligibility,
       s.hold_reason,
       s.lifecycle_status,
       s.committed_version,
       v.committed_utc,
       CASE WHEN v.battery_id IS NULL THEN 'none'
            WHEN r.battery_id IS NULL THEN 'pending'
            ELSE 'written' END AS snapshot_state,
       s.footprint_per_kwh,
       CAST(CASE WHEN s.committed_version > 0 AND s.footprint_per_kwh IS NOT NULL
                  AND EXISTS (SELECT 1 FROM STRING_SPLIT(s.lot_ids, ',') AS x
                              JOIN dbp.PackageLots AS l ON l.build_id = s.build_id AND l.lot_id = x.value
                              WHERE l.material IN ('cobalt', 'graphite', 'lithium', 'nickel'))
                  AND NOT EXISTS (SELECT 1 FROM STRING_SPLIT(s.lot_ids, ',') AS x
                                  JOIN dbp.PackageLots AS l ON l.build_id = s.build_id AND l.lot_id = x.value
                                  WHERE l.material IN ('cobalt', 'graphite', 'lithium', 'nickel')
                                    AND (EXISTS (SELECT 1 FROM dbp.ConnectorMessages AS m
                                                 JOIN dbp.Parties AS pa ON pa.party_id = m.sender
                                                 WHERE m.message_type = 'supplier_claim' AND m.subject = l.lot_id
                                                   AND m.result = 'verified' AND pa.revoked = 1)
                                         OR EXISTS (SELECT 1 FROM dbp.ConnectorMessages AS m
                                                    WHERE m.message_type = 'supplier_claim' AND m.subject = l.lot_id
                                                      AND m.reason_code = 'revoked')
                                         OR NOT EXISTS (SELECT 1 FROM dbp.ConnectorMessages AS m
                                                        WHERE m.message_type = 'supplier_claim'
                                                          AND m.subject = l.lot_id AND m.result = 'verified'
                                                          AND m.payload_sha256 = l.claim_sha256)))
            THEN 1 ELSE 0 END AS int) AS footprint_verified,
       (SELECT CAST(ROUND(100 * SUM(c.mass_kg * l.recycled_share_pct / 100) / NULLIF(SUM(c.mass_kg), 0), 1)
                    AS decimal(9, 1))
        FROM dbp.PackageComposition AS c
        JOIN dbp.PackageLots AS l ON l.build_id = c.build_id AND l.lot_id = c.lot_id
        WHERE c.build_id = s.build_id AND c.battery_id = s.battery_id AND c.material = 'cobalt') AS recycled_co_pct,
       (SELECT CAST(ROUND(100 * SUM(c.mass_kg * l.recycled_share_pct / 100) / NULLIF(SUM(c.mass_kg), 0), 1)
                    AS decimal(9, 1))
        FROM dbp.PackageComposition AS c
        JOIN dbp.PackageLots AS l ON l.build_id = c.build_id AND l.lot_id = c.lot_id
        WHERE c.build_id = s.build_id AND c.battery_id = s.battery_id AND c.material = 'lithium') AS recycled_li_pct,
       (SELECT CAST(ROUND(100 * SUM(c.mass_kg * l.recycled_share_pct / 100) / NULLIF(SUM(c.mass_kg), 0), 1)
                    AS decimal(9, 1))
        FROM dbp.PackageComposition AS c
        JOIN dbp.PackageLots AS l ON l.build_id = c.build_id AND l.lot_id = c.lot_id
        WHERE c.build_id = s.build_id AND c.battery_id = s.battery_id AND c.material = 'nickel') AS recycled_ni_pct,
       (SELECT TOP (1) k.linked_id FROM dbp.PassportLinks AS k
        WHERE k.battery_id = s.battery_id AND k.link_type = 'successor') AS successor_id,
       (SELECT TOP (1) q.illustrative_value_eur FROM dbp.SecondLifeRequests AS q
        WHERE q.battery_id = s.battery_id AND q.status = 'approved') AS second_life_value_eur_illustrative,
       CAST('not_exercised' AS varchar(20)) AS registry_status,
       CAST('synthetic' AS varchar(20)) AS data_label
FROM dbp.PassportState AS s
LEFT JOIN dbp.PassportVersions AS v ON v.battery_id = s.battery_id AND v.version = s.committed_version
LEFT JOIN dbp.SnapshotReceipts AS r ON r.battery_id = s.battery_id AND r.version = s.committed_version;
GO

-- One row per lot: the latest delivered version. claim_status comes from the mock connector's supplier claims
-- (verified when an accepted claim's payload hash equals the lot's claim_sha256). case_state is the status of the
-- lot's compliance case (open or accepted; a lot has at most one case, reopened in place), else 'none'.
CREATE OR ALTER VIEW report.v_lot_claim_status AS
SELECT l.lot_id,
       l.supplier_id,
       l.material,
       CAST(CASE WHEN EXISTS (SELECT 1 FROM dbp.ConnectorMessages AS m
                              JOIN dbp.Parties AS pa ON pa.party_id = m.sender
                              WHERE m.message_type = 'supplier_claim' AND m.subject = l.lot_id
                                AND m.result = 'verified' AND pa.revoked = 1)
                   OR EXISTS (SELECT 1 FROM dbp.ConnectorMessages AS m
                              WHERE m.message_type = 'supplier_claim' AND m.subject = l.lot_id
                                AND m.reason_code = 'revoked') THEN 'revoked'
                 WHEN EXISTS (SELECT 1 FROM dbp.ConnectorMessages AS m
                              WHERE m.message_type = 'supplier_claim' AND m.subject = l.lot_id
                                AND m.result = 'verified' AND m.payload_sha256 = l.claim_sha256) THEN 'verified'
                 WHEN EXISTS (SELECT 1 FROM dbp.ConnectorMessages AS m
                              WHERE m.message_type = 'supplier_claim' AND m.subject = l.lot_id) THEN 'invalid'
                 ELSE 'missing' END AS varchar(20)) AS claim_status,
       CAST(ISNULL((SELECT TOP (1) c.status FROM dbp.ComplianceCases AS c WHERE c.lot_id = l.lot_id
                    ORDER BY c.opened_utc DESC), 'none') AS varchar(20)) AS case_state,
       CAST(CASE WHEN NOT EXISTS (SELECT 1 FROM STRING_SPLIT(l.required_fields, ',') AS f
                                  WHERE f.value <> ''
                                    AND f.value NOT IN (SELECT g.value FROM STRING_SPLIT(l.present_fields, ',') AS g))
            THEN 1 ELSE 0 END AS int) AS fields_complete,
       CAST('synthetic' AS varchar(20)) AS data_label
FROM dbp.PackageLots AS l
JOIN dbp.Packages AS p ON p.build_id = l.build_id
WHERE p.build_sequence = (SELECT MAX(p2.build_sequence)
                          FROM dbp.PackageLots AS l2
                          JOIN dbp.Packages AS p2 ON p2.build_id = l2.build_id
                          WHERE l2.lot_id = l.lot_id);
GO
IF NOT EXISTS (SELECT 1 FROM dbp.SchemaInfo WHERE component = 'dbp')
    INSERT INTO dbp.SchemaInfo (component, schema_version, applied_utc) VALUES ('dbp', 4, SYSUTCDATETIME());
GO