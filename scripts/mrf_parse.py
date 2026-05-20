#!/usr/bin/env python3
"""Stage 4.1: Parse hospital MRF (any format) → canonical JSON.

Canonical schema (subset of CMS HPT v2.0):
{
  "ccn": "050228",
  "hospital_name": "ZSFG",
  "source_url": "...",
  "fetched_at": "2026-05-11T...",
  "format_detected": "csv-tall" | "csv-wide" | "json-v2" | "xlsx" | "zip",
  "row_count": 12345,
  "items": [
    {
      "code": "99213",
      "code_type": "CPT" | "HCPCS" | "DRG" | "MS-DRG" | "REV" | "NDC" | "ICD-10" | "CDM" | "",
      "description": "Office visit, established patient, 20-29 min",
      "setting": "inpatient" | "outpatient" | "",
      "billing_class": "professional" | "facility" | "",
      "gross_charge": 250.00,
      "cash_discount": 150.00,
      "min_negotiated": 80.00,
      "max_negotiated": 320.00,
      "payer_rates": [
        {"payer": "Aetna", "plan": "PPO", "rate_dollar": 175.00, "methodology": "fee schedule"}
      ]
    }
  ]
}

Handles common variations:
 - CSV with custom column names (uses keyword matching)
 - JSON v2.0 (CMS spec)
 - JSON v1 (legacy "standard_charges" array)
 - XLSX (.xlsx, .xls)
 - ZIP archives (auto-extract + recurse)
"""
import argparse, sys, os, json, re, csv, io, gzip, zipfile, datetime, subprocess, tempfile, html as html_lib, codecs, itertools
from urllib.parse import urlparse, urljoin
from pathlib import Path

try:
    import httpx
except ImportError:
    sys.exit("Install: pip install httpx openpyxl pandas tqdm")

try:
    import ijson
except ImportError:
    ijson = None

try:
    import simdjson
except ImportError:
    simdjson = None

try:
    import duckdb
except ImportError:
    duckdb = None

try:
    import polars as pl
except ImportError:
    pl = None

try:
    import xlrd
except ImportError:
    xlrd = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARSED_DIR = os.path.join(ROOT, 'data', 'parsed')
RAW_DIR = os.path.join(ROOT, 'data', 'raw')
os.makedirs(PARSED_DIR, exist_ok=True)
os.makedirs(RAW_DIR, exist_ok=True)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
FAST_JSON = os.environ.get('HPT_FAST_JSON', '1').lower() not in ('0', 'false', 'no')
FAST_CSV_ENGINE = os.environ.get('HPT_FAST_CSV_ENGINE', 'auto').lower()
FAST_CSV_MIN_BYTES = int(os.environ.get('HPT_FAST_CSV_MIN_BYTES', str(2 * 1024 * 1024)))
SIMDJSON_PARSER = simdjson.Parser() if simdjson is not None else None

# Column name keywords → canonical field
CODE_COL_HINTS = [
    'cpt', 'hcpcs', 'code', 'procedure code', 'service_code', 'item_code',
    'cdm', 'drg', 'ms_drg', 'msdrg', 'rev_code', 'ndc', 'internal id',
    'charge #', 'px code', 'item no', 'erx id', 'service id', 'service_id',
]
DESC_COL_HINTS = [
    'description', 'desc', 'service', 'procedure_name', 'procedure name',
    'item_name', 'item name', 'svc_description', 'service name', 'svc_name',
    'bill description', 'billing description', 'medication',
]
GROSS_COL_HINTS = ['gross', 'standard charge', 'standard_charge', 'list_price', 'charge_master', 'cdm_price', 'price', 'amount', 'eff rate', 'rate amt', 'charge amt']
CASH_COL_HINTS = ['cash', 'self_pay', 'self-pay', 'discounted_cash', 'discount_cash_price']
MIN_COL_HINTS = ['min_negotiated', 'minimum_negotiated', 'min_charge', 'minimum']
MAX_COL_HINTS = ['max_negotiated', 'maximum_negotiated', 'max_charge', 'maximum']
SETTING_HINTS = ['setting', 'inpatient_outpatient', 'ip_op']
BILLING_HINTS = ['billing_class', 'professional_facility', 'class']
CODE_TYPE_HINTS = ['code type', 'billing code type', 'procedure code type']
PLAN_HINTS = ['plan_name', 'plan name', 'plan']
PAYER_HINTS = ['payer_name', 'payer name', 'payer']
NEGOTIATED_DOLLAR_HINTS = [
    'negotiated_dollar', 'negotiated dollar', 'standard_charge|negotiated_dollar',
    'standard charge negotiated dollar', 'payer_specific_negotiated_charge',
    'negotiated_rate', 'negotiated rate', 'rate_dollar', 'rate dollar',
    'standard_charge_dollar', 'standard charge dollar',
]
RATE_EXCLUDE_HINTS = [
    'minimum', 'maximum', 'min_', 'max_', 'percentage', 'percent', 'methodology',
    'method', 'estimated', 'algorithm', 'notes', 'note', 'count', 'volume',
]
WIDE_PAYER_HINTS = [
    'commercial', 'medicare', 'medicaid', 'aetna', 'anthem', 'blue', 'cigna',
    'united', 'uhc', 'humana', 'payer', 'plan', 'insurance',
]
HEADER_SIGNAL_HINTS = [
    CODE_COL_HINTS,
    DESC_COL_HINTS,
    GROSS_COL_HINTS,
    CASH_COL_HINTS,
    PAYER_HINTS,
    PLAN_HINTS,
    NEGOTIATED_DOLLAR_HINTS,
    ['price', 'charge', 'standard_charge', 'standard charge'],
]
HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
DIRECT_FILE_RE = re.compile(r'\.(csv|json|xlsx?|zip)(\?|$)', re.IGNORECASE)
FILE_HINT_RE = re.compile(
    r'standard[\s\-_]?charges?|chargemaster|price[\s\-_]?transparency|'
    r'machine[\s\-_]?readable|download',
    re.IGNORECASE,
)
GDRIVE_VIEW_RE = re.compile(r'drive\.google\.com/file/d/([A-Za-z0-9_-]+)', re.IGNORECASE)
URLDEFENSE_V3_RE = re.compile(r'urldefense\.com/v3/__(.+?)__;', re.IGNORECASE)
GENERIC_NAME_TOKENS = {
    'hospital', 'hosp', 'medical', 'center', 'centre', 'health', 'system',
    'memorial', 'regional', 'community', 'county', 'district', 'saint',
    'clinic', 'clinics', 'city', 'the', 'and', 'for', 'of', 'llc', 'inc',
}


def kw_match(col, kws):
    c = col.lower().replace(' ', '_').replace('-', '_')
    return any(k.replace(' ', '_') in c for k in kws)


def row_get(row, idx):
    if idx is None or idx >= len(row):
        return ''
    return str(row[idx]).strip()


def header_signal_count(cells):
    found = set()
    for cell in cells:
        c = str(cell).strip()
        if not c:
            continue
        for i, hints in enumerate(HEADER_SIGNAL_HINTS):
            if kw_match(c, hints):
                found.add(i)
    return len(found)


def sniff_delimiter(text):
    sample = text[:16384]
    lines = [line for line in sample.splitlines() if line.strip()][:20]
    tab_header_hits = 0
    for line in lines:
        if '\t' not in line:
            continue
        cells = next(csv.reader([line], delimiter='\t'))
        non_empty = sum(1 for cell in cells if str(cell).strip())
        score = header_signal_count(cells)
        if non_empty >= 3 and score >= 2:
            tab_header_hits += 1
    if tab_header_hits:
        return '\t'
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',\t;|')
        return dialect.delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in [',', '\t', ';', '|']}
        return max(counts, key=counts.get)


def find_header_row(rows, max_scan=30):
    """Find the real table header after optional hospital metadata rows."""
    best_idx, best_score, best_non_empty = 0, 0, 0
    for i, cells in enumerate(rows[:max_scan]):
        if not cells or all(not str(cell).strip() for cell in cells):
            continue
        if len(cells) < 2:
            continue
        non_empty = sum(1 for cell in cells if str(cell).strip())
        if non_empty < 2:
            continue
        score = header_signal_count(cells)
        if score >= 2 and (score > best_score or (score == best_score and non_empty > best_non_empty)):
            best_idx, best_score, best_non_empty = i, score, non_empty
    return best_idx


def delimited_rows(text):
    delimiter = sniff_delimiter(text)
    # newline='' lets csv.reader handle \r, \n and \r\n itself. Some legacy
    # hospital CDM exports use a bare \r as the line separator AND embed stray
    # \r inside unquoted fields, which trips csv.reader ("new-line character
    # seen in unquoted field"). On that failure, normalize line endings to \n
    # and retry — a bare \r is then treated as a plain character.
    try:
        reader = csv.reader(io.StringIO(text, newline=''), delimiter=delimiter)
        rows = [row for row in reader]
    except csv.Error:
        normalized = text.replace('\r\n', '\n').replace('\r', '\n')
        reader = csv.reader(io.StringIO(normalized, newline=''), delimiter=delimiter)
        rows = [row for row in reader]
    return delimiter, rows


