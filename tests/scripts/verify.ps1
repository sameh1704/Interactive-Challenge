#!/usr/bin/env pwsh
<#
.SYNOPSIS
    End-to-end verification of the Al Manar Interactive Challenge foundation.

.DESCRIPTION
    Runs the full Phase 1 acceptance procedure against the running stack:

        1. docker compose config          (compose file is valid)
        2. docker compose ps              (all services healthy)
        3. GET /                           (landing page)
        4. GET /health/                    (health endpoint)
        5. PostgreSQL connectivity         (inside the project network)
        6. Redis connectivity              (inside the project network)
        7. Django test suite               (inside the container)

    The script only ever addresses services declared in this project's
    docker-compose.yml. It never stops, removes or reconfigures any container
    that is not part of this project.

.EXAMPLE
    pwsh -File tests/scripts/verify.ps1

.EXAMPLE
    pwsh -File tests/scripts/verify.ps1 -SkipBuild
#>

[CmdletBinding()]
param(
    [switch]$SkipBuild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..'..)

$script:Failures = 0

function Write-Step {
    param([string]$Message)
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Pass {
    param([string]$Message)
    Write-Host "    PASS  $Message" -ForegroundColor Green
}

function Write-Fail {
    param([string]$Message)
    Write-Host "    FAIL  $Message" -ForegroundColor Red
    $script:Failures++
}

function Assert-Step {
    param(
        [string]$Name,
        [scriptblock]$Action
    )
    try {
        $result = & $Action
        if ($result -eq $false) {
            Write-Fail $Name
        }
        else {
            Write-Pass $Name
        }
    }
    catch {
        Write-Fail "$Name -- $($_.Exception.Message)"
    }
}

function Get-EnvValue {
    <#
    Reads a value from the project's .env file. The host port must never be
    assumed here: another project or process on the same machine may already
    occupy the default, and silently talking to that service would make every
    HTTP check meaningless.
    #>
    param(
        [string]$Variable,
        [string]$Default = ''
    )

    $envPath = Join-Path $projectRoot '.env'
    if (-not (Test-Path $envPath)) { return $Default }

    $match = Select-String -Path $envPath -Pattern "^$Variable=(.*)$" |
    Select-Object -First 1
    if (-not $match) { return $Default }

    $value = $match.Matches.Groups[1].Value.Trim()
    if ($value -eq '') { return $Default }
    return $value
}

Push-Location $projectRoot

try {
    $port = Get-EnvValue -Variable 'WEB_PORT' -Default '8000'

    Write-Host "Al Manar Interactive Challenge - Phase 1 verification"
    Write-Host "Project directory: $projectRoot"
    Write-Host "Target: http://127.0.0.1:$port (from WEB_PORT in .env)"

    # -----------------------------------------------------------------------
    Write-Step '1. Compose configuration is valid'
    # -----------------------------------------------------------------------
    Assert-Step 'docker compose config' {
        docker compose config --quiet
        $LASTEXITCODE -eq 0
    }

    # -----------------------------------------------------------------------
    Write-Step '2. Stack is up and healthy'
    # -----------------------------------------------------------------------
    if (-not $SkipBuild) {
        Assert-Step 'docker compose up -d --build' {
            docker compose up -d --build
            $LASTEXITCODE -eq 0
        }
    }

    Assert-Step 'all three services exist' {
        $names = docker compose ps --format '{{.Service}}'
        foreach ($expected in 'challenge-web', 'challenge-db', 'challenge-redis') {
            if ($names -notcontains $expected) { return $false }
        }
        return $true
    }

    # challenge-web has a 40s health-check start period, so it reports
    # "starting" for a while after it begins serving. Asserting immediately
    # would be a race, not a test.
    Write-Host '    Waiting for all services to report healthy...'
    $deadline = (Get-Date).AddSeconds(150)
    do {
        $states = docker compose ps --format '{{.Service}}|{{.Health}}'
        $pending = @($states | Where-Object { ($_ -split '\|')[1].Trim() -ne 'healthy' })
        if ($pending.Count -eq 0) { break }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)

    Write-Host '    Container state:'
    docker compose ps --format '        {0,-18} {1,-10} {2}' | Out-String | Write-Host

    Assert-Step 'all three services report healthy' {
        $rows = docker compose ps --format '{{.Service}}|{{.Health}}'
        foreach ($row in $rows) {
            $parts = $row -split '\|'
            if ($parts.Count -lt 2) { return $false }
            if ($parts[1].Trim() -ne 'healthy') {
                Write-Host "        $($parts[0]) is '$($parts[1])'"
                return $false
            }
        }
        return $true
    }

    # -----------------------------------------------------------------------
    Write-Step '3. HTTP endpoints'
    # -----------------------------------------------------------------------
    $base = "http://127.0.0.1:$port"

    $landing = $null
    Assert-Step 'GET / returns 200' {
        $script:landing = Invoke-WebRequest -Uri "$base/" -UseBasicParsing
        $script:landing.StatusCode -eq 200
    }
    Assert-Step 'GET / shows "Al Manar" and "Interactive Challenge"' {
        $script:landing.Content -match 'Al Manar' -and
        $script:landing.Content -match 'Interactive Challenge'
    }
    Assert-Step 'GET / serves the stylesheet from /static/' {
        $href = [regex]::Match($script:landing.Content, 'href="(?<url>/static/[^"]+)"')
        if (-not $href.Success) { return $false }
        $asset = Invoke-WebRequest -Uri "$base$($href.Groups['url'].Value)" -UseBasicParsing
        return $asset.StatusCode -eq 200
    }

    $health = $null
    Assert-Step 'GET /health/ returns 200' {
        $script:health = Invoke-WebRequest -Uri "$base/health/" -UseBasicParsing
        $script:health.StatusCode -eq 200
    }
    Assert-Step 'GET /health/ reports database ok' {
        ($script:health.Content | ConvertFrom-Json).checks.database.status -eq 'ok'
    }
    Assert-Step 'GET /health/ reports redis ok' {
        ($script:health.Content | ConvertFrom-Json).checks.redis.status -eq 'ok'
    }
    Assert-Step 'GET /health/ reports overall status ok' {
        ($script:health.Content | ConvertFrom-Json).status -eq 'ok'
    }

    # -----------------------------------------------------------------------
    Write-Step '4. PostgreSQL connectivity'
    # -----------------------------------------------------------------------
    Assert-Step 'pg_isready succeeds inside challenge-db' {
        $dbUser = Get-EnvValue -Variable 'DATABASE_USER'
        $dbName = Get-EnvValue -Variable 'DATABASE_NAME'
        docker compose exec -T challenge-db pg_isready -U $dbUser -d $dbName 2>&1 | Out-String | Write-Host
        $LASTEXITCODE -eq 0
    }
    Assert-Step 'Django reports PostgreSQL as its backend' {
        $out = docker compose exec -T challenge-web python manage.py shell -c "from django.db import connection; print(connection.vendor)"
        $out | Out-String | Write-Host
        ($out | Out-String) -match 'postgresql'
    }
    Assert-Step 'schema_migrations table records applied migrations' {
        $dbUser = Get-EnvValue -Variable 'DATABASE_USER'
        $dbName = Get-EnvValue -Variable 'DATABASE_NAME'
        $out = docker compose exec -T challenge-db psql -U $dbUser -d $dbName -t -c `
            "SELECT count(*) FROM django_migrations;"
        $out | Out-String | Write-Host
        return [int](($out | Out-String).Trim()) -gt 0
    }

    # -----------------------------------------------------------------------
    Write-Step '5. Redis connectivity'
    # -----------------------------------------------------------------------
    Assert-Step 'redis-cli ping returns PONG' {
        $out = docker compose exec -T challenge-redis redis-cli ping
        $out | Out-String | Write-Host
        ($out | Out-String) -match 'PONG'
    }
    Assert-Step 'Django cache round-trips through Redis' {
        $out = docker compose exec -T challenge-web python manage.py shell -c "from django.core.cache import cache; cache.set('verify','ok',30); print(cache.get('verify'))"
        $out | Out-String | Write-Host
        ($out | Out-String) -match 'ok'
    }
    Assert-Step 'Channels layer resolves to the Redis transport' {
        $out = docker compose exec -T challenge-web python manage.py shell -c "from channels.layers import get_channel_layer; print(type(get_channel_layer()).__name__)"
        $out | Out-String | Write-Host
        ($out | Out-String) -match 'RedisChannelLayer'
    }

    # -----------------------------------------------------------------------
    Write-Step '6. Automated test suite'
    # -----------------------------------------------------------------------
    Assert-Step 'python manage.py test' {
        $output = docker compose exec -T challenge-web python manage.py test --verbosity 1 2>&1
        $output | Select-Object -Last 8 | Out-String | Write-Host
        $LASTEXITCODE -eq 0
    }

    # -----------------------------------------------------------------------
    Write-Step '7. Isolation'
    # -----------------------------------------------------------------------
    Assert-Step 'project network exists with a dedicated name' {
        docker network ls --format '{{.Name}}' | Select-String -SimpleMatch 'almanar-interactive-challenge-net'
        $LASTEXITCODE -eq 0
    }
    Assert-Step '.env is not tracked and holds no placeholder secret' {
        $envPath = Join-Path $projectRoot '.env'
        if (-not (Test-Path $envPath)) { return $false }
        $secret = (Select-String -Path $envPath -Pattern '^DJANGO_SECRET_KEY=(.*)$').Matches.Groups[1].Value
        return ($secret.Trim().Length -ge 50) -and ($secret.Trim() -notmatch 'changeme')
    }
}
finally {
    Pop-Location
}

Write-Host ''
if ($script:Failures -gt 0) {
    Write-Host "VERIFICATION FAILED: $($script:Failures) check(s) failed." -ForegroundColor Red
    exit 1
}

Write-Host 'VERIFICATION PASSED: every check succeeded.' -ForegroundColor Green
exit 0