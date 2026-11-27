// Microsoft Fabric capacity for the analytics path (lakehouse, mirrored passport data, reports).
// Billed per hour while running: the orchestrator pauses it after deployment, because Bicep cannot.
targetScope = 'resourceGroup'

param prefix string
param location string
param tags object

@description('Fabric capacity administrator (UPN) in this tenant.')
@minLength(3)
param operatorUpn string

@description('F2 is the smallest paid Fabric capacity.')
@allowed([ 'F2', 'F4', 'F8', 'F16', 'F32', 'F64' ])
param fabricSku string

var suffix = uniqueString(resourceGroup().id)

resource fabric 'Microsoft.Fabric/capacities@2023-11-01' = {
  name: '${prefix}fabric${suffix}'
  location: location
  tags: tags
  sku: { name: fabricSku, tier: 'Fabric' }
  properties: {
    administration: { members: [ operatorUpn ] }
  }
}

output capacityName string = fabric.name
output capacityId string = fabric.id
output sku string = fabric.sku.name
