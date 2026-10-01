"""
update_dashboard.py
Builds data.json for the DataMasters Outreach Dashboard from the Apollo.io API.

index.html is a fixed template that fetches data.json on load, so this script
only has to write data.json — the layout is never patched.

What it pulls:
  - All sequences (emailer_campaigns/search) with per-sequence engagement stats.
  - Per-step detail for ACTIVE sequences (emailer_campaigns/{id}) -> the real
    "current step / how far along" view AND exact per-step send performance,
    plus the subject lines used (for the Insights page).
  - Analytics breakdowns (analytics/sync_report): KPI totals, monthly trend,
    best region / industry / seniority, contact stages, manual-vs-auto.
  - Manual numbers (demos, all-time replies) from config.json.

Fail-safe: if an analytics call comes back empty, the previous value in the
existing data.json is kept rather than overwritten with zeros. So a transient
Apollo hiccup never blanks the dashboard.

Runs daily via GitHub Actions. Requires env var APOLLO_API_KEY.
"""

import requests, json, os, re, sys, time
from datetime import datetime
from collections import defaultdict

API_KEY = os.environ.get("APOLLO_API_KEY")
BASE = "https://api.apollo.io/v1"
HEADERS = {"Content-Type": "application/json", "Cache-Control": "no-cache", "X-Api-Key": API_KEY}
ENRICH_STEPS = True          # one API call per active sequence; fine for a daily job
STEP_LABELS = {1: "Step 1 — First touch", 2: "Step 2 — Follow-up", 3: "Step 3 — Follow-up",
               4: "Step 4 — Follow-up", 5: "Step 5 — Follow-up", 6: "Step 6 — Breakup"}


# ───────────────────────── http ─────────────────────────
def post(path, payload, tries=3):
    for i in range(tries):
        try:
            r = requests.post(f"{BASE}/{path}", headers=HEADERS, json=payload, timeout=60)
            if r.status_code == 200:
                return r.json()
            print(f"  ! POST {path} -> HTTP {r.status_code} (try {i+1})")
        except Exception as e:
            print(f"  ! POST {path} -> {e} (try {i+1})")
        time.sleep(2)
    return {}

def get(path, tries=3):
    for i in range(tries):
        try:
            r = requests.get(f"{BASE}/{path}", headers=HEADERS, timeout=60)
            if r.status_code == 200:
                return r.json()
            print(f"  ! GET {path} -> HTTP {r.status_code} (try {i+1})")
        except Exception as e:
            print(f"  ! GET {path} -> {e} (try {i+1})")
        time.sleep(2)
    return {}


# ───────────────────────── small utils ─────────────────────────
def si(v):
    try:
        return 0 if v in (None, "loading") else int(float(v))
    except Exception:
        return 0

def rate(v):
    try:
        return 0.0 if v in (None, "loading") else round(float(v) * 100, 1)
    except Exception:
        return 0.0

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def load_config():
    return load_json("config.json",
                     {"demos_alltime": 0, "demos_ytd": 0, "demos_mtd": 0, "replies_alltime": 0})

def load_prev():
    """Previous data.json, used as a fallback when a fetch fails."""
    return load_json("data.json", {})


# ───────────────────────── analytics parsing ─────────────────────────
def analytics(metrics, modality=None, rng=None, group_by=None, sort=None):
    payload = {"metrics": metrics, "date_range": {"modality": modality} if modality else rng}
    if group_by:
        payload["group_by"] = group_by
    if sort:
        payload["sort"] = sort
    return post("analytics/sync_report", payload)

def extract_rows(resp):
    """Return a list of row dicts from any plausible sync_report shape."""
    if not isinstance(resp, dict):
        return []
    for key in ("rows", "data", "results", "entries", "report"):
        v = resp.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, dict) and isinstance(v.get("rows"), list):
            return v["rows"]
    return []

def extract_totals(resp):
    """Return a single dict of aggregate metric totals from any plausible shape."""
    if not isinstance(resp, dict):
        return {}
    for key in ("totals", "aggregate", "summary", "total"):
        v = resp.get(key)
        if isinstance(v, dict):
            return v
    rows = extract_rows(resp)
    if len(rows) == 1 and isinstance(rows[0], dict):
        return rows[0]
    # some shapes nest metric->value at top level
    if resp.get("num_emails_sent") is not None:
        return resp
    return {}

def metric(d, *names):
    for n in names:
        if isinstance(d, dict) and d.get(n) is not None:
            return si(d[n])
    return 0


