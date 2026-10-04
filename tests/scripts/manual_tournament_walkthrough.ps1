#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Phase 7 manual walkthrough: run a championship through the real pages.

.DESCRIPTION
    Performs the twelve checks from the Phase 7 manual test procedure against the
    running stack over HTTP, using a real sign-in session and the CSRF tokens from
    the real forms. Nothing here touches the test client or the test database, so
    what passes has passed through Daphne, Django's middleware, PostgreSQL and Redis
    exactly as a teacher's browser would.

    Each page's HTML is inspected rather than merely fetched for a status code: a
    page that renders 200 with an empty table would otherwise pass. Assertions name
    distinctive text that can only appear if the intended state was reached.

    Requires the seed in tests/scripts/seed_phase7_demo.py to have been run.

.EXAMPLE
    pwsh -File tests/scripts/manual_tournament_walkthrough.ps1
#>

[CmdletBinding()]
param(
    [int]$Port = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..\..')
Push-Location $projectRoot

$script:Failures = 0
$script:Step = 0
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession

function Say {
    param([string]$Message, [string]$Colour = 'Cyan')
    Write-Host "    $Message" -ForegroundColor $Colour
}

function Step {
    param([string]$Message)
    $script:Step++
    Write-Host ''
    Write-Host "==> $script:Step. $Message" -ForegroundColor Cyan
}

function Ok {
    param([string]$Message)
    Say "PASS  $Message" 'Green'
}

function Bad {
    param([string]$Message)
    Say "FAIL  $Message" 'Red'
    $script:Failures++
}

# Asserts a condition derived from a read-only database dump.
function Check {
    param([bool]$Condition, [string]$Message)
    if ($Condition) { Ok $Message }
    else { Bad $Message }
}

function Get-Base {
    if ($Port -gt 0) { return "http://127.0.0.1:$Port" }
    $match = Select-String -Path (Join-Path $projectRoot '.env') -Pattern '^WEB_PORT=(\d+)$' |
        Select-Object -First 1
    if (-not $match) { return 'http://127.0.0.1:8000' }
    return "http://127.0.0.1:$($match.Matches.Groups[1].Value)"
}

function Get-Page {
    param([string]$Path)
    return Invoke-WebRequest -Uri "$script:Base$Path" -WebSession $session -UseBasicParsing
}

function Get-Csrf {
    param([string]$Html)
    $m = [regex]::Match($Html, 'name="csrfmiddlewaretoken" value="([^"]+)"')
    if (-not $m.Success) { throw 'no CSRF token in the page' }
    return $m.Groups[1].Value
}

# Posts a form the way a browser does: the real CSRF token plus the fields.
function Post-Form {
    param([string]$GetPath, [string]$PostPath, [hashtable]$Fields)
    $token = Get-Csrf (Get-Page $GetPath).Content
    $body = @{ 'csrfmiddlewaretoken' = $token }
    foreach ($key in $Fields.Keys) { $body[$key] = $Fields[$key] }
    return Invoke-WebRequest -Uri "$script:Base$PostPath" -Method POST -Body $body `
        -WebSession $session -UseBasicParsing -MaximumRedirection 5
}

# Returns every option in the <select> with the given name, as id/name pairs.
function Get-Options {
    param([string]$Html, [string]$SelectName)
    $select = [regex]::Match($Html, "<select[^>]*name=`"$SelectName`"[^>]*>([\s\S]*?)</select>")
    if (-not $select.Success) { return @() }
    $found = @()
    foreach ($m in [regex]::Matches($select.Groups[1].Value, '<option value="(\d+)">([^<]+)</option>')) {
        $found += [pscustomobject]@{ Id = $m.Groups[1].Value; Name = $m.Groups[2].Value.Trim() }
    }
    return $found
}

# Counts occurrences of a distinctive marker, so "two advanced" is really two.
function Count-Marker {
    param([string]$Html, [string]$Marker)
    return ([regex]::Matches($Html, [regex]::Escape($Marker))).Count
}

function Assert-Contains {
    param([string]$Haystack, [string]$Needle, [string]$Message)
    if ($Haystack -match [regex]::Escape($Needle)) { Ok $Message }
    else { Bad "$Message -- '$Needle' was not in the page" }
}

function Assert-NotContains {
    param([string]$Haystack, [string]$Needle, [string]$Message)
    if ($Haystack -match [regex]::Escape($Needle)) { Bad "$Message -- '$Needle' was in the page" }
    else { Ok $Message }
}

# Reads the state dump from tests/scripts/phase7_state.py inside the web container
# and returns it as an object. Read-only: the script serialises what the database
# already holds and writes nothing. Every *action* in this walkthrough still goes
# over HTTP with a real session and real CSRF tokens; this only checks what those
# actions left behind.
function Get-State {
    $helper = Join-Path $PSScriptRoot 'phase7_state.py'
    $raw = (Get-Content -Raw $helper |
        docker compose exec -T challenge-web python manage.py shell 2>&1 | Out-String)
    $match = [regex]::Match($raw, '(?m)^STATE_JSON:(?<json>\{.*\})')
    if (-not $match.Success) {
        throw "the read-only state dump did not report STATE_JSON:`n$raw"
    }
    return ($match.Groups['json'].Value | ConvertFrom-Json)
}

try {
    $script:Base = Get-Base
    Write-Host 'Al Manar Interactive Challenge - Phase 7 manual walkthrough'
    Write-Host "Target: $($script:Base)"

    # -----------------------------------------------------------------------
    Step 'Sign in as an administrator'
    # -----------------------------------------------------------------------
    $password = if ($env:SEED_PASSWORD) { $env:SEED_PASSWORD } else { 'manual-test-password-not-real' }
    $login = Get-Page '/accounts/login/'
    $dashboard = Invoke-WebRequest -Uri "$($script:Base)/accounts/login/" -Method POST `
        -Body @{
            'csrfmiddlewaretoken' = (Get-Csrf $login.Content)
            'username'             = 'champ.admin'
            'password'             = $password
            'next'                 = '/dashboard/'
        } -WebSession $session -UseBasicParsing -MaximumRedirection 5

    if ($dashboard.Content -match 'Dashboard') { Ok 'signed in and reached the dashboard' }
    else { Bad "sign-in failed (status $($dashboard.StatusCode))"; throw 'cannot continue without a session' }

    Assert-Contains $dashboard.Content '/tournaments/' 'the site navigation links to tournaments'
    Assert-Contains $dashboard.Content '/reports/' 'the site navigation links to reports'

    # -----------------------------------------------------------------------
    Step 'Create a tournament'
    # -----------------------------------------------------------------------
    $create = Get-Page '/tournaments/new/'
    $created = Invoke-WebRequest -Uri "$($script:Base)/tournaments/new/" -Method POST `
        -Body @{
            'csrfmiddlewaretoken' = (Get-Csrf $create.Content)
            'name'                = 'Manual Primary Championship'
            'subject'             = 'Science'
            'grade'               = 'Grade 5'
            'season'              = '2026'
            'description'         = 'Created by the Phase 7 manual walkthrough.'
        } -WebSession $session -UseBasicParsing -MaximumRedirection 5

    Assert-Contains $created.Content 'Manual Primary Championship' 'the tournament was created'
    Assert-Contains $created.Content 'status-draft' 'a new tournament is a draft'
    Assert-Contains $created.Content 'name="action" value="open"' 'a draft offers to be opened'
    Assert-Contains $created.Content 'Add a stage' 'a draft can be built up'

    $list = Get-Page '/tournaments/'
    $link = [regex]::Match($list.Content, 'href="(/tournaments/\d+/)"[^>]*>\s*Manual Primary Championship')
    if (-not $link.Success) { $link = [regex]::Match($list.Content, '/tournaments/(\d+)/') }
    if (-not $link.Success) { Bad 'the new tournament was not listed'; throw 'cannot continue' }

    $detailPath = $link.Groups[1].Value
    $tournamentId = [regex]::Match($detailPath, '\d+').Value
    $actions = "/tournaments/$tournamentId/actions/"
    Ok "the championship is at $detailPath"

    # -----------------------------------------------------------------------
    Step 'Add classrooms'
    # -----------------------------------------------------------------------
    # Only the four laboratories that played the seeded qualification round take
    # part. The development database also holds classrooms from earlier runs, and
    # the outcome of the championship depends on who entered, so the walkthrough
    # picks out the four it wants rather than taking whatever the dropdown offers.
    # Which four those are comes from the round's own frozen standings, read-only.
    $seeded = Get-State
    $expectedLabs = @($seeded.seeded_classrooms | ForEach-Object { $_.name })
    if ($expectedLabs.Count -eq 4) { Ok 'the seeded round names four laboratories' }
    else { Bad "expected four seeded laboratories, found $($expectedLabs.Count)" }

    $page = Get-Page $detailPath
    $offered = Get-Options $page.Content 'classroom'
    $rooms = @()
    foreach ($lab in $seeded.seeded_classrooms) {
        $option = $offered | Where-Object { $_.Id -eq "$($lab.id)" } | Select-Object -First 1
        if ($option -and $option.Name -eq $lab.label) { $rooms += $option }
        else { Bad "the classroom select does not offer '$($lab.label)'" }
    }
    if ($rooms.Count -eq 4) { Ok 'the form offers all four seeded laboratories' }
    else { Bad "expected the four seeded laboratories, the form offered $($rooms.Count)" }

    foreach ($room in $rooms) {
        Post-Form -GetPath $detailPath -PostPath $actions `
            -Fields @{ 'action' = 'add_participant'; 'classroom' = $room.Id } | Out-Null
    }

    $page = Get-Page $detailPath
    $listed = 0
    foreach ($room in $rooms) {
        if ($page.Content -match [regex]::Escape("<td>$($room.Name)</td>")) { $listed++ }
    }
    if ($listed -eq $rooms.Count) { Ok "all $($rooms.Count) classrooms are participants" }
    else { Bad "only $listed of $($rooms.Count) classrooms appear in the participants table" }

    # A duplicate is refused with a reason, not silently ignored.
    $dup = Post-Form -GetPath $detailPath -PostPath $actions `
        -Fields @{ 'action' = 'add_participant'; 'classroom' = $rooms[0].Id }
    Assert-Contains $dup.Content 'already taking part' 'adding the same classroom twice is refused'
    Assert-Contains $dup.Content 'already taking part' 'the refusal explains itself'

    # -----------------------------------------------------------------------
    Step 'Open the tournament'
    # -----------------------------------------------------------------------
    $opened = Post-Form -GetPath $detailPath -PostPath $actions -Fields @{ 'action' = 'open' }
    Assert-Contains $opened.Content 'status-open' 'the tournament is now open'
    Assert-Contains $opened.Content 'Tournament opened.' 'the confirmation names what happened'

    # -----------------------------------------------------------------------
    Step 'Create the qualification stage'
    # -----------------------------------------------------------------------
    $stage = Post-Form -GetPath $detailPath -PostPath $actions -Fields @{
        'action'          = 'create_stage'
        'stage_type'      = 'qualification'
        'advancing_count' = '2'
        'name'            = 'Qualification'
    }
    Assert-Contains $stage.Content 'Top 2 advance' 'the cut-off is shown as configurable (top 2)'
    Assert-Contains $stage.Content 'status-running' 'an attached-round stage shows as running'

    # -----------------------------------------------------------------------
    Step 'Assign an existing competition'
    # -----------------------------------------------------------------------
    $page = Get-Page $detailPath
    $stageId = [regex]::Match($page.Content, 'name="stage" value="(\d+)"').Groups[1].Value
    $rounds = Get-Options $page.Content 'competition'
    if ($rounds.Count -eq 0) { Bad 'no finished competition was offered to assign' }
    else {
        Ok "the form offers $($rounds.Count) finished competitions"
        $qualifier = $rounds | Where-Object { $_.Name -eq 'Demo qualification round' } | Select-Object -First 1
        if (-not $qualifier) { $qualifier = $rounds[0] }

        $assigned = Post-Form -GetPath $detailPath -PostPath $actions -Fields @{
            'action' = 'attach_competition'; 'stage' = $stageId; 'competition' = $qualifier.Id
        }
        Assert-Contains $assigned.Content "Assigned &quot;$($qualifier.Name)&quot;" 'the assignment is confirmed by name'

        # The same round may not be counted in two stages.
        $again = Post-Form -GetPath $detailPath -PostPath $actions -Fields @{
            'action' = 'create_stage'; 'stage_type' = 'semi_final'; 'advancing_count' = '1'
        }
        $reused = Post-Form -GetPath $detailPath -PostPath $actions -Fields @{
            'action' = 'attach_competition'; 'stage' = ([regex]::Match($again.Content, 'name="stage" value="(\d+)"').Groups[1].Value)
            'competition' = $qualifier.Id
        }
        Assert-Contains $reused.Content 'already belongs to a tournament stage' 'a round cannot be reused in another stage'
    }

    # -----------------------------------------------------------------------
    Step 'Complete the stage'
    # -----------------------------------------------------------------------
    $completed = Post-Form -GetPath $detailPath -PostPath $actions `
        -Fields @{ 'action' = 'complete_stage'; 'stage' = $stageId }
    Assert-Contains $completed.Content 'status-completed' 'the stage shows as completed'
    Assert-Contains $completed.Content 'Process advancement' 'a completed stage offers advancement'

    # -----------------------------------------------------------------------
    Step 'Process advancement'
    # -----------------------------------------------------------------------
    $advanced = Post-Form -GetPath $detailPath -PostPath $actions `
        -Fields @{ 'action' = 'process_advancement'; 'stage' = $stageId }

    if ($advanced.Content -match 'tie was found') {
        Ok 'a tie was reported instead of being guessed at'
    }
    else {
        Assert-Contains $advanced.Content 'Advanced:' 'advancement was processed'
    }

    $advancedCount = Count-Marker $advanced.Content '>Advanced<'
    if ($advancedCount -eq 2) { Ok 'exactly two classrooms advanced, as configured' }
    else { Bad "expected 2 advanced, the page shows $advancedCount" }

    $eliminatedCount = Count-Marker $advanced.Content '>Eliminated<'
    if ($eliminatedCount -eq 2) { Ok 'the other two were eliminated' }
    else { Bad "expected 2 eliminated, the page shows $eliminatedCount" }

    $qualifiedCount = Count-Marker $advanced.Content 'status-qualified'
    if ($qualifiedCount -ge 2) { Ok 'the participants table shows the qualified status' }
    else { Bad "expected at least 2 qualified statuses, found $qualifiedCount" }

    # Advancement must not be repeatable.
    $page = Get-Page $detailPath
    Assert-NotContains $page.Content 'name="action" value="process_advancement"' 'the button is withdrawn once processed'

    # -----------------------------------------------------------------------
    Step 'Create and run the next stage with the existing competition engine'
    # -----------------------------------------------------------------------
    $final = Post-Form -GetPath $detailPath -PostPath $actions -Fields @{
        'action' = 'create_stage'; 'stage_type' = 'final'; 'advancing_count' = ''; 'name' = 'Final'
    }
    Assert-Contains $final.Content 'All advance' 'a final with no cut-off advances everyone who reached it'

    $page = Get-Page $detailPath
    $finalStageId = ([regex]::Matches($page.Content, 'name="stage" value="(\d+)"') |
        Select-Object -Last 1).Groups[1].Value
    $finalRound = Get-Options $page.Content 'competition' |
        Where-Object { $_.Name -eq 'Demo final round' } | Select-Object -First 1
    if ($finalRound) {
        $linked = Post-Form -GetPath $detailPath -PostPath $actions -Fields @{
            'action' = 'attach_competition'; 'stage' = $finalStageId; 'competition' = $finalRound.Id
        }
        Assert-Contains $linked.Content "Assigned &quot;$($finalRound.Name)&quot;" 'the final round was attached'

        $finalStageId = ([regex]::Matches((Get-Page $detailPath).Content, 'name="stage" value="(\d+)"') |
            Select-Object -Last 1).Groups[1].Value
    }

    Post-Form -GetPath $detailPath -PostPath $actions `
        -Fields @{ 'action' = 'complete_stage'; 'stage' = $finalStageId } | Out-Null
    $finalAdvanced = Post-Form -GetPath $detailPath -PostPath $actions `
        -Fields @{ 'action' = 'process_advancement'; 'stage' = $finalStageId }
    Assert-Contains $finalAdvanced.Content 'Advanced:' 'the final stage advanced its finalists'

    # -----------------------------------------------------------------------
    Step 'Complete the tournament'
    # -----------------------------------------------------------------------
    $finalized = Post-Form -GetPath $detailPath -PostPath $actions -Fields @{ 'action' = 'finalize' }
    Assert-Contains $finalized.Content 'status-completed' 'the tournament shows as completed'
    Assert-Contains $finalized.Content 'Final result' 'the final result is shown'

    $winner = [regex]::Match($finalized.Content, 'Champion:\s*<strong>([^<]+)</strong>')
    if ($winner.Success) { Ok "champion: $($winner.Groups[1].Value.Trim())" }
    else { Bad 'no champion was named' }

    # A concluded championship must stop accepting changes.
    $closed = Post-Form -GetPath $detailPath -PostPath $actions -Fields @{ 'action' = 'add_participant'; 'classroom' = $rooms[0].Id }
    Assert-Contains $closed.Content 'closed' 'a completed tournament refuses new participants'

    # -----------------------------------------------------------------------
    Step 'Verify the persisted state (read-only)'
    # -----------------------------------------------------------------------
    # Every action above went over HTTP, through Daphne, Django and PostgreSQL.
    # This step changes nothing: it only reads back the rows those actions wrote,
    # so what is asserted here is what the database holds rather than what the
    # pages claimed.
    $state = Get-State
    if (-not $state.tournament) {
        Bad 'the read-only state dump reported no tournament'
    }
    else {
        Check ($state.tournament.id -eq [int]$tournamentId) `
            "the dump describes tournament $tournamentId (reported $($state.tournament.id))"
        Check ($state.tournament.name -eq 'Manual Primary Championship') `
            'the dump names the championship the walkthrough created'

        # 1. Exactly four participants, and they are the four seeded laboratories.
        Check ($state.participants.Count -eq 4) `
            "exactly 4 participants are persisted (found $($state.participants.Count))"

        # 2. The right classrooms, with the statuses the run should have left.
        $persistedNames = @($state.participants | ForEach-Object { $_.classroom }) | Sort-Object
        $wantedNames = @($expectedLabs) | Sort-Object
        Check (($persistedNames -join ', ') -eq ($wantedNames -join ', ')) `
            "the persisted classrooms are the four seeded labs ($($persistedNames -join ', '))"

        # The record and the standings name classrooms as the UI labels them, so
        # map each seeded classroom to the label the screens show.
        $labelOf = @{}
        foreach ($lab in $seeded.seeded_classrooms) { $labelOf[$lab.name] = $lab.label }

        $statuses = @{}
        foreach ($participant in $state.participants) { $statuses[$participant.classroom] = $participant.status }
        Check ($statuses['Science Lab A'] -eq 'champion') `
            "Science Lab A is the champion (status: $($statuses['Science Lab A']))"
        # The runner-up is not eliminated: a final advances everyone who reached it,
        # and a finalist that was never knocked out stays qualified so it still
        # ranks in the final table. Only the winner is promoted to champion.
        Check ($statuses['Science Lab B'] -eq 'qualified') `
            "the runner-up is still a qualified finalist, not eliminated (status: $($statuses['Science Lab B']))"
        Check ($statuses['Science Lab C'] -eq 'eliminated') `
            "Science Lab C was eliminated (status: $($statuses['Science Lab C']))"
        Check ($statuses['Science Lab D'] -eq 'eliminated') `
            "Science Lab D was eliminated (status: $($statuses['Science Lab D']))"

        # 3./4. The qualification round is attached to the qualification stage
        #      and to no other stage anywhere.
        Check ($state.qualification_stage.competitions.Count -eq 1 -and
            $state.qualification_stage.competitions[0] -eq 'Demo qualification round') `
            "the qualification stage runs 'Demo qualification round' alone (found: $($state.qualification_stage.competitions -join ', '))"
        Check (-not $state.wrong_stage_attached) `
            'the qualification round is not attached to any other stage'

        # 5. The qualification stage really completed.
        Check ($state.qualification_stage.status -eq 'completed') `
            "the qualification stage is persisted as completed (status: $($state.qualification_stage.status))"
        Check ($state.qualification_advancement.status -eq 'processed') `
            "its advancement is persisted as processed (status: $($state.qualification_advancement.status))"

        # The second stage ran through the same engine and froze likewise.
        Check ($state.final_stage.status -eq 'completed') `
            "the final stage is persisted as completed (status: $($state.final_stage.status))"
        Check ($state.final_stage.competitions.Count -eq 1 -and
               $state.final_stage.competitions[0] -eq 'Demo final round') `
            "the final stage runs 'Demo final round' alone (found: $($state.final_stage.competitions -join ', '))"
        Check ($state.final_stage.advancing_count -eq $null) `
            "the final stage kept its no-cut-off rule (found: $($state.final_stage.advancing_count))"

        # 6. Advancement advanced and eliminated exactly the expected classrooms.
        Check ($state.qualified.Count -eq 2) `
            "2 participants qualified (found $($state.qualified.Count): $($state.qualified -join ', '))"
        Check (($state.qualified -join ', ') -eq 'Science Lab A, Science Lab B') `
            "the qualified pair is Science Lab A and Science Lab B (found: $($state.qualified -join ', '))"
        Check ($state.eliminated.Count -eq 2) `
            "2 participants were eliminated (found $($state.eliminated.Count): $($state.eliminated -join ', '))"
        Check (($state.eliminated -join ', ') -eq 'Science Lab C, Science Lab D') `
            "the eliminated pair is Science Lab C and Science Lab D (found: $($state.eliminated -join ', '))"
        Check (($state.qualification_advancement.advanced -join ', ') -eq 'Science Lab A, Science Lab B') `
            "the qualification advancement itself advanced those two (found: $($state.qualification_advancement.advanced -join ', '))"
        Check (($state.qualification_advancement.eliminated -join ', ') -eq 'Science Lab C, Science Lab D') `
            "the qualification advancement itself eliminated the other two (found: $($state.qualification_advancement.eliminated -join ', '))"

        # 7. Finalisation persisted a completed tournament and a matching record.
        Check ($state.tournament.status -eq 'completed') `
            "the tournament is persisted as completed (status: $($state.tournament.status))"
        Check ($state.record.winner_name -eq $labelOf['Science Lab A']) `
            "the record names Science Lab A as winner (found '$($state.record.winner_name)')"
        Check ($state.record.participants_count -eq 4) `
            "the record counts 4 participants (found $($state.record.participants_count))"
        Check ($state.record.stages_completed -eq 2) `
            "the record counts 2 completed stages (found $($state.record.stages_completed))"

        # The frozen table ranks the finalists, not everyone who entered: the two
        # knocked out in qualification were never in the final, so their absence
        # is the correct outcome rather than a missing row.
        $table = @($state.record.standings)
        Check ($table.Count -eq 2) `
            "the frozen final table ranks the 2 finalists (found $($table.Count) row(s))"
        Check ($table[0].rank -eq 1 -and $table[0].classroom -eq $labelOf['Science Lab A'] -and
               $table[0].status -eq 'champion') `
            "rank 1 is Science Lab A, as champion (found rank $($table[0].rank) $($table[0].classroom) / $($table[0].status))"
        Check ($table[1].rank -eq 2 -and $table[1].classroom -eq $labelOf['Science Lab B']) `
            "rank 2 is Science Lab B (found rank $($table[1].rank) $($table[1].classroom))"
        Check (@($table | Where-Object { $_.classroom -in @($labelOf['Science Lab C'], $labelOf['Science Lab D']) }).Count -eq 0) `
            'neither qualification-stage elimination appears in the final table'
    }

    # -----------------------------------------------------------------------
    Step 'View tournament history'
    # -----------------------------------------------------------------------
    $page = Get-Page $detailPath
    Assert-Contains $page.Content 'History' 'the history panel is present'
    Assert-Contains $page.Content 'Qualification' 'history lists the qualification stage'
    Assert-Contains $page.Content 'Final' 'history lists the final stage'
    Assert-Contains $page.Content 'Tournament report' 'the report is linked from the tournament'

    # -----------------------------------------------------------------------
    Step 'View the Hall of Fame'
    # -----------------------------------------------------------------------
    $hall = Get-Page '/tournaments/hall-of-fame/'
    Assert-Contains $hall.Content 'Manual Primary Championship' 'the completed tournament is listed'
    Assert-Contains $hall.Content '2026' 'the season is shown'
    Assert-NotContains $hall.Content 'name="action"' 'the Hall of Fame offers no action form'
    Assert-NotContains $hall.Content 'name="classroom_ids"' 'the Hall of Fame cannot resolve anything'

    # -----------------------------------------------------------------------
    Step 'Export a CSV report'
    # -----------------------------------------------------------------------
    $csv = Invoke-WebRequest -Uri "$($script:Base)/reports/tournaments/$tournamentId/export.csv" `
        -WebSession $session -UseBasicParsing
    if ($csv.StatusCode -eq 200) { Ok 'the tournament CSV downloaded' }
    else { Bad "the tournament CSV returned $($csv.StatusCode)" }

    if ($csv.Headers['Content-Type'] -match 'text/csv') { Ok 'it is served as text/csv' }
    else { Bad "unexpected content type: $($csv.Headers['Content-Type'])" }

    if ($csv.Headers['Content-Disposition'] -match 'attachment') { Ok 'it is a download, not a page' }
    else { Bad 'it is not offered as a download' }

    Assert-Contains $csv.Content 'Manual Primary Championship' 'the CSV names the tournament'
    Assert-Contains $csv.Content 'Science Lab' 'the CSV lists the classrooms'

    $classroomId = $rooms[0].Id
    $classroomCsv = Invoke-WebRequest -Uri "$($script:Base)/reports/classrooms/$classroomId/export.csv" `
        -WebSession $session -UseBasicParsing
    Assert-Contains $classroomCsv.Content 'Competitions played' 'the classroom CSV has its header row'

    # -----------------------------------------------------------------------
    Step 'A screen client cannot administer a tournament'
    # -----------------------------------------------------------------------
    $anon = New-Object Microsoft.PowerShell.Commands.WebRequestSession
    foreach ($path in @('/tournaments/', '/tournaments/hall-of-fame/', '/reports/',
                        "/tournaments/$tournamentId/", "/reports/tournaments/$tournamentId/")) {
        try {
            $r = Invoke-WebRequest -Uri "$($script:Base)$path" -WebSession $anon `
                -UseBasicParsing -MaximumRedirection 0 -ErrorAction Stop
            if ($r.StatusCode -eq 200) { Bad "$path was readable without signing in" }
            else { Ok "$path refused without a session ($($r.StatusCode))" }
        }
        catch {
            $code = [int]$_.Exception.Response.StatusCode
            if ($code -in 302, 403, 404) { Ok "$path refused without a session ($code)" }
            else { Bad "$path returned an unexpected $code" }
        }
    }

    # A CSRF-less POST must not work either.
    try {
        $r = Invoke-WebRequest -Uri "$($script:Base)$actions" -Method POST `
            -Body @{ 'action' = 'finalize' } -WebSession $anon -UseBasicParsing -ErrorAction Stop
        if ($r.StatusCode -eq 200) { Bad 'a POST without a CSRF token was accepted' }
        else { Ok "a POST without a CSRF token was refused ($($r.StatusCode))" }
    }
    catch {
        $code = [int]$_.Exception.Response.StatusCode
        if ($code -in 403, 404) { Ok "a POST without a CSRF token was refused ($code)" }
        else { Bad "a CSRF-less POST returned an unexpected $code" }
    }
}
finally {
    Pop-Location
}

Write-Host ''
if ($script:Failures -gt 0) {
    Write-Host "WALKTHROUGH FAILED: $($script:Failures) check(s) failed." -ForegroundColor Red
    exit 1
}

Write-Host 'WALKTHROUGH PASSED: every check succeeded against the running stack.' -ForegroundColor Green
exit 0
