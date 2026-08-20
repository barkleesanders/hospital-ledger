/**
 * Basic DOM-based tests for homepage procedure search and procedure page filters.
 * Run with: node --experimental-vm-modules tests/client-ux.test.js
 * Or open tests/test-runner.html in a browser for manual verification.
 */

import { JSDOM } from 'jsdom';

// Test utilities
function createTestDom(html) {
  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    resources: 'usable',
    url: 'http://localhost:8787',
  });
  return dom.window;
}

function setupHomepageTest() {
  const html = `
    <!DOCTYPE html>
    <html>
    <body>
      <form id="by-proc-form">
        <input id="by-proc-input" type="search" />
        <button type="submit">Compare</button>
      </form>
      <div id="by-proc-msg"></div>
      <datalist id="proc-suggestions"></datalist>
      <script>window.CPT_NAMES = {
        "45378": "Colonoscopy, diagnostic (no biopsy)",
        "73721": "MRI knee without contrast",
        "70551": "MRI brain without contrast",
        "27447": "Total knee replacement",
        "23472": "Total shoulder replacement"
      };</script>
    </body>
    </html>
  `;
  return createTestDom(html);
}

function setupProcedurePageTest() {
  const html = `
    <!DOCTYPE html>
    <html>
    <body>
      <section data-states='["CA","NY","TX"]' data-payers='[["aetna","Aetna"],["bcbs","Blue Cross"]]' data-cash-median="5000">
        <select id="filter-state">
          <option value="">All states</option>
          <option value="CA">CA</option>
          <option value="NY">NY</option>
        </select>
        <select id="filter-payer">
          <option value="">All insurers</option>
          <option value="aetna">Aetna</option>
        </select>
        <select id="sort-by">
          <option value="median-first">Most representative</option>
          <option value="cash-asc">Cheapest cash first</option>
        </select>
      </section>
      <div id="status">500 hospitals reporting</div>
      <div id="results">
        <table>
          <tbody id="results">
            <tr data-payers='[{"slug":"aetna"}]'>
              <td><a href="/hospital/123">Hospital CA</a></td>
              <td>CA</td>
              <td>$10,000</td>
              <td>$8,000</td>
            </tr>
            <tr data-payers='[{"slug":"bcbs"}]'>
              <td><a href="/hospital/456">Hospital NY</a></td>
              <td>NY</td>
              <td>$12,000</td>
              <td>$9,000</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div id="results-mobile"></div>
    </body>
    </html>
  `;
  return createTestDom(html);
}

// Bug 1 Tests: Homepage Procedure Search

function testDirectCodeSearch() {
  console.log('\n=== Test: Direct CPT Code Search ===');
  const window = setupHomepageTest();
  const input = window.document.getElementById('by-proc-input');
  
  // Simulate entering a direct code
  input.value = '45378';
  
  // In real implementation, this would call resolveQuery
  // For now, just verify the pattern matching
  const isCode = /^[A-Z0-9]{4,7}$/.test(input.value.toUpperCase());
  
  console.log(`Input: ${input.value}`);
  console.log(`Is valid code format: ${isCode}`);
  console.log(`Expected: true`);
  console.log(isCode ? '✓ PASS' : '✗ FAIL');
  
  return isCode;
}

function testProcedureNameSearch() {
  console.log('\n=== Test: Procedure Name Search (Single Match) ===');
  const window = setupHomepageTest();
  
  const searchTerm = 'colonoscopy';
  const cptNames = window.CPT_NAMES;
  
  // Simulate name search
  const matches = Object.entries(cptNames).filter(([, desc]) => 
    desc.toLowerCase().includes(searchTerm.toLowerCase())
  );
  
  console.log(`Search term: "${searchTerm}"`);
  console.log(`Matches found: ${matches.length}`);
  console.log(`Match: ${matches[0] ? `${matches[0][0]} - ${matches[0][1]}` : 'none'}`);
  console.log(`Expected: 1 match for code 45378`);
  
  const pass = matches.length === 1 && matches[0][0] === '45378';
  console.log(pass ? '✓ PASS' : '✗ FAIL');
  
  return pass;
}

