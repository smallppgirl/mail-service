import subprocess
import time
from mail_service.cli import bootstrap
from mail_service.storage import initialize_bucket

subprocess.run(["alembic", "upgrade", "head"], check=True)
for attempt in range(30):
    try:
        initialize_bucket()
        break
    except Exception:
        if attempt == 29:
            raise
        time.sleep(2)
bootstrap()