# ───────────────────────── classification ─────────────────────────
REGION_RULES = [
    (["SERBIA","SRB","SRBIJA","SERBIAN","EKAVIAN"], "Serbia"),
    (["BOSNIA","BIH","BOSNA"], "Bosnia"),
    (["CROATIA","HRV","HRVATSKA","CROATIAN"], "Croatia"),
    (["MONTENEGRO","MNE","CRNA GORA"], "Montenegro"),
    (["SLOVENIA","SVN","SOLVERA"], "Slovenia"),
    (["NORTH MACEDONIA","MKD","MACEDONIA"," MK","| MK","ANHOCH","TEHNOMARKET","SPAR MK"], "N. Macedonia"),
    (["ALBANIA","ALB"], "Albania"),
    (["BULGARIA","BGR","BULGAR"], "Bulgaria"),
    (["ROMANIA","ROU","ROMAN"], "Romania"),
    (["ESTONI","LATVIA","LITHUANIA","BALTIC"], "Baltics"),
    (["MOLDOVA"], "Moldova"),
    (["KOSOVO","KOS "], "Kosovo"),
    (["GREECE","GRC","GREEK","ATHENS"], "Greece"),
    (["FINLAND","TURVA","FINNISH"], "Nordics"),
    (["NORDIC","SCANDINAV","SWEDEN","SWE","NORWAY","DENMARK","DNK","STOCKHOLM","SWEDISH","NORWEGIAN"], "Nordics"),
    (["DACH","GERMANY","DEU","AUSTRIA","AUT","SWITZERLAND","CHE","PRAGUE","CZECH"], "DACH/CEE"),
    (["BENELUX","NETHERLANDS","NLD","BELGIUM","BEL"], "Benelux"),
    (["CHEMIST4U","UNITED KING","GBR","IRELAND"," UK "], "UK/IE"),
    (["SFTW","SF TECH","EUROPE","EUROZONE"," EU "], "EU/Global"),
]
INDUSTRY_RULES = [
    (["BANK","FINANC","FSI","BANKING"], "Banking"),
    (["INSUR","OSIGUR","TRIGLAV","TURVA"], "Insurance"),
    (["RETAIL","ECOMM","E-COMM","SPAR","TEHNOMARKET","ANHOCH","DINERS"], "Retail"),
    (["TELCO","TELECOM"], "Telco"),
    (["HEALTH","PHARMA","CHEMIST","BOEHRINGER"], "Healthcare"),
    (["TECH","SAAS","SOFTWARE","SFTW","DWH"], "Tech"),
    (["INVEST","FUND","INVESTOR"], "Investment"),
    (["FOUNDER","STARTUP"], "Startup"),
    (["EVENT","SUMMIT","FORUM","CONFERENCE","LEADS","STOCKHOLM","DIS "], "Event"),
    (["WEBSITE VISITOR","INTENT SIGNAL","SIGNAL"], "Intent/Web"),
    (["PARTNERSHIP"], "Partnership"),
]

def classify(name, rules, default):
    n = name.upper()
    for keys, label in rules:
        if any(k in n for k in keys):
            return label
    return default

def classify_size(name):
    m = re.search(r"(\d+)\s*[-–]\s*(\d+)", name)
    return f"{m.group(1)}-{m.group(2)}" if m else "Mixed"


# ───────────────────────── sequences ─────────────────────────
def fetch_sequences():
    out, page = [], 1
    while True:
        data = post("emailer_campaigns/search", {"page": page, "per_page": 50})
        batch = data.get("emailer_campaigns", [])
        out.extend(batch)
        tp = data.get("pagination", {}).get("total_pages", 1)
        print(f"  sequences page {page}/{tp} ({len(batch)})")
        if page >= tp or not batch:
            break
        page += 1
    return list({s["id"]: s for s in out}.values())

def build_sequences(raw, step_perf_acc, subjects_acc):
    seqs, active_ids = [], []
    for s in raw:
        name = (s.get("name") or "Unnamed").replace("&amp;", "&")
        nsteps = si(s.get("num_steps"))
        obj = {
            "id": s["id"], "name": name, "active": bool(s.get("active")),
            "region": classify(name, REGION_RULES, "SEE/Other"),
            "industry": classify(name, INDUSTRY_RULES, "Other"),
            "size": classify_size(name),
            "created": (s.get("created_at") or "")[:10],
            "lastUsed": (s.get("last_used_at") or "")[:10],
            "steps": nsteps,
            "del": si(s.get("unique_delivered")), "opn": si(s.get("unique_opened")),
            "clk": si(s.get("unique_clicked")), "rep": si(s.get("unique_replied")),
            "bnc": si(s.get("unique_bounced")), "spm": si(s.get("unique_spam_blocked")),
            "or": rate(s.get("open_rate")), "cr": rate(s.get("click_rate")),
            "rr": rate(s.get("reply_rate")), "br": rate(s.get("bounce_rate")),
            "curStep": nsteps, "stepStatus": "paused", "stepExact": False, "stepDist": [],
        }
        if obj["active"]:
            active_ids.append(obj["id"])
        seqs.append(obj)

    if ENRICH_STEPS and active_ids:
        by_id = {o["id"]: o for o in seqs}
        print(f"  enriching {len(active_ids)} active sequences…")
        for i, sid in enumerate(active_ids):
            enrich_one(by_id[sid], step_perf_acc, subjects_acc)
            if (i + 1) % 20 == 0:
                print(f"    …{i+1}/{len(active_ids)}")
    return seqs

