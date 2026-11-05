import os, sys, json, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import oci

for _k in ("OCI_USER", "OCI_TENANCY", "OCI_FINGERPRINT", "OCI_REGION",
           "OCI_PRIVATE_KEY", "OCI_SUBNET_ID", "SSH_PUBLIC_KEY",
           "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID",
           "SUPABASE_URL", "SUPABASE_SECRET_KEY"):
    if _k in os.environ:
        os.environ[_k] = os.environ[_k].strip()
os.environ["SSH_PUBLIC_KEY"] = " ".join(os.environ["SSH_PUBLIC_KEY"].split()[:2])

NAME = "phone-server"
SHAPE = "VM.Standard.A1.Flex"
OCPUS = 2
MEMORY = 12
BOOT_GB = 100
CRON_MINUTES = (5, 10, 15, 20, 25, 30)

SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SECRET_KEY", "")


def tg(text):
    url = "https://api.telegram.org/bot" + os.environ["TELEGRAM_TOKEN"] + "/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text}
    ).encode()
    try:
        urllib.request.urlopen(url, data, timeout=20)
    except Exception as e:
        print("Telegram message failed:", type(e).__name__)


def set_done():
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write("done=true\n")


def sb(method, path, body=None):
    if not SB_URL or not SB_KEY:
        return None
    headers = {
        "apikey": SB_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    if SB_KEY.startswith("eyJ"):
        headers["Authorization"] = "Bearer " + SB_KEY
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        SB_URL + "/rest/v1/" + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode()
            return json.loads(text) if text else []
    except Exception as e:
        code = getattr(e, "code", "")
        body = e.read().decode()[:300] if hasattr(e, "read") else ""
        print("Supabase problem:", type(e).name, code, body)
        return None


def log(result, detail="", notified=False):
    sb("POST", "watcher_log",
       {"result": result, "detail": str(detail)[:300], "notified": notified})


def minutes_since_last_message():
    rows = sb("GET", "watcher_log?select=created_at&notified=eq.true&order=created_at.desc&limit=1")
    if rows is None:
        return -1
    if not rows:
        return 9999
    t = datetime.fromisoformat(rows[0]["created_at"].replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - t).total_seconds() / 60


def stats_text():
    rows = sb("GET", "watcher_stats?select=*")
    if not rows:
        return "Stats are not available yet."
    s = rows[0]
    return ("Attempts so far: last hour " + str(s["last_hour"])
            + ", last 24 hours " + str(s["last_24h"])
            + ", last 7 days " + str(s["last_7d"])
            + ", total " + str(s["total"]) + ".")


def next_run_text():
    now = datetime.now(timezone.utc)
    t = None
    for m in CRON_MINUTES:
        if m > now.minute:
            t = now.replace(minute=m, second=0, microsecond=0)
            break
    if t is None:
        t = (now + timedelta(hours=1)).replace(minute=CRON_MINUTES[0], second=0, microsecond=0)
    return t.astimezone(ZoneInfo("Europe/London")).strftime("%H:%M") + " UK time (GitHub can be a few minutes late)"


config = {
    "user": os.environ["OCI_USER"],
    "tenancy": os.environ["OCI_TENANCY"],
    "fingerprint": os.environ["OCI_FINGERPRINT"],
    "region": os.environ["OCI_REGION"],
    "key_content": os.environ["OCI_PRIVATE_KEY"],
}
oci.config.validate_config(config)

tenancy = config["tenancy"]
compute = oci.core.ComputeClient(config)
identity = oci.identity.IdentityClient(config)
manual = os.environ.get("EVENT_NAME") == "workflow_dispatch" and os.environ.get("RUN_SOURCE", "") != "worker"
existing = compute.list_instances(tenancy, display_name=NAME).data
if [i for i in existing if i.lifecycle_state not in ("TERMINATED", "TERMINATING")]:
    log("exists", "computer already exists")
    tg("Your Oracle computer already exists. The watcher is stopping.")
    set_done()
    sys.exit(0)

images = compute.list_images(
    tenancy,
    operating_system="Canonical Ubuntu",
    operating_system_version="22.04",
    shape=SHAPE,
    sort_by="TIMECREATED",
    sort_order="DESC",
).data
images = [i for i in images if "minimal" not in i.display_name.lower()]
if not images:
    log("error", "no Ubuntu 22.04 ARM image found")
    tg("Watcher problem: could not find an Ubuntu 22.04 ARM image.")
    sys.exit(1)
image_id = images[0].id

full = False
for ad in identity.list_availability_domains(tenancy).data:
    details = oci.core.models.LaunchInstanceDetails(
        compartment_id=tenancy,
        availability_domain=ad.name,
        display_name=NAME,
        shape=SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=OCPUS, memory_in_gbs=MEMORY
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=image_id, boot_volume_size_in_gbs=BOOT_GB
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=os.environ["OCI_SUBNET_ID"], assign_public_ip=True
        ),
        metadata={"ssh_authorized_keys": os.environ["SSH_PUBLIC_KEY"]},
    )
    try:
        compute.launch_instance(details)
        log("success", "computer created", True)
        tg("SUCCESS! Oracle created your computer (" + NAME + "). Check the Oracle console.")
        set_done()
        sys.exit(0)
    except oci.exceptions.ServiceError as e:
        if e.status == 500 or "capacity" in (e.message or "").lower():
            full = True
        elif e.status == 429:
            log("rate_limited", "too many requests")
            tg("Oracle says too many requests. The watcher will try again later.")
            sys.exit(0)
        else:
            print("Oracle error:", e.status, e.code, e.message)
            log("error", str(e.status) + " " + str(e.code) + " " + str(e.message))
            tg("Watcher error from Oracle: " + str(e.status) + " " + str(e.code) + " - " + str(e.message)[:400])
            sys.exit(1)

if full:
    age = minutes_since_last_message()
    send = manual or age >= 55 or (age == -1 and datetime.now(timezone.utc).minute < 10)
    log("full", "no capacity", send)
    if send:
        tg("Still full at Oracle (Paris).\n" + stats_text()
           + "\nNext check around " + next_run_text())
