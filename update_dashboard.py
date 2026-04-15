"""
update_dashboard.py
Fetches all sequences from Apollo.io and rewrites index.html with fresh data.
Reads config.json for manually tracked numbers (demos, replies).
Runs automatically every Monday via GitHub Actions.
"""

import requests
import json
import os
import re
from datetime import datetime

APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY")
BASE_URL = "https://api.apollo.io/v1"
HEADERS = {
    "Content-Type": "application/json",
    "Cache-Control": "no-cache",
    "X-Api-Key": APOLLO_API_KEY,
}

# ── Load manual config ────────────────────────────────────────────────────────
def load_config():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)

# ── Apollo fetch ──────────────────────────────────────────────────────────────
def fetch_all_sequences():
    all_sequences = []
    page = 1
    while True:
        response = requests.post(
            f"{BASE_URL}/emailer_campaigns/search",
            headers=HEADERS,
            json={"page": page, "per_page": 25}
        )
        data = response.json()
        sequences = data.get("emailer_campaigns", [])
        all_sequences.extend(sequences)
        pagination = data.get("pagination", {})
        total_pages = pagination.get("total_pages", 1)
        print(f"  Page {page}/{total_pages} — {len(sequences)} sequences")
        if page >= total_pages:
            break
        page += 1
    print(f"  Total: {len(all_sequences)} sequences")
    return all_sequences

# ── Helpers ───────────────────────────────────────────────────────────────────
def safe_float(val, fallback=0.0):
    if val is None or val == "loading":
        return fallback
    try:
        return float(val)
    except (ValueError, TypeError):
        return fallback

def safe_int(val, fallback=0):
    if val is None or val == "loading":
        return fallback
    try:
        return int(val)
    except (ValueError, TypeError):
        return fallback

def classify_industry(name):
    n = name.lower()
    if any(x in n for x in ["bank", "banking", "banks", "financial", "finans",
                              "bif", "czech republic", "kosovo", "bosnia", "croatia",
                              "serbia | bank", "montenegro | bank", "albania",
                              "bulgaria", "slovenia"]):
        return "Banking"
    if any(x in n for x in ["insurance", "insur"]):
        return "Insurance"
    if any(x in n for x in ["retail", "e-commerce", "ecommerce", "ananas", "vitaminka"]):
        return "Retail"
    if any(x in n for x in ["pharma", "pharmacy", "pharmacies"]):
        return "Pharmacies"
    if any(x in n for x in ["partnership", "igor | partnership", "dis linkedin", "dis 2025"]):
        return "Partnership"
    if any(x in n for x in ["ceo", "coo", "cdo", "c-suite", "diners", "nye",
                              "ageas", "after nye"]):
        return "C-Suite / Diners"
    return "Other"

def classify_title(name):
    n = name.lower()
    if any(x in n for x in ["ceo", "coo", "cdo", "c-suite", "mp,", "md,",
                              "managing director"]):
        return "C-Suite"
    if any(x in n for x in ["founder", "partnership opportunity"]):
        return "Founders"
    if any(x in n for x in ["hr", "hiring", "people ops", "talent"]):
        return "HR / People"
    if any(x in n for x in ["data role", "data roles", "analytics", "data 2030"]):
        return "Data / Analytics"
    if any(x in n for x in ["outsourc", "it ", "tech role", "developer"]):
        return "IT / Tech"
    if any(x in n for x in ["cmo", "marketing"]):
        return "CMO / Marketing"
    return "Mixed"