def enrich_one(obj, step_perf_acc, subjects_acc):
    """Fetch one sequence: fill current-step distribution, accumulate per-step
    send performance, and capture its first-step subject line."""
    d = get(f"emailer_campaigns/{obj['id']}")
    if not d:
        return
    # step -> position map and current-contact distribution
    steps = d.get("emailer_steps", [])
    pos_by_step = {}
    dist = []
    for st in steps:
        pos = st.get("position")
        pos_by_step[st.get("id")] = pos
        c = st.get("counts", {}) or {}
        dist.append({"pos": pos, "active": si(c.get("active")), "finished": si(c.get("finished"))})
    obj["stepDist"] = dist
    active_steps = [x for x in dist if x["active"] > 0]
    if active_steps:
        obj.update(curStep=max(x["pos"] for x in active_steps), stepStatus="running", stepExact=True)
    elif dist:
        obj.update(curStep=obj["steps"], stepStatus="completed", stepExact=True)

    # per-step send performance from touches (exact)
    for t in d.get("emailer_touches", []):
        pos = pos_by_step.get(t.get("emailer_step_id"))
        if not pos or pos > 6:
            continue
        a = step_perf_acc[pos]
        a["sent"] += si(t.get("unique_delivered"))
        a["opened"] += si(t.get("unique_opened"))
        a["clicked"] += si(t.get("unique_clicked"))
        a["replied"] += si(t.get("unique_replied"))

    # first-step subject line (for Insights "best subjects")
    subj = ""
    first_touch_tmpl = None
    for t in d.get("emailer_touches", []):
        if pos_by_step.get(t.get("emailer_step_id")) == 1:
            first_touch_tmpl = t.get("emailer_template_id"); break
    for tmpl in d.get("emailer_templates", []):
        if first_touch_tmpl and tmpl.get("id") == first_touch_tmpl and tmpl.get("subject"):
            subj = tmpl["subject"]; break
    if not subj:
        for tmpl in d.get("emailer_templates", []):
            if tmpl.get("subject"):
                subj = tmpl["subject"]; break
    if subj and obj["del"] >= 20:
        subjects_acc.append({"subject": subj, "seq": obj["name"], "sent": obj["del"],
                             "open_rate": obj["or"], "reply": obj["rep"]})


# ───────────────────────── KPIs / trend / insights ─────────────────────────
def kpi_block(modality):
    d = analytics(["num_emails_sent","num_emails_delivered","num_emails_opened",
                   "num_emails_clicked","num_emails_replied","num_emails_bounced"], modality=modality)
    t = extract_totals(d)
    return {"sent": metric(t,"num_emails_sent"), "delivered": metric(t,"num_emails_delivered"),
            "opened": metric(t,"num_emails_opened"), "clicked": metric(t,"num_emails_clicked"),
            "replied": metric(t,"num_emails_replied"), "bounced": metric(t,"num_emails_bounced")}

def trend_block():
    d = analytics(["num_emails_sent","num_emails_opened","num_emails_replied","num_emails_clicked"],
                  modality="current_year", group_by=["smart_datetime_month"])
    out = []
    for r in extract_rows(d):
        raw = str(r.get("smart_datetime_month") or "")
        m = re.match(r"([A-Za-z]{3})", raw)
        out.append({"m": m.group(1) if m else raw[:3], "sent": metric(r,"num_emails_sent"),
                    "opened": metric(r,"num_emails_opened"), "replied": metric(r,"num_emails_replied"),
                    "clicked": metric(r,"num_emails_clicked")})
    return out

def grp(group_by, sort_metric="num_emails_replied"):
    d = analytics(["num_emails_sent","num_emails_opened","num_emails_replied","num_contacts_emailed"],
                  modality="current_year", group_by=[group_by],
                  sort={"metric": sort_metric, "asc": False})
    return extract_rows(d)

