// One virtual network for the MVP. SQL, Blob and Foundry are reachable only through private endpoints,
// so everything that seeds data or calls agents runs on the developer VM inside this network.
targetScope = 'resourceGroup'

param prefix string
param location string
param tags object

@description('source-ip: the NSG allows RDP from clientIp only. jit: the NSG denies all inbound; Defender JIT opens RDP on request.')
@allowed([ 'source-ip', 'jit' ])
param vmAccess string

@description('Public IPv4 address (no prefix length) allowed to reach the developer VM over RDP.')
@minLength(7)
param clientIp string

@description('NAT gateway resource ID for the agents subnet; empty for none.')
param natGatewayId string = ''

var zoneNames = {
  blob: 'privatelink.blob.${environment().suffixes.storage}'
  sql: 'privatelink${environment().suffixes.sqlServerHostname}'
  aiServices: 'privatelink.services.ai.azure.com'
  openAi: 'privatelink.openai.azure.com'
  cognitiveServices: 'privatelink.cognitiveservices.azure.com'
}

var denyAllInbound = {
  name: 'DenyAllInbound'
  properties: {
    priority: 4096
    access: 'Deny'
    direction: 'Inbound'
    protocol: '*'
    sourceAddressPrefix: '*'
    sourcePortRange: '*'
    destinationAddressPrefix: '*'
    destinationPortRange: '*'
  }
}

var rdpFromClient = {
  name: 'AllowRdpFromClient'
  properties: {
    priority: 100
    access: 'Allow'
    direction: 'Inbound'
    protocol: 'Tcp'
    sourceAddressPrefix: '${clientIp}/32'
    sourcePortRange: '*'
    destinationAddressPrefix: '*'
    destinationPortRange: '3389'
  }
}

// The access rule lives here, with the subnet's NSG, so a redeployment never drops it.
resource devboxNsg 'Microsoft.Network/networkSecurityGroups@2026-03-01' = {
  name: '${prefix}-devbox-nsg'
  location: location
  tags: tags
  properties: {
    securityRules: vmAccess == 'source-ip' ? [ rdpFromClient, denyAllInbound ] : [ denyAllInbound ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2026-03-01' = {
  name: '${prefix}-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: [ '10.64.0.0/16' ] }
    subnets: [
      {
        name: 'apps'
        properties: {
          addressPrefix: '10.64.0.0/24'
          delegations: [ { name: 'apps', properties: { serviceName: 'Microsoft.App/environments' } } ]
        }
      }
      {
        name: 'endpoints'
        properties: {
          addressPrefix: '10.64.1.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
          defaultOutboundAccess: false
        }
      }
      {
        // Outbound for tool installs and the Azure, Fabric and Graph APIs comes from the VM's public IP.
        name: 'devbox'
        properties: {
          addressPrefix: '10.64.2.0/27'
          networkSecurityGroup: { id: devboxNsg.id }
          defaultOutboundAccess: false
        }
      }
      {
        // Foundry agent network injection. Hosted agent builds need outbound access (packages, mcr.microsoft.com);
        // without the optional NAT gateway, Azure's default outbound access is kept on explicitly.
        name: 'agents'
        properties: {
          addressPrefix: '10.64.4.0/24'
          delegations: [ { name: 'agents', properties: { serviceName: 'Microsoft.App/environments' } } ]
          natGateway: empty(natGatewayId) ? null : { id: natGatewayId }
          defaultOutboundAccess: empty(natGatewayId)
        }
      }
    ]
  }
}

resource zones 'Microsoft.Network/privateDnsZones@2024-06-01' = [for zone in items(zoneNames): {
  name: zone.value
  location: 'global'
  tags: tags
}]

// Serialize link writes to the shared VNet; other network resources can still deploy in parallel.
@batchSize(1)
resource links 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (zone, i) in items(zoneNames): {
  parent: zones[i]
  name: '${prefix}-link'
  location: 'global'
  tags: tags
  properties: { registrationEnabled: false, virtualNetwork: { id: vnet.id } }
}]

output vnetName string = vnet.name
output vnetId string = vnet.id
output subnetIds object = {
  apps: '${vnet.id}/subnets/apps'
  endpoints: '${vnet.id}/subnets/endpoints'
  devbox: '${vnet.id}/subnets/devbox'
  agents: '${vnet.id}/subnets/agents'
}
output dnsZoneIds object = {
  blob: resourceId('Microsoft.Network/privateDnsZones', zoneNames.blob)
  sql: resourceId('Microsoft.Network/privateDnsZones', zoneNames.sql)
  ai: [
    resourceId('Microsoft.Network/privateDnsZones', zoneNames.aiServices)
    resourceId('Microsoft.Network/privateDnsZones', zoneNames.openAi)
    resourceId('Microsoft.Network/privateDnsZones', zoneNames.cognitiveServices)
  ]
}
output devboxNsgName string = devboxNsg.name
