// Optional NAT gateway for the agents subnet (enableNatGateway). Use it if hosted agents cannot reach
// the passport app or the Foundry endpoints during the smoke test. It adds an hourly charge.
targetScope = 'resourceGroup'

param prefix string
param location string
param tags object

resource address 'Microsoft.Network/publicIPAddresses@2026-03-01' = {
  name: '${prefix}-nat-ip'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: { publicIPAllocationMethod: 'Static' }
}

resource gateway 'Microsoft.Network/natGateways@2026-03-01' = {
  name: '${prefix}-nat'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: {
    idleTimeoutInMinutes: 4
    publicIpAddresses: [ { id: address.id } ]
  }
}

output natGatewayId string = gateway.id