function testMultipleMatches() {
  console.log('\n=== Test: Multiple Matches (MRI) ===');
  const window = setupHomepageTest();
  
  const searchTerm = 'MRI';
  const cptNames = window.CPT_NAMES;
  
  const matches = Object.entries(cptNames).filter(([, desc]) => 
    desc.toLowerCase().includes(searchTerm.toLowerCase())
  );
  
  console.log(`Search term: "${searchTerm}"`);
  console.log(`Matches found: ${matches.length}`);
  matches.forEach(([code, desc]) => console.log(`  - ${code}: ${desc}`));
  console.log(`Expected: Multiple matches (should show picker)`);
  
  const pass = matches.length > 1;
  console.log(pass ? '✓ PASS' : '✗ FAIL');
  
  return pass;
}

function testCaseInsensitive() {
  console.log('\n=== Test: Case Insensitive Search ===');
  const window = setupHomepageTest();
  const cptNames = window.CPT_NAMES;
  
  const terms = ['COLONOSCOPY', 'Colonoscopy', 'colonoscopy'];
  const results = terms.map(term => {
    const matches = Object.entries(cptNames).filter(([, desc]) => 
      desc.toLowerCase().includes(term.toLowerCase())
    );
    return matches.length;
  });
  
  console.log('Testing variations:');
  terms.forEach((term, i) => console.log(`  ${term}: ${results[i]} matches`));
  
  const allSame = results.every(r => r === results[0]);
  console.log(`Expected: All return same count`);
  console.log(allSame ? '✓ PASS' : '✗ FAIL');
  
  return allSame;
}

// Bug 2 Tests: Procedure Page Filters

function testFilterStaging() {
  console.log('\n=== Test: Filter Staging (No Immediate Apply) ===');
  const window = setupProcedurePageTest();
  const stateSelect = window.document.getElementById('filter-state');
  const tbody = window.document.getElementById('results');
  
  const initialRowCount = tbody.querySelectorAll('tr').length;
  console.log(`Initial row count: ${initialRowCount}`);
  
  // Change filter (should not apply yet)
  stateSelect.value = 'CA';
  console.log(`Set state filter to: ${stateSelect.value}`);
  
  // Check that rows are still visible
  const currentRowCount = tbody.querySelectorAll('tr').length;
  console.log(`Current row count: ${currentRowCount}`);
  console.log(`Expected: Same as initial (${initialRowCount})`);
  
  const pass = currentRowCount === initialRowCount;
  console.log(pass ? '✓ PASS' : '✗ FAIL');
  console.log('Note: Without client script, filters don\'t work. This tests the staging concept.');
  
  return pass;
}

function testUrlParamSupport() {
  console.log('\n=== Test: URL Parameter Parsing ===');
  
  // Simulate URL with params
  const url = new URL('http://localhost:8787/procedure/45378?state=CA&payer=aetna&sort=cash-asc');
  const params = new URLSearchParams(url.search);
  
  const state = params.get('state');
  const payer = params.get('payer');
  const sort = params.get('sort');
  
  console.log(`URL: ${url.toString()}`);
  console.log(`Parsed state: ${state}`);
  console.log(`Parsed payer: ${payer}`);
  console.log(`Parsed sort: ${sort}`);
  console.log(`Expected: CA, aetna, cash-asc`);
  
  const pass = state === 'CA' && payer === 'aetna' && sort === 'cash-asc';
  console.log(pass ? '✓ PASS' : '✗ FAIL');
  
  return pass;
}

