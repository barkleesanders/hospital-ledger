# Test Plan for UX Bug Fixes

## Bug 1: Homepage Procedure Search CPT Resolution

### Test Cases

#### TC1.1: Direct CPT Code Search
- **Input:** `45378` (colonoscopy code)
- **Expected:** Navigate directly to `/procedure/45378`
- **Status:** PASS / FAIL

#### TC1.2: Procedure Name Search - Single Match
- **Input:** `colonoscopy` or `MRI knee` or `total shoulder`
- **Expected:** 
  - System queries CPT index
  - Finds matching CPT code
  - Shows "Matched: [description]" message
  - Navigates to procedure page
- **Status:** PASS / FAIL

#### TC1.3: Procedure Name Search - Multiple Matches
- **Input:** `MRI` (matches multiple procedures)
- **Expected:**
  - System queries CPT index
  - Shows picker UI with list of matching procedures
  - Each option shows CPT code + description
  - Clicking an option navigates to that procedure
- **Status:** PASS / FAIL

#### TC1.4: Procedure Name Search - No Match
- **Input:** `xyz123invalidprocedure`
- **Expected:** Error message "No matching procedures found"
- **Status:** PASS / FAIL

#### TC1.5: Mixed Case Search
- **Input:** `COLONOSCOPY`, `Colonoscopy`, `colonoscopy`
- **Expected:** All should find the same results (case-insensitive)
- **Status:** PASS / FAIL

#### TC1.6: Partial Name Match
- **Input:** `knee` or `shoulder`
- **Expected:** Shows all procedures containing that term
- **Status:** PASS / FAIL

### Manual Test Steps

1. Navigate to homepage (http://localhost:8787 or https://hospitalledger.com)
2. Find section "01 Find a price" → "A. What you need"
3. Test each case above
4. Verify:
   - Search resolves procedure names to CPT codes
   - Picker UI appears for multiple matches
   - Single matches navigate directly
   - Code searches still work (backward compatible)

## Bug 2: Procedure Page Filter Apply on Update

### Test Cases

#### TC2.1: Filter Staging (No Immediate Apply)
- **Action:** Select a state from "Filter by state" dropdown
- **Expected:**
  - Dropdown shows new selection
  - Table/list does NOT update yet
  - No page refetch occurs
- **Status:** PASS / FAIL

#### TC2.2: Multiple Filter Staging
- **Action:** 
  1. Select state (e.g., CA)
  2. Select insurance (e.g., Aetna)
  3. Change sort order
- **Expected:**
  - All three selections are staged
  - Table/list still shows original unfiltered results
- **Status:** PASS / FAIL

#### TC2.3: Apply via Update Button
- **Action:** 
  1. Stage filters (state: CA, payer: Aetna)
  2. Click "Update search" button
- **Expected:**
  - Table/list updates to show only matching hospitals
  - Status text shows "X hospitals reporting"
  - URL updates with query params (?state=CA&payer=...)
  - Filter status shows active filters
- **Status:** PASS / FAIL

#### TC2.4: Apply via Enter Key
- **Action:**
  1. Select state filter
  2. Press Enter while filter dropdown has focus
- **Expected:**
  - Filters apply (same as clicking Update button)
  - Results update
  - URL updates
- **Status:** PASS / FAIL

#### TC2.5: Clear Filters
- **Action:**
  1. Apply some filters
  2. Click "Clear filters" button
- **Expected:**
  - All dropdowns reset to default
  - Table shows all hospitals again
  - URL clears query params
  - Status text clears
- **Status:** PASS / FAIL

#### TC2.6: Shareable Filtered URLs
- **Action:**
  1. Apply filters (state: CA)
  2. Copy URL (should be /procedure/45378?state=CA)
  3. Open URL in new tab or share with someone
- **Expected:**
  - Page loads with CA filter already applied
  - Results show only CA hospitals
  - Filter dropdown shows CA selected
- **Status:** PASS / FAIL

#### TC2.7: Sort Without Filters
- **Action:**
  1. Change sort to "Cheapest cash first"
  2. Click Update search
- **Expected:**
  - Results re-sort (lowest cash price first)
  - All hospitals still visible (no filter)
  - URL may update with sort param
- **Status:** PASS / FAIL

#### TC2.8: Combined State + Payer Filter
- **Action:**
  1. Filter by state: CA
  2. Filter by payer: Blue Cross
  3. Click Update search
- **Expected:**
  - Shows only CA hospitals that have Blue Cross rates
  - Count reflects filtered subset
  - Both filters shown in status
- **Status:** PASS / FAIL

### Manual Test Steps

1. Navigate to a procedure page (e.g., `/procedure/45378`)
2. Verify "Update search" and "Clear filters" buttons are present below filter dropdowns
3. Test each case above
4. Verify:
   - Changing dropdowns does NOT immediately update results
   - Clicking "Update search" applies all staged changes
   - Enter key works as alternative to button
   - URL query params enable shareable filtered links
   - Clear filters resets everything

## Regression Tests

### R1: Homepage Other Search Methods Still Work
- **Test:** "B. Your insurance" and "C. A specific hospital" forms
- **Expected:** No change in behavior
- **Status:** PASS / FAIL

### R2: Procedure Page SSR Still Works
- **Test:** Load procedure page with JS disabled
- **Expected:** Page renders with default sort/filters
- **Status:** PASS / FAIL

### R3: Mobile View
- **Test:** All fixes work on mobile (filter staging, apply button)
- **Expected:** Same behavior as desktop
- **Status:** PASS / FAIL

## Performance Tests

### P1: CPT Index Load Time
- **Test:** Homepage procedure search with name (first time)
- **Expected:** CPT index loads within 1-2 seconds
- **Measurement:** Check Network tab for /api/cpt-index timing
- **Status:** PASS / FAIL

### P2: Filter Apply Performance
- **Test:** Apply filters on procedure page with 500 hospitals
- **Expected:** Results update within 100ms (client-side filtering)
- **Status:** PASS / FAIL

## Browser Compatibility

Test all fixes in:
- [ ] Chrome/Edge (latest)
- [ ] Firefox (latest)
- [ ] Safari (latest)
- [ ] Mobile Safari (iOS)
- [ ] Mobile Chrome (Android)

## Test Results Summary

| Bug | Test Date | Tester | Overall Status | Notes |
|-----|-----------|--------|----------------|-------|
| Bug 1 | YYYY-MM-DD | | PASS/FAIL | |
| Bug 2 | YYYY-MM-DD | | PASS/FAIL | |

## Test Evidence

When testing, capture:
1. Screenshot/video of homepage search finding CPT code by name
2. Screenshot of picker UI for multiple matches
3. Screenshot/video of procedure filter staging → apply flow
4. Screenshot of filtered URL loading with params pre-applied

Place evidence in `test-evidence/` directory or PR description.
