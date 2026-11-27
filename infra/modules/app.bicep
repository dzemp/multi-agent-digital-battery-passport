// The passport app (Container Apps) with Easy Auth sign-in in front. Deployed on its own, after main.bicep,
// once the image is in the registry and the Entra app registration exists:
//   az deployment group create -g <rg> --template-file infra/modules/app.bicep --parameters ...
// It only references the platform resources; it never redeploys the VM, data stores or Foundry.
targetScope = 'resourceGroup'

param prefix string
param location string = resourceGroup().location
param tags object = {
  project: 'battery-passport'
  environment: 'synthetic-mvp'
}

@description('Existing resources created by main.bicep (its appParameters output).')
param environmentName string
param appIdentityName string
param registryName string
param appStorageName string
param digestStorageName string
param sqlServerName string
param databaseName string = 'passport'
param applicationInsightsName string
@description('VNet created by main.bicep; its endpoints subnet holds the scheduler\'s private endpoint.')
param vnetName string = '${prefix}-vnet'

@description('Full image name, for example <registry>.azurecr.io/passport-app:20261112-1200.')
param image string

@description('Application (client) ID of the Entra app registration that protects the app.')
param appClientId string

@description('Client secret Easy Auth uses for browser sign-in; stored only as a Container Apps secret.')
@secure()
param appClientSecret string

@description('Keep at least one replica running. With agent mode enabled, the minimum is always one.')
param warmReplica bool = false

@description('Extra client IDs allowed to call the app with a token, for example the hosted agents\' identities.')
param extraAllowedClientIds array = []

@description('Portal links to the four persona workbooks: { executive, compliance, agents, operations }.')
param workbookLinks object = {}

@description('true: issuance waits for the compliance agent to request publication.')
param agentMode bool = false

@description('Optional base64 JSON settings; empty values are not set.')
param foundryConfigB64 string = ''
@description('Base64 JSON party registry (dbp-party-registry-1, public keys only) for the mock connector.')
param partyRegistryB64 string = ''
param pricesB64 string = ''

var appName = '${prefix}-passport'
var azureCliAppId = '04b07795-8ddb-461a-bbee-02f9e1bf7b46' // Microsoft's public client ID for the Azure CLI
var workflowName = '${prefix}-dts'
var workflowTaskHubName = '${prefix}-passport-hub'
var roles = {
  durableTaskDataContributor: '0ad04412-c4d5-4796-b79c-f76d14c8d402'
}

resource appEnvironment 'Microsoft.App/managedEnvironments@2026-07-01' existing = {
  name: environmentName
}

resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: appIdentityName
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' existing = {
  name: registryName
}

resource appStorage 'Microsoft.Storage/storageAccounts@2026-09-01' existing = {
  name: appStorageName
}

resource digestStorage 'Microsoft.Storage/storageAccounts@2026-09-01' existing = {
  name: digestStorageName
}

resource sqlServer 'Microsoft.Sql/servers@2025-01-01' existing = {
  name: sqlServerName
}

resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: applicationInsightsName
}

resource vnet 'Microsoft.Network/virtualNetworks@2026-03-01' existing = {
  name: vnetName
}

// Like SQL and Blob, the scheduler is reachable only through a private endpoint in the VNet.
resource workflowScheduler 'Microsoft.DurableTask/schedulers@2026-02-01' = if (agentMode) {
  name: workflowName
  location: location
  tags: tags
  properties: {
    ipAllowlist: []
    publicNetworkAccess: 'Disabled'
    sku: {
      name: 'Consumption'
      // Only Dedicated accepts a provisioned capacity.
    }
  }
}

resource schedulerDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (agentMode) {
  name: 'privatelink.durabletask.io'
  location: 'global'
  tags: tags
}

resource schedulerDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (agentMode) {
  parent: schedulerDnsZone
  name: '${prefix}-link'
  location: 'global'
  tags: tags
  properties: { registrationEnabled: false, virtualNetwork: { id: vnet.id } }
}

module schedulerEndpoint 'private-endpoint.bicep' = if (agentMode) {
  name: 'scheduler-endpoint'
  dependsOn: [ schedulerDnsLink ] // one VNet write at a time
  params: {
    name: '${prefix}-dts-pe'
    location: location
    tags: tags
    subnetId: '${vnet.id}/subnets/endpoints'
    targetId: workflowScheduler.id
    groupId: 'scheduler'
    dnsZoneIds: [ schedulerDnsZone.id ]
  }
}

resource workflowTaskHub 'Microsoft.DurableTask/schedulers/taskHubs@2026-02-01' = if (agentMode) {
  parent: workflowScheduler
  name: workflowTaskHubName
}

