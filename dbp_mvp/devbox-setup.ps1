param(
    [string]$RepoUrl = '',
    [string]$RepoRef = '',
    [string]$Subfolder = '',
    [string]$ConfigBase64 = ''
)
# Runs on the developer VM as SYSTEM through Azure Run Command (python -m dbp_mvp bootstrap, phase A2).
# Installs Git, Python 3.14, Azure CLI, Azure Developer CLI and Bicep for all users, clones the repository and
# writes the non-secret settings. Windows PowerShell 5.1 compatible. The bootstrap reads only the last output line
# (Run Command returns the last 4,096 bytes), so the result marker must stay last.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = Join-Path $env:ProgramData 'battery-passport'
$checkout = 'C:\battery-passport'
New-Item -ItemType Directory -Path $root -Force | Out-Null
$log = Join-Path $root 'devbox-setup.log'
$temporary = Join-Path $env:TEMP ('dbp-mvp-setup-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $temporary | Out-Null
# Pinned installers. Each download must match its SHA-256 and carry a valid signature before it runs. To update a
# tool, change its URL and SHA-256 together, taking the digest from the publisher's release page.
$pinned = @{
    Git    = @{
        Url    = 'https://github.com/git-for-windows/git/releases/download/v2.56.1.windows.1/Git-2.56.1-64-bit.exe'
        Sha256 = 'c330c72e10b408cc39994bfefdb63542b0d93d21699325c3e0bd874af8a5a559'
    }
    Python = @{
        Url    = 'https://www.python.org/ftp/python/3.14.10/python-3.14.10-amd64.exe'
        Sha256 = '92480239264744fe0386a1bd8275786fefa126e3129e56a68e22b52c73329965'
    }
    AzCli  = @{
        Url    = 'https://github.com/Azure/azure-cli/releases/download/azure-cli-2.93.0/azure-cli-2.93.0-x64.msi'
        Sha256 = '66c7241d1a10d7c27110e4b434b4ecd3b850db4ddfce12c2e1f0b0d00071e45e'
    }
    Azd    = @{
        Url    = 'https://github.com/Azure/azure-dev/releases/download/azure-dev-cli_1.40.0/azd-windows-amd64.msi'
        Sha256 = 'd53b2e7adbbe9516f4f36e596f48fa96d9a075112de572f27a6be2af402f8929'
    }
    Bicep  = @{
        Url    = 'https://github.com/Azure/bicep/releases/download/v0.49.2/bicep-win-x64.exe'
        Sha256 = 'acfff0453c9bcf442aa97103acbcd845ef366634e6ea2a2ba5734514409ce9c3'
    }
}
$tools = 'failed'
$clone = 'skipped'
$venv = 'skipped'
$settings = ''

function Write-Stage([string]$Message) {
    $line = "$(Get-Date -Format o) $Message"
    Add-Content -Path $log -Value $line
    Write-Output $line
}

function Invoke-Native([string]$Path, [string[]]$Arguments) {
    # Native stderr under 'Stop' throws in Windows PowerShell 5.1; log it instead and return the exit code.
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    & $Path @Arguments 2>&1 | ForEach-Object { Add-Content -Path $log -Value "    $_" }
    return $global:LASTEXITCODE
}

function Get-NativeOutput([string]$Path, [string[]]$Arguments) {
    $ErrorActionPreference = 'Continue'
    & $Path @Arguments 2>$null
}

function Get-VerifiedFile([string]$Name, [hashtable]$Pin, [string]$FileName) {
    $file = Join-Path $temporary $FileName
    Invoke-WebRequest -UseBasicParsing -Uri $Pin.Url -OutFile $file
    $actual = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash
    if ($actual -ne $Pin.Sha256) {
        Remove-Item -LiteralPath $file -Force
        throw "$Name download has SHA-256 $actual, expected $($Pin.Sha256); installation refused"
    }
    if ((Get-AuthenticodeSignature $file).Status -ne 'Valid') {
        throw "$Name download has no valid trusted signature; installation refused"
    }
    return $file
}

function Install-Tool([string]$Name, [string]$CheckPath, [hashtable]$Pin, [string]$FileName, [string]$Arguments) {
    if (Test-Path -LiteralPath $CheckPath) {
        Write-Stage "$Name already installed"
        return
    }
    Write-Stage "Downloading and installing $Name"
    $installer = Get-VerifiedFile $Name $Pin $FileName
    if ($FileName.EndsWith('.msi')) {
        $process = Start-Process msiexec.exe -ArgumentList "/i `"$installer`" $Arguments" -Wait -PassThru
    } else {
        $process = Start-Process $installer -ArgumentList $Arguments -Wait -PassThru
    }
    if ($process.ExitCode -notin @(0, 3010)) {
        throw "$Name installer failed with exit code $($process.ExitCode)"
    }
    if (-not (Test-Path -LiteralPath $CheckPath)) {
        throw "$Name was not found at $CheckPath after installation"
    }
    Write-Stage "$Name installed"
}

function Assert-Safe([string]$Value, [string]$Pattern, [string]$Name) {
    if ($Value -and ($Value -notmatch $Pattern -or $Value -match '\.\.')) {
        throw "$Name has an unexpected format; nothing was cloned"
    }
}

if ($ConfigBase64) {
    $settings = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($ConfigBase64))
    [IO.File]::WriteAllText((Join-Path $root 'mvp.toml'), $settings, (New-Object Text.UTF8Encoding($false)))
    Write-Stage "Settings written to $root\mvp.toml"
}

try {
    $git = Join-Path $env:ProgramFiles 'Git\cmd\git.exe'
    Install-Tool 'Git' $git $pinned.Git 'git.exe' '/VERYSILENT /NORESTART /SP- /SUPPRESSMSGBOXES'
    $python = Join-Path $env:ProgramFiles 'Python314\python.exe'
    Install-Tool 'Python 3.14' $python $pinned.Python 'python.exe' `
        '/quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1 InstallLauncherAllUsers=1 Include_test=0'
    $az = Join-Path $env:ProgramFiles 'Microsoft SDKs\Azure\CLI2\wbin\az.cmd'
    Install-Tool 'Azure CLI' $az $pinned.AzCli 'azure-cli.msi' '/qn /norestart'
    # The azd MSI defaults to a per-user install; SYSTEM's profile would hide it from the signed-in user.
    $azd = Join-Path $env:ProgramFiles 'Azure Dev CLI\azd.exe'
    Install-Tool 'Azure Developer CLI' $azd $pinned.Azd 'azd.msi' `
        "/qn /norestart ALLUSERS=1 INSTALLDIR=`"$env:ProgramFiles\Azure Dev CLI`""

    Write-Stage 'Verifying tools'
    if ((Invoke-Native $git @('--version')) -ne 0) { throw 'Git does not start' }
    $check = 'import sys, ssl; ssl.create_default_context(); sys.exit(0 if sys.version_info[:2] >= (3, 14) else 1)'
    if ((Invoke-Native $python @('-c', $check)) -ne 0) { throw 'Python 3.14 does not start' }
    if ((Invoke-Native $az @('version')) -ne 0) { throw 'Azure CLI does not start' }
    if ((Invoke-Native $azd @('version')) -ne 0) { throw 'Azure Developer CLI does not start' }

    # Run Command executes as SYSTEM: put Bicep on the machine PATH, not only in SYSTEM's profile.
    $bin = Join-Path $root 'bin'
    $bicep = Join-Path $bin 'bicep.exe'
    New-Item -ItemType Directory -Path $bin -Force | Out-Null
    if (-not (Test-Path -LiteralPath $bicep)) {
        Write-Stage 'Downloading and installing Bicep'
        Copy-Item (Get-VerifiedFile 'Bicep' $pinned.Bicep 'bicep.exe') $bicep
    }
    if ((Invoke-Native $bicep @('--version')) -ne 0) { throw 'Bicep does not start' }
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    if ($bin -notin $machinePath.Split(';')) {
        [Environment]::SetEnvironmentVariable('Path', "$machinePath;$bin", 'Machine')
    }
    $tools = 'ready'
    Write-Stage 'Developer tools ready'
} catch {
    Write-Stage "Tool installation failed: $($_.Exception.Message)"
}

if ($tools -eq 'ready') {
    try {
        Assert-Safe $RepoUrl '^https://[A-Za-z0-9.-]+(:[0-9]+)?/[A-Za-z0-9._~/-]+$' 'RepoUrl'
        Assert-Safe $RepoRef '^[A-Za-z0-9][A-Za-z0-9._/-]*$' 'RepoRef'
        Assert-Safe $Subfolder '^[A-Za-z0-9._/-]+$' 'Subfolder'
        $safe = @(Get-NativeOutput $git @('config', '--system', '--get-all', 'safe.directory'))
        if ('C:/battery-passport' -notin $safe) {
            Invoke-Native $git @('config', '--system', '--add', 'safe.directory', 'C:/battery-passport') | Out-Null
        }
        if (-not $RepoUrl) {
            Write-Stage 'No repository URL; clone manually'
        } elseif (Test-Path -LiteralPath (Join-Path $checkout '.git')) {
            $clone = 'existing'
            Write-Stage "Keeping the existing checkout in $checkout"
        } else {
            Write-Stage "Cloning $RepoUrl into $checkout"
            $env:GIT_TERMINAL_PROMPT = '0'
            $env:GCM_INTERACTIVE = 'never'
            $arguments = @('clone', '--quiet')
            if ($RepoRef) { $arguments += @('--branch', $RepoRef) }
            $arguments += @($RepoUrl, $checkout)
            if ((Invoke-Native $git $arguments) -eq 0) {
                $clone = 'cloned'
            } else {
                $clone = 'failed'
                Write-Stage 'Clone failed (a private repository needs a signed-in clone); clone manually'
            }
        }
        $project = $checkout
        if ($Subfolder) { $project = Join-Path $checkout ($Subfolder -replace '/', '\') }
        if ($clone -in @('cloned', 'existing')) {
            if (-not (Test-Path -LiteralPath (Join-Path $project 'requirements.txt'))) {
                throw "requirements.txt not found in $project"
            }
            if ($settings) {
                New-Item -ItemType Directory -Path (Join-Path $project 'config') -Force | Out-Null
                [IO.File]::WriteAllText((Join-Path $project 'config\mvp.toml'), $settings,
                    (New-Object Text.UTF8Encoding($false)))
            }
            Write-Stage "Creating the Python environment in $project\.venv"
            $venv = 'failed'
            $venvPython = Join-Path $project '.venv\Scripts\python.exe'
            if (-not (Test-Path -LiteralPath $venvPython)) {
                if ((Invoke-Native $python @('-m', 'venv', (Join-Path $project '.venv'))) -ne 0) {
                    throw 'Python environment creation failed'
                }
            }
            $requirements = Join-Path $project 'requirements.txt'
            if ((Invoke-Native $venvPython @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '-r',
                    $requirements)) -ne 0) {
                throw 'Python package installation failed'
            }
            $venv = 'ready'
            Write-Stage 'Python environment ready'
        }
    } catch {
        if ($clone -eq 'skipped' -and $RepoUrl) { $clone = 'failed' }
        Write-Stage "Checkout setup failed: $($_.Exception.Message)"
    }
}

Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
Write-Output "DBP_MVP_DEVBOX: tools=$tools clone=$clone venv=$venv"
exit 0
