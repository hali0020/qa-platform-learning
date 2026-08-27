import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.domain.models import utc_now
from app.main import create_app


ADMIN_PASSWORD = "LocalAdmin!12345"
VIEWER_PASSWORD = "LocalViewer!1234"


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "app_env": "test",
        "database_url": f"sqlite+aiosqlite:///{(tmp_path / 'auth.db').as_posix()}",
        "upload_root": str(tmp_path / "uploads"),
        "password_time_cost": 1,
        "password_memory_cost_kib": 1024,
        "password_parallelism": 1,
    }
    values.update(overrides)
    return Settings(**values)


async def setup_admin(client: AsyncClient) -> tuple[dict, str]:
    response = await client.post(
        "/api/v1/auth/setup",
        json={
            "username": "admin",
            "display_name": "Local Admin",
            "password": ADMIN_PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    return data["user"], data["csrf_token"]


@pytest.mark.asyncio
async def test_setup_session_cookie_csrf_logout_and_no_plaintext_storage(
    tmp_path: Path,
) -> None:
    application = create_app(make_settings(tmp_path))
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        status_before = await client.get("/api/v1/auth/status")
        assert status_before.json()["data"] == {
            "enabled": True,
            "authentication_method": "local_accounts",
            "setup_required": True,
            "setup_allowed": True,
            "authenticated": False,
            "csrf_cookie_name": "qa_csrf",
        }

        user, csrf = await setup_admin(client)
        assert user["roles"] == ["system_admin"]
        assert "users.manage" in user["permissions"]
        assert "qa.write" in user["permissions"]

        setup_again = await client.post(
            "/api/v1/auth/setup",
            json={
                "username": "second-admin",
                "display_name": "Second",
                "password": ADMIN_PASSWORD,
            },
        )
        assert setup_again.status_code == 409

        me = await client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["data"]["username"] == "admin"

        missing_csrf = await client.post(
            "/api/v1/projects",
            json={"key": "AUTH", "name": "Auth"},
        )
        assert missing_csrf.status_code == 403
        created = await client.post(
            "/api/v1/projects",
            json={"key": "AUTH", "name": "Auth"},
            headers={"X-CSRF-Token": csrf},
        )
        assert created.status_code == 201

        logout = await client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": csrf},
        )
        assert logout.status_code == 200
        assert (await client.get("/api/v1/auth/me")).status_code == 401

    database_bytes = (tmp_path / "auth.db").read_bytes()
    assert ADMIN_PASSWORD.encode() not in database_bytes


@pytest.mark.asyncio
async def test_cookie_flags_origin_check_and_invalid_login_are_safe(
    tmp_path: Path,
) -> None:
    application = create_app(make_settings(tmp_path))
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        rejected = await client.post(
            "/api/v1/auth/setup",
            headers={"Origin": "https://evil.example"},
            json={
                "username": "admin",
                "display_name": "Admin",
                "password": ADMIN_PASSWORD,
            },
        )
        assert rejected.status_code == 403

        response = await client.post(
            "/api/v1/auth/setup",
            headers={"Origin": "http://127.0.0.1:5173"},
            json={
                "username": "admin",
                "display_name": "Admin",
                "password": ADMIN_PASSWORD,
            },
        )
        cookies = response.headers.get_list("set-cookie")
        session_cookie = next(item for item in cookies if item.startswith("qa_session="))
        csrf_cookie = next(item for item in cookies if item.startswith("qa_csrf="))
        assert "HttpOnly" in session_cookie
        assert "SameSite=strict" in session_cookie
        assert "Path=/api/v1" in session_cookie
        assert "HttpOnly" not in csrf_cookie
        assert "Path=/" in csrf_cookie
        await client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": response.json()["data"]["csrf_token"]},
        )

        unknown = await client.post(
            "/api/v1/auth/login",
            json={"username": "nobody", "password": "WrongPassword!12"},
        )
        wrong = await client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "WrongPassword!12"},
        )
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["message"] == wrong.json()["message"]


