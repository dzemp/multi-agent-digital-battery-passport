// Battery passport MVP: one deployment for the whole platform except the app itself.
// Synthetic data only; not production. Creates the resource group, the private network, the developer VM,
// the passport platform (SQL, Blob, registry, Container Apps environment), monitoring, a private Foundry
// project with model deployments and, optionally, a Fabric capacity.
// The app follows in a separate step (modules/app.bicep, using the appParameters output), because it needs
// a built image and an Entra app registration first.
targetScope = 'subscription'

@description('Short lowercase letters/digits used in every resource name. Keep it stable across redeployments.')
@minLength(3)
@maxLength(6)
param prefix string

@description('Region for every resource. Must offer Fabric, Foundry agents and the chosen models.')
param location string = 'swedencentral'

param resourceGroupName string = '${prefix}-rg'

@description('Sign-in name (UPN) of the operator: SQL administrator login and Fabric capacity administrator.')
param operatorUpn string

@description('Object ID of the operator (az ad signed-in-user show --query id).')
param operatorObjectId string

@description('Public IPv4 address of the operator\'s machine, without prefix length; used for RDP to the developer VM.')
@minLength(7)
param clientIp string

@description('source-ip: RDP open to clientIp only. jit: RDP closed until requested through Defender just-in-time access.')
@allowed([ 'source-ip', 'jit' ])
param vmAccess string = 'source-ip'

param vmAdminUsername string = 'mvpadmin'

@description('Generated at runtime by the orchestrator; never stored in parameter files or state.')
@secure()
@minLength(12)
param vmAdminPassword string

param vmSize string = 'Standard_D4s_v5'

@description('false when the developer VM already exists: reruns then leave it, and its password, untouched.')
param createDevboxVm bool = true

@description('Daily auto-shutdown time of the developer VM, HH:mm.')
param vmAutoShutdownTime string = '19:00'

@description('Windows time zone ID for the auto-shutdown schedule.')
param timeZone string = 'W. Europe Standard Time'

param deployFabric bool = true

@allowed([ 'F2', 'F4', 'F8', 'F16', 'F32', 'F64' ])
param fabricSku string = 'F2'

@description('NAT gateway for the agents subnet; only if hosted agents need it (hourly charge).')
param enableNatGateway bool = false

@description('Enable only after instrumenting the app, validating the queries and agreeing an alert owner.')
param enableAlerts bool = false

param alertEmail string = ''

@minValue(1)
@maxValue(10080)
param holdAgeMinutes int = 60

@minValue(1)
param dailyTokenBudget int = 100000

@description('Foundry model deployments; must cover every model named by the agent contracts. DataZoneStandard processes prompts within the Microsoft-defined data zone of the location (the EU data zone for swedencentral).')
param models array = [
  {
    name: 'gpt-6.1-sol'
    model: { format: 'OpenAI', name: 'gpt-6.1-sol', version: '2026-09-29' }
    sku: { name: 'DataZoneStandard', capacity: 200 }
  }
  {
    name: 'gpt-5.6-luna'
    model: { format: 'OpenAI', name: 'gpt-5.6-luna', version: '2026-07-09' }
    sku: { name: 'DataZoneStandard', capacity: 200 }
  }
  {
    name: 'gpt-5.6-terra'
    model: { format: 'OpenAI', name: 'gpt-5.6-terra', version: '2026-07-09' }
    sku: { name: 'DataZoneStandard', capacity: 200 }
  }
]

var tags = {
  project: 'battery-passport'
  environment: 'synthetic-mvp'
}

resource rg 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module nat 'modules/nat.bicep' = if (enableNatGateway) {
  name: 'nat'
  scope: rg
  params: { prefix: prefix, location: location, tags: tags }
}

module network 'modules/network.bicep' = {
  name: 'network'
  scope: rg
  params: {
    prefix: prefix
    location: location
    tags: tags
    vmAccess: vmAccess
    clientIp: clientIp
    natGatewayId: enableNatGateway ? nat!.outputs.natGatewayId : ''
  }
}

module devbox 'modules/devbox.bicep' = {
  name: 'devbox'
  scope: rg
  params: {
    prefix: prefix
    location: location
    tags: tags
    subnetId: network.outputs.subnetIds.devbox
    vmAccess: vmAccess
    clientIp: clientIp
    adminUsername: vmAdminUsername
    adminPassword: vmAdminPassword
    vmSize: vmSize
    autoShutdownTime: vmAutoShutdownTime
    timeZone: timeZone
    createVm: createDevboxVm
  }
}

