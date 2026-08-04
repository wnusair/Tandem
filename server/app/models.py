from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.extensions import db


class Deployment(db.Model):
    __tablename__ = "deployments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    pid: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False, index=True
    )
    # Stable owner reference. We resolve deployment access through this instead
    # of api_key, because api_key rotation deletes the old UserAPI row and would
    # otherwise orphan every deployment the user made under the old key.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Kept only as the quota bucket the deployment was created under -- quota is
    # tracked per api_key, not per user, so we still need the key here. It is no
    # longer used to decide ownership.
    api_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)


class User(db.Model):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    password: Mapped[str] = mapped_column(String(255), nullable=False)

    api_keys: Mapped[list["UserAPI"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserAPI(db.Model):
    __tablename__ = "user_api_rel"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    api_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)

    user: Mapped["User"] = relationship(back_populates="api_keys")


class NodePublicKey(db.Model):
    __tablename__ = "node_public_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    node_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    rsa_public_key_pem: Mapped[str] = mapped_column(Text, nullable=False)
    registered_at = mapped_column(DateTime, nullable=False, default=func.now())


class TaskEncryptionKey(db.Model):
    __tablename__ = "task_encryption_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # A task's DEK is wrapped once per node, so tid is no longer unique on its
    # own -- there's one row per (tid, target_node_id). That's what lets a task
    # fail over to any node and still be decryptable there.
    tid: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    encrypted_dek_b64: Mapped[str] = mapped_column(Text, nullable=False)
    iv_b64: Mapped[str] = mapped_column(String(64), nullable=False)
    target_node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at = mapped_column(DateTime, nullable=False, default=func.now())
