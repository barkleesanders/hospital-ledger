/**
 * Curated CPT/HCPCS code → human description map.
 * Ported verbatim from site/cpt-names.js (which mirrors site/index.html
 * COMMON_PROCEDURES). Keep these three call sites in sync.
 */

export const CPT_NAMES: Record<string, string> = {
  // Office / preventive
  "99213": "Office visit, established patient, 20-29 min",
  "99214": "Office visit, established patient, 30-39 min",
  "99203": "Office visit, new patient, 30-44 min",
  "99204": "Office visit, new patient, 45-59 min",
  "99396": "Adult preventive exam (40-64)",
  "99397": "Adult preventive exam (65+)",
  "99385": "Adult preventive exam (18-39), new",
  G0438: "Annual wellness visit, initial",
  G0439: "Annual wellness visit, subsequent",
  // ER / urgent
  "99281": "Emergency dept, level 1 (minor)",
  "99282": "Emergency dept, level 2",
  "99283": "Emergency dept, level 3",
  "99284": "Emergency dept, level 4 (moderate-high)",
  "99285": "Emergency dept, level 5 (severe)",
  // Imaging
  "71046": "Chest X-ray, 2 views",
  "73721": "MRI knee without contrast",
  "73564": "X-ray knee, 4+ views",
  "70551": "MRI brain without contrast",
  "70553": "MRI brain with and without contrast",
  "72148": "MRI lumbar spine without contrast",
  "73218": "MRI upper extremity (non-joint)",
  "74176": "CT abdomen/pelvis without contrast",
  "74177": "CT abdomen/pelvis with contrast",
  "70450": "CT head/brain without contrast",
  "76700": "Abdominal ultrasound, complete",
  "76705": "Abdominal ultrasound, limited",
  "77067": "Screening mammogram, bilateral",
  "76536": "Thyroid ultrasound",
  "76830": "Transvaginal ultrasound",
  // Labs
  "80053": "Comprehensive metabolic panel",
  "80048": "Basic metabolic panel",
  "80061": "Lipid panel",
  "85025": "Complete blood count (CBC) with differential",
  "85027": "Complete blood count (CBC), automated",
  "84443": "TSH (thyroid)",
  "83036": "Hemoglobin A1c",
  "82947": "Glucose, blood",
  "82565": "Creatinine, blood",
  "82607": "Vitamin B-12",
  "82306": "Vitamin D, 25-hydroxy",
  "81002": "Urinalysis, automated",
  "81003": "Urinalysis, automated, no microscopy",
  "86580": "TB test (skin)",
  "87086": "Urine culture",
  "87880": "Strep test, rapid",
  "87804": "Influenza test, rapid",
  "87635": "COVID-19 test (PCR)",
  "36415": "Routine venipuncture (blood draw)",
  // Cardiology
  "93000": "Electrocardiogram (EKG), complete",
  "93005": "Electrocardiogram (EKG), tracing only",
  "93010": "EKG interpretation only",
  "93306": "Echocardiogram, complete",
  "93880": "Carotid duplex ultrasound",
  "93015": "Cardiac stress test (with EKG monitoring)",
  // Procedures
  "45378": "Colonoscopy, diagnostic (no biopsy)",
  "45380": "Colonoscopy with biopsy",
  "45385": "Colonoscopy with polyp removal (snare)",
  G0121: "Colorectal cancer screening (average risk)",
  "43235": "Upper endoscopy (EGD)",
  "43239": "Upper endoscopy with biopsy",
  "10060": "Drainage of skin abscess (simple)",
  "11042": "Wound debridement (subcutaneous)",
  "12001": "Simple wound repair (≤2.5 cm)",
  "12002": "Simple wound repair (2.6-7.5 cm)",
  "29881": "Knee arthroscopy with meniscectomy",
  "27447": "Total knee replacement",
  "27130": "Total hip replacement",
  "23472": "Total shoulder replacement",
  "47562": "Laparoscopic gallbladder removal",
  "47563": "Laparoscopic gallbladder removal with cholangiogram",
  "49505": "Inguinal hernia repair (open, age 5+)",
  // OB/GYN
  "59400": "Vaginal delivery (global obstetric care)",
  "59510": "Cesarean delivery (global obstetric care)",
  "58150": "Total abdominal hysterectomy",
  "58661": "Laparoscopic tubal removal",
  "57452": "Colposcopy of cervix",
  Q0091: "Pap smear collection",
  "88141": "Pap smear interpretation",
  // Mental health
  "90791": "Psychiatric diagnostic evaluation",
  "90834": "Psychotherapy, 45 min",
  "90837": "Psychotherapy, 60 min",
  "96127": "Brief behavioral health assessment",
  // Vaccines
  "90471": "Vaccine administration",
  "90686": "Influenza vaccine (4-strain, IM)",
  "90715": "Tdap vaccine",
  "90707": "MMR vaccine",
  "90651": "HPV vaccine (9-valent)",
  "91300": "COVID-19 mRNA vaccine (Pfizer)",
  "91301": "COVID-19 mRNA vaccine (Moderna)",
  // Anesthesia / surgery support
  "01967": "Labor analgesia (epidural)",
  "01992": "Anesthesia for spine, prone",
  // Therapy
  "97110": "Physical therapy, therapeutic exercise (15 min)",
  "97140": "Manual therapy (15 min)",
  "97530": "Therapeutic activities (15 min)",
  "97162": "Physical therapy evaluation, moderate",
  "92507": "Speech therapy treatment",
  // PT/OT/Chiro
  "98940": "Chiropractic manipulation, 1-2 regions",
  "98941": "Chiropractic manipulation, 3-4 regions",
  // Common HCPCS
  J3490: "Unclassified injectable drug",
  J1100: "Dexamethasone injection",
  G0008: "Flu shot administration",
  G0009: "Pneumococcal vaccine administration",
  G0010: "Hepatitis B vaccine administration",
};

export function lookupCptName(code: string): string | undefined {
  return CPT_NAMES[code.toUpperCase()];
}
