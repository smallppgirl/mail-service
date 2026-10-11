import argparse
import hashlib
import secrets
from sqlalchemy import select
from mail_service.config import settings
from mail_service.db import session
from mail_service.models import Application, Mailbox
from mail_service.storage import initialize_bucket


def bootstrap():
    s = settings()
    if not s.graph_user_id or not s.mailbox_address:
        raise SystemExit("Set GRAPH_USER_ID and MAILBOX_ADDRESS before initialization")
    if len(s.bootstrap_api_key) < 32:
        raise SystemExit("BOOTSTRAP_API_KEY must be a random value of at least 32 characters")
    with session() as db:
        mailbox = db.scalar(select(Mailbox).where(Mailbox.graph_user_id == s.graph_user_id))
        if mailbox is None:
            mailbox = Mailbox(graph_user_id=s.graph_user_id, address=s.mailbox_address)
            db.add(mailbox)
            db.flush()
        key_hash = hashlib.sha256(s.bootstrap_api_key.encode()).hexdigest()
        if not db.scalar(select(Application).where(Application.key_hash == key_hash)):
            db.add(
                Application(
                    name="bootstrap-admin", key_hash=key_hash, scopes=["admin"], mailbox_ids=[mailbox.id]
                )
            )
        db.commit()
        print(f"Mailbox initialized: {mailbox.id}")
    initialize_bucket()


def create_application(args):
    key = secrets.token_urlsafe(36)
    with session() as db:
        if not db.get(Mailbox, args.mailbox):
            raise SystemExit("Mailbox not found")
        db.add(
            Application(
                name=args.name,
                key_hash=hashlib.sha256(key.encode()).hexdigest(),
                scopes=args.scopes.split(","),
                mailbox_ids=[args.mailbox],
            )
        )
        db.commit()
    print("Application API key (shown once): " + key)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap")
    create = sub.add_parser("create-app")
    create.add_argument("--name", required=True)
    create.add_argument("--mailbox", required=True)
    create.add_argument("--scopes", default="mail:read,mail:send,mail:process")
    args = parser.parse_args()
    if args.command == "bootstrap":
        bootstrap()
    else:
        create_application(args)


if __name__ == "__main__":
    main()
