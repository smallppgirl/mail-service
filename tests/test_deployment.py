from pathlib import Path
import yaml


def test_compose_has_durable_volumes_and_private_database_ports():
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    services = compose["services"]
    assert {"postgres", "redis", "minio", "api", "worker", "sender", "beat", "init"} <= services.keys()
    assert "ports" not in services["postgres"]
    assert "ports" not in services["redis"]
    assert services["api"]["ports"][0].startswith("127.0.0.1:")
    assert "noeviction" in services["redis"]["command"]
    assert services["api"]["depends_on"]["init"]["condition"] == "service_completed_successfully"
    assert (
        services["sender"]["command"][services["sender"]["command"].index("--concurrency=1")]
        == "--concurrency=1"
    )


def test_runtime_dependency_lock_does_not_contain_test_tools_or_local_paths():
    lock = Path("requirements.lock").read_text()
    assert "pytest" not in lock and "moto" not in lock and "file://" not in lock
    assert "psycopg-binary==" in lock.lower()