def duckdb_delimited_rows(content_bytes, max_rows):
    if duckdb is None or FAST_CSV_ENGINE not in ('auto', 'duckdb'):
        return None
    if len(content_bytes) < FAST_CSV_MIN_BYTES:
        return None
    tmp_path = ''
    try:
        with tempfile.NamedTemporaryFile(suffix='.csv', delete=False) as tmp:
            tmp.write(content_bytes)
            tmp_path = tmp.name
        conn = duckdb.connect(database=':memory:')
        try:
            result = conn.execute(
                """
                SELECT *
                FROM read_csv_auto(
                  ?,
                  all_varchar=true,
                  header=false,
                  ignore_errors=true,
                  null_padding=true,
                  sample_size=20480
                )
                LIMIT ?
                """,
                [tmp_path, max_rows],
            ).fetchall()
        finally:
            conn.close()
        return [[('' if cell is None else str(cell)) for cell in row] for row in result]
    except Exception:
        return None
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def polars_delimited_rows(content_bytes, max_rows):
    if pl is None or FAST_CSV_ENGINE not in ('auto', 'polars'):
        return None
    if len(content_bytes) < FAST_CSV_MIN_BYTES:
        return None
    try:
        frame = pl.read_csv(
            io.BytesIO(content_bytes),
            has_header=False,
            infer_schema_length=0,
            ignore_errors=True,
            truncate_ragged_lines=True,
            n_rows=max_rows,
        )
        return [[('' if cell is None else str(cell)) for cell in row] for row in frame.rows()]
    except Exception:
        return None


def read_delimited_rows(content_bytes, max_rows):
    limit = max_rows + 64
    rows = duckdb_delimited_rows(content_bytes, limit)
    if rows is None:
        rows = polars_delimited_rows(content_bytes, limit)
    if rows is not None:
        return rows
    text = content_bytes.decode('utf-8', errors='replace')
    _, rows = delimited_rows(text)
    return rows[:limit]


def looks_like_delimited_text(content_bytes):
    """Heuristic for CSV/TSV content served behind the wrong extension."""
    try:
        sample = content_bytes[:8192].decode('utf-8', errors='replace')
    except Exception:
        return False
    if not sample or ('\n' not in sample and '\r' not in sample):
        return False
    lower = sample.lower()
    if 'hospital standard charges' in lower and ('\t' in sample or ',' in sample):
        return True
    best_score = 0
    for line in [line for line in sample.splitlines() if line.strip()][:10]:
        if '\t' not in line and ',' not in line:
            continue
        cells = [cell.strip() for cell in re.split(r'[\t,]', line)]
        best_score = max(best_score, header_signal_count(cells))
        if best_score >= 2:
            return True
    return False


def first_col(cols, hints):
    return next((i for i, c in enumerate(cols) if kw_match(c, hints)), None)


def first_code_col(cols):
    priority_hints = [
        'cpt hcpcs', 'cpt_hcpcs', 'hcpcs cpt', 'hcpcs_cpt',
        'hcpcs', 'cpt', 'ms_drg', 'msdrg', 'drg', 'rev_code',
        'revenue code', 'ndc',
    ]
    for hint in priority_hints:
        for i, col in enumerate(cols):
            lower = str(col).strip().lower()
            if 'code type' in lower:
                continue
            if kw_match(col, [hint]):
                return i
    for i, col in enumerate(cols):
        lower = str(col).strip().lower()
        if 'code type' in lower:
            continue
        if kw_match(col, CODE_COL_HINTS):
            return i
    return None


def first_gross_col(cols):
    exact = next((i for i, c in enumerate(cols) if kw_match(c, ['gross', 'charge_master', 'cdm_price', 'list_price', 'price', 'amount'])), None)
    if exact is not None:
        return exact
    return next((
        i for i, c in enumerate(cols)
        if kw_match(c, ['standard_charge', 'standard charge']) and not kw_match(c, CASH_COL_HINTS + MIN_COL_HINTS + MAX_COL_HINTS + NEGOTIATED_DOLLAR_HINTS)
    ), None)


def is_excluded_rate_col(col):
    return kw_match(col, RATE_EXCLUDE_HINTS)


def is_negotiated_dollar_col(col):
    lower = col.lower()
    if is_excluded_rate_col(col):
        return False
    if any(token in lower for token in ['percent', 'percentage', 'methodology', 'method']):
        return False
    return kw_match(col, NEGOTIATED_DOLLAR_HINTS) or (
        'negotiated' in lower and any(token in lower for token in ['dollar', 'rate', 'charge'])
    )


def is_wide_payer_col(col):
    return kw_match(col, WIDE_PAYER_HINTS) and not is_excluded_rate_col(col)


def detect_code_type(code):
    """Heuristic: 5-digit numeric = CPT; alphanumeric = HCPCS; 3-digit = DRG/REV."""
    if not code:
        return ''
    c = str(code).strip().upper()
    if re.fullmatch(r'\d{5}', c):
        return 'CPT'
    if re.fullmatch(r'[A-Z]\d{4}', c):
        return 'HCPCS'
    if re.fullmatch(r'\d{3}', c):
        return 'DRG'
    if re.fullmatch(r'\d{1,4}-\d{1,4}', c):
        return 'MS-DRG'
    if re.fullmatch(r'\d{11}', c):
        return 'NDC'
    if re.fullmatch(r'[A-Z]\d{2}(\.\d{1,4})?', c):
        return 'ICD-10'
    return 'CDM'


def normalize_code_and_type(raw_code, raw_type=''):
    code = str(raw_code or '').strip()
    code_type = canonicalize_code_type(raw_type) if raw_type else ''
    cleaned = code.replace('\ufffd', ' ').replace('\u00a0', ' ')
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    match = re.match(r'^(HCPCS|CPT|DRG|MS[- ]?DRG|REV|RC|NDC|ICD[- ]?10)\s*[:#-]?\s*([A-Z0-9.\-]+)$', cleaned, re.IGNORECASE)
    if match:
        extracted_type = canonicalize_code_type(match.group(1))
        extracted_code = match.group(2).strip()
        return extracted_code, extracted_type or code_type or detect_code_type(extracted_code)
    return code, code_type or detect_code_type(code)


def to_float(x):
    if x is None: return None
    s = str(x).strip().replace('$', '').replace(',', '')
    if s in ('', 'N/A', 'n/a', '-', 'null', 'None'):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def canonicalize_code_type(label):
    text = str(label or '').strip().upper().replace(' ', '').replace('_', '-')
    mapping = {
        'HCPCS': 'HCPCS',
        'CPT': 'CPT',
        'DRG': 'DRG',
        'MS-DRG': 'MS-DRG',
        'MSDRG': 'MS-DRG',
        'RC': 'REV',
        'REV': 'REV',
        'REVCODE': 'REV',
        'NDC': 'NDC',
        'ICD10': 'ICD-10',
        'ICD-10': 'ICD-10',
        'CDM': 'CDM',
        'CHARGECODE': 'CDM',
        'PROCEDURECODE': '',
        'BILLINGCODE': '',
        'CODE': '',
        'CPTHCPCS': '',
        'CPTHCPCSCODE': '',
    }
    return mapping.get(text, text)


def normalize_source_url(url):
    url = str(url or '').strip()
    if not url:
        return url
    match = URLDEFENSE_V3_RE.search(url)
    if match:
        url = match.group(1)
    return html_lib.unescape(url)


