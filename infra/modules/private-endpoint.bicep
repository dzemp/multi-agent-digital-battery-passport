// One private endpoint and its DNS zone group, so the service name resolves to a private address in the VNet.
targetScope = 'resourceGroup'

param name string
param location string
param tags object
param subnetId string
param targetId string
param groupId string
param dnsZoneIds array

resource endpoint 'Microsoft.Network/privateEndpoints@2026-03-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    subnet: { id: subnetId }
    privateLinkServiceConnections: [
      { name: name, properties: { privateLinkServiceId: targetId, groupIds: [ groupId ] } }
    ]
  }
}

resource dns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2026-03-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      for (id, i) in dnsZoneIds: { name: 'zone-${i}', properties: { privateDnsZoneId: id } }
    ]
  }
}

output endpointId string = endpoint.id
