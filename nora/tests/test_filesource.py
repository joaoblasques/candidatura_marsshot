"""Files domain — access (FileSource), Layer A."""

from __future__ import annotations

from app.filesource import GoogleDriveFileSource, NullFileSource


def test_null_source_is_off():
    assert NullFileSource().read("anything.pdf") is None


class _Files:
    def __init__(self, sink, *, listing, media=b"", export=b""):
        self._sink = sink
        self._listing = listing
        self._media = media
        self._export = export

    def list(self, *, q, fields, pageSize):  # noqa: N803
        self._sink["q"] = q
        return _Exec(self._listing)

    def get_media(self, *, fileId):  # noqa: N803
        self._sink["get_media"] = fileId
        return _Exec(self._media)

    def export_media(self, *, fileId, mimeType):  # noqa: N803
        self._sink["export"] = (fileId, mimeType)
        return _Exec(self._export)

    def get(self, *, fileId, fields):  # noqa: N803
        self._sink["get"] = (fileId, fields)
        return _Exec((self._listing.get("files") or [{}])[0])


class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _Service:
    def __init__(self, **kw):
        self.sink: dict = {}
        self._files = _Files(self.sink, **kw)

    def files(self):
        return self._files


def test_reads_binary_file_via_extractor():
    svc = _Service(listing={"files": [{"id": "f1", "name": "plan.txt", "mimeType": "text/plain"}]},
                   media=b"Quarterly plan: grow 20%")
    out = GoogleDriveFileSource(service=svc).read("plan.txt")
    assert out is not None and "Quarterly plan" in out
    assert svc.sink["get_media"] == "f1"
    assert "name = 'plan.txt'" in svc.sink["q"]


def test_reads_google_native_doc_via_export():
    svc = _Service(
        listing={"files": [{"id": "d1", "name": "Notes", "mimeType": "application/vnd.google-apps.document"}]},
        export=b"Exported google doc text",
    )
    out = GoogleDriveFileSource(service=svc).read("Notes")
    assert out is not None and "Exported google doc" in out
    assert svc.sink["export"] == ("d1", "text/plain")


def test_missing_file_returns_none():
    svc = _Service(listing={"files": []})
    assert GoogleDriveFileSource(service=svc).read("nope.pdf") is None


def test_query_injection_is_neutralized():
    svc = _Service(listing={"files": []})
    GoogleDriveFileSource(service=svc).read("x' or name='secret")
    assert "'" not in svc.sink["q"].split("name = ", 1)[1].split(" and ")[0].strip("'")


def test_query_literal_cannot_break_out():
    svc = _Service(listing={"files": []})
    GoogleDriveFileSource(service=svc).read("evil' or name != 'x")
    # the ref contributes ZERO quotes, so it cannot close the name = '...' literal and
    # inject a clause — the only two single-quotes are our own wrapping pair.
    q = svc.sink["q"]
    assert q.startswith("name = '") and q.count("'") == 2


def test_search_is_fuzzy_across_tokens():
    # "Q4's earnings" must resolve "Q4-2026-earnings.docx" even though the descriptor is
    # not a substring of the filename — each token is ANDed as a `name contains` clause.
    svc = _Service(listing={"files": [{"name": "Q4-2026-earnings.docx"}]})
    out = GoogleDriveFileSource(service=svc).search("Q4's earnings")
    assert out == ["Q4-2026-earnings.docx"]
    q = svc.sink["q"]
    assert "name contains 'Q4'" in q and "name contains 'earnings'" in q
    assert "trashed = false" in q


def test_search_single_term_surfaces_all_candidates():
    files = [{"name": n} for n in (
        "Q1-2026-earnings.txt", "Q2-2026-earnings.txt",
        "Q3-2026-earnings.txt", "Q4-2026-earnings.docx")]
    svc = _Service(listing={"files": files})
    out = GoogleDriveFileSource(service=svc).search("earnings")
    assert len(out) == 4
    assert svc.sink["q"].count("name contains") == 1  # one term -> lists every match


def test_search_drops_stopwords_and_noise_without_querying():
    svc = _Service(listing={"files": []})
    assert GoogleDriveFileSource(service=svc).search("the file of") == []
    assert "q" not in svc.sink  # no real tokens -> never hits Drive


def test_search_literal_cannot_break_out():
    svc = _Service(listing={"files": []})
    GoogleDriveFileSource(service=svc).search("evil' or name != 'x")
    q = svc.sink["q"]
    # every single-quote is one of our own wrapping pairs; the ref contributed none.
    assert q.count("'") % 2 == 0 and "!= '" not in q