def extract_nested_code(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            code = str(inner or '').strip()
            if code and code.upper() not in ('N/A', 'NONE', 'NULL'):
                return code, canonicalize_code_type(key)
    if isinstance(value, list):
        for item in value:
            found = extract_nested_code(item)
            if found:
                return found
    return None


def row_scalar(row, keys=None, hints=None, exclude_hints=None):
    if keys:
        for key in keys:
            if key in row and not isinstance(row.get(key), (dict, list)):
                value = row.get(key)
                if value not in (None, ''):
                    return value
    hints = hints or []
    exclude_hints = exclude_hints or []
    for key, value in row.items():
        if isinstance(value, (dict, list)):
            continue
        if hints and not kw_match(key, hints):
            continue
        if exclude_hints and kw_match(key, exclude_hints):
            continue
        if value not in (None, ''):
            return value
    return None


def extract_json_row_code(row):
    # Prefer nested structured code fields like {" HCPCS": "10009"}.
    for key, value in row.items():
        if isinstance(value, (dict, list)):
            found = extract_nested_code(value)
            if found:
                return found
    preferred_keys = [
        'code', 'Code', 'billingCode', 'billing_code', 'Billing_Code',
        'hcpcs', 'HCPCS', 'cpt', 'CPT', 'drg', 'DRG',
        'MS DRG', 'MS_DRG', 'ms_drg', 'Rev Code', 'Revenue Code',
        'NDC', 'Charge Code',
    ]
    for key in preferred_keys:
        value = row.get(key)
        if isinstance(value, (dict, list)):
            found = extract_nested_code(value)
            if found:
                return found
        code = str(value or '').strip()
        if code and code.upper() not in ('N/A', 'NONE', 'NULL'):
            code_type = canonicalize_code_type(key)
            if code_type in ('', 'CODE'):
                code_type = ''
            code, code_type = normalize_code_and_type(code, code_type)
            return code, code_type
    return '', ''


def looks_like_payer_key(key):
    lower = str(key or '').lower()
    if kw_match(lower, CODE_COL_HINTS + DESC_COL_HINTS + GROSS_COL_HINTS + CASH_COL_HINTS + MIN_COL_HINTS + MAX_COL_HINTS + ['rev code', 'revenue code', 'ndc', 'mod']):
        return False
    return any(token in lower for token in [
        'medicare', 'medicaid', 'self pay', 'self-pay', 'aetna', 'anthem',
        'blue', 'cigna', 'humana', 'united', 'health plan', 'ppo', 'hmo',
        'pos', 'advantage', 'outpatient', 'inpatient', 'commercial', 'worker',
        'choice care', 'network', 'traditional', 'molina', 'plan',
    ])


def parse_csv_tall(content_bytes, max_rows=200_000):
    """CSV-tall: one row per (code, payer) — common modern format."""
    rows = read_delimited_rows(content_bytes, max_rows)
    if not rows:
        return []
    header_idx = find_header_row(rows)
    data_rows = rows[header_idx:]
    if not data_rows:
        return []
    header = data_rows[0]
    cols = [h.strip() for h in header]
    code_col = first_code_col(cols)
    code_type_col = first_col(cols, CODE_TYPE_HINTS)
    desc_col = first_col(cols, DESC_COL_HINTS)
    gross_col = first_gross_col(cols)
    cash_col = first_col(cols, CASH_COL_HINTS)
    min_col = first_col(cols, MIN_COL_HINTS)
    max_col = first_col(cols, MAX_COL_HINTS)
    setting_col = first_col(cols, SETTING_HINTS)
    billing_col = first_col(cols, BILLING_HINTS)
    payer_col = first_col(cols, PAYER_HINTS)
    plan_col = first_col(cols, PLAN_HINTS)
    rate_col = next((i for i, c in enumerate(cols) if is_negotiated_dollar_col(c)), None)
    items = {}
    row_count = 0
    for row in data_rows[1:]:
        row_count += 1
        if row_count > max_rows: break
        if not row: continue
        code = row_get(row, code_col)
        desc = row_get(row, desc_col)
        code, code_type = normalize_code_and_type(code, row_get(row, code_type_col))
        key = (code, desc)
        if key not in items:
            items[key] = {
                'code': code, 'code_type': code_type,
                'description': desc[:300],
                'setting': row_get(row, setting_col),
                'billing_class': row_get(row, billing_col),
                'gross_charge': to_float(row_get(row, gross_col)),
                'cash_discount': to_float(row_get(row, cash_col)),
                'min_negotiated': to_float(row_get(row, min_col)),
                'max_negotiated': to_float(row_get(row, max_col)),
                'payer_rates': [],
            }
        if payer_col is not None and rate_col is not None:
            payer = row_get(row, payer_col)
            plan = row_get(row, plan_col)
            rate = to_float(row_get(row, rate_col))
            if payer and rate is not None and rate > 0:
                items[key]['payer_rates'].append({
                    'payer': payer[:100],
                    'plan': plan[:100],
                    'rate_dollar': rate,
                })
    return list(items.values())


def parse_csv_wide(content_bytes, max_rows=200_000):
    """CSV-wide: each payer is a column."""
    rows = read_delimited_rows(content_bytes, max_rows)
    if not rows:
        return []
    header_idx = find_header_row(rows)
    data_rows = rows[header_idx:]
    if not data_rows:
        return []
    header = data_rows[0]
    cols = [h.strip() for h in header]
    code_col = first_code_col(cols)
    code_type_col = first_col(cols, CODE_TYPE_HINTS)
    desc_col = first_col(cols, DESC_COL_HINTS)
    gross_col = first_gross_col(cols)
    cash_col = first_col(cols, CASH_COL_HINTS)
    min_col = first_col(cols, MIN_COL_HINTS)
    max_col = first_col(cols, MAX_COL_HINTS)
    setting_col = first_col(cols, SETTING_HINTS)
    billing_col = first_col(cols, BILLING_HINTS)
    canonical_indices = {
        x for x in (
            code_col, desc_col, gross_col, cash_col, min_col, max_col,
            setting_col, billing_col,
        ) if x is not None
    }
    payer_cols = [i for i, c in enumerate(cols) if i not in canonical_indices and is_wide_payer_col(c)]
    items = []
    rc = 0
    for row in data_rows[1:]:
        rc += 1
        if rc > max_rows: break
        if not row or all(not c for c in row): continue
        code = row_get(row, code_col)
        desc = row_get(row, desc_col)
        code, code_type = normalize_code_and_type(code, row_get(row, code_type_col))
        if not code and not desc: continue
        item = {
            'code': code, 'code_type': code_type,
            'description': desc[:300],
            'setting': row_get(row, setting_col),
            'billing_class': row_get(row, billing_col),
            'gross_charge': to_float(row_get(row, gross_col)),
            'cash_discount': to_float(row_get(row, cash_col)),
            'min_negotiated': to_float(row_get(row, min_col)),
            'max_negotiated': to_float(row_get(row, max_col)),
            'payer_rates': [],
        }
        for pi in payer_cols:
            v = to_float(row_get(row, pi))
            if v is not None and v > 0:
                item['payer_rates'].append({'payer': cols[pi][:100], 'rate_dollar': v})
        if code or item['gross_charge'] is not None or item['payer_rates']:
            items.append(item)
    return items


def first_present(obj, *keys):
    for key in keys:
        if key in obj and obj.get(key) not in (None, '', [], {}):
            return obj.get(key)
    return None


def load_json_bytes(content_bytes):
    """Decode JSON bytes with a few pragmatic fallback encodings."""
    if FAST_JSON and SIMDJSON_PARSER is not None:
        try:
            return SIMDJSON_PARSER.parse(content_bytes, recursive=True)
        except Exception:
            pass
    try:
        return json.loads(content_bytes)
    except UnicodeDecodeError:
        pass
    except json.JSONDecodeError:
        return None
    for encoding in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            return json.loads(content_bytes.decode(encoding))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return None


def normalize_code_records(value):
    """Normalize assorted code-information shapes into dict records."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        text = str(value).strip()
        return [{'code': text, 'type': ''}] if text else []

    records = []
    for item in value:
        if isinstance(item, dict):
            records.append(item)
            continue
        if isinstance(item, list):
            code = str(item[0]).strip() if len(item) > 0 and item[0] is not None else ''
            code_type = str(item[1]).strip() if len(item) > 1 and item[1] is not None else ''
            if code or code_type:
                records.append({'code': code, 'type': code_type})
            continue
        text = str(item).strip()
        if text:
            records.append({'code': text, 'type': ''})
    return records


def flatten_top_level_json_rows(data):
    """Vendor JSON exports often group charge rows under arbitrary top-level keys."""
    rows = []
    for value in data.values():
        if isinstance(value, list):
            rows.extend(value)
            continue
        if not isinstance(value, dict):
            continue
        nested_lists = [inner for inner in value.values() if isinstance(inner, list)]
        if nested_lists:
            for nested in nested_lists:
                rows.extend(nested)
            continue
        rows.append(value)
    return rows


def iter_json_variant_rows(data):
    for entry in data:
        if isinstance(entry, list):
            yield from iter_json_variant_rows(entry)
            continue
        if not isinstance(entry, dict):
            continue
        rows = entry.get('item')
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    yield row
            continue
        yield entry


def merge_json_list_row(grouped, row):
    code, code_type = extract_json_row_code(row)
    desc = str(row_scalar(
        row,
        keys=['description', 'Description', 'service', 'Service', 'item_description', 'Item_Description'],
        hints=DESC_COL_HINTS,
    ) or '')
    key = (code, desc)
    if key not in grouped:
        gross_charge = to_float(row_scalar(
            row,
            keys=['gross_charge', 'Gross_Charge', 'Gross Charge'],
            hints=GROSS_COL_HINTS,
        ))
        cash_discount = to_float(row_scalar(
            row,
            keys=['cash_discount', 'Cash_Discount', 'Discounted Cash Price', 'Self Pay - Outpatient', 'Self Pay - Inpatient'],
            hints=['cash', 'self pay', 'self-pay', 'discounted cash'],
        ))
        grouped[key] = {
            'code': code,
            'code_type': code_type or detect_code_type(code),
            'description': desc[:300],
            'setting': str(row_scalar(row, keys=['setting', 'Setting'], hints=SETTING_HINTS) or ''),
            'billing_class': str(row_scalar(row, keys=['billing_class', 'Billing_Class'], hints=BILLING_HINTS) or ''),
            'gross_charge': gross_charge,
            'cash_discount': cash_discount,
            'min_negotiated': to_float(first_present(
                row, 'minimum', 'Minimum', 'Deidentified_Min_Allowed', 'DeIdentified_Min_Allowed', 'Min'
            )),
            'max_negotiated': to_float(first_present(
                row, 'maximum', 'Maximum', 'Deidentified_Max_Allowed', 'DeIdentified_Max_Allowed', 'Max'
            )),
            'payer_rates': [],
        }
    payer = str(first_present(row, 'payer', 'Payer', 'payer_name', 'Payer_Name') or '')[:100]
    plan = str(first_present(row, 'plan', 'Plan', 'plan_name', 'Plan_Name') or '')[:100]
    rate = to_float(first_present(
        row,
        'standard_charge_dollar', 'Standard_Charge_Dollar',
        'standard_charge', 'Standard_Charge',
        'negotiated_rate', 'Negotiated_Rate',
        'payer_rate', 'Payer_Rate', 'Rate',
    ))
    if rate is None and payer:
        min_v = grouped[key]['min_negotiated']
        max_v = grouped[key]['max_negotiated']
        if min_v is not None and max_v is not None and min_v == max_v:
            rate = min_v
    if payer and rate is not None and rate > 0:
        grouped[key]['payer_rates'].append({
            'payer': payer,
            'plan': plan,
            'rate_dollar': rate,
        })
    elif not payer:
        for payer_key, payer_value in row.items():
            if not looks_like_payer_key(payer_key):
                continue
            if isinstance(payer_value, dict):
                for plan_name, plan_value in payer_value.items():
                    plan_rate = to_float(plan_value)
                    if plan_rate is not None and plan_rate > 0:
                        grouped[key]['payer_rates'].append({
                            'payer': str(payer_key)[:100],
                            'plan': str(plan_name)[:100],
                            'rate_dollar': plan_rate,
                        })
            else:
                payer_rate = to_float(payer_value)
                if payer_rate is not None and payer_rate > 0:
                    grouped[key]['payer_rates'].append({
                        'payer': str(payer_key)[:100],
                        'plan': '',
                        'rate_dollar': payer_rate,
                    })


def parse_json_list_variant(data, max_items=200_000):
    """Handle list-shaped JSON variants such as HPI/CDM exports."""
    grouped = {}
    seen = 0
    for row in iter_json_variant_rows(data):
        seen += 1
        if seen > max_items:
            break
        merge_json_list_row(grouped, row)
    return list(grouped.values())


def parse_json_v2(content_bytes, max_items=200_000):
    """CMS HPT v2.0 JSON schema."""
    data = load_json_bytes(content_bytes)
    if data is None:
        return []
    if isinstance(data, list):
        return parse_json_list_variant(data, max_items=max_items)
    if not isinstance(data, dict):
        return []
    sci = data.get('standard_charge_information', []) or data.get('standard_charges', [])
    if sci:
        items = []
        for entry in sci[:max_items]:
            items.extend(expand_json_v2_entry(entry))
        return items

    fallback_rows = flatten_top_level_json_rows(data)
    if fallback_rows:
        return parse_json_list_variant(fallback_rows, max_items=max_items)
    return []


def expand_json_v2_entry(entry):
    if not isinstance(entry, dict):
        return []
    codes = normalize_code_records(entry.get('code_information', []))
    if not codes and entry.get('code') is not None:
        codes = normalize_code_records([{
            'code': entry['code'],
            'type': entry.get('code_type', ''),
        }])
    code_record = codes[0] if codes else {}
    code = str(code_record.get('code', '') or code_record.get('billing_code', '') or '')
    code_type = str(
        code_record.get('type', '')
        or code_record.get('code_type', '')
        or code_record.get('billing_code_type', '')
        or ''
    )
    code, code_type = normalize_code_and_type(code, code_type)
    desc = entry.get('description', '') or entry.get('billing_code_description', '')
    charges_list = entry.get('standard_charges', [])
    if not isinstance(charges_list, list):
        charges_list = [charges_list] if charges_list else []
    items = []
    for sc in charges_list:
        if not isinstance(sc, dict):
            continue
        payers = []
        for p in (sc.get('payers_information', []) or sc.get('payer_specific', []) or []):
            if not isinstance(p, dict):
                continue
            payers.append({
                'payer': str(p.get('payer_name', '') or p.get('payer', ''))[:100],
                'plan': str(p.get('plan_name', '') or p.get('plan', ''))[:100],
                'rate_dollar': to_float(p.get('standard_charge_dollar') or p.get('standard_charge')),
                'methodology': str(p.get('methodology', ''))[:80],
            })
        items.append({
            'code': str(code), 'code_type': str(code_type),
            'description': str(desc)[:300],
            'setting': str(sc.get('setting', '')),
            'billing_class': str(sc.get('billing_class', '')),
            'gross_charge': to_float(sc.get('gross_charge')),
            'cash_discount': to_float(sc.get('discounted_cash')),
            'min_negotiated': to_float(sc.get('minimum')),
            'max_negotiated': to_float(sc.get('maximum')),
            'payer_rates': payers,
        })
    return items


def parse_xlsx(content_bytes, max_rows=200_000):
    """Convert each XLSX sheet to CSV; parse and concatenate items.

    Many hospital XLSX MRFs are multi-sheet: cover/index sheet + per-category
    sheets (procedures, drugs, supplies, DRGs). The cover sheet alone has no
    codes. We must walk all sheets and pick the ones that yield real items.
    """
    try:
        from openpyxl import load_workbook
    except ImportError:
        return []
    items = []
    try:
        wb = load_workbook(io.BytesIO(content_bytes), read_only=True, data_only=True)
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            buf = io.StringIO()
            w = csv.writer(buf)
            n = 0
            for row in ws.iter_rows(values_only=True):
                w.writerow(['' if c is None else c for c in row])
                n += 1
                if n > max_rows:
                    break
            csv_bytes = buf.getvalue().encode('utf-8')
            # Prefer tall if a "payer" column is present in this sheet
            preview = buf.getvalue()[:8192].lower()
            if 'payer_name' in preview or 'payer ' in preview or 'negotiated_rate' in preview:
                items.extend(parse_csv_tall(csv_bytes, max_rows=max_rows))
            else:
                items.extend(parse_csv_wide(csv_bytes, max_rows=max_rows))
            if len(items) > max_rows:
                break
        # Filter out empty rows (no code AND no real description)
        items = [i for i in items if i.get('code') or (i.get('gross_charge') is not None)]
        return items
    except Exception:
        return []


def parse_xls_legacy(content_bytes, max_rows=200_000):
    """Parse legacy BIFF .xls workbooks when xlrd is available."""
    if xlrd is None:
        return []
    items = []
    try:
        wb = xlrd.open_workbook(file_contents=content_bytes)
        for sheet in wb.sheets():
            buf = io.StringIO()
            writer = csv.writer(buf)
            row_cap = min(sheet.nrows, max_rows)
            for row_idx in range(row_cap):
                row = []
                for cell in sheet.row_values(row_idx):
                    if cell is None:
                        row.append('')
                    else:
                        row.append(str(cell))
                writer.writerow(row)
            csv_bytes = buf.getvalue().encode('utf-8')
            preview = buf.getvalue()[:8192].lower()
            if 'payer_name' in preview or 'payer ' in preview or 'negotiated_rate' in preview:
                items.extend(parse_csv_tall(csv_bytes, max_rows=max_rows))
            else:
                items.extend(parse_csv_wide(csv_bytes, max_rows=max_rows))
            if len(items) > max_rows:
                break
        items = [item for item in items if item.get('code') or (item.get('gross_charge') is not None)]
        return items
    except Exception:
        return []


# SpreadsheetML 2003 ("Excel XML") namespace. Henry Ford and Intermountain ship
# their MRFs as a SpreadsheetML .xml file (often inside a .zip / .ashx). It is
# NOT an OOXML .xlsx — openpyxl cannot read it — and the Intermountain file is
# ~650 MB, so it must be streamed with iterparse rather than loaded as a tree.
_SSML_NS = '{urn:schemas-microsoft-com:office:spreadsheet}'


def looks_like_spreadsheetml(content_bytes):
    head = content_bytes[:4096]
    if head.startswith(b'\xef\xbb\xbf'):
        head = head[3:]
    head = head.lstrip()
    if not head.startswith(b'<?xml') and not head.startswith(b'<Workbook'):
        return False
    probe = content_bytes[:8192]
    return (b'urn:schemas-microsoft-com:office:spreadsheet' in probe
            or b'mso-application' in probe)


def parse_spreadsheetml(content_bytes, max_rows=200_000):
    """Parse a SpreadsheetML 2003 (.xml) workbook by streaming rows.

    Each <Worksheet> is converted to CSV (honoring sparse <Cell ss:Index="N">)
    and routed through parse_csv_wide / parse_csv_tall, mirroring parse_xlsx.
    """
    try:
        import xml.etree.ElementTree as ET
    except ImportError:
        return []
    cell_tag = _SSML_NS + 'Cell'
    row_tag = _SSML_NS + 'Row'
    data_tag = _SSML_NS + 'Data'
    ws_tag = _SSML_NS + 'Worksheet'
    index_attr = _SSML_NS + 'Index'

    items = []
    try:
        sheets = []  # list of list-of-rows
        current_rows = None
        col = 0
        cur_row = None
        source = io.BytesIO(content_bytes)
        for event, elem in ET.iterparse(source, events=('start', 'end')):
            if event == 'start':
                if elem.tag == ws_tag:
                    current_rows = []
                elif elem.tag == row_tag and current_rows is not None:
                    cur_row = []
                    col = 0
                elif elem.tag == cell_tag and cur_row is not None:
                    idx = elem.get(index_attr)
                    if idx:
                        try:
                            target = int(idx) - 1
                            while col < target:
                                cur_row.append('')
                                col += 1
                        except ValueError:
                            pass
                continue
            # end events
            if elem.tag == data_tag and cur_row is not None:
                cur_row.append(elem.text or '')
                col += 1
            elif elem.tag == cell_tag and cur_row is not None:
                # a <Cell/> with no <Data> child still occupies a column
                # (handled: Data appends; empty cell appends nothing here but
                # the next indexed cell pads). Pad bare empty cells.
                pass
            elif elem.tag == row_tag and current_rows is not None and cur_row is not None:
                current_rows.append(cur_row)
                cur_row = None
                if len(current_rows) > max_rows:
                    elem.clear()
                    break
                elem.clear()
            elif elem.tag == ws_tag and current_rows is not None:
                if current_rows:
                    sheets.append(current_rows)
                current_rows = None
                elem.clear()
            else:
                elem.clear()

        for rows in sheets:
            if not rows:
                continue
            buf = io.StringIO()
            w = csv.writer(buf)
            for r in rows[:max_rows]:
                w.writerow(r)
            csv_bytes = buf.getvalue().encode('utf-8')
            preview = buf.getvalue()[:8192].lower()
            if 'payer_name' in preview or 'payer ' in preview or 'negotiated_rate' in preview:
                items.extend(parse_csv_tall(csv_bytes, max_rows=max_rows))
            else:
                items.extend(parse_csv_wide(csv_bytes, max_rows=max_rows))
            if len(items) > max_rows:
                break
        items = [i for i in items if i.get('code') or (i.get('gross_charge') is not None)]
        return items
    except Exception:
        return []


def detect_format(content_bytes, url):
    """Return (format_str, normalized_bytes_or_inner_zip)."""
    # Strip leading whitespace / BOM. Some hospital JSON files are served behind
    # redirect wrappers and arrive with a UTF-8 BOM, so raw byte-prefix checks
    # need to normalize that before falling back to CSV.
    head = content_bytes[:256]
    if head.startswith(b'\xef\xbb\xbf'):
        head = head[3:]
    head = head.lstrip()
    url_lower = normalize_source_url(url).lower()
    # JSON detection
    if head.startswith(b'{') or head.startswith(b'['):
        return 'json', content_bytes
    # XLSX signature inside the zip-magic block (PK + 'xl/' folder marker)
    if content_bytes[:4] == b'PK\x03\x04':
        # Cheap peek: openpyxl-style XLSX has "xl/workbook.xml" near the start
        if b'xl/workbook.xml' in content_bytes[:16384] or b'word/document.xml' in content_bytes[:16384]:
            return 'xlsx', content_bytes
        return 'zip', content_bytes
    # GZ
    if content_bytes[:2] == b'\x1f\x8b':
        try:
            inner = gzip.decompress(content_bytes)
            return detect_format(inner, url)
        except OSError:
            pass
    # XLS legacy
    if content_bytes[:4] == b'\xd0\xcf\x11\xe0':
        return 'xls-legacy', content_bytes
    # SpreadsheetML 2003 (.xml) — must be checked before the HTML heuristic,
    # since an XML document trips looks_like_html's tag detection.
    if looks_like_spreadsheetml(content_bytes):
        return 'spreadsheetml', content_bytes
    if looks_like_html(content_bytes):
        return 'html', content_bytes
    # Some hospitals serve CSV/TSV from a .json URL; prefer the content shape
    # over the path suffix when the bytes clearly look tabular.
    if looks_like_delimited_text(content_bytes):
        return 'csv', content_bytes
    # Fall back to the URL hint only after content sniffing. Some hospitals
    # serve real CSV bodies from .xlsx/.xls-looking URLs.
    if '.xlsx' in url_lower or '.xls' in url_lower:
        return 'xlsx', content_bytes
    # JSON hint by URL
    if '.json' in url_lower:
        return 'json', content_bytes
    # Default: assume CSV
    return 'csv', content_bytes


def parse(content_bytes, url, hospital_name='', ccn=''):
    fmt, content = detect_format(content_bytes, url)
    if fmt == 'zip':
        try:
            zf = zipfile.ZipFile(io.BytesIO(content))
            members = [m for m in zf.namelist() if not m.endswith('/')]
            scored = []
            for member in members:
                try:
                    file_size = zf.getinfo(member).file_size
                except KeyError:
                    continue
                member_score = score_zip_member(member, file_size, hospital_name=hospital_name, ccn=ccn)
                if member_score is None:
                    continue
                token_hits, score = member_score
                scored.append((token_hits, score, file_size, member))
            scored.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
            if len(members) > 10 and scored and scored[0][0] == 0:
                return 'zip', []
            selected = [member for _, _, _, member in scored[:12]] if scored else []
            if not selected:
                members.sort(key=lambda m: zf.getinfo(m).file_size, reverse=True)
                selected = members[:3]
            for m in selected:
                inner = zf.read(m)
                inner_fmt, inner_b = detect_format(inner, m)
                if inner_fmt in ('csv', 'json', 'xlsx', 'spreadsheetml', 'xls-legacy'):
                    return inner_fmt, parse_inner(inner_fmt, inner_b)
            return 'zip', []
        except Exception:
            return 'zip-error', []
    return fmt, parse_inner(fmt, content)


def parse_inner(fmt, content_bytes):
    if fmt == 'json':
        return parse_json_v2(content_bytes)
    if fmt == 'xlsx':
        return parse_xlsx(content_bytes)
    if fmt == 'xls-legacy':
        return parse_xls_legacy(content_bytes)
    if fmt == 'spreadsheetml':
        return parse_spreadsheetml(content_bytes)
    if fmt == 'csv':
        # Decide tall vs wide: tall has "payer" or "negotiated_rate" column, wide doesn't
        try:
            text = content_bytes[:8192].decode('utf-8', errors='replace').lower()
            if 'payer_name' in text or 'payer ' in text or 'negotiated_rate' in text:
                return parse_csv_tall(content_bytes)
        except Exception:
            pass
        return parse_csv_wide(content_bytes)
    if fmt == 'html':
        return []
    return []


def looks_like_html(raw, content_type=''):
    ctype = (content_type or '').lower()
    head = raw[:1024].lstrip().lower()
    return (
        'text/html' in ctype
        or head.startswith(b'<!doctype html')
        or head.startswith(b'<html')
        or b'<html' in head[:512]
    )


def hospital_hint_tokens(name):
    tokens = []
    for raw in str(name or '').lower().replace("'", ' ').replace('-', ' ').split():
        token = ''.join(ch for ch in raw if ch.isalnum())
        if len(token) < 4 or token in GENERIC_NAME_TOKENS:
            continue
        tokens.append(token)
    return tokens


def score_candidate_url(url):
    lower = normalize_source_url(url).lower()
    score = 0
    if DIRECT_FILE_RE.search(lower):
        score += 10
    if FILE_HINT_RE.search(lower):
        score += 4
    if 'drive.google.com/uc?' in lower or 'export=download' in lower:
        score += 6
    if '/download' in lower:
        score += 2
    if '/view' in lower and 'drive.google.com/file/d/' in lower:
        score -= 2
    return score


def score_zip_member(member_name, file_size, hospital_name='', ccn=''):
    lower = normalize_source_url(member_name).lower()
    filename = lower.rsplit('/', 1)[-1]
    if filename.endswith(('.pdf', '.xml', '.txt', '.doc', '.docx')):
        return None
    if not DIRECT_FILE_RE.search(lower):
        return None
    score = 0
    if FILE_HINT_RE.search(lower):
        score += 20
    if filename.endswith(('.csv', '.json', '.xlsx', '.xls')):
        score += 12
    if filename.endswith('.zip'):
        score += 4
    token_hits = 0
    for token in hospital_hint_tokens(hospital_name):
        if token in lower:
            token_hits += 1
    score += token_hits * 15
    if ccn and ccn in lower:
        score += 20
    if file_size > 0:
        score += min(file_size / (1024 * 1024), 40)
    return (token_hits, score)


def extract_hub_candidates(raw, base_url):
    text = raw.decode('utf-8', errors='replace')
    candidates = {}

    # Google Drive viewer pages can be converted into direct-download URLs.
    for source in (base_url, text):
        for match in GDRIVE_VIEW_RE.finditer(source):
            file_id = match.group(1)
            candidate = f'https://drive.google.com/uc?export=download&id={file_id}'
            candidates[candidate] = max(candidates.get(candidate, 0), score_candidate_url(candidate))

    for href in HREF_RE.findall(text):
        href = html_lib.unescape(href.strip())
        if not href or href.startswith(('javascript:', 'mailto:', '#')):
            continue
        full = normalize_source_url(urljoin(base_url, href))
        score = score_candidate_url(full)
        if score > 0:
            candidates[full] = max(candidates.get(full, 0), score)

    return [url for url, _ in sorted(candidates.items(), key=lambda item: (-item[1], item[0]))]


def curl_fetch(url, max_bytes):
    url = normalize_source_url(url)
    with tempfile.NamedTemporaryFile(delete=False) as body_file:
        body_path = body_file.name
    try:
        proc = subprocess.run(
            [
                'curl',
                '-fsSL',
                '--max-filesize',
                str(max_bytes),
                '-o',
                body_path,
                '-w',
                '%{url_effective}\n%{content_type}',
                url,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or '').strip()
            raise RuntimeError(f'curl:{proc.returncode}:{detail[:120]}')
        meta = (proc.stdout or '').splitlines()
        final_url = meta[0].strip() if meta else url
        content_type = meta[1].strip() if len(meta) > 1 else ''
        with open(body_path, 'rb') as f:
            raw = f.read()
        return raw, final_url, content_type, False
    finally:
        try:
            os.remove(body_path)
        except FileNotFoundError:
            pass


# Large JSON MRFs are parsed incrementally with ijson rather than buffered
# into memory and handed to json.load(). json.load() of a 280MB-1GB document
# OOMs or gets OOM-killed; the streaming path holds only one entry at a time.
# Files at or below this size still take the fast in-memory path.
JSON_STREAM_THRESHOLD = 50 * 1024 * 1024
# CSV files larger than this are parsed row-by-row over a streamed download
# instead of being buffered+decoded in memory (the buffered path would OOM on a
# multi-GB file). CSV_STREAM_HARD_CAP bounds the streamed read so a runaway /
# mislabelled file cannot hang the ingest indefinitely.
CSV_STREAM_THRESHOLD = 100 * 1024 * 1024
CSV_STREAM_HARD_CAP = 2560 * 1024 * 1024  # 2.5 GB


def _curl_head_size(url):
    """Return the Content-Length of a URL via a curl HEAD, or None.

    Used when httpx is WAF-blocked: curl's TLS fingerprint is often allowed
    where httpx's is not, so a curl HEAD can still reveal the file size and
    let fetch() decide whether to route to the streaming CSV parser.
    """
    url = normalize_source_url(url)
    try:
        proc = subprocess.run(
            ['curl', '-fsSLI', '-A', UA, url],
            text=True, capture_output=True, check=False, timeout=60,
        )
        if proc.returncode != 0:
            return None
        # With -L there may be several header blocks (redirects); the last
        # Content-Length wins.
        size = None
        for line in (proc.stdout or '').splitlines():
            low = line.lower()
            if low.startswith('content-length:'):
                try:
                    size = int(line.split(':', 1)[1].strip())
                except ValueError:
                    pass
        return size
    except Exception:
        return None


def fetch(url, max_bytes=500 * 1024 * 1024):
    """Stream-download MRF, cap at 500MB.

    For JSON files larger than JSON_STREAM_THRESHOLD (50MB) the body is NOT
    buffered — fetch() returns (b'', final_url, content_type, True) so that
    ingest_one() routes to stream_parse_large_json(), which re-opens the URL
    and parses it incrementally with ijson. This avoids OOM on 280MB-1GB MRFs.
    """
    url = normalize_source_url(url)
    try:
        with httpx.Client(headers={'User-Agent': UA}, follow_redirects=True, timeout=60.0, verify=False) as client:
            with client.stream('GET', url) as r:
                r.raise_for_status()
                content_type = r.headers.get('content-type', '')
                content_length = int(r.headers.get('content-length') or 0)
                final_url = str(r.url)
                is_json = '.json' in final_url.lower() or 'json' in content_type.lower()
                ct_lower = content_type.lower()
                is_csv = (
                    not is_json
                    and ('.csv' in final_url.lower()
                         or 'text/csv' in ct_lower
                         or 'application/csv' in ct_lower)
                )
                # Peek the body's first bytes: some hospital MRFs are served
                # with a generic content-type (application/octet-stream) from
                # an extension-less URL, so neither the URL nor the header
                # reveals that it is JSON. A leading '{' / '[' upgrades the
                # routing so a large JSON file goes to the streaming parser
                # instead of being truncated at max_bytes.
                chunk_iter = r.iter_bytes()
                first_chunk = b''
                if not is_json:
                    for first_chunk in chunk_iter:
                        if first_chunk.strip():
                            break
                    head = first_chunk.lstrip()[:1]
                    if head in (b'{', b'['):
                        is_json = True
                        is_csv = False
                        # Ensure downstream routing (ingest_one) recognizes
                        # this as JSON even though the server's content-type
                        # was generic — append a json marker.
                        if 'json' not in content_type.lower():
                            content_type = (content_type + '; x-detected=json').strip('; ')
                # Route hard-over-cap JSON, or merely-large JSON, to the
                # streaming parser. ijson is required for the streaming path;
                # without it, only the >max_bytes case can be skipped (a
                # too-large file would OOM anyway), and 50-500MB JSON falls
                # through to the buffered path as before.
                if is_json and content_length > max_bytes:
                    return b'', final_url, content_type, True
                if is_json and ijson is not None and content_length > JSON_STREAM_THRESHOLD:
                    return b'', final_url, content_type, True
                # Large CSV: a multi-hundred-MB / multi-GB CSV (e.g. the 1.99 GB
                # Munson v3 file) cannot be buffered + decoded in memory. Route
                # it to stream_parse_large_csv(), which re-opens the URL and
                # parses it row-by-row with csv.reader over a streamed body.
                if is_csv and content_length > CSV_STREAM_THRESHOLD:
                    return b'', final_url, content_type, True
                buf = io.BytesIO()
                total = 0
                truncated = False
                if first_chunk:
                    buf.write(first_chunk)
                    total += len(first_chunk)
                for chunk in chunk_iter:
                    buf.write(chunk)
                    total += len(chunk)
                    # No-Content-Length fallback: a JSON body that crosses the
                    # streaming threshold mid-download is abandoned and routed
                    # to the streaming parser instead of being json.load()'d.
                    if (
                        is_json
                        and ijson is not None
                        and content_length <= JSON_STREAM_THRESHOLD
                        and total > JSON_STREAM_THRESHOLD
                    ):
                        return b'', final_url, content_type, True
                    # No-Content-Length CSV that grows past the streaming
                    # threshold mid-download: abandon the buffer and route to
                    # the streaming CSV parser.
                    if (
                        is_csv
                        and content_length <= CSV_STREAM_THRESHOLD
                        and total > CSV_STREAM_THRESHOLD
                    ):
                        return b'', final_url, content_type, True
                    if total > max_bytes:
                        truncated = True
                        break
                raw = buf.getvalue()
                if DIRECT_FILE_RE.search(url) and looks_like_html(raw, content_type):
                    try:
                        return curl_fetch(url, max_bytes)
                    except Exception:
                        pass
                return raw, final_url, content_type, truncated
    except Exception as httpx_error:
        # httpx WAF-blocked on a CSV (some hospital CDNs 403 httpx's TLS/HTTP2
        # fingerprint but allow curl). If a curl HEAD shows the file is large,
        # route to the streaming CSV parser instead of curl_fetch — curl_fetch
        # buffers the whole body and would reject / OOM a multi-GB file.
        if '.csv' in url.lower():
            size = _curl_head_size(url)
            if size is not None and size > CSV_STREAM_THRESHOLD:
                return b'', url, 'text/csv', True
        if DIRECT_FILE_RE.search(url) or '.ashx' in url.lower():
            try:
                return curl_fetch(url, max_bytes)
            except Exception:
                pass
        raise httpx_error


class IterStream:
    """Minimal read() wrapper over an iterator of bytes chunks for ijson."""

    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self._buffer = bytearray()

    def read(self, n=-1):
        if n is None or n < 0:
            out = bytes(self._buffer)
            self._buffer.clear()
            for chunk in self._chunks:
                out += chunk
            return out
        while len(self._buffer) < n:
            try:
                self._buffer.extend(next(self._chunks))
            except StopIteration:
                break
        out = bytes(self._buffer[:n])
        del self._buffer[:n]
        return out


def sanitize_utf8_chunks(chunks):
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    for chunk in chunks:
        text = decoder.decode(chunk)
        if text:
            yield text.encode('utf-8')
    tail = decoder.decode(b'', final=True)
    if tail:
        yield tail.encode('utf-8')


# Bytes that are legal JSON whitespace OUTSIDE a string token.
_JSON_WS = frozenset(b' \t\n\r')


def sanitize_json_control_chars(chunks):
    """Escape raw control characters (0x00-0x1F) that appear inside JSON string
    values. Strict JSON forbids unescaped control chars inside strings, but
    several hospital MRF exports embed raw TAB/CR/LF (and occasionally NUL) in
    description fields — strict parsers (and ijson) reject the whole file.

    A single boolean tracks string state across the entire byte stream by
    toggling on each unescaped double-quote. Control chars seen inside a string
    are replaced with their \\uXXXX escape; outside a string they are left as-is
    (structural whitespace). Operates on UTF-8 bytes already normalized by
    sanitize_utf8_chunks, so multi-byte sequences are intact.
    """
    in_string = False
    escaped = False
    for chunk in chunks:
        out = bytearray()
        for b in chunk:
            if in_string:
                if escaped:
                    out.append(b)
                    escaped = False
                    continue
                if b == 0x5C:  # backslash
                    out.append(b)
                    escaped = True
                    continue
                if b == 0x22:  # closing quote
                    out.append(b)
                    in_string = False
                    continue
                if b < 0x20:  # raw control char inside string -> escape
                    out.extend(b'\\u%04x' % b)
                    continue
                out.append(b)
            else:
                if b == 0x22:  # opening quote
                    out.append(b)
                    in_string = True
                    continue
                out.append(b)
        if out:
            yield bytes(out)


def stream_parse_large_json(url, max_items=200_000):
    """Incrementally parse huge v2/v3 JSON files without loading them fully."""
    if ijson is None:
        return None
    url = normalize_source_url(url)
    with httpx.Client(headers={'User-Agent': UA}, follow_redirects=True, timeout=120.0, verify=False) as client:
        with client.stream('GET', url) as r:
            r.raise_for_status()
            chunks = r.iter_bytes()
            preview_parts = []
            preview_size = 0
            while preview_size < 4096:
                try:
                    chunk = next(chunks)
                except StopIteration:
                    break
                preview_parts.append(chunk)
                preview_size += len(chunk)
            preview = b''.join(preview_parts)
            compact = re.sub(rb'\s+', b'', preview[:256])
            # Prefix selection for ijson.items():
            #   '[['               -> nested array of rows               -> item.item
            #   '[{...},[...'      -> [metadata-obj, [rows]] (AdventHealth) -> item.item
            #   '[{"Code"...'      -> flat array of row objects           -> item
            #   '{"standard_..."'  -> CMS HPT v2.0 object                 -> standard_charge_information.item
            # The AdventHealth v3-style export wraps the row array behind one or
            # more leading objects: [ {}, [ {row}, {row}, ... ] ]. ijson with the
            # 'item' prefix would try to materialize that whole nested array as a
            # single Python object (OOM) and raises UnexpectedSymbol; 'item.item'
            # descends one level and streams the rows.
            # NDJSON / object-stream detection: some vendor exports (e.g. the
            # 'moad-outputs' template) are a stream of flat row objects, one
            # per line, NOT wrapped in an array and NOT a CMS HPT object. They
            # begin with '{' but carry no standard_charge_information /
            # standard_charges / hospital_name key in the first 4 KB. Parsed
            # with multiple_values=True at the root prefix '', each top-level
            # object becomes one row.
            head_keys = preview[:4096].lower()
            looks_like_hpt_object = (
                b'standard_charge_information' in head_keys
                or b'standard_charges' in head_keys
                or b'hospital_name' in head_keys
            )
            # Some vendor wrappers (e.g. Panacea's HHSC export) put the row
            # array under a non-standard top-level key:
            #   {"TitleBlock":[...], "MRF":[ {row}, {row}, ... ]}
            # Detect a known row-array key and stream rows from it. Match
            # against the whitespace-stripped first 4 KB so pretty-printed
            # JSON ('"MRF": [') is still recognized.
            compact_head = re.sub(rb'\s+', b'', preview[:4096]).lower()
            wrapper_key = None
            wrapper_map = {
                b'"mrf":[{': 'MRF', b'"charges":[{': 'charges',
                b'"items":[{': 'items', b'"data":[{': 'data',
                b'"rows":[{': 'rows', b'"records":[{': 'records',
            }
            for cand, key in wrapper_map.items():
                if cand in compact_head:
                    wrapper_key = key
                    break
            nested_after_obj = re.match(rb'\[\{.*?\},\[', compact, re.DOTALL) is not None
            if compact.startswith(b'[['):
                prefix = 'item.item'
            elif nested_after_obj:
                prefix = 'item.item'
            elif compact.startswith(b'[{'):
                prefix = 'item'
            elif compact.startswith(b'{') and not looks_like_hpt_object and wrapper_key:
                prefix = f'{wrapper_key}.item'
            elif compact.startswith(b'{') and not looks_like_hpt_object:
                prefix = ''
            else:
                prefix = 'standard_charge_information.item'

            combined_chunks = itertools.chain([preview], chunks)
            stream = IterStream(
                sanitize_json_control_chars(sanitize_utf8_chunks(combined_chunks))
            )
            seen_entries = 0
            # ijson raises JSONError ("Additional data found") or
            # IncompleteJSONError once the root value ends but trailing bytes
            # remain (a stray second object, a duplicated newline, etc). When
            # that happens AFTER we have already streamed real entries, the
            # parse is effectively complete — keep what we collected instead of
            # discarding the whole file.
            # multiple_values=True lets ijson stream a sequence of concatenated
            # JSON documents (a handful of hospital MRFs duplicate the whole
            # file, or append a second per-location document) instead of
            # raising "trailing garbage" / "Additional data found" after the
            # first root. For a normal single-document file it is a no-op.
            if prefix == 'standard_charge_information.item':
                items = []
                try:
                    for entry in ijson.items(stream, prefix, multiple_values=True):
                        seen_entries += 1
                        items.extend(expand_json_v2_entry(entry))
                        if seen_entries >= max_items:
                            break
                except (ijson.JSONError, ijson.IncompleteJSONError):
                    if not seen_entries:
                        raise
                if seen_entries:
                    return items, str(r.url), r.headers.get('content-type', '')
                return None

            grouped = {}
            try:
                for entry in ijson.items(stream, prefix, multiple_values=True):
                    if not isinstance(entry, dict):
                        continue
                    seen_entries += 1
                    merge_json_list_row(grouped, entry)
                    if seen_entries >= max_items:
                        break
            except (ijson.JSONError, ijson.IncompleteJSONError):
                if not seen_entries:
                    raise
            if seen_entries:
                return list(grouped.values()), str(r.url), r.headers.get('content-type', '')
    return None


def _curl_range_download(url, tmp_path, total_size, slice_bytes=48 * 1024 * 1024):
    """Download a URL to tmp_path in HTTP Range slices via curl.

    Some hospital WAFs allow short Range requests but 403 a sustained full-file
    GET. Requires the server to honour `accept-ranges: bytes`. Each slice is
    appended to tmp_path; a slice failure aborts the whole download.
    """
    written = 0
    with open(tmp_path, 'wb') as fh:
        start = 0
        while start < total_size:
            end = min(start + slice_bytes - 1, total_size - 1)
            proc = subprocess.run(
                ['curl', '-fsS', '-r', f'{start}-{end}', '-A', UA, '--', url],
                capture_output=True, check=False, timeout=300,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or b'').decode('utf-8', 'replace').strip()
                raise RuntimeError(f'curl-range:{proc.returncode}:{detail[:120]}')
            fh.write(proc.stdout)
            written += len(proc.stdout)
            start = end + 1
            if written > CSV_STREAM_HARD_CAP:
                break
    return written


def _curl_stream_to_tempfile(url):
    """Download a URL to a temp file with curl (streamed to disk).

    Used when httpx is WAF-blocked (some hospital CDNs 403 httpx's TLS/HTTP2
    fingerprint but allow curl). If a sustained full GET is also 403'd but the
    server honours Range requests, falls back to a slice-by-slice download.
    Returns the temp path and final content-type; caller deletes the file.
    """
    url = normalize_source_url(url)
    with tempfile.NamedTemporaryFile(suffix='.csv', delete=False) as tf:
        tmp_path = tf.name
    proc = subprocess.run(
        [
            'curl', '-fsSL',
            '--max-filesize', str(CSV_STREAM_HARD_CAP),
            '-A', UA,
            '-o', tmp_path,
            '-w', '%{content_type}',
            url,
        ],
        text=True, capture_output=True, check=False,
    )
    if proc.returncode == 0:
        return tmp_path, (proc.stdout or '').strip()
    # Full GET failed — if the file is range-friendly, slice-download it.
    detail = (proc.stderr or proc.stdout or '').strip()
    size = _curl_head_size(url)
    if size and size > 0:
        try:
            _curl_range_download(url, tmp_path, size)
            return tmp_path, 'text/csv'
        except Exception:
            pass
    try:
        os.remove(tmp_path)
    except FileNotFoundError:
        pass
    raise RuntimeError(f'curl:{proc.returncode}:{detail[:120]}')


def stream_parse_large_csv(url, max_rows=200_000):
    """Incrementally parse a very large CSV (>100 MB) without buffering it.

    Streams the download, decodes UTF-8 incrementally, and feeds csv.reader a
    line iterator. The first window of rows is scanned for the real header
    (CMS HPT v3 CSVs prepend a hospital-metadata header+value pair); remaining
    rows are processed one at a time. Routes to the tall or wide row builder
    based on the detected header, mirroring parse_inner's CSV dispatch.

    If httpx is WAF-blocked (HTTP 403/406/429), falls back to a curl download
    to a temp file and parses that — curl's TLS fingerprint is often allowed
    where httpx's is not.

    Returns (items, final_url, content_type) or None.
    """
    url = normalize_source_url(url)
    httpx_client = None
    httpx_response = None
    httpx_ctx = None
    tmp_path = None
    final_url = url
    content_type = ''
    try:
        try:
            httpx_client = httpx.Client(
                headers={'User-Agent': UA}, follow_redirects=True,
                timeout=120.0, verify=False)
            httpx_ctx = httpx_client.stream('GET', url)
            httpx_response = httpx_ctx.__enter__()
            httpx_response.raise_for_status()
            final_url = str(httpx_response.url)
            content_type = httpx_response.headers.get('content-type', '')
        except httpx.HTTPStatusError as e:
            # WAF block on the httpx fingerprint — retry the download via curl.
            if httpx_ctx is not None:
                httpx_ctx.__exit__(None, None, None)
                httpx_ctx = None
            if httpx_client is not None:
                httpx_client.close()
                httpx_client = None
            status = e.response.status_code if e.response is not None else 0
            if status not in (401, 403, 406, 429):
                raise
            tmp_path, content_type = _curl_stream_to_tempfile(url)

        # csv.reader must be fed lines with their newline endings INTACT, so it
        # can correctly stitch back together quoted fields that span multiple
        # physical lines (embedded newlines in description text are common).
        if tmp_path is not None:
            def line_iter():
                with open(tmp_path, 'r', encoding='utf-8', errors='replace',
                          newline='') as fh:
                    for ln in fh:
                        yield ln
        else:
            def line_iter():
                decoder = codecs.getincrementaldecoder('utf-8')('replace')
                pending = ''
                total = 0
                for chunk in httpx_response.iter_bytes():
                    total += len(chunk)
                    if total > CSV_STREAM_HARD_CAP:
                        break
                    pending += decoder.decode(chunk)
                    # Emit complete lines, KEEPING the newline; hold the final
                    # partial line in `pending`.
                    if '\n' in pending:
                        lines = pending.splitlines(keepends=True)
                        if lines and not lines[-1].endswith(('\n', '\r')):
                            pending = lines.pop()
                        else:
                            pending = ''
                        for ln in lines:
                            yield ln
                pending += decoder.decode(b'', final=True)
                if pending:
                    yield pending

        reader = csv.reader(line_iter())
        # Buffer a header window to locate the real column header.
        window = []
        for row in reader:
            window.append(row)
            if len(window) >= 30:
                break
        if not window:
            return None
        header_idx = find_header_row(window)
        header = window[header_idx]
        cols = [h.strip() for h in header]
        lower_header = ','.join(cols).lower()
        tall = (
            'payer_name' in lower_header
            or 'payer ' in lower_header
            or 'negotiated_rate' in lower_header
            or any('payer' in c.lower() for c in cols)
        )
        # Rows after the header inside the window, then the live stream.
        tail_rows = window[header_idx + 1:]
        data_iter = itertools.chain(tail_rows, reader)

        if tall:
            code_col = first_code_col(cols)
            code_type_col = first_col(cols, CODE_TYPE_HINTS)
            desc_col = first_col(cols, DESC_COL_HINTS)
            gross_col = first_gross_col(cols)
            cash_col = first_col(cols, CASH_COL_HINTS)
            min_col = first_col(cols, MIN_COL_HINTS)
            max_col = first_col(cols, MAX_COL_HINTS)
            setting_col = first_col(cols, SETTING_HINTS)
            billing_col = first_col(cols, BILLING_HINTS)
            payer_col = first_col(cols, PAYER_HINTS)
            plan_col = first_col(cols, PLAN_HINTS)
            rate_col = next(
                (i for i, c in enumerate(cols) if is_negotiated_dollar_col(c)),
                None,
            )
            items = {}
            seen = 0
            for row in data_iter:
                seen += 1
                if seen > max_rows or len(items) > max_rows:
                    break
                if not row:
                    continue
                code = row_get(row, code_col)
                desc = row_get(row, desc_col)
                code, code_type = normalize_code_and_type(
                    code, row_get(row, code_type_col))
                key = (code, desc)
                if key not in items:
                    items[key] = {
                        'code': code, 'code_type': code_type,
                        'description': desc[:300],
                        'setting': row_get(row, setting_col),
                        'billing_class': row_get(row, billing_col),
                        'gross_charge': to_float(row_get(row, gross_col)),
                        'cash_discount': to_float(row_get(row, cash_col)),
                        'min_negotiated': to_float(row_get(row, min_col)),
                        'max_negotiated': to_float(row_get(row, max_col)),
                        'payer_rates': [],
                    }
                if payer_col is not None and rate_col is not None:
                    payer = row_get(row, payer_col)
                    plan = row_get(row, plan_col)
                    rate = to_float(row_get(row, rate_col))
                    if payer and rate is not None and rate > 0:
                        items[key]['payer_rates'].append({
                            'payer': payer[:100],
                            'plan': plan[:100],
                            'rate_dollar': rate,
                        })
            result = list(items.values())
        else:
            code_col = first_code_col(cols)
            code_type_col = first_col(cols, CODE_TYPE_HINTS)
            desc_col = first_col(cols, DESC_COL_HINTS)
            gross_col = first_gross_col(cols)
            cash_col = first_col(cols, CASH_COL_HINTS)
            min_col = first_col(cols, MIN_COL_HINTS)
            max_col = first_col(cols, MAX_COL_HINTS)
            setting_col = first_col(cols, SETTING_HINTS)
            billing_col = first_col(cols, BILLING_HINTS)
            canonical = {
                x for x in (code_col, desc_col, gross_col, cash_col,
                            min_col, max_col, setting_col, billing_col)
                if x is not None
            }
            payer_cols = [
                i for i, c in enumerate(cols)
                if i not in canonical and is_wide_payer_col(c)
            ]
            result = []
            seen = 0
            for row in data_iter:
                seen += 1
                if seen > max_rows:
                    break
                if not row or all(not c for c in row):
                    continue
                code = row_get(row, code_col)
                desc = row_get(row, desc_col)
                code, code_type = normalize_code_and_type(
                    code, row_get(row, code_type_col))
                if not code and not desc:
                    continue
                item = {
                    'code': code, 'code_type': code_type,
                    'description': desc[:300],
                    'setting': row_get(row, setting_col),
                    'billing_class': row_get(row, billing_col),
                    'gross_charge': to_float(row_get(row, gross_col)),
                    'cash_discount': to_float(row_get(row, cash_col)),
                    'min_negotiated': to_float(row_get(row, min_col)),
                    'max_negotiated': to_float(row_get(row, max_col)),
                    'payer_rates': [],
                }
                for pi in payer_cols:
                    v = to_float(row_get(row, pi))
                    if v is not None and v > 0:
                        item['payer_rates'].append(
                            {'payer': cols[pi][:100], 'rate_dollar': v})
                if code or item['gross_charge'] is not None or item['payer_rates']:
                    result.append(item)

        if result:
            return result, final_url, content_type
        return None
    finally:
        if httpx_ctx is not None:
            try:
                httpx_ctx.__exit__(None, None, None)
            except Exception:
                pass
        if httpx_client is not None:
            try:
                httpx_client.close()
            except Exception:
                pass
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except FileNotFoundError:
                pass


def resolve_html_wrapper(raw, final_url, content_type, hospital_name, ccn, max_depth=2):
    queue = [(raw, final_url, content_type, 0)]
    visited = {normalize_source_url(final_url)}
    while queue:
        current_raw, current_url, current_type, depth = queue.pop(0)
        if not looks_like_html(current_raw, current_type):
            continue
        for candidate in extract_hub_candidates(current_raw, current_url):
            normalized = normalize_source_url(candidate)
            if normalized in visited:
                continue
            visited.add(normalized)
            try:
                inner_raw, inner_final_url, inner_content_type, _ = fetch(candidate)
            except Exception:
                continue
            try:
                inner_fmt, inner_items = parse(
                    inner_raw,
                    inner_final_url,
                    hospital_name=hospital_name,
                    ccn=ccn,
                )
            except Exception:
                continue
            if inner_items:
                return inner_raw, inner_final_url, inner_content_type, inner_fmt, inner_items, candidate
            if depth + 1 < max_depth and looks_like_html(inner_raw, inner_content_type):
                queue.append((inner_raw, inner_final_url, inner_content_type, depth + 1))
    return None


def ingest_one(ccn, name, url, save_raw=False):
    """Fetch + parse + write canonical JSON. Returns (ok, n_items, fmt, error)."""
    out_path = os.path.join(PARSED_DIR, f"{ccn}.json")
    try:
        raw, final_url, content_type, truncated = fetch(url)
    except Exception as e:
        return False, 0, '', f"fetch:{type(e).__name__}:{str(e)[:80]}"

    if truncated and ('.json' in final_url.lower() or 'json' in (content_type or '').lower()):
        try:
            streamed = stream_parse_large_json(final_url)
        except Exception as e:
            streamed = None
            stream_err = f"stream:{type(e).__name__}:{str(e)[:80]}"
        else:
            stream_err = ''
        if streamed is not None:
            items, streamed_final_url, streamed_type = streamed
            record = {
                'ccn': ccn, 'hospital_name': name, 'source_url': streamed_final_url,
                'fetched_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds') + 'Z',
                'format_detected': 'json-stream', 'row_count': len(items),
                'items': items,
            }
            with open(out_path, 'w') as f:
                json.dump(record, f, separators=(',', ':'))
            return True, len(items), 'json-stream', ''
        if stream_err:
            return False, 0, '', stream_err
        # streamed is None with no exception: the streaming parser found no
        # entries under any known prefix. raw is b'' here (the body was never
        # buffered), so do NOT fall through to parse() — report it instead.
        return False, 0, 'json-stream', 'stream:no_entries_under_known_prefix'

    if truncated and not raw:
        # fetch() flagged a large CSV (the only other truncated+empty-body
        # case): parse it row-by-row over a streamed download.
        try:
            streamed = stream_parse_large_csv(final_url)
        except Exception as e:
            return False, 0, '', f"stream:{type(e).__name__}:{str(e)[:80]}"
        if streamed is not None:
            items, streamed_final_url, _ = streamed
            record = {
                'ccn': ccn, 'hospital_name': name, 'source_url': streamed_final_url,
                'fetched_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds') + 'Z',
                'format_detected': 'csv-stream', 'row_count': len(items),
                'items': items,
            }
            with open(out_path, 'w') as f:
                json.dump(record, f, separators=(',', ':'))
            return True, len(items), 'csv-stream', ''
        return False, 0, 'csv-stream', 'stream:no_rows_parsed'

    try:
        fmt, items = parse(raw, final_url, hospital_name=name, ccn=ccn)
    except Exception as e:
        return False, 0, '', f"parse:{type(e).__name__}:{str(e)[:80]}"
    resolved_url = final_url

    # Some hospital "MRF URLs" are actually transparency landing pages or
    # Google Drive viewer pages. Follow the best candidate file link once.
    if not items and looks_like_html(raw, content_type):
        resolved = resolve_html_wrapper(raw, final_url, content_type, name, ccn)
        if resolved is not None:
            raw, final_url, content_type, fmt, items, resolved_url = resolved
    if save_raw:
        with open(os.path.join(RAW_DIR, f"{ccn}.bin"), 'wb') as f:
            f.write(raw)
    record = {
        'ccn': ccn, 'hospital_name': name, 'source_url': resolved_url,
        'fetched_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds') + 'Z',
        'format_detected': fmt, 'row_count': len(items),
        'items': items,
    }
    with open(out_path, 'w') as f:
        json.dump(record, f, separators=(',', ':'))
    return True, len(items), fmt, ''


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--ccn', required=True)
    p.add_argument('--name', required=True)
    p.add_argument('--url', required=True)
    p.add_argument('--save-raw', action='store_true')
    args = p.parse_args()
    ok, n, fmt, err = ingest_one(args.ccn, args.name, args.url, args.save_raw)
    print(f"{args.ccn} {args.name[:40]:40} {fmt:10} {n:>7} items  {'OK' if ok else 'ERR: ' + err}")
    if (not ok) or n == 0:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
