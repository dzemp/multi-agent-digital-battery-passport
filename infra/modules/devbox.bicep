// Windows 11 developer VM. Every step that needs the private network (seeding, agents, ledger, smoke test)
// runs here. Inbound RDP is governed by the devbox subnet NSG (network.bicep) and, in jit mode, Defender JIT.
targetScope = 'resourceGroup'

param prefix string
param location string
param tags object
param subnetId string

@allowed([ 'source-ip', 'jit' ])
param vmAccess string

@description('Public IPv4 address (no prefix length) that JIT may open RDP to.')
param clientIp string

param adminUsername string

@description('Generated at runtime by the orchestrator; never written to configuration or state.')
@secure()
@minLength(12)
param adminPassword string

param vmSize string

@description('Daily shutdown time, HH:mm, in timeZone.')
param autoShutdownTime string

@description('Windows time zone ID for the shutdown schedule, for example W. Europe Standard Time.')
param timeZone string

@description('false when the VM already exists. ARM cannot change the administrator password of an existing VM, so reruns leave the VM untouched; reset the password with the orchestrator\'s "vm reset-password" instead.')
param createVm bool = true

var vmName = '${prefix}-devbox'

resource address 'Microsoft.Network/publicIPAddresses@2026-03-01' = {
  name: '${vmName}-ip'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: { publicIPAllocationMethod: 'Static' }
}

resource nic 'Microsoft.Network/networkInterfaces@2026-03-01' = {
  name: '${vmName}-nic'
  location: location
  tags: tags
  properties: {
    ipConfigurations: [
      {
        name: 'primary'
        properties: {
          primary: true
          privateIPAllocationMethod: 'Dynamic'
          subnet: { id: subnetId }
          publicIPAddress: { id: address.id }
        }
      }
    ]
  }
}

// Windows 11 Enterprise with a Windows_Client licence: the operator must hold an eligible licence.
resource vm 'Microsoft.Compute/virtualMachines@2026-04-01' = if (createVm) {
  name: vmName
  location: location
  tags: tags
  properties: {
    licenseType: 'Windows_Client'
    hardwareProfile: { vmSize: vmSize }
    storageProfile: {
      imageReference: {
        publisher: 'MicrosoftWindowsDesktop'
        offer: 'windows-11'
        sku: 'win11-24h2-ent'
        version: 'latest'
      }
      osDisk: {
        createOption: 'FromImage'
        managedDisk: { storageAccountType: 'StandardSSD_LRS' }
        deleteOption: 'Delete'
      }
    }
    osProfile: {
      computerName: vmName
      adminUsername: adminUsername
      adminPassword: adminPassword
      windowsConfiguration: {
        provisionVMAgent: true
        enableAutomaticUpdates: true
        patchSettings: { patchMode: 'AutomaticByOS' }
      }
    }
    securityProfile: {
      securityType: 'TrustedLaunch'
      uefiSettings: { secureBootEnabled: true, vTpmEnabled: true }
    }
    networkProfile: { networkInterfaces: [ { id: nic.id, properties: { primary: true } } ] }
  }
}

// The same VM whether created now or by an earlier run; resolves to a resource ID without a read.
resource devboxVm 'Microsoft.Compute/virtualMachines@2026-04-01' existing = {
  name: vmName
}

// Safety net for cost: the VM is deallocated daily even if the operator forgets "vm stop".
resource shutdown 'Microsoft.DevTestLab/schedules@2018-09-15' = {
  name: 'shutdown-computevm-${vmName}'
  location: location
  tags: tags
  properties: {
    status: 'Enabled'
    taskType: 'ComputeVmShutdownTask'
    dailyRecurrence: { time: replace(autoShutdownTime, ':', '') }
    timeZoneId: timeZone
    targetResourceId: devboxVm.id
    notificationSettings: { status: 'Disabled' }
  }
  dependsOn: [ vm ]
}

// jit mode only: needs Defender for Servers Plan 2 on the subscription (checked by the orchestrator first).
resource jit 'Microsoft.Security/locations/jitNetworkAccessPolicies@2020-01-01' = if (vmAccess == 'jit') {
  name: '${location}/default'
  kind: 'Basic'
  properties: {
    virtualMachines: [
      {
        id: devboxVm.id
        ports: [
          {
            number: 3389
            protocol: 'TCP'
            allowedSourceAddressPrefix: clientIp
            maxRequestAccessDuration: 'PT3H'
          }
        ]
      }
    ]
  }
  dependsOn: [ vm ]
}

output vmName string = vmName
output vmId string = devboxVm.id
output nicName string = nic.name
output publicIpName string = address.name
output publicIpAddress string = address.properties.ipAddress
output adminUsername string = adminUsername
