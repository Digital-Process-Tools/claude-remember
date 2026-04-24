#!/bin/bash
# ============================================================================
# run-tests.sh — Full pipeline verification: Python tests + shell + integration
# ============================================================================
#
# DESCRIPTION
#   Comprehensive test runner for the memory pipeline. Validates shell syntax,
#   Python imports, unit tests, shell bridge commands, prompt templates, and
#   end-to-end dry runs. Optionally includes a live Haiku API call.
#
# USAGE
#   run-tests.sh             # all tests except live Haiku
#   run-tests.sh --live      # include tests that call real Haiku (costs tokens)
#
# ARGUMENTS
#   --live    Include test 8 (real Haiku API call). Costs tokens. Skipped by default.
#
# DEPENDENCIES
#   python3, pytest, bash, jq
#   Fixtures: tests/fixtures/sample-session.jsonl, tests/fixtures/team/
#   Scripts: save-session.sh (for dry-run test)
#   Python: pipeline.shell, pipeline.haiku (for live test)
#
# EXIT CODES
#   0   All tests passed
#   1   One or more tests failed, or python3 not found
#
# TEST SECTIONS
#   1. Prerequisites     — python3 and pytest availability
#   2. Shell syntax      — bash -n on all pipeline scripts
#   3. Python unit tests — pytest on tests/ directory
#   4. Module imports    — import every pipeline.* module
#   5. Shell bridge      — extract, parse-haiku, save-position, build-prompt,
#                          build-ndc-prompt, team-digest via pipeline.shell
#   6. Prompt templates  — verify files exist and contain expected placeholders
#   7. Dry-run save      — save-session.sh --dry (full flow, no Haiku)
#   8. Live Haiku        — [--live only] real API call + response validation
#
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIPELINE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
FIXTURES="$PIPELINE_DIR/tests/fixtures"

LIVE=false
[ "$1" = "--live" ] && LIVE=true

PASS=0
FAIL=0
SKIP=0

# Record a passing test. Args: $1 — test description.
pass() { echo "  ✓ $1"; PASS=$((PASS + 1)); }
# Record a failing test. Args: $1 — test description, $2 — failure detail.
fail() { echo "  ✗ $1: $2"; FAIL=$((FAIL + 1)); }
# Record a skipped test (requires --live). Args: $1 — test description.
skip() { echo "  ○ $1 (skipped — use --live)"; SKIP=$((SKIP + 1)); }

cleanup_files=()
# Remove all accumulated temp files on exit.
cleanup() { rm -f "${cleanup_files[@]}"; }
trap cleanup EXIT

echo "=== Memory Pipeline Tests ==="
echo ""

# ── 1. Prerequisites ──────────────────────────────────────────────────────
echo "1. Prerequisites"

if command -v python3 >/dev/null 2>&1; then
    pass "python3 found ($(python3 --version 2>&1 | cut -d' ' -f2))"
else
    fail "python3" "not found"; echo "FATAL: cannot continue without python3"; exit 1
fi

if python3 -m pytest --version >/dev/null 2>&1; then
    pass "pytest found"
else
    fail "pytest" "not installed (pip3 install pytest)"
fi

echo ""

# ── 2. Shell syntax ───────────────────────────────────────────────────────
echo "2. Shell syntax"

for script in save-session.sh run-consolidation.sh log.sh; do
    if bash -n "$SCRIPT_DIR/$script" 2>/dev/null; then
        pass "$script"
    else
        fail "$script" "syntax error"
    fi
done

echo ""

# ── 3. Python unit tests ─────────────────────────────────────────────────
echo "3. Python unit tests"

cd "$PIPELINE_DIR"
PYTEST_OUTPUT=$(python3 -m pytest tests/ -q --tb=short 2>&1)
PYTEST_EXIT=$?

if [ "$PYTEST_EXIT" -eq 0 ]; then
    COUNT=$(echo "$PYTEST_OUTPUT" | tail -1 | grep -oE '[0-9]+ passed' | head -1)
    pass "pytest: $COUNT"
else
    fail "pytest" "$(echo "$PYTEST_OUTPUT" | tail -3)"
fi

echo ""

# ── 4. Module imports ────────────────────────────────────────────────────
echo "4. Module imports"

for mod in types extract haiku log prompts consolidate shell; do
    if python3 -c "import sys; sys.path.insert(0,'$PIPELINE_DIR'); import pipeline.$mod" 2>/dev/null; then
        pass "pipeline.$mod"
    else
        fail "pipeline.$mod" "import failed"
    fi
done

echo ""

# ── 5. Shell bridge commands ─────────────────────────────────────────────
echo "5. Shell bridge commands"