# ── Build JS sequence object ──────────────────────────────────────────────────
def build_seq_object(s):
    delivered = safe_int(s.get("unique_delivered"))
    opened    = safe_int(s.get("unique_opened"))
    replied   = safe_int(s.get("unique_replied"))
    bounced   = safe_int(s.get("unique_bounced"))
    spam      = safe_int(s.get("unique_spam_blocked"))
    demoed    = safe_int(s.get("unique_demoed"))
    clicked   = safe_int(s.get("unique_clicked"))

    or_ = round(safe_float(s.get("open_rate"))       * 100, 1)
    cr_ = round(safe_float(s.get("click_rate"))      * 100, 1)
    rr_ = round(safe_float(s.get("reply_rate"))      * 100, 1)
    br_ = round(safe_float(s.get("bounce_rate"))     * 100, 1)
    sr_ = round(safe_float(s.get("spam_block_rate")) * 100, 1)

    name    = s.get("name", "Unnamed").replace('"', '\\"').replace("'", "\\'")
    created = s.get("created_at", "")[:10]
    steps   = safe_int(s.get("num_steps"))
    active  = "true" if s.get("active", False) else "false"
    ind     = classify_industry(name)
    title   = classify_title(name)

    return (
        f'  {{name:"{name}",del:{delivered},opn:{opened},rep:{replied},'
        f'bnc:{bounced},spm:{spam},dem:{demoed},clk:{clicked},'
        f'or:{or_},rr:{rr_},br:{br_},sr:{sr_},cr:{cr_},'
        f'steps:{steps},active:{active},created:"{created}",'
        f'industry:"{ind}",title:"{title}"}}'
    )

def build_seq_array(sequences):
    lines = [build_seq_object(s) for s in sequences]
    return "const SEQ = [\n" + ",\n".join(lines) + "\n];"

# ── Patch index.html ──────────────────────────────────────────────────────────
def update_index_html(seq_array_js, config):
    with open("index.html", "r", encoding="utf-8") as f:
        content = f.read()

    d_all = str(config["demos_alltime"])
    d_ytd = str(config["demos_ytd"])
    d_mtd = str(config["demos_mtd"])
    rep   = str(config["replies_alltime"])

    # 1. Replace SEQ array
    content = re.sub(
        r'const SEQ = \[.*?\];',
        seq_array_js,
        content,
        flags=re.DOTALL
    )

    # 2. All-time replies (two occurrences in the renderKPIs function)
    content = re.sub(r"setText\('v-rep','(\d+)'\)", f"setText('v-rep','{rep}')", content)

    # 3. All-time demos
    content = re.sub(r"setText\('v-dem','(\d+)'\)(?=[^;]*LinkedIn)", f"setText('v-dem','{d_all}')", content)

    # 4. YTD demos
    content = re.sub(r"setText\('v-dem','(\d+)'\)(?=[^;]*LinkedIn · Events · Apollo · Smartlead')", f"setText('v-dem','{d_ytd}')", content)

    # 5. Patch the donut demos line
    content = re.sub(
        r"const d=period==='ytd'\?\d+:period==='mtd'\?\d+:\d+",
        f"const d=period==='ytd'?{d_ytd}:period==='mtd'?{d_mtd}:{d_all}",
        content
    )

    # 6. MTD demos in the MTD object
    content = re.sub(
        r"(const MTD = \{[^}]*dem:)\d+",
        f"\\g<1>{d_mtd}",
        content
    )

    # 7. MTD demos setText
    content = re.sub(
        r"setText\('v-dem',String\(MTD\.dem\)\)",
        f"setText('v-dem','{d_mtd}')",
        content
    )
    content = re.sub(
        r"setText\('v-dem',String\(\d+\)\)",
        f"setText('v-dem','{d_mtd}')",
        content
    )

    # 8. Footer date
    today = datetime.utcnow().strftime("%B %d, %Y")
    content = re.sub(
        r'Apollo\.io live data · [\w ]+ \d{4}',
        f'Apollo.io live data · {today}',
        content
    )

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(content)

    print(f"  index.html updated — {today}")
    print(f"  Demos: {d_all} all-time · {d_ytd} YTD · {d_mtd} this month")
    print(f"  Replies all-time: {rep}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=== DataMasters Dashboard Auto-Update ===")
    print(f"Started: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n")

    config = load_config()
    print(f"Config: demos_alltime={config['demos_alltime']} · replies_alltime={config['replies_alltime']}\n")

    print("Fetching sequences from Apollo...")
    sequences = fetch_all_sequences()

    print("\nBuilding sequence array...")
    seq_js = build_seq_array(sequences)

    print("Patching index.html...")
    update_index_html(seq_js, config)

    print("\n=== Done ===")

if __name__ == "__main__":
    main()