def test_drive_service_scopes_readonly_default_write_explicit(monkeypatch):
    # The runtime path must stay read-only; only the explicit seed/author helper gets
    # full `drive`. Capture the scopes handed to the credentials builder.
    import app.filesource as fs
    from google.oauth2 import service_account

    captured: dict = {}

    def fake_creds(path, scopes, subject):  # noqa: ANN001
        captured["scopes"] = scopes
        return object()

    monkeypatch.setattr(service_account.Credentials, "from_service_account_file", staticmethod(fake_creds))
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: object())

    fs.build_drive_service("k.json", "nora@demo")
    assert captured["scopes"] == ["https://www.googleapis.com/auth/drive.readonly"]
    fs.build_drive_write_service("k.json", "nora@demo")
    assert captured["scopes"] == ["https://www.googleapis.com/auth/drive"]


def test_fetch_rejects_oversized_file():
    from app.filesource import _SHARE_MAX_BYTES
    svc = _Service(listing={"files": [{"id": "f1", "name": "big.bin", "mimeType": "application/octet-stream"}]},
                   media=b"x" * (_SHARE_MAX_BYTES + 1))
    assert GoogleDriveFileSource(service=svc).fetch("big.bin") is None


def test_fetch_by_id_returns_binary_attachment():
    svc = _Service(listing={"files": [{"id": "f1", "name": "report.pdf", "mimeType": "application/pdf"}]},
                   media=b"%PDF-1.4 data")
    att = GoogleDriveFileSource(service=svc).fetch_by_id("f1")
    assert att is not None and att.filename == "report.pdf" and att.content == b"%PDF-1.4 data"
    assert att.content_type == "application/pdf" and svc.sink["get"][0] == "f1"


def test_fetch_by_id_exports_native_doc():
    svc = _Service(
        listing={"files": [{"id": "d1", "name": "Model", "mimeType": "application/vnd.google-apps.spreadsheet"}]},
        export=b"col,val\n1,2\n")
    att = GoogleDriveFileSource(service=svc).fetch_by_id("d1")
    assert att is not None and att.filename.endswith(".xlsx") and att.content == b"col,val\n1,2\n"


def test_fetch_by_id_empty_id_and_null_source():
    assert GoogleDriveFileSource(service=_Service(listing={"files": []})).fetch_by_id("") is None
    assert NullFileSource().fetch_by_id("anything") is None


# --- Graph / OneDrive --------------------------------------------------------
from app.filesource import GraphFileSource  # noqa: E402


class _GraphResp:
    def __init__(self, *, json_body=None, content=b""):
        self._json = json_body or {}
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self._json


class _GraphSession:
    """Fake httpx-like client: first .get is the search, second is the content."""

    def __init__(self, *, search, content=b""):
        self._search = search
        self._content = content
        self.urls: list = []

    def get(self, url, params=None):
        self.urls.append(url)
        if url.endswith("/content"):
            return _GraphResp(content=self._content)
        return _GraphResp(json_body=self._search)


def test_graph_reads_file_via_extractor():
    sess = _GraphSession(search={"value": [{"id": "01ABC", "name": "plan.txt"}]},
                         content=b"Quarterly plan: grow 20%")
    out = GraphFileSource("ceo@client.com", session=sess).read("plan.txt")
    assert out is not None and "Quarterly plan" in out
    assert any("search(q='plan.txt')" in u for u in sess.urls)
    assert any("/drive/items/01ABC/content" in u for u in sess.urls)


def test_graph_fetch_returns_attachment():
    sess = _GraphSession(search={"value": [{"id": "01X", "name": "deck.pptx"}]}, content=b"PKbinary")
    att = GraphFileSource("ceo@client.com", session=sess).fetch("deck.pptx")
    assert att is not None and att.filename == "deck.pptx" and att.content == b"PKbinary"


def test_graph_missing_file_returns_none():
    sess = _GraphSession(search={"value": []})
    assert GraphFileSource("ceo@client.com", session=sess).read("nope.pdf") is None


def test_graph_search_literal_neutralized():
    sess = _GraphSession(search={"value": []})
    GraphFileSource("ceo@client.com", session=sess).read("x' or '1'='1")
    search_url = next(u for u in sess.urls if "search(q=" in u)
    assert search_url.count("'") == 2  # only the wrapping pair; ref contributed none


# --- R3 in-boundary FileVault (path containment) -----------------------------
import os as _os  # noqa: E402

from app.filesource import LocalFileVault  # noqa: E402


def test_filevault_reads_a_file_in_root(tmp_path):
    (tmp_path / "plan.txt").write_text("Quarterly plan: grow 20%")
    out = LocalFileVault(str(tmp_path)).read("plan.txt")
    assert out is not None and "Quarterly plan" in out


def test_filevault_never_shares_out(tmp_path):
    (tmp_path / "plan.txt").write_text("x")
    assert LocalFileVault(str(tmp_path)).fetch("plan.txt") is None  # R3 read-only


def test_filevault_rejects_path_traversal(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "ok.txt").write_text("fine")
    vault = LocalFileVault(str(vault_dir))
    assert vault.read("ok.txt") == "fine"
    assert vault.read("../secret.txt") is None          # parent escape
    assert vault.read("../../etc/hostname") is None      # deep escape
    assert vault.read(str(secret)) is None               # absolute path outside root