# 5a. Extract — use fixture JSONL
if [ -f "$FIXTURES/sample-session.jsonl" ]; then
    # Create a temp project dir structure pointing to the fixture
    TMP_PROJECT=$(mktemp -d /tmp/remember-test-project-XXXXXX)
    cleanup_files+=("$TMP_PROJECT")
    SESSION_DIR="$HOME/.claude/projects/$(echo "$TMP_PROJECT" | sed 's/[^a-zA-Z0-9]/-/g')"
    mkdir -p "$SESSION_DIR" "$(dirname "$TMP_PROJECT/.remember/tmp/last-save.json")"
    mkdir -p "$TMP_PROJECT/.remember/tmp"
    cp "$FIXTURES/sample-session.jsonl" "$SESSION_DIR/test-session.jsonl"

    EXTRACT_OUT=$(cd "$PIPELINE_DIR" && python3 -m pipeline.shell extract "test-session" "$TMP_PROJECT" 2>&1)
    if echo "$EXTRACT_OUT" | grep -q "POSITION=10" && echo "$EXTRACT_OUT" | grep -q "EXTRACT_FILE="; then
        pass "shell extract (fixture: 10 lines)"
        # Clean up the extract temp file
        EXTRACT_TMP=$(echo "$EXTRACT_OUT" | grep "EXTRACT_FILE=" | sed "s/EXTRACT_FILE=//;s/'//g")
        [ -f "$EXTRACT_TMP" ] && cleanup_files+=("$EXTRACT_TMP")
    else
        fail "shell extract" "unexpected output: $(echo "$EXTRACT_OUT" | head -3)"
    fi
    rm -rf "$SESSION_DIR" "$TMP_PROJECT"
else
    fail "shell extract" "fixture not found"
fi

# 5b. Parse Haiku — mock JSON input
MOCK_HAIKU='{"result":"## 10:30 | test\nDid stuff","usage":{"input_tokens":500,"output_tokens":100,"cache_read_input_tokens":200},"total_cost_usd":0.005}'
PARSE_OUT=$(echo "$MOCK_HAIKU" | (cd "$PIPELINE_DIR" && python3 -m pipeline.shell parse-haiku) 2>&1)
if echo "$PARSE_OUT" | grep -q "IS_SKIP=false" && echo "$PARSE_OUT" | grep -q "TK_IN=500"; then
    pass "shell parse-haiku (mock response)"
    HAIKU_TMP=$(echo "$PARSE_OUT" | grep "HAIKU_TEXT_FILE=" | sed "s/HAIKU_TEXT_FILE=//;s/'//g")
    [ -f "$HAIKU_TMP" ] && cleanup_files+=("$HAIKU_TMP")
else
    fail "shell parse-haiku" "unexpected output"
fi

# 5c. Parse Haiku — SKIP detection
MOCK_SKIP='{"result":"SKIP — duplicate","usage":{"input_tokens":100,"output_tokens":10,"cache_read_input_tokens":0},"total_cost_usd":0.001}'
SKIP_OUT=$(echo "$MOCK_SKIP" | (cd "$PIPELINE_DIR" && python3 -m pipeline.shell parse-haiku) 2>&1)
if echo "$SKIP_OUT" | grep -q "IS_SKIP=true"; then
    pass "shell parse-haiku SKIP detection"
    SKIP_TMP=$(echo "$SKIP_OUT" | grep "HAIKU_TEXT_FILE=" | sed "s/HAIKU_TEXT_FILE=//;s/'//g")
    [ -f "$SKIP_TMP" ] && cleanup_files+=("$SKIP_TMP")
else
    fail "shell parse-haiku SKIP" "not detected"
fi

# 5d. Save position — round trip
TMP_POS=$(mktemp /tmp/remember-test-pos-XXXXXX)
cleanup_files+=("$TMP_POS")
(cd "$PIPELINE_DIR" && python3 -m pipeline.shell save-position "$TMP_POS" "test-session-xyz" 42)
SAVED=$(python3 -c "import json; d=json.load(open('$TMP_POS')); print(d['session'], d['line'])")
if [ "$SAVED" = "test-session-xyz 42" ]; then
    pass "shell save-position (round trip)"
else
    fail "shell save-position" "got: $SAVED"
fi

# 5e. Build prompt — verify substitution
TMP_EXTRACT_F=$(mktemp /tmp/remember-test-extract-XXXXXX)
TMP_LAST_F=$(mktemp /tmp/remember-test-last-XXXXXX)
TMP_PROMPT_F=$(mktemp /tmp/remember-test-prompt-XXXXXX)
cleanup_files+=("$TMP_EXTRACT_F" "$TMP_LAST_F" "$TMP_PROMPT_F")
echo "[HUMAN] hello" > "$TMP_EXTRACT_F"
echo "(no previous entry)" > "$TMP_LAST_F"
(cd "$PIPELINE_DIR" && python3 -m pipeline.shell build-prompt "$TMP_EXTRACT_F" "$TMP_LAST_F" "10:30" "master" "$TMP_PROMPT_F")
if [ -s "$TMP_PROMPT_F" ] && ! grep -q '{{TIME}}' "$TMP_PROMPT_F" && ! grep -q '{{BRANCH}}' "$TMP_PROMPT_F"; then
    pass "shell build-prompt (substitution clean)"