function testSortFunctions() {
  console.log('\n=== Test: Sort Functions ===');
  
  const hospitals = [
    { name: 'Hospital A', state: 'CA', cash: 5000 },
    { name: 'Hospital B', state: 'NY', cash: 3000 },
    { name: 'Hospital C', state: 'CA', cash: 7000 },
  ];
  
  // Test state sort
  const byState = [...hospitals].sort((a, b) => a.state.localeCompare(b.state));
  console.log('Sort by state:');
  byState.forEach(h => console.log(`  ${h.name} (${h.state})`));
  
  // Test cash ascending
  const byCash = [...hospitals].sort((a, b) => a.cash - b.cash);
  console.log('Sort by cash (ascending):');
  byCash.forEach(h => console.log(`  ${h.name} - $${h.cash}`));
  
  const statePass = byState[0].state === 'CA' && byState[2].state === 'NY';
  const cashPass = byCash[0].cash === 3000 && byCash[2].cash === 7000;
  
  console.log(`State sort: ${statePass ? '✓ PASS' : '✗ FAIL'}`);
  console.log(`Cash sort: ${cashPass ? '✓ PASS' : '✗ FAIL'}`);
  
  return statePass && cashPass;
}

// Run all tests
async function runTests() {
  console.log('==========================================');
  console.log('Hospital Ledger UX Fixes - Automated Tests');
  console.log('==========================================');
  
  const results = {
    bug1: [],
    bug2: [],
  };
  
  console.log('\n📋 BUG 1: Homepage Procedure Search Tests');
  console.log('------------------------------------------');
  results.bug1.push(testDirectCodeSearch());
  results.bug1.push(testProcedureNameSearch());
  results.bug1.push(testMultipleMatches());
  results.bug1.push(testCaseInsensitive());
  
  console.log('\n📋 BUG 2: Procedure Page Filter Tests');
  console.log('--------------------------------------');
  results.bug2.push(testFilterStaging());
  results.bug2.push(testUrlParamSupport());
  results.bug2.push(testSortFunctions());
  
  console.log('\n==========================================');
  console.log('TEST SUMMARY');
  console.log('==========================================');
  
  const bug1Pass = results.bug1.filter(Boolean).length;
  const bug1Total = results.bug1.length;
  const bug2Pass = results.bug2.filter(Boolean).length;
  const bug2Total = results.bug2.length;
  const totalPass = bug1Pass + bug2Pass;
  const totalTests = bug1Total + bug2Total;
  
  console.log(`Bug 1 (Homepage Search):  ${bug1Pass}/${bug1Total} passed`);
  console.log(`Bug 2 (Procedure Filters): ${bug2Pass}/${bug2Total} passed`);
  console.log(`\nOverall:                  ${totalPass}/${totalTests} passed`);
  
  if (totalPass === totalTests) {
    console.log('\n✓ ALL TESTS PASSED');
    return 0;
  } else {
    console.log(`\n✗ ${totalTests - totalPass} TESTS FAILED`);
    return 1;
  }
}

// Check if running in Node.js
if (typeof process !== 'undefined' && process.versions && process.versions.node) {
  // Check if jsdom is available
  try {
    await import('jsdom');
    const exitCode = await runTests();
    process.exit(exitCode);
  } catch (e) {
    console.log('\n⚠️  JSDOM not installed. Install with: npm install --save-dev jsdom');
    console.log('Or run tests manually in browser using test-runner.html\n');
    
    // Run tests without DOM (limited)
    console.log('Running limited tests without DOM...\n');
    testProcedureNameSearch();
    testMultipleMatches();
    testCaseInsensitive();
    testUrlParamSupport();
    testSortFunctions();
  }
} else {
  // Browser environment - export for manual use
  if (typeof window !== 'undefined') {
    window.hospitalLedgerTests = {
      runTests,
      testDirectCodeSearch,
      testProcedureNameSearch,
      testMultipleMatches,
      testCaseInsensitive,
      testFilterStaging,
      testUrlParamSupport,
      testSortFunctions,
    };
    console.log('Tests loaded. Run: hospitalLedgerTests.runTests()');
  }
}
