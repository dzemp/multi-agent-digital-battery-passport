// Manual deployment path (the orchestrator passes the same values itself):
//   set DBP_PREFIX, DBP_OPERATOR_UPN, DBP_OPERATOR_OBJECT_ID, DBP_CLIENT_IP and DBP_VM_PASSWORD, then
//   az deployment sub create --location swedencentral --parameters infra/main.bicepparam
// Never write the VM password into this file. Every other parameter keeps its main.bicep default.
using 'main.bicep'

param prefix = readEnvironmentVariable('DBP_PREFIX')
param location = readEnvironmentVariable('DBP_LOCATION', 'swedencentral')
param operatorUpn = readEnvironmentVariable('DBP_OPERATOR_UPN')
param operatorObjectId = readEnvironmentVariable('DBP_OPERATOR_OBJECT_ID')
param clientIp = readEnvironmentVariable('DBP_CLIENT_IP')
param vmAdminPassword = readEnvironmentVariable('DBP_VM_PASSWORD')