else
    fail "shell build-prompt" "unsubstituted vars or empty"
fi

# 5f. Build NDC prompt
TMP_MEM=$(mktemp /tmp/remember-test-mem-XXXXXX)
TMP_NDC=$(mktemp /tmp/remember-test-ndc-XXXXXX)
cleanup_files+=("$TMP_MEM" "$TMP_NDC")
echo "## 10:30 | test branch\nDid stuff" > "$TMP_MEM"
(cd "$PIPELINE_DIR" && python3 -m pipeline.shell build-ndc-prompt "$TMP_MEM" "$TMP_NDC")
if [ -s "$TMP_NDC" ] && ! grep -q '{{NOW_CONTENT}}' "$TMP_NDC"; then
    pass "shell build-ndc-prompt"
else
    fail "shell build-ndc-prompt" "unsubstituted or empty"
fi

echo ""

# ── 6. Prompt templates ──────────────────────────────────────────────────
echo "6. Prompt templates"

PROMPTS_DIR="$PIPELINE_DIR/prompts"
for tpl in save-session.prompt.txt compress-ndc.prompt.txt consolidate-staging.prompt.txt; do
    if [ -f "$PROMPTS_DIR/$tpl" ]; then
        pass "$tpl exists"
    else
        fail "$tpl" "missing"
    fi
done

# Verify consolidation prompt has section markers
if grep -q '===RECENT===' "$PROMPTS_DIR/consolidate-staging.prompt.txt" && grep -q '===ARCHIVE===' "$PROMPTS_DIR/consolidate-staging.prompt.txt"; then
    pass "consolidate-staging.prompt.txt has section markers"
else
    fail "consolidate-staging.prompt.txt" "missing ===RECENT=== or ===ARCHIVE=== markers"
fi

# Verify all templates have their expected placeholders
if grep -q '{{TIME}}' "$PROMPTS_DIR/save-session.prompt.txt"; then
    pass "save-session.prompt.txt has {{TIME}} placeholder"
else
    fail "save-session.prompt.txt" "missing {{TIME}}"
fi

if grep -q '{{NOW_CONTENT}}' "$PROMPTS_DIR/compress-ndc.prompt.txt"; then
    pass "compress-ndc.prompt.txt has {{NOW_CONTENT}} placeholder"
else
    fail "compress-ndc.prompt.txt" "missing {{NOW_CONTENT}}"
fi

echo ""

# ── 7. Dry-run save ──────────────────────────────────────────────────────
echo "7. Dry-run save (full flow, no Haiku)"

DRY_OUT=$(cd "$PIPELINE_DIR" && bash scripts/save-session.sh --dry 2>/dev/null) || true
DRY_EXIT=$?
if echo "$DRY_OUT" 2>/dev/null | grep -q "DRY RUN"; then
    LINES=$(echo "$DRY_OUT" | grep -c '^\[' 2>/dev/null || echo 0)
    pass "save-session.sh --dry ($LINES exchanges shown)"
else
    fail "save-session.sh --dry" "exit $DRY_EXIT"
fi

echo ""

# ── 8. Live Haiku test (optional) ────────────────────────────────────────
echo "8. Live Haiku test"

if [ "$LIVE" = true ]; then
    LIVE_OUT=$(cd "$PIPELINE_DIR" && python3 -c "
import sys; sys.path.insert(0, '.')
from pipeline.haiku import call_haiku
r = call_haiku('Reply with exactly one word: WORKING', timeout=30)
print(f'text={r.text.strip()}')
print(f'tokens_in={r.tokens.input}')
print(f'tokens_out={r.tokens.output}')
" 2>&1)
    if echo "$LIVE_OUT" | grep -qi "WORKING"; then
        TK_IN=$(echo "$LIVE_OUT" | grep "tokens_in=" | cut -d= -f2)
        pass "Haiku call (${TK_IN} input tokens)"
    else
        fail "Haiku call" "$LIVE_OUT"
    fi
else
    skip "Haiku call"
fi

echo ""

# ── Summary ───────────────────────────────────────────────────────────────
echo "=== Results ==="
echo "  Passed:  $PASS"
echo "  Failed:  $FAIL"
echo "  Skipped: $SKIP"
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo "FAILED — $FAIL test(s) need attention"
    exit 1
else
    echo "ALL PASSED"
    exit 0
fi