def test_filevault_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("ESCAPED")
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    link = vault_dir / "link.txt"
    try:
        _os.symlink(outside, link)
    except (OSError, NotImplementedError):
        return  # symlinks unsupported here — skip
    assert LocalFileVault(str(vault_dir)).read("link.txt") is None  # realpath escapes root


def test_filevault_off_when_no_root():
    assert LocalFileVault("").read("anything") is None


# --- grant (Layer C: share via permission grant) -----------------------------
class _GrantFiles:
    def __init__(self, sink):
        self._sink = sink

    def list(self, *, q, fields, pageSize):  # noqa: N803
        self._sink["q"] = q
        return _Exec({"files": [{"id": "f9", "name": "deck.pdf"}]})

    def get(self, *, fileId, fields):  # noqa: N803
        return _Exec({"webViewLink": "https://drive/deck"})


class _GrantPerms:
    def __init__(self, sink):
        self._sink = sink

    def create(self, *, fileId, body, sendNotificationEmail, fields):  # noqa: N803
        self._sink["grant"] = (fileId, body["emailAddress"], sendNotificationEmail)
        return _Exec({"id": "perm1"})


class _GrantService:
    def __init__(self):
        self.sink: dict = {}

    def files(self):
        return _GrantFiles(self.sink)

    def permissions(self):
        return _GrantPerms(self.sink)


def test_gdrive_grant_is_silent_and_returns_link():
    svc = _GrantService()
    link = GoogleDriveFileSource(service=svc).grant("deck.pdf", "sarah@acme.com")
    assert link == "https://drive/deck"
    assert svc.sink["grant"] == ("f9", "sarah@acme.com", False)  # silent grant (no Drive email)


def test_graph_grant_is_silent_and_returns_link():
    class _GraphSession2(_GraphSession):
        def post(self, url, json):
            self.urls.append(("post", url, json["sendInvitation"]))
            return _GraphResp(json_body={"value": [{"link": {"webUrl": "https://sp/deck"}}]})
    sess = _GraphSession2(search={"value": [{"id": "01Z", "name": "deck.pptx", "webUrl": "x"}]})
    link = GraphFileSource("ceo@client.com", session=sess).grant("deck.pptx", "sarah@acme.com")
    assert link == "https://sp/deck"
    assert any(c[0] == "post" and c[2] is False for c in sess.urls)  # sendInvitation false


def test_local_vault_never_grants(tmp_path):
    (tmp_path / "x.txt").write_text("y")
    from app.filesource import LocalFileVault
    assert LocalFileVault(str(tmp_path)).grant("x.txt", "sarah@acme.com") is None


# --- create (Drive write — file own artifacts) -------------------------------
class _CreateFiles:
    def __init__(self, sink):
        self._sink = sink

    def create(self, *, body, media_body, fields):
        self._sink["name"] = body["name"]
        return _Exec({"id": "n1", "webViewLink": "https://drive/new"})


class _CreateService:
    def __init__(self):
        self.sink: dict = {}

    def files(self):
        return _CreateFiles(self.sink)


def test_gdrive_create_uploads_and_returns_link():
    svc = _CreateService()
    link = GoogleDriveFileSource(service=svc).create("brief.docx", b"PKxx", "application/x")
    assert link == "https://drive/new" and svc.sink["name"] == "brief.docx"


def test_gdrive_create_routes_to_write_service_when_present():
    # With a separate write_service (the default-off full-`drive` seam), create() must use
    # IT — not the read-only read service — so PR.SV writes don't hit drive.readonly.
    read, write = _CreateService(), _CreateService()
    link = GoogleDriveFileSource(service=read, write_service=write).create(
        "brief.docx", b"PKxx", "application/x"
    )
    assert link == "https://drive/new"
    assert write.sink.get("name") == "brief.docx"  # written via the write service
    assert read.sink == {}                          # the read-only service was untouched


def test_null_and_vault_create_return_none(tmp_path):
    from app.filesource import LocalFileVault, NullFileSource
    assert NullFileSource().create("a", b"x", "text/plain") is None
    assert LocalFileVault(str(tmp_path)).create("a", b"x", "text/plain") is None


def test_graph_create_uploads_via_put():
    class _PutResp:
        def raise_for_status(self): return None
        def json(self): return {"webUrl": "https://sp/new"}
    class _PutSession:
        def __init__(self): self.calls = []
        def put(self, url, content, headers):
            self.calls.append((url, content))
            return _PutResp()
    sess = _PutSession()
    link = GraphFileSource("ceo@client.com", session=sess).create("brief.docx", b"PK", "application/x")
    assert link == "https://sp/new"
    assert any("root:/brief.docx:/content" in u for u, _ in sess.calls)
