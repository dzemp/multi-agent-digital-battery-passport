// Passport platform: logs, the app identity, container registry, Blob storage, the ledger digest store,
// Azure SQL (Entra-only, the system of record) and the Container Apps environment. The app itself is
// deployed later by app.bicep, once its image and app registration exist.
targetScope = 'resourceGroup'

param prefix string
param location string
param tags object

@description('Object ID of the operator. Becomes the SQL Entra administrator and may write Blob data.')
param operatorObjectId string

@description('Sign-in name (UPN) of the operator; shown as the SQL administrator login.')
param operatorLogin string

@description('Subnet IDs from network.bicep: apps and endpoints are used here.')
param subnetIds object

@description('Private DNS zone IDs from network.bicep: blob and sql are used here.')
param dnsZoneIds object

var suffix = uniqueString(resourceGroup().id)
var roles = {
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  blobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  blobDataReader: '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
}
var databaseName = 'passport'

// ---- shared basics ----------------------------------------------------------------------------
resource logs 'Microsoft.OperationalInsights/workspaces@2026-03-01' = {
  name: '${prefix}-logs-${suffix}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

// The app's own identity: pulls the image and reads/writes Blob and SQL without any password.
resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: '${prefix}-app-identity'
  location: location
  tags: tags
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' = {
  name: '${prefix}acr${suffix}'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
    networkRuleBypassOptions: 'None'
  }
}

resource appPullsImages 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, appIdentity.id, roles.acrPull)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---- Blob storage for evidence files, passport snapshots, package intake and settings ----------
resource appStorage 'Microsoft.Storage/storageAccounts@2026-09-01' = {
  name: take('${prefix}app${suffix}', 24)
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    allowSharedKeyAccess: false // only Microsoft Entra identities, no account keys
    allowBlobPublicAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Disabled'
    networkAcls: { defaultAction: 'Deny', bypass: 'None' }
  }
}

resource appBlobService 'Microsoft.Storage/storageAccounts/blobServices@2026-09-01' = {
  parent: appStorage
  name: 'default'
  properties: {
    isVersioningEnabled: true // every overwrite keeps the old version: the app records version IDs as proof
    deleteRetentionPolicy: { enabled: true, days: 7 }
    containerDeleteRetentionPolicy: { enabled: true, days: 7 }
  }
}

resource appContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2026-09-01' = [
  for name in [ 'evidence', 'passports', 'intake', 'settings' ]: {
    parent: appBlobService
    name: name
    properties: { publicAccess: 'None' }
  }
]

resource appUsesBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appStorage.id, appIdentity.id, roles.blobDataContributor)
  scope: appStorage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobDataContributor)
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// The operator uploads packages and supplier evidence into intake, and alters a copy in the tamper check.
resource operatorUsesBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appStorage.id, operatorObjectId, roles.blobDataContributor)
  scope: appStorage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobDataContributor)
    principalId: operatorObjectId
    principalType: 'User'
  }
}

// ---- digest store: the operator anchors ledger digests through the private endpoints -----------
resource digestStorage 'Microsoft.Storage/storageAccounts@2026-09-01' = {
  name: take('${prefix}dig${suffix}', 24)
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Disabled'
    networkAcls: { defaultAction: 'Deny', bypass: 'None' }
  }
}

resource digestBlobService 'Microsoft.Storage/storageAccounts/blobServices@2026-09-01' = {
  parent: digestStorage
  name: 'default'
}

resource digestContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2026-09-01' = {
  parent: digestBlobService
  name: 'sqldbledgerdigests'
  properties: { publicAccess: 'None' }
}

// Write-once rule for the digests. Left UNLOCKED so teardown can remove the environment; production locks it.
resource digestRetention 'Microsoft.Storage/storageAccounts/blobServices/containers/immutabilityPolicies@2026-09-01' = {
  parent: digestContainer
  name: 'default'
  properties: {
    immutabilityPeriodSinceCreationInDays: 1
    allowProtectedAppendWrites: true
  }
}

resource operatorStoresDigests 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(digestStorage.id, operatorObjectId, roles.blobDataContributor)
  scope: digestStorage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobDataContributor)
    principalId: operatorObjectId
    principalType: 'User'
  }
}

// The app only reads the anchored digests, for the trail screen's ledger verification. It can never write them.
resource appReadsDigests 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(digestContainer.id, appIdentity.id, roles.blobDataReader)
  scope: digestContainer
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobDataReader)
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---- Azure SQL: the ledger, the system of record for passport decisions -------------------------
resource sqlServer 'Microsoft.Sql/servers@2025-01-01' = {
  name: '${prefix}-sql-${suffix}'
  location: location
  tags: tags
  properties: {
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Disabled'
    administrators: {
      administratorType: 'ActiveDirectory'
      azureADOnlyAuthentication: true // no SQL passwords exist at all
      login: operatorLogin
      sid: operatorObjectId
      tenantId: tenant().tenantId
      principalType: 'User'
    }
  }
}