module platform 'modules/platform.bicep' = {
  name: 'platform'
  scope: rg
  params: {
    prefix: prefix
    location: location
    tags: tags
    operatorObjectId: operatorObjectId
    operatorLogin: operatorUpn
    subnetIds: network.outputs.subnetIds
    dnsZoneIds: network.outputs.dnsZoneIds
  }
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  scope: rg
  params: {
    prefix: prefix
    location: location
    tags: tags
    workspaceName: platform.outputs.logsName
    enableAlerts: enableAlerts
    alertEmail: alertEmail
    holdAgeMinutes: holdAgeMinutes
    dailyTokenBudget: dailyTokenBudget
  }
}

module foundry 'modules/foundry.bicep' = {
  name: 'foundry'
  scope: rg
  params: {
    prefix: prefix
    location: location
    tags: tags
    agentsSubnetId: network.outputs.subnetIds.agents
    endpointsSubnetId: network.outputs.subnetIds.endpoints
    aiDnsZoneIds: network.outputs.dnsZoneIds.ai
    operatorObjectId: operatorObjectId
    appPrincipalId: platform.outputs.appIdentityPrincipalId
    applicationInsightsName: monitoring.outputs.applicationInsightsName
    workspaceName: platform.outputs.logsName
    models: models
  }
}

module fabric 'modules/fabric-capacity.bicep' = if (deployFabric) {
  name: 'fabric-capacity'
  scope: rg
  params: {
    prefix: prefix
    location: location
    tags: tags
    operatorUpn: operatorUpn
    fabricSku: fabricSku
  }
}

output resourceGroupName string = rg.name
output location string = location

output vnetName string = network.outputs.vnetName
output agentsSubnetId string = network.outputs.subnetIds.agents
output devboxNsgName string = network.outputs.devboxNsgName
output vmName string = devbox.outputs.vmName
output vmPublicIp string = devbox.outputs.publicIpAddress
output vmAdminUsername string = devbox.outputs.adminUsername

output registryName string = platform.outputs.registryName
output registryLoginServer string = platform.outputs.registryLoginServer
output appStorageName string = platform.outputs.appStorageName
output appStorageUrl string = platform.outputs.appStorageUrl
output digestStorageName string = platform.outputs.digestStorageName
output digestStorageUrl string = platform.outputs.digestStorageUrl
output sqlServerName string = platform.outputs.sqlServerName
output sqlServerFqdn string = platform.outputs.sqlServerFqdn
output databaseName string = platform.outputs.databaseName
output appIdentityName string = platform.outputs.appIdentityName
output appIdentityClientId string = platform.outputs.appIdentityClientId
output appIdentityPrincipalId string = platform.outputs.appIdentityPrincipalId
output environmentName string = platform.outputs.environmentName
output environmentDomain string = platform.outputs.environmentDomain
output logsName string = platform.outputs.logsName

output applicationInsightsName string = monitoring.outputs.applicationInsightsName
output applicationInsightsId string = monitoring.outputs.applicationInsightsId
output applicationInsightsAppId string = monitoring.outputs.applicationInsightsAppId
output workbookIds array = monitoring.outputs.workbookIds
output alertsEnabled bool = monitoring.outputs.alertsEnabled

output foundryAccountName string = foundry.outputs.accountName
output foundryAccountId string = foundry.outputs.accountId
output foundryProjectName string = foundry.outputs.projectName
output foundryProjectEndpoint string = foundry.outputs.endpoint
output foundryProjectId string = foundry.outputs.projectId
output foundryProjectPrincipalId string = foundry.outputs.projectPrincipalId
output modelDeploymentNames array = foundry.outputs.modelDeploymentNames

output fabricCapacityName string = deployFabric ? fabric!.outputs.capacityName : ''
output fabricCapacityId string = deployFabric ? fabric!.outputs.capacityId : ''

@description('Ready-made parameters for modules/app.bicep; the deployment step adds image, sign-in and optional settings.')
output appParameters object = {
  prefix: prefix
  location: location
  tags: tags
  environmentName: platform.outputs.environmentName
  appIdentityName: platform.outputs.appIdentityName
  registryName: platform.outputs.registryName
  appStorageName: platform.outputs.appStorageName
  digestStorageName: platform.outputs.digestStorageName
  sqlServerName: platform.outputs.sqlServerName
  databaseName: platform.outputs.databaseName
  applicationInsightsName: monitoring.outputs.applicationInsightsName
}
