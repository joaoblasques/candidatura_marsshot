from app.users import FileUserStore, NullUserStore


def test_null_store_is_empty():
    n = NullUserStore()
    n.add_user("nora-x", "a@x.com")
    assert n.all() == {} and n.find_by_sender("a@x.com") is None


def test_file_store_add_find_all(tmp_path):
    s = FileUserStore(str(tmp_path / "users.json"))
    s.add_user("nora-abc", "Founder@Acme.com")
    assert s.find_by_sender("founder@acme.com") == "nora-abc"  # case-insensitive
    assert s.all() == {"nora-abc": ["founder@acme.com"]}
    assert s.find_by_sender("nobody@x.com") is None