// A single private TCP/1433 connection also works from the developer VM.
resource sqlConnectionPolicy 'Microsoft.Sql/servers/connectionPolicies@2025-01-01' = {
  parent: sqlServer
  name: 'default'
  properties: { connectionType: 'Proxy' }
}

resource database 'Microsoft.Sql/servers/databases@2025-01-01' = {
  parent: sqlServer
  name: databaseName
  location: location
  tags: tags
  sku: { name: 'GP_S_Gen5_2', tier: 'GeneralPurpose', family: 'Gen5', capacity: 2 }
  properties: {
    autoPauseDelay: 60
    minCapacity: json('0.5')
    requestedBackupStorageRedundancy: 'Local'
    useFreeLimit: false
  }
}

// Server audit to Log Analytics: sign-ins and permission, principal and schema changes. Statement auditing
// (BATCH_COMPLETED_GROUP) is left out: the worker polls every few seconds, and the ledger records data changes.
resource masterDatabase 'Microsoft.Sql/servers/databases@2025-01-01' existing = {
  parent: sqlServer
  name: 'master'
}

// Azure Monitor receives server audit events through the master database's diagnostic setting. Diagnostic
// settings are the MVP's only preview API version: the stable 2016-09-01 version cannot target another resource.
resource sqlAuditLogs 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'sql-audit'
  scope: masterDatabase
  properties: {
    workspaceId: logs.id
    logs: [ { category: 'SQLSecurityAuditEvents', enabled: true } ]
  }
}

resource sqlAuditing 'Microsoft.Sql/servers/auditingSettings@2025-01-01' = {
  parent: sqlServer
  name: 'default'
  dependsOn: [ sqlAuditLogs ]
  properties: {
    state: 'Enabled'
    isAzureMonitorTargetEnabled: true
    auditActionsAndGroups: [
      'SUCCESSFUL_DATABASE_AUTHENTICATION_GROUP'
      'FAILED_DATABASE_AUTHENTICATION_GROUP'
      'DATABASE_PERMISSION_CHANGE_GROUP'
      'DATABASE_PRINCIPAL_CHANGE_GROUP'
      'DATABASE_ROLE_MEMBER_CHANGE_GROUP'
      'SCHEMA_OBJECT_CHANGE_GROUP'
      'SCHEMA_OBJECT_PERMISSION_CHANGE_GROUP'
    ]
  }
}

// ---- Container Apps environment (the app is added by app.bicep) ---------------------------------
resource appEnvironment 'Microsoft.App/managedEnvironments@2026-07-01' = {
  name: '${prefix}-env'
  location: location
  tags: tags
  properties: {
    publicNetworkAccess: 'Enabled'
    vnetConfiguration: {
      internal: false
      infrastructureSubnetId: subnetIds.apps
    }
    workloadProfiles: [ { name: 'Consumption', workloadProfileType: 'Consumption' } ]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

var endpointSpecs = [
  { name: 'app-blob', target: appStorage.id, group: 'blob', zone: dnsZoneIds.blob }
  { name: 'digest-blob', target: digestStorage.id, group: 'blob', zone: dnsZoneIds.blob }
  { name: 'sql', target: sqlServer.id, group: 'sqlServer', zone: dnsZoneIds.sql }
]

module serviceEndpoints 'private-endpoint.bicep' = [for spec in endpointSpecs: {
  name: '${spec.name}-endpoint'
  params: {
    name: '${prefix}-${spec.name}-pe'
    location: location
    tags: tags
    subnetId: subnetIds.endpoints
    targetId: spec.target
    groupId: spec.group
    dnsZoneIds: [ spec.zone ]
  }
}]

output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output appStorageName string = appStorage.name
output appStorageUrl string = appStorage.properties.primaryEndpoints.blob
output digestStorageName string = digestStorage.name
output digestStorageUrl string = digestStorage.properties.primaryEndpoints.blob
output sqlServerName string = sqlServer.name
output sqlServerFqdn string = sqlServer.properties.fullyQualifiedDomainName
output databaseName string = databaseName
output appIdentityName string = appIdentity.name
output appIdentityClientId string = appIdentity.properties.clientId
output appIdentityPrincipalId string = appIdentity.properties.principalId
output environmentName string = appEnvironment.name
output environmentDomain string = appEnvironment.properties.defaultDomain
output logsName string = logs.name