@pytest.mark.asyncio
async def test_system_roles_enforce_read_write_and_user_administration(
    tmp_path: Path,
) -> None:
    application = create_app(make_settings(tmp_path))
    admin_transport = ASGITransport(app=application)
    async with AsyncClient(
        transport=admin_transport,
        base_url="http://testserver",
    ) as admin:
        admin_user, csrf = await setup_admin(admin)
        roles = await admin.get("/api/v1/roles")
        assert {item["key"] for item in roles.json()["data"]} == {
            "system_admin",
            "qa_lead",
            "tester",
            "developer",
            "viewer",
        }
        created_user = await admin.post(
            "/api/v1/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "username": "viewer",
                "display_name": "Read Only",
                "password": VIEWER_PASSWORD,
                "role": "viewer",
            },
        )
        assert created_user.status_code == 201, created_user.text
        viewer_id = created_user.json()["data"]["id"]
        project = await admin.post(
            "/api/v1/projects",
            headers={"X-CSRF-Token": csrf},
            json={"key": "RBAC", "name": "RBAC"},
        )
        assert project.status_code == 201

        async with AsyncClient(
            transport=admin_transport,
            base_url="http://testserver",
        ) as viewer:
            login = await viewer.post(
                "/api/v1/auth/login",
                json={"username": "viewer", "password": VIEWER_PASSWORD},
            )
            viewer_csrf = login.json()["data"]["csrf_token"]
            assert (await viewer.get("/api/v1/projects")).status_code == 200
            denied = await viewer.post(
                "/api/v1/projects",
                headers={"X-CSRF-Token": viewer_csrf},
                json={"key": "NOPE", "name": "Nope"},
            )
            assert denied.status_code == 403
            assert (await viewer.get("/api/v1/users")).status_code == 200

        disabled = await admin.patch(
            f"/api/v1/users/{viewer_id}",
            headers={"X-CSRF-Token": csrf},
            json={"status": "disabled"},
        )
        assert disabled.status_code == 200
        relogin = await admin.post(
            "/api/v1/auth/login",
            json={"username": "viewer", "password": VIEWER_PASSWORD},
        )
        assert relogin.status_code == 401

        cannot_self_disable = await admin.patch(
            f"/api/v1/users/{admin_user['id']}",
            headers={"X-CSRF-Token": csrf},
            json={"status": "disabled"},
        )
        assert cannot_self_disable.status_code == 409


@pytest.mark.asyncio
async def test_change_password_revokes_all_sessions(tmp_path: Path) -> None:
    application = create_app(make_settings(tmp_path))
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as first:
        _, csrf = await setup_admin(first)
        async with AsyncClient(transport=transport, base_url="http://testserver") as second:
            second_login = await second.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": ADMIN_PASSWORD},
            )
            assert second_login.status_code == 200
            changed = await first.post(
                "/api/v1/auth/change-password",
                headers={"X-CSRF-Token": csrf},
                json={
                    "current_password": ADMIN_PASSWORD,
                    "new_password": "NewLocalAdmin!123",
                },
            )
            assert changed.status_code == 200
            assert (await first.get("/api/v1/auth/me")).status_code == 401
            assert (await second.get("/api/v1/auth/me")).status_code == 401

            old_login = await second.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": ADMIN_PASSWORD},
            )
            new_login = await second.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "NewLocalAdmin!123"},
            )
            assert old_login.status_code == 401
            assert new_login.status_code == 200


@pytest.mark.asyncio
async def test_concurrent_failed_logins_increment_and_lock_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = create_app(make_settings(tmp_path))
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        await setup_admin(client)
        identity = application.state.container.identity
        original_verify = identity._verify_password
        arrived = 0
        arrived_lock = asyncio.Lock()
        all_verified = asyncio.Event()
        release_updates = asyncio.Event()

        async def verify_then_wait(encoded: str, password: str) -> bool:
            nonlocal arrived
            verified = await original_verify(encoded, password)
            if password == "WrongPassword!12":
                async with arrived_lock:
                    arrived += 1
                    if arrived == identity._MAX_FAILED_LOGINS:
                        all_verified.set()
                await release_updates.wait()
            return verified

        monkeypatch.setattr(identity, "_verify_password", verify_then_wait)
        requests = [
            asyncio.create_task(
                client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "WrongPassword!12"},
                )
            )
            for _ in range(identity._MAX_FAILED_LOGINS)
        ]
        await asyncio.wait_for(all_verified.wait(), timeout=10)
        release_updates.set()
        responses = await asyncio.gather(*requests)
        monkeypatch.setattr(identity, "_verify_password", original_verify)

        assert all(response.status_code == 401 for response in responses)
        account = await identity._repository.get_user_by_username("admin")
        assert account is not None
        assert account.failed_login_count == identity._MAX_FAILED_LOGINS
        assert account.locked_until is not None
        assert account.locked_until > utc_now()
        locked_login = await client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": ADMIN_PASSWORD},
        )
        assert locked_login.status_code == 401


