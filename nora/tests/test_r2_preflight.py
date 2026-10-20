"""R2 preflight script: create → delete a draft via injected fakes, both backends."""

from __future__ import annotations

import scripts.r2_preflight as preflight
from app.config import Settings


# --- Gmail fakes ------------------------------------------------------------
class _GDrafts:
    def __init__(self, log):
        self._log = log

    def create(self, *, userId, body):  # noqa: N803
        self._log.append(("create", userId))
        return _Exec({"id": "draft-pf"})

    def delete(self, *, userId, id):  # noqa: N803, A002
        self._log.append(("delete", userId, id))
        return _Exec({})


class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _GService:
    def __init__(self):
        self.log: list = []

    def users(self):
        class _U:
            def __init__(self, log):
                self._log = log

            def drafts(self):
                return _GDrafts(self._log)

        return _U(self.log)


# --- Graph fakes ------------------------------------------------------------
class _Resp:
    def raise_for_status(self):
        return None

    def json(self):
        return {"id": "msg-pf"}


class _GraphSession:
    def __init__(self):
        self.log: list = []

    def post(self, url, json):  # noqa: A002
        self.log.append(("post", url))
        return _Resp()

    def delete(self, url):
        self.log.append(("delete", url))
        return _Resp()


def test_preflight_gmail(monkeypatch, capsys):
    svc = _GService()
    s = Settings(tenant_backend="gmail", gmail_tenant_user="ceo@client.com",
                 gmail_credentials_json="/k.json")
    monkeypatch.setattr(preflight, "get_settings", lambda: s)
    monkeypatch.setattr("app.tenant.build_gmail_service", lambda creds, user: svc)
    assert preflight.main([]) == 0
    assert ("create", "ceo@client.com") in svc.log
    assert ("delete", "ceo@client.com", "draft-pf") in svc.log
    assert "R2 PREFLIGHT: PASS" in capsys.readouterr().out


def test_preflight_graph(monkeypatch, capsys):
    sess = _GraphSession()
    s = Settings(tenant_backend="graph", graph_tenant_id="t", graph_client_id="c",
                 graph_client_secret="x", graph_tenant_user="ceo@client.com")
    monkeypatch.setattr(preflight, "get_settings", lambda: s)
    monkeypatch.setattr("app.tenant.build_graph_session", lambda tid, cid, sec: sess)
    assert preflight.main([]) == 0
    assert any(c[0] == "post" and c[1].endswith("/users/ceo@client.com/messages") for c in sess.log)
    assert any(c[0] == "delete" and "msg-pf" in c[1] for c in sess.log)
    assert "R2 PREFLIGHT: PASS" in capsys.readouterr().out


def test_preflight_fails_without_creds(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "get_settings", lambda: Settings(tenant_backend="gmail"))
    assert preflight.main([]) == 2
    assert "FAIL" in capsys.readouterr().out
