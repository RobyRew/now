"""The HTTP side: /api/now, the admin sign-in through a fake Logto, and the accounts API."""

from __future__ import annotations

import os
import tempfile
import unittest
import urllib.parse
from dataclasses import replace
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from now.auth import Auth
from now.config import Settings
from now.crypto import Sealer
from now.hub import Hub
from now.store import Store
from now.web import create_app
from tests.fakes import EXPIRED, GOOD, FakeClient, FakeLogto, FakeObserver, fake_sign_in, fake_username

ORIGIN = "https://now.test"


class WebTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeObserver.instances.clear()
        FakeClient.instances.clear()
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        (root / "static").mkdir()
        (root / "static" / "index.html").write_text("<!doctype html><title>admin</title>")
        self.settings = Settings(
            public_url=ORIGIN, data_dir=root, key=os.urandom(32), previous_key=None, session_secret="s" * 48,
            logto_endpoint="https://auth.test", logto_app_id="app-id", logto_app_secret="app-secret",
            admin_subs=frozenset({"w2nbkvwmzzyp"}), static_dir=root / "static",
        )
        self.logto = FakeLogto("app-id", "app-secret")
        self.store = Store(root / "now.db")
        self.responses: list[str] = []

    def tearDown(self) -> None:
        self.store.close()
        self.dir.cleanup()

    def client(self, settings: Settings | None = None) -> TestClient:
        settings = settings or self.settings
        hub = Hub(self.store, Sealer(settings.key), sign_in=fake_sign_in, username_of=fake_username,
                  watcher_options={"observer_cls": FakeObserver})
        auth = Auth(settings, client_kwargs={"transport": self.logto.transport})
        return TestClient(create_app(settings, hub, auth), base_url=ORIGIN, follow_redirects=False)

    def remember(self, response: Any) -> Any:
        self.responses.append(response.text + repr(dict(response.headers)))
        return response

    def sign_in(self, client: TestClient) -> Any:
        start = self.remember(client.get("/admin/login"))
        self.assertEqual(start.status_code, 302)
        location = start.headers["location"]
        self.assertTrue(location.startswith("https://auth.test/oidc/auth?"))
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))
        self.assertEqual(query["redirect_uri"], f"{ORIGIN}/admin/callback")
        self.assertIn("openid", query["scope"])
        code, state = self.logto.authorize(location)
        return self.remember(client.get(f"/admin/callback?code={code}&state={state}"))

    def test_public_endpoints(self) -> None:
        with self.client() as client:
            now = self.remember(client.get("/api/now"))
            self.assertEqual(now.status_code, 200)
            self.assertEqual(set(now.json()), {"now", "live", "at"})
            self.assertEqual(now.json()["now"], None)
            self.assertEqual(now.headers["cache-control"], "no-store")
            self.assertEqual(now.headers["content-security-policy"], "default-src 'none'; frame-ancestors 'none'")
            self.assertEqual(client.get("/healthz").text, "ok")
            self.assertEqual(client.get("/").headers["location"], "https://github.com/RobyRew")
            page = client.get("/admin/")
            self.assertEqual(page.status_code, 200)
            self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
            self.assertEqual(page.headers["x-frame-options"], "DENY")

    def test_admin_needs_a_session(self) -> None:
        with self.client() as client:
            self.assertEqual(client.get("/admin/api/me").status_code, 401)
            self.assertEqual(client.get("/admin/api/accounts").status_code, 401)
            put = client.put("/admin/api/accounts/1", json={"sp_dc": GOOD}, headers={"origin": ORIGIN})
            self.assertEqual(put.status_code, 401)

    def test_sign_in_and_manage_accounts(self) -> None:
        with self.client() as client:
            done = self.sign_in(client)
            self.assertEqual((done.status_code, done.headers["location"]), (303, "/admin/"))
            cookie = done.headers["set-cookie"]
            self.assertIn("__Host-now=", cookie)
            for flag in ("secure", "httponly", "samesite=lax", "path=/"):
                self.assertIn(flag, cookie.lower())
            self.assertEqual(self.logto.token_requests[0]["grant_type"], "authorization_code")
            self.assertEqual(client.get("/admin/api/me").json(), {"name": "robyrew"})

            # Writes need the service's own Origin and a JSON body.
            self.assertEqual(client.put("/admin/api/accounts/1", json={"sp_dc": GOOD}).status_code, 403)
            self.assertEqual(client.put("/admin/api/accounts/1", json={"sp_dc": GOOD},
                                        headers={"origin": "https://evil.test"}).status_code, 403)
            form = client.put("/admin/api/accounts/1", content=f"sp_dc={GOOD}", headers={"origin": ORIGIN,
                              "content-type": "application/x-www-form-urlencoded"})
            self.assertEqual(form.status_code, 400)

            refused = self.remember(client.put("/admin/api/accounts/1", json={"sp_dc": EXPIRED}, headers={"origin": ORIGIN}))
            self.assertEqual((refused.status_code, refused.json()["error"]), (422, "session_refused"))
            bad = client.put("/admin/api/accounts/1", json={"sp_dc": "nope"}, headers={"origin": ORIGIN})
            self.assertEqual(bad.json()["error"], "invalid_sp_dc")
            big = client.put("/admin/api/accounts/1", json={"sp_dc": "a" * 9000}, headers={"origin": ORIGIN})
            self.assertEqual(big.status_code, 400)

            added = self.remember(client.put("/admin/api/accounts/3", json={"sp_dc": GOOD}, headers={"origin": ORIGIN}))
            self.assertEqual(added.status_code, 200)
            self.assertEqual((added.json()["slot"], added.json()["username"]), (3, "cosmin"))
            renamed = client.patch("/admin/api/accounts/3", json={"label": "Cosmin"}, headers={"origin": ORIGIN})
            self.assertEqual(renamed.json()["label"], "Cosmin")
            listed = self.remember(client.get("/admin/api/accounts")).json()["accounts"]
            self.assertEqual([a["slot"] for a in listed if not a["empty"]], [3])
            self.assertEqual(client.delete("/admin/api/accounts/3", headers={"origin": ORIGIN}).status_code, 204)
            self.assertEqual(client.delete("/admin/api/accounts/3", headers={"origin": ORIGIN}).status_code, 404)

            out = client.post("/admin/logout", headers={"origin": ORIGIN})
            self.assertEqual(out.status_code, 303)
            self.assertEqual(client.get("/admin/api/me").status_code, 401)
        self.assertFalse(any(GOOD in text or EXPIRED in text for text in self.responses))

    def test_a_user_outside_admin_subs_gets_no_session(self) -> None:
        self.logto.sub = "someone-else"
        with self.client() as client:
            done = self.sign_in(client)
            self.assertEqual(done.headers["location"], "/admin/?signin=refused")
            self.assertEqual(client.get("/admin/api/me").status_code, 401)

    def test_a_wrong_nonce_or_state_fails(self) -> None:
        self.logto.nonce_override = "not-the-nonce"
        with self.client() as client:
            self.assertEqual(self.sign_in(client).headers["location"], "/admin/?signin=failed")
            self.assertEqual(client.get("/admin/api/me").status_code, 401)
        self.logto.nonce_override = None
        with self.client() as client:
            start = client.get("/admin/login")
            code, _ = self.logto.authorize(start.headers["location"])
            forged = client.get(f"/admin/callback?code={code}&state=forged")
            self.assertEqual(forged.headers["location"], "/admin/?signin=failed")

    def test_admin_off_without_a_logto_app(self) -> None:
        with self.client(replace(self.settings, logto_app_id="")) as client:
            self.assertEqual(client.get("/admin/login").status_code, 503)
            self.assertEqual(client.get("/admin/api/me").json()["error"], "admin_not_configured")
            self.assertEqual(client.get("/api/now").status_code, 200)


if __name__ == "__main__":
    unittest.main()
