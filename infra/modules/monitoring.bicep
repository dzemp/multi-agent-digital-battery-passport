// Application Insights on the platform's Log Analytics workspace, four persona workbooks and three log
// alert rules. Rules are always created but stay disabled until enableAlerts is true.
targetScope = 'resourceGroup'

param prefix string
param location string
param tags object

@description('Log Analytics workspace created by platform.bicep in this resource group.')
param workspaceName string

@description('Enable only after instrumenting the app, validating the queries and agreeing an alert owner.')
param enableAlerts bool

@description('Notification address. Required when enableAlerts is true; no automatic remediation.')
param alertEmail string

@minValue(1)
@maxValue(10080)
param holdAgeMinutes int

@minValue(1)
param dailyTokenBudget int

var insightsName = '${prefix}-insights'

resource workspace 'Microsoft.OperationalInsights/workspaces@2026-03-01' existing = {
  name: workspaceName
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: insightsName
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    IngestionMode: 'LogAnalytics'
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

var definitions = [
  {
    key: 'executive'
    title: 'DBP - Executive service and cost'
    data: loadTextContent('../workbooks/executive.workbook.json')
  }
  {
    key: 'compliance'
    title: 'DBP - Compliance and review'
    data: loadTextContent('../workbooks/compliance.workbook.json')
  }
  {
    key: 'agents'
    title: 'DBP - Agents and AI quality'
    data: loadTextContent('../workbooks/agents.workbook.json')
  }
  {
    key: 'operations'
    title: 'DBP - Platform operations and security'
    data: loadTextContent('../workbooks/operations.workbook.json')
  }
]

resource workbooks 'Microsoft.Insights/workbooks@2023-06-01' = [for definition in definitions: {
  name: guid(resourceGroup().id, insightsName, 'workbook', definition.key)
  location: location
  kind: 'shared'
  tags: tags
  properties: {
    displayName: definition.title
    category: 'workbook'
    sourceId: insights.id
    version: '1.0'
    serializedData: replace(definition.data, '__APPLICATION_INSIGHTS_ID__', insights.id)
  }
}]

resource notifications 'Microsoft.Insights/actionGroups@2023-01-01' = if (enableAlerts) {
  name: '${insightsName}-owners'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'dbp-alerts'
    enabled: true
    emailReceivers: [
      {
        name: 'mvp-owner'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

var alerts = [
  {
    key: 'aged-hold'
    title: 'DBP - Fresh snapshot reports an aged hold or compliance case'
    severity: 2
    window: 'PT30M'
    query: '''
customEvents
| where timestamp > ago(30m)
| where cloud_RoleName == "passport-app" and name == "passport.series.snapshot"
| where tostring(customDimensions["dbp.schema_version"]) == "2"
| extend series = tostring(customDimensions.series_id)
| where isnotempty(series)
| summarize arg_max(timestamp, *) by series
| where timestamp > ago(15m)
| where todouble(customMeasurements.oldest_hold_minutes) > __HOLD_AGE_MINUTES__
    or todouble(customMeasurements.oldest_case_minutes) > __HOLD_AGE_MINUTES__
| project timestamp, series
'''
  }
  {
    key: 'ledger-failed'
    title: 'DBP - Ledger verification failed'
    severity: 1
    window: 'PT15M'
    query: '''
customEvents
| where timestamp > ago(15m)
| where cloud_RoleName == "passport-app" and name == "ledger.verified"
| where tostring(customDimensions["dbp.schema_version"]) == "2"
| where tostring(customDimensions.result) == "failure"
| project timestamp, operation_Id
'''
  }
  {
    key: 'stuck-work'
    title: 'DBP - Battery remains pending after 15 minutes'
    severity: 2
    window: 'PT30M'
    query: '''
customEvents
| where timestamp > ago(30m)
| where cloud_RoleName == "passport-app" and name == "passport.series.snapshot"
| where tostring(customDimensions["dbp.schema_version"]) == "2"
| extend series = tostring(customDimensions.series_id)
| where isnotempty(series)
| summarize arg_max(timestamp, *) by series
| where timestamp > ago(15m)
| where tolong(customMeasurements.stuck_batteries) > 0
| project timestamp, series, stuck_batteries = tolong(customMeasurements.stuck_batteries)
'''
  }
  {
    key: 'token-budget'
    title: 'DBP - Recorded inference tokens exceed UTC daily budget'
    severity: 2
    window: 'P1D'
    query: '''
dependencies
| where timestamp >= startofday(now())
| where tostring(customDimensions["gen_ai.operation.name"]) == "chat"
| summarize arg_max(timestamp, *) by operation_Id, id
| extend inputTokens = tolong(customDimensions["gen_ai.usage.input_tokens"]),
         outputTokens = tolong(customDimensions["gen_ai.usage.output_tokens"])
| where isnotnull(inputTokens) and isnotnull(outputTokens)
| summarize recordedTokens = sum(inputTokens + outputTokens)
| where recordedTokens > __DAILY_TOKEN_BUDGET__
'''
  }
]

resource rules 'Microsoft.Insights/scheduledQueryRules@2026-03-01' = [for alert in alerts: {
  name: '${insightsName}-${alert.key}'
  location: location
  tags: tags
  kind: 'LogAlert'
  properties: {
    displayName: alert.title
    description: 'Synthetic MVP. Missing telemetry is unknown, not healthy. Thresholds are illustrative assumptions.'
    enabled: enableAlerts
    severity: alert.severity
    evaluationFrequency: 'PT5M'
    windowSize: alert.window
    scopes: [ insights.id ]
    autoMitigate: true
    criteria: {
      allOf: [
        {
          query: replace(replace(alert.query, '__HOLD_AGE_MINUTES__', string(holdAgeMinutes)), '__DAILY_TOKEN_BUDGET__', string(dailyTokenBudget))
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: enableAlerts ? [ notifications!.id ] : []
    }
  }
}]

output applicationInsightsId string = insights.id
output applicationInsightsName string = insights.name
@description('Application ID for the Application Insights query API, used by the deployment checks.')
output applicationInsightsAppId string = insights.properties.AppId
output workbookIds array = [for (definition, index) in definitions: {
  persona: definition.key
  id: workbooks[index].id
}]
output alertIds array = [for (alert, index) in alerts: rules[index].id]
output alertsEnabled bool = enableAlerts