@pytest.mark.parametrize("administrative_change", ["disable", "role", "password"])
@pytest.mark.asyncio
async def test_login_cas_cannot_overwrite_concurrent_administrative_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    administrative_change: str,
) -> None:
    application = create_app(make_settings(tmp_path))
    transport = ASGITransport(app=application)
    reset_password = "ResetViewer!5678"
    async with AsyncClient(transport=transport, base_url="http://testserver") as admin:
        _, csrf = await setup_admin(admin)
        created = await admin.post(
            "/api/v1/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "username": "viewer",
                "display_name": "Concurrent Viewer",
                "password": VIEWER_PASSWORD,
                "role": "viewer",
            },
        )
        assert created.status_code == 201, created.text
        viewer_id = created.json()["data"]["id"]
        identity = application.state.container.identity
        original_verify = identity._verify_password
        password_verified = asyncio.Event()
        release_login = asyncio.Event()

        async def verify_then_wait(encoded: str, password: str) -> bool:
            verified = await original_verify(encoded, password)
            if password == VIEWER_PASSWORD:
                password_verified.set()
                await release_login.wait()
            return verified

        monkeypatch.setattr(identity, "_verify_password", verify_then_wait)
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as viewer:
            pending_login = asyncio.create_task(
                viewer.post(
                    "/api/v1/auth/login",
                    json={"username": "viewer", "password": VIEWER_PASSWORD},
                )
            )
            await asyncio.wait_for(password_verified.wait(), timeout=10)

            if administrative_change == "disable":
                mutation = await admin.patch(
                    f"/api/v1/users/{viewer_id}",
                    headers={"X-CSRF-Token": csrf},
                    json={"status": "disabled"},
                )
            elif administrative_change == "role":
                mutation = await admin.patch(
                    f"/api/v1/users/{viewer_id}",
                    headers={"X-CSRF-Token": csrf},
                    json={"role": "developer"},
                )
            else:
                mutation = await admin.post(
                    f"/api/v1/users/{viewer_id}/reset-password",
                    headers={"X-CSRF-Token": csrf},
                    json={"new_password": reset_password},
                )
            assert mutation.status_code == 200, mutation.text

            release_login.set()
            login = await pending_login
            monkeypatch.setattr(identity, "_verify_password", original_verify)
            assert login.status_code == 401
            assert viewer.cookies.get("qa_session") is None

        account = await identity._repository.get_user_by_username("viewer")
        assert account is not None
        if administrative_change == "disable":
            assert account.status.value == "disabled"
        elif administrative_change == "role":
            assert account.role_key == "developer"
        else:
            assert await original_verify(account.password_hash, reset_password)
            assert not await original_verify(account.password_hash, VIEWER_PASSWORD)


@pytest.mark.asyncio
async def test_concurrent_session_touch_cannot_resurrect_revoked_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = create_app(make_settings(tmp_path))
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as admin:
        _, csrf = await setup_admin(admin)
        created = await admin.post(
            "/api/v1/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "username": "viewer",
                "display_name": "Touch Race Viewer",
                "password": VIEWER_PASSWORD,
                "role": "viewer",
            },
        )
        viewer_id = created.json()["data"]["id"]
        identity = application.state.container.identity

        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as viewer:
            login = await viewer.post(
                "/api/v1/auth/login",
                json={"username": "viewer", "password": VIEWER_PASSWORD},
            )
            assert login.status_code == 200
            raw_token = viewer.cookies.get("qa_session")
            assert raw_token is not None
            stored_session = await identity._repository.get_session_by_token_hash(
                identity._token_hash(raw_token)
            )
            assert stored_session is not None
            assert await identity._repository.touch_session(
                stored_session.id,
                utc_now() - timedelta(minutes=10),
            )

            original_touch = identity._repository.touch_session
            touch_started = asyncio.Event()
            release_touch = asyncio.Event()

            async def touch_after_revoke(session_id, last_seen_at):
                if session_id == stored_session.id:
                    touch_started.set()
                    await release_touch.wait()
                return await original_touch(session_id, last_seen_at)

            monkeypatch.setattr(
                identity._repository,
                "touch_session",
                touch_after_revoke,
            )
            pending_me = asyncio.create_task(viewer.get("/api/v1/auth/me"))
            await asyncio.wait_for(touch_started.wait(), timeout=10)
            revoked = await admin.post(
                f"/api/v1/users/{viewer_id}/revoke-sessions",
                headers={"X-CSRF-Token": csrf},
            )
            assert revoked.status_code == 200, revoked.text
            assert revoked.json()["data"]["revoked_sessions"] == 1

            release_touch.set()
            me = await pending_me
            monkeypatch.setattr(identity._repository, "touch_session", original_touch)
            assert me.status_code == 401

            persisted = await identity._repository.get_session_by_token_hash(
                identity._token_hash(raw_token)
            )
            assert persisted is not None
            assert persisted.revoked_at is not None
            assert (await viewer.get("/api/v1/auth/me")).status_code == 401
