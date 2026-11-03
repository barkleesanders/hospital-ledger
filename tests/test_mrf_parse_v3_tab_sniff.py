"""Delimiter sniffing vs CMS V3.0.0 tab-laden headers.

V3.0.0 CSV exports (found 2026-11-03 on CCN 454093, Sunrise Canyon
Hospital) embed literal TAB characters inside comma-delimited header
field names -- ``"license_number|<TAB>TX"`` and trailing-tab column
names like ``median_amount<TAB>``. The tab-preference branch of
sniff_delimiter() split the metadata line on tabs, got a few fat cells
that are themselves comma-joined field lists, saw header keywords inside
them, and declared the whole file TSV. Read as TSV, the 65-row file
collapsed to 2 rows and parsed to zero items on every lane.

The fix: a tab-split line whose cells contain runs of commas is a comma
file with stray tabs, never a TSV header. Genuine TSV files (whose cells
carry no commas) must keep sniffing as tab.
"""
import importlib.util
from pathlib import Path
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("mrf_parse", SCRIPTS / "mrf_parse.py")
mp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mp)

# Synthetic V3.0.0 dual-header shape: metadata header + identity row
# (tabs embedded in header field names), then the data header and rows.
V3_CSV = (
    'hospital_name,last_updated_on,version,location_name,hospital_address,'
    '"license_number|\tTX",type_2_npi\n'
    'Sunrise Canyon Hospital,10/15/2026,3.0.0,Sunrise Canyon Hospital,'
    '"1905 Aspen Avenue, Lubbock, TX 79404",100703,1234567890\n'
    'Description,code|1,code|1|type,setting,standard_charge|gross,'
    'standard_charge|discounted_cash,payer_name,plan_name,'
    'standard_charge|negotiated_dollar,median_amount\t,count\t\n'
    'Psychiatric Hospital Bed Day,124,RC,inpatient,1500,770,'
    'Medicaid Managed Care Organization,Superior,610,750,1 through 10\n'
    'Psychiatric Hospital Bed Day,124,RC,inpatient,1500,770,'
    'Medicaid Managed Care Organization,Wellpoint,,750,1 through 10\n'
)

TSV = (
    'description\tcode\tcode_type\tsetting\tgross_charge\n'
    'Room and Board\t124\tRC\tinpatient\t1500\n'
)


class TestV3TabSniff(unittest.TestCase):
    def test_v3_tab_laden_header_sniffs_comma(self):
        self.assertEqual(mp.sniff_delimiter(V3_CSV), ',')

    def test_genuine_tsv_still_sniffs_tab(self):
        self.assertEqual(mp.sniff_delimiter(TSV), '\t')

    def test_v3_file_parses_items(self):
        items = mp.parse_csv_tall(V3_CSV.encode('utf-8'))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['description'], 'Psychiatric Hospital Bed Day')
        self.assertEqual(items[0]['gross_charge'], 1500.0)


if __name__ == '__main__':
    unittest.main()
