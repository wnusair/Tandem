import os
import tempfile
import unittest

os.environ["TANDEM_DISABLE_SWEEPER"] = "1"
os.environ.setdefault("TANDEM_NODE_REGISTRATION_TOKEN", "test-registration-token")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp_db.name}")

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Deployment, User, UserAPI  # noqa: E402
from app.utils.auth import ensure_deployment_access  # noqa: E402


class DeploymentAccessTests(unittest.TestCase):
    """The whole point of storing user_id on a deployment: rotating the owner's
    API key (old UserAPI row deleted, new one created) must NOT orphan the
    deployment behind a 403."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = create_app()
        cls.ctx = cls.app.app_context()
        cls.ctx.push()

    @classmethod
    def tearDownClass(cls) -> None:
        db.session.remove()
        cls.ctx.pop()

    def setUp(self) -> None:
        db.drop_all()
        db.create_all()

    def _rotate_key(self, user_id: int, old_key: str, new_key: str) -> UserAPI:
        # This mirrors real rotation: the old key row goes away, a fresh one
        # takes its place under the same user.
        old = db.session.scalars(
            db.select(UserAPI).where(UserAPI.api_key == old_key)
        ).first()
        db.session.delete(old)
        rotated = UserAPI(user_id=user_id, api_key=new_key)
        db.session.add(rotated)
        db.session.commit()
        return rotated

    def test_access_survives_key_rotation(self) -> None:
        user = User(username="owner", password="unused")
        db.session.add(user)
        db.session.flush()
        db.session.add(UserAPI(user_id=user.id, api_key="OLD_KEY"))
        db.session.add(
            Deployment(name="app", pid="p1", user_id=user.id, api_key="OLD_KEY")
        )
        db.session.commit()

        rotated = self._rotate_key(user.id, "OLD_KEY", "NEW_KEY")
        deployment = db.session.scalars(
            db.select(Deployment).where(Deployment.pid == "p1")
        ).first()

        # Before the fix this returned a 403 tuple because the old key no longer
        # resolved to an owner. Now access is decided by user_id, so it's allowed.
        self.assertIsNone(ensure_deployment_access(rotated, deployment))

    def test_other_user_is_denied(self) -> None:
        owner = User(username="owner", password="unused")
        intruder = User(username="intruder", password="unused")
        db.session.add_all([owner, intruder])
        db.session.flush()
        intruder_key = UserAPI(user_id=intruder.id, api_key="INTRUDER_KEY")
        db.session.add(intruder_key)
        db.session.add(
            Deployment(name="app", pid="p1", user_id=owner.id, api_key="OWNER_KEY")
        )
        db.session.commit()

        deployment = db.session.scalars(
            db.select(Deployment).where(Deployment.pid == "p1")
        ).first()
        result = ensure_deployment_access(intruder_key, deployment)
        self.assertIsNotNone(result)
        self.assertEqual(result[1], 403)


if __name__ == "__main__":
    unittest.main()