def pack(rows, namekey, top=8):
    out = []
    for r in rows[:top]:
        sent = metric(r,"num_emails_sent"); rep = metric(r,"num_emails_replied")
        out.append({"name": r.get(namekey) or "—", "sent": sent,
                    "opened": metric(r,"num_emails_opened"), "replied": rep,
                    "rr": round(rep/sent*100, 1) if sent else 0.0})
    return out


def main():
    print("=== DataMasters Dashboard — build data.json ===")
    print(datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    if not API_KEY:
        print("ERROR: APOLLO_API_KEY not set"); sys.exit(1)

    cfg = load_config()
    prev = load_prev()

    step_perf_acc = {p: {"sent":0,"opened":0,"clicked":0,"replied":0} for p in range(1,7)}
    subjects_acc = []

    print("Fetching sequences…")
    raw = fetch_sequences()
    if not raw and prev.get("sequences"):
        print("  ! sequence fetch empty — keeping previous data.json unchanged")
        return
    seqs = build_sequences(raw, step_perf_acc, subjects_acc)
    active = sum(1 for s in seqs if s["active"])

    def keep(new, path):
        """Return new if it has content, else the previous value at dotted path."""
        if new:
            return new
        node = prev
        for k in path.split("."):
            node = node.get(k, {}) if isinstance(node, dict) else {}
        return node or new

    print("Fetching KPIs…")
    kpis = {}
    for name, mod in (("ytd","current_year"),("sep","previous_month"),("oct","current_month")):
        blk = kpi_block(mod)
        kpis[name] = blk if blk.get("sent") else keep(None, f"kpis.{name}") or blk

    print("Building step performance…")
    step_perf = [dict(step=p, label=STEP_LABELS.get(p,f"Step {p}"), **step_perf_acc[p])
                 for p in range(1,7) if step_perf_acc[p]["sent"] > 0]
    step_perf = keep(step_perf, "step_perf")

    print("Fetching insights…")
    region = keep(pack(grp("person_location_country"), "person_location_country"), "insights.best_region")
    industry = keep(pack(grp("organization_industries"), "organization_industries"), "insights.best_industry")
    seniority = keep(pack(grp("person_seniority"), "person_seniority"), "insights.best_seniority")

    stages = [{"stage": r.get("contact_stage_id") or "—",
               "emailed": metric(r,"num_contacts_emailed","num_emails_sent"),
               "replied": metric(r,"num_emails_replied")}
              for r in grp("contact_stage_id","num_emails_sent")[:6]]
    stages = keep(stages, "insights.contact_stages")

    msgtype = []
    for r in grp("emailer_message_type","num_emails_sent")[:4]:
        sent = metric(r,"num_emails_sent"); rep = metric(r,"num_emails_replied")
        msgtype.append({"type": r.get("emailer_message_type") or "—", "sent": sent,
                        "replied": rep, "rr": round(rep/sent*100,1) if sent else 0.0})
    msgtype = keep(msgtype, "insights.msg_type")

    best_sequence = sorted(
        [{"name": s["name"], "del": s["del"], "opn": s["opn"], "rep": s["rep"], "rr": s["rr"], "or": s["or"]}
         for s in seqs if s["del"] >= 20],
        key=lambda x: (x["rep"], x["rr"]), reverse=True)[:10]

    best_subjects = sorted(subjects_acc, key=lambda x: x["open_rate"], reverse=True)[:6]
    best_subjects = keep(best_subjects, "insights.best_subjects")

    trend = keep(trend_block(), "trend")

    data = {
        "generated": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "generated_date": datetime.utcnow().strftime("%B %d, %Y"),
        "totals": {"demos_alltime": cfg.get("demos_alltime",0), "demos_ytd": cfg.get("demos_ytd",0),
                   "demos_mtd": cfg.get("demos_mtd",0), "replies_alltime": cfg.get("replies_alltime",0)},
        "kpis": kpis,
        "counts": {"sequences": len(seqs), "active": active, "paused": len(seqs)-active},
        "sequences": seqs,
        "step_perf": step_perf,
        "insights": {"best_subjects": best_subjects, "best_seniority": seniority,
                     "best_region": region, "best_industry": industry,
                     "best_sequence": best_sequence, "contact_stages": stages, "msg_type": msgtype},
        "trend": trend,
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"Wrote data.json — {len(seqs)} sequences ({active} active), "
          f"YTD sent {kpis.get('ytd',{}).get('sent','?')}")
    print("=== Done ===")


if __name__ == "__main__":
    main()
