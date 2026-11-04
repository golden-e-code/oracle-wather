import os, sys, urllib.parse, urllib.request
from datetime import datetime, timezone
import oci

NAME = "phone-server"
SHAPE = "VM.Standard.A1.Flex"
OCPUS = 2
MEMORY = 12
BOOT_GB = 100


def tg(text):
    url = "https://api.telegram.org/bot" + os.environ["TELEGRAM_TOKEN"] + "/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text}
    ).encode()
    try:
        urllib.request.urlopen(url, data, timeout=20)
    except Exception as e:
        print("Telegram message failed:", type(e).name)


def set_done():
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write("done=true\n")


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
manual = os.environ.get("EVENT_NAME") == "workflow_dispatch"

# 1. Stop if the computer already exists
existing = compute.list_instances(tenancy, display_name=NAME).data
if [i for i in existing if i.lifecycle_state not in ("TERMINATED", "TERMINATING")]:
    tg("Your Oracle computer already exists. The watcher is stopping.")
    set_done()
    sys.exit(0)

# 2. Find the Ubuntu 22.04 ARM image
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
    tg("Watcher problem: could not find an Ubuntu 22.04 ARM image.")
    sys.exit(1)
image_id = images[0].id

# 3. Try to create the computer
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
        tg("SUCCESS! Oracle created your computer (" + NAME + "). Check the Oracle console.")
        set_done()
        sys.exit(0)
    except oci.exceptions.ServiceError as e:
        if e.status == 500 or "capacity" in (e.message or "").lower():
            full = True
        elif e.status == 429:
            tg("Oracle says too many requests. The watcher will try again later.")
            sys.exit(0)
        else:
            tg("Watcher error from Oracle: " + str(e.status) + " " + str(e.code))
            sys.exit(1)

# 4. Still full: send a message about once an hour
if full and (manual or datetime.now(timezone.utc).minute < 10):
    tg("Still full at Oracle (Paris). The watcher is still trying every 10 minutes.")
