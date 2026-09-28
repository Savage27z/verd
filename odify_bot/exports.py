"""CSV / vCard export."""
import csv
import io
import re


# ---- CSV ----

CSV_COLUMNS = [
    ("Business Name", "name"), ("Phone", "phone"), ("International Phone", "intl_phone"),
    ("Email", "email"), ("Address", "address"), ("Category", "category"), ("Rating", "rating"),
    ("Reviews", "reviews"), ("Web Presence", "web_presence"), ("Social / Profile Link", "website"),
    ("Google Maps", "maps_url"), ("Lead Score", "score"), ("Status", "status"), ("Notes", "notes"),
]
_PHONEISH = re.compile(r"^[+\-]?[\d\s().\-]+$")


def _csv_safe(value) -> str:
    """Neutralise spreadsheet formula injection (a business name like '=HYPERLINK(...)')
    without mangling phone numbers such as '+234 803 ...'."""
    if value is None:
        return ""
    s = str(value)
    if s[:1] in ("=", "@", "\t", "\r") or (s[:1] in ("+", "-") and not _PHONEISH.match(s)):
        return "'" + s
    return s


def generate_csv(leads: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([label for label, _ in CSV_COLUMNS])
    for lead in leads:
        writer.writerow([_csv_safe(lead.get(key)) for _, key in CSV_COLUMNS])
    return buf.getvalue()


# ---- vCard 3.0 ----

def _vc(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def generate_vcard(lead: dict) -> str:
    name = lead.get("name") or "Unknown business"
    lines = ["BEGIN:VCARD", "VERSION:3.0", f"FN:{_vc(name)}", f"N:;{_vc(name)};;;", f"ORG:{_vc(name)}"]
    phone = lead.get("intl_phone") or lead.get("phone")
    if phone:
        lines.append(f"TEL;TYPE=WORK,VOICE:{_vc(phone)}")
    if lead.get("email"):
        lines.append(f"EMAIL;TYPE=WORK:{_vc(lead['email'])}")
    if lead.get("address"):
        lines.append(f"ADR;TYPE=WORK:;;{_vc(lead['address'])};;;;")
    if lead.get("website"):
        lines.append(f"URL:{_vc(lead['website'])}")
    note = " | ".join(x for x in [
        lead.get("category", ""),
        f"{lead['rating']}★ ({lead.get('reviews', 0)} reviews)" if lead.get("rating") else "",
        lead.get("maps_url", ""),
        "Lead via Odify",
    ] if x)
    lines.append(f"NOTE:{_vc(note)}")
    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"


def generate_all_vcards(leads: list[dict]) -> str:
    return "".join(generate_vcard(lead) for lead in leads)
