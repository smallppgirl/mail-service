from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://mail:mail@postgres:5432/mail"
    redis_url: str = "redis://redis:6379/0"
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_user_id: str = ""
    mailbox_address: str = ""
    bootstrap_api_key: str = ""
    s3_endpoint: str = "http://minio:9000"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "mail-attachments"
    s3_region: str = "us-east-1"
    sync_interval_seconds: int = 60
    folder_interval_seconds: int = 900
    reconcile_interval_seconds: int = 86400
    max_attachment_bytes: int = 20 * 1024 * 1024
    max_send_total_bytes: int = 25 * 1024 * 1024
    retry_limit: int = 8
    unknown_reconcile_seconds: int = 900


@lru_cache
def settings() -> Settings:
    return Settings()
