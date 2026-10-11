"""Create a private .env file without overwriting an existing configuration."""

import os
from pathlib import Path
import secrets

root = Path(__file__).resolve().parents[1]
data = (root / ".env.example").read_text()
for key in ["POSTGRES_PASSWORD", "REDIS_PASSWORD", "MINIO_ROOT_PASSWORD", "BOOTSTRAP_API_KEY"]:
    lines = data.splitlines()
    data = (
        "\n".join(f"{key}={secrets.token_hex(32)}" if line.startswith(key + "=") else line for line in lines)
        + "\n"
    )
fd = os.open(root / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as file:
    file.write(data)
print("Created .env. Set the six Graph/mailbox fields before starting Docker Compose.")