resource workflowRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (agentMode) {
  name: guid(workflowTaskHub.id, appIdentity.id, roles.durableTaskDataContributor)
  scope: workflowTaskHub
  properties: {
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions',
      roles.durableTaskDataContributor)
  }
}

var baseEnv = [
  { name: 'DBP_AUTH_MODE', value: 'easyauth' }
  { name: 'DBP_STORE', value: 'sql' }
  { name: 'DBP_BLOBS', value: 'azure' }
  { name: 'DBP_BLOB_ACCOUNT_URL', value: appStorage.properties.primaryEndpoints.blob }
  { name: 'DBP_SQL_SERVER', value: sqlServer.properties.fullyQualifiedDomainName }
  { name: 'DBP_SQL_DATABASE', value: databaseName }
  { name: 'AZURE_CLIENT_ID', value: appIdentity.properties.clientId }
  { name: 'DBP_WORKER_INTERVAL', value: '5' }
  { name: 'DBP_DIGEST_ACCOUNT_URL', value: digestStorage.properties.primaryEndpoints.blob }
  // Settings page overrides (Passport.Admin); the app reads them at start-up.
  { name: 'DBP_SETTINGS_BLOB_URL', value: '${appStorage.properties.primaryEndpoints.blob}settings/passport-settings.json' }
  { name: 'DBP_RESOURCE_GROUP', value: resourceGroup().name }
  { name: 'DBP_PUBLIC_BASE_URL', value: 'https://${appName}.${appEnvironment.properties.defaultDomain}' }
  { name: 'DBP_AGENT_MODE', value: agentMode ? 'on' : 'off' }
  { name: 'DBP_DTS_ENDPOINT', value: agentMode ? workflowScheduler!.properties.endpoint : '' }
  { name: 'DBP_DTS_TASKHUB', value: agentMode ? workflowTaskHub.name : '' }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'appinsights-connection-string' }
]

var workbookEnv = [for persona in [ 'executive', 'compliance', 'agents', 'operations' ]: {
  name: 'DBP_WORKBOOK_${toUpper(persona)}'
  value: workbookLinks[?persona] ?? ''
}]

var optionalEnv = [
  { name: 'DBP_FOUNDRY_CONFIG_B64', value: foundryConfigB64 }
  { name: 'DBP_PARTY_REGISTRY_B64', value: partyRegistryB64 }
  { name: 'DBP_PRICES_B64', value: pricesB64 }
]

resource app 'Microsoft.App/containerApps@2026-07-01' = {
  name: appName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${appIdentity.id}': {} }
  }
  dependsOn: agentMode ? [ workflowRole, schedulerEndpoint ] : []
  properties: {
    managedEnvironmentId: appEnvironment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [ { server: registry.properties.loginServer, identity: appIdentity.id } ]
      secrets: [
        { name: 'microsoft-provider-authentication-secret', value: appClientSecret }
        { name: 'appinsights-connection-string', value: insights.properties.ConnectionString }
      ]
    }
    template: {
      containers: [
        {
          name: 'passport'
          image: image
          resources: { cpu: 1, memory: '2Gi' }
          env: concat(baseEnv, filter(concat(workbookEnv, optionalEnv), setting => !empty(setting.value)))
          probes: [
            { type: 'Liveness', httpGet: { path: '/health/live', port: 8000 }, periodSeconds: 30 }
            { type: 'Readiness', httpGet: { path: '/health/ready', port: 8000 }, periodSeconds: 30, timeoutSeconds: 10 }
          ]
        }
      ]
      scale: {
        minReplicas: (warmReplica || agentMode) ? 1 : 0 // the durable workflow worker needs a live process
        maxReplicas: 3
      }
    }
  }
}

// Easy Auth: Container Apps checks the Microsoft Entra sign-in before a request reaches the app.
// Anonymous requests are allowed through so visitors can read public passports; the app decides by role.
resource signIn 'Microsoft.App/containerApps/authConfigs@2026-07-01' = {
  parent: app
  name: 'current'
  properties: {
    platform: { enabled: true }
    globalValidation: { unauthenticatedClientAction: 'AllowAnonymous' }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: appClientId
          clientSecretSettingName: 'microsoft-provider-authentication-secret'
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenant().tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [ 'api://${appClientId}', appClientId ]
          // Only these client programs may present tokens: the app's own sign-in, the Azure CLI (scripts)
          // and, once published, the hosted agents' identities.
          defaultAuthorizationPolicy: {
            allowedApplications: concat([ appClientId, azureCliAppId ], extraAllowedClientIds)
          }
        }
      }
    }
  }
}

output appName string = app.name
output appFqdn string = app.properties.configuration.ingress.fqdn
output appUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
