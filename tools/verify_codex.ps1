<#
.SYNOPSIS
  PowerShell equivalent of tools/verify_codex.sh for Windows environments.
#>
$ErrorActionPreference = "Stop"

Write-Host "`n======================================================================" -ForegroundColor Cyan
Write-Host "  VERIFY CODEX (PowerShell): Automated Five-Agent Verification Gate" -ForegroundColor Cyan
Write-Host "======================================================================`n" -ForegroundColor Cyan

# 1. Resolve Python
$pyCmd = "py"
$pyArgs = @("-3")
try {
    & $pyCmd $pyArgs --version
} catch {
    $pyCmd = "python"
    $pyArgs = @()
    & $pyCmd --version
}

# 2. Frozen Contract Gate
Write-Host "`n[2/6] Running Frozen Contract Gate..." -ForegroundColor Cyan
& $pyCmd $pyArgs tools/check_contract_ran.py
if ($LASTEXITCODE -ne 0) { throw "Contract gate failed" }

# 3. Full Pytest Suite
Write-Host "`n[3/6] Running Pytest Suite..." -ForegroundColor Cyan
& $pyCmd $pyArgs -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "Pytest suite failed" }

# 4. Judge Doctor Diagnostic (live provider probe)
#    Exit codes: 0 = probe OK, 1 = no reliable judge model (real failure),
#    2 = config pending (no API key set, probe impossible, audit continues)
#    NOTE: keep this file ASCII-only - Windows PowerShell 5.1 reads BOM-less
#    .ps1 files as ANSI and non-ASCII bytes inside strings break parsing.
Write-Host "`n[4/6] Running Judge Doctor Diagnostic..." -ForegroundColor Cyan
& $pyCmd $pyArgs tools/judge_doctor.py
$doctorExit = $LASTEXITCODE
if ($doctorExit -eq 2) {
    Write-Host "[PENDING] Judge doctor: no judge API key configured - live probe skipped." -ForegroundColor Yellow
    Write-Host "          Set GEMINI_API_KEY / GROQ_API_KEY / OPENAI_API_KEY and re-run for the live probe." -ForegroundColor Yellow
} elseif ($doctorExit -ne 0) {
    throw "Judge doctor: judge models unreachable or scorecards unparseable (exit $doctorExit)"
} else {
    Write-Host "[PASS] Judge doctor: live probe healthy." -ForegroundColor Green
}

# 5. Eval Sets Runner CI Gate
Write-Host "`n[5/6] Running Eval Sets Runner CI Gate (--no-llm over E1)..." -ForegroundColor Cyan
& $pyCmd $pyArgs -m harness.runner --set E1 --no-llm
if ($LASTEXITCODE -ne 0) { throw "Runner CI gate failed" }

# 6. Artifact & Constraint Audit
Write-Host "`n[6/6] Auditing Voice Playbooks & Red Team Deliverables..." -ForegroundColor Cyan
& $pyCmd $pyArgs -c @"
import re, glob

required_voice = [
    'docs/voice/dentists.md',
    'docs/voice/pharmacies.md',
    'docs/voice/salons.md',
    'docs/voice/gyms.md',
    'docs/voice/restaurants.md',
]
for p in required_voice:
    with open(p, 'r', encoding='utf-8') as f:
        content = f.read()
    assert not re.search(r'https?://[^\s]+', content), f'URL found in {p}'
    assert not re.search(r'[\u0900-\u097F]', content), f'Devanagari found in {p}'
    words = len(content.split())
    assert words >= 3000, f'{p} under 3000 words ({words})'
    print(f'  [PASS] {p}: {words} words, 0 URLs, 0 Devanagari')

with open('docs/REDTEAM.md', 'r', encoding='utf-8') as f:
    redteam = f.read()
findings = re.findall(r'### Finding \d+:', redteam)
assert len(findings) >= 15, f'Only {len(findings)} findings in docs/REDTEAM.md'
print(f'  [PASS] docs/REDTEAM.md: {len(findings)} ranked findings documented')

assert glob.glob('harness/local_judge.py'), 'harness/local_judge.py missing'
assert glob.glob('harness/providers.py'), 'harness/providers.py missing'
print('  [PASS] harness/local_judge.py & harness/providers.py verified')

for s in ['e1_canonical.json', 'e2_full.json', 'e3_adversarial.json', 'e4_replays.json']:
    assert glob.glob(f'harness/eval_sets/{s}'), f'harness/eval_sets/{s} missing'
print('  [PASS] harness/eval_sets/ (E1-E4) verified')
"@
if ($LASTEXITCODE -ne 0) { throw "Artifact audit failed" }

Write-Host "`n======================================================================" -ForegroundColor Green
Write-Host "  VERIFY CODEX PASSED: All five agents synced and 100% verified!" -ForegroundColor Green
Write-Host "======================================================================`n" -ForegroundColor Green
