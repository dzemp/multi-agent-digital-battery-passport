// Private Microsoft Foundry account and the "passport" project for the hosted agents. Public access is
// disabled; the account is reached through a private endpoint and agents run injected into the agents subnet.
// Adapted from the Foundry samples' private-network basic VNet pattern.
targetScope = 'resourceGroup'

param prefix string
param location string
param tags object

@description('Agents subnet ID for network injection.')
param agentsSubnetId string

@description('Endpoints subnet ID for the account private endpoint.')
param endpointsSubnetId string

@description('Private DNS zone IDs for services.ai, openai and cognitiveservices.')
param aiDnsZoneIds array

@description('Object ID of the operator; receives Foundry Project Manager on the project.')
param operatorObjectId string

@description('Principal ID of the passport app identity; receives Foundry User on the project.')
param appPrincipalId string

@description('Application Insights resource (monitoring.bicep) connected to the project for agent tracing.')
param applicationInsightsName string

@description('Log Analytics workspace (platform.bicep) that receives the account request logs and metrics.')
param workspaceName string

@description('Model deployments: [{ name, model: { format, name, version }, sku: { name, capacity } }].')
param models array

var suffix = uniqueString(resourceGroup().id)
var accountName = '${prefix}-ai-${suffix}'
var roles = {
  foundryProjectManager: 'eadc314b-1a2d-4efa-be10-5d325db5065e'
  foundryUser: '53ca6127-db72-4b80-b1b0-d745d6d5456d'
  cognitiveServicesUser: 'a97b65f3-24c7-4388-baec-2e87135dc908'
}

resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: applicationInsightsName
}

resource account 'Microsoft.CognitiveServices/accounts@2026-09-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    allowProjectManagement: true
    customSubDomainName: accountName
    disableLocalAuth: true
    publicNetworkAccess: 'Disabled'
    networkAcls: { defaultAction: 'Deny', ipRules: [], virtualNetworkRules: [] }
    networkInjections: [
      { scenario: 'agent', subnetArmId: agentsSubnetId, useMicrosoftManagedNetwork: false }
    ]
  }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2026-03-01' existing = {
  name: workspaceName
}

// Account request logs (Shield Prompt and model calls) and metrics go to the app's own workspace, so service
// calls and app decisions can be queried together. A tenant policy may add its own setting alongside this one.
resource accountLogs 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'foundry-logs'
  scope: account
  properties: {
    workspaceId: logs.id
    logs: [ { categoryGroup: 'allLogs', enabled: true } ]
    metrics: [ { category: 'AllMetrics', enabled: true } ]
  }
}

// Model writes share a parent-account lock, even when their deployment names differ.
@batchSize(1)
resource deployments 'Microsoft.CognitiveServices/accounts/deployments@2026-09-01' = [for model in models: {
  parent: account
  name: model.name
  sku: model.sku
  properties: { model: model.model }
}]

module endpoint 'private-endpoint.bicep' = {
  name: 'foundry-endpoint'
  params: {
    name: '${prefix}-foundry-pe'
    location: location
    tags: tags
    subnetId: endpointsSubnetId
    targetId: account.id
    groupId: 'account'
    dnsZoneIds: aiDnsZoneIds
  }
  // Endpoint approval and project setup must not overlap the account's model writes.
  dependsOn: [ deployments ]
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2026-09-01' = {
  parent: account
  name: 'passport'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'Battery passport'
    description: 'Synthetic battery passport MVP: hosted agents for intake, evidence, compliance, publication and support.'
  }
  dependsOn: [ endpoint ]
}

// Project audit and trace logs go to the same workspace as account requests and metrics.
resource projectLogs 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'foundry-logs'
  scope: project
  properties: {
    workspaceId: logs.id
    logs: [ { categoryGroup: 'allLogs', enabled: true } ]
    metrics: [ { category: 'AllMetrics', enabled: true } ]
  }
}

resource capabilityHost 'Microsoft.CognitiveServices/accounts/projects/capabilityHosts@2026-09-01' = {
  parent: project
  name: 'agents'
  // The official basic VNet sample uses this property; the preview Bicep type is incomplete.
  #disable-next-line BCP037
  properties: { capabilityHostKind: 'Agents' }
}

// Sends agent traces to the same Application Insights as the app. Shape from the Foundry samples'
// connection-application-insights pattern; if it fails, connect it in the portal under Tracing.
resource tracing 'Microsoft.CognitiveServices/accounts/projects/connections@2026-09-01' = {
  parent: project
  name: insights.name
  properties: {
    category: 'AppInsights'
    target: insights.id
    authType: 'ApiKey'
    isSharedToAll: false
    credentials: { key: insights.properties.ConnectionString }
    metadata: { ApiType: 'Azure', ResourceId: insights.id }
  }
  // Serialise project writes.
  dependsOn: [ capabilityHost ]
}

resource operatorManagesProject 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, operatorObjectId, roles.foundryProjectManager)
  scope: project
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.foundryProjectManager)
    principalId: operatorObjectId
    principalType: 'User'
  }
}

// The app only invokes the published agents; it cannot change the project.
resource appInvokesAgents 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, appPrincipalId, roles.foundryUser)
  scope: project
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.foundryUser)
    principalId: appPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Prompt Shields (Content Safety) is an account-level data-plane API. Cognitive Services User is the documented
// role; it is broader than shieldPrompt alone, so it is limited to this account and the two identities that call it.
resource appScreensPrompts 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, appPrincipalId, roles.cognitiveServicesUser)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.cognitiveServicesUser)
    principalId: appPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource operatorScreensPrompts 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, operatorObjectId, roles.cognitiveServicesUser)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.cognitiveServicesUser)
    principalId: operatorObjectId
    principalType: 'User'
  }
}

output endpoint string = 'https://${account.name}.services.ai.azure.com/api/projects/${project.name}'
output accountName string = account.name
output accountId string = account.id
output projectName string = project.name
output projectId string = project.id
output projectPrincipalId string = project.identity.principalId
output modelDeploymentNames array = [for (model, i) in models: deployments[i].name]
output tracingConnectionName string = tracing.name
