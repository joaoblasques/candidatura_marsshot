import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "scripts" / "check-mirror.sh"


def make_repo(tmp, files=None, author="Dev <dev@users.noreply.github.com>"):
    """Throwaway git repo standing in for the real repo root (gate 3/4 scan it)."""
    repo = Path(tmp) / "repo"
    repo.mkdir(exist_ok=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "Dev", "GIT_COMMITTER_NAME": "Dev"}
    name, email = author.split(" <")
    env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email.rstrip(">"),
               GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email.rstrip(">"))
    for rel, content in (files or {"README.md": "hello\n"}).items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(repo), *cmd], env=env, check=True, capture_output=True)
    return repo


def run(directory, names=None, path=None, repo=None, names_site=None):
    import tempfile

    env = {**os.environ, "SKIP_PYTEST": "1"}
    if names is not None:
        env["NAMES_FILE"] = str(names)
    env["NAMES_SITE_FILE"] = str(names_site or names or "/nonexistent")
    env["REPO_ROOT"] = str(repo or make_repo(tempfile.mkdtemp()))
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        ["bash", str(CHECK), str(directory)], env=env, capture_output=True, text=True
    )


def names_file(tmp_path, *patterns):
    f = tmp_path / "names.txt"
    f.write_text("\n".join(patterns) + "\n")
    return f


def test_fails_closed_without_names_file(tmp_path):
    r = run(tmp_path, names=tmp_path / "missing.txt")
    assert r.returncode == 2


def test_fails_closed_without_gitleaks(tmp_path):
    r = run(tmp_path, names=names_file(tmp_path, "secretname"), path="/usr/bin:/bin")
    assert r.returncode == 2


def test_detects_planted_name(tmp_path):
    target = tmp_path / "mirror"
    target.mkdir()
    (target / "leak.py").write_text("client = 'Acme Secret Corp'\n")
    r = run(target, names=names_file(tmp_path, "acme secret corp"))
    assert r.returncode != 0
    assert "leak.py" in r.stdout + r.stderr


def test_detects_name_in_hidden_file(tmp_path):
    target = tmp_path / "mirror" / ".github"
    target.mkdir(parents=True)
    (target / "ci.yml").write_text("path: /Users/acme secret corp/x\n")
    r = run(tmp_path / "mirror", names=names_file(tmp_path, "acme secret corp"))
    assert r.returncode == 1 and "ci.yml" in r.stdout + r.stderr


def test_repo_scan_catches_name_outside_mirror(tmp_path):
    (tmp_path / "mirror").mkdir()
    repo = make_repo(tmp_path, {"docs/plan.md": "ask Acme Secret Corp\n"})
    r = run(tmp_path / "mirror", names=names_file(tmp_path, "acme secret corp"), repo=repo)
    assert r.returncode == 1 and "docs/plan.md" in r.stdout + r.stderr


def test_history_scan_catches_author_email(tmp_path):
    (tmp_path / "mirror").mkdir()
    repo = make_repo(tmp_path, author="Dev <leak@example.com>")
    r = run(tmp_path / "mirror", names=names_file(tmp_path, "leak@example.com"), repo=repo)
    assert r.returncode == 1 and "history" in (r.stdout + r.stderr).lower()


def test_fails_closed_without_site_names_file(tmp_path):
    (tmp_path / "mirror").mkdir()
    names = names_file(tmp_path, "secretname")
    r = run(tmp_path / "mirror", names=names, names_site=tmp_path / "missing.txt")
    assert r.returncode == 2


def test_clean_dir_passes_name_gate(tmp_path):
    target = tmp_path / "mirror"
    target.mkdir()
    (target / "ok.py").write_text("print('hello')\n")
    r = run(target, names=names_file(tmp_path, "acme secret corp"))
    assert r.returncode == 0, r.stdout + r.stderr


# ---- check-site.py ----
SITE = ROOT / "scripts" / "check-site.py"
SECTIONS = ("abertura", "nora", "como-trabalho", "ideias", "contacto")


def page(tmp_path, body_extra="", skip=None, lang="pt-PT"):
    secs = "".join(
        f'<section id="{s}"><h2>{s}</h2></section>' for s in SECTIONS if s != skip
    )
    (tmp_path / "index.html").write_text(
        f'<!doctype html><html lang="{lang}"><body>{secs}{body_extra}</body></html>'
    )
    return tmp_path


def check_site(root, names_patterns=("zzz-forbidden",)):
    names = root / "names.txt"
    names.write_text("\n".join(names_patterns) + "\n")
    return subprocess.run(
        ["python3", str(SITE), "--root", str(root), "--names", str(names)],
        capture_output=True, text=True,
    )


def test_site_passes_when_valid(tmp_path):
    r = check_site(page(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr


def test_site_requires_all_sections(tmp_path):
    r = check_site(page(tmp_path, skip="contacto"))
    assert r.returncode == 1 and "contacto" in r.stdout + r.stderr


def test_site_requires_ideias_section(tmp_path):
    r = check_site(page(tmp_path, skip="ideias"))
    assert r.returncode == 1 and "ideias" in r.stdout + r.stderr


def test_site_requires_portuguese_lang(tmp_path):
    assert check_site(page(tmp_path, lang="en")).returncode == 1


def test_site_rejects_absolute_paths(tmp_path):
    r = check_site(page(tmp_path, '<a href="/x">x</a>'))
    assert r.returncode == 1 and "/x" in r.stdout + r.stderr


def test_site_rejects_unlisted_external_link(tmp_path):
    assert check_site(page(tmp_path, '<a href="https://evil.example">x</a>')).returncode == 1


def test_site_allows_github_profile_but_not_lookalike(tmp_path):
    ok = check_site(page(tmp_path, '<a href="https://github.com/joaoblasques">me</a>'))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    bad = check_site(page(tmp_path, '<a href="https://github.com/joaoblasquesx">x</a>'))
    assert bad.returncode == 1


def test_site_allows_forecastfactor_link(tmp_path):
    r = check_site(page(tmp_path, '<a href="https://www.forecastfactor.ai/">x</a>'))
    assert r.returncode == 0, r.stdout + r.stderr


def test_site_allows_contact_links(tmp_path):
    body = (
        '<a href="https://www.linkedin.com/in/joaoblasques">l</a>'
        '<a href="https://joaoblasques.com">s</a>'
        '<a href="mailto:joaoeduardoblasques@gmail.com">e</a>'
    )
    r = check_site(page(tmp_path, body))
    assert r.returncode == 0, r.stdout + r.stderr


def test_site_rejects_other_linkedin_profile(tmp_path):
    assert check_site(page(tmp_path, '<a href="https://www.linkedin.com/in/someoneelse">x</a>')).returncode == 1


def test_site_allows_own_site_subpages_but_not_lookalikes(tmp_path):
    ok = check_site(page(tmp_path, '<a href="https://joaoblasques.com/vitals/">v</a>'))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    bad = check_site(page(tmp_path, '<a href="https://joaoblasques.com.evil.tld/x">x</a>'))
    assert bad.returncode == 1


def test_site_rejects_app_domain(tmp_path):
    r = check_site(page(tmp_path, "<p>app.nora-bennett.com</p>"))
    assert r.returncode == 1


def test_site_rejects_forbidden_name(tmp_path):
    r = check_site(page(tmp_path, "<p>ZZZ-Forbidden</p>"))
    assert r.returncode == 1


def test_site_rejects_missing_local_file(tmp_path):
    assert check_site(page(tmp_path, '<link href="assets/missing.css" rel="stylesheet">')).returncode == 1


# ---- sync-excerpts.py ----
SYNC = ROOT / "scripts" / "sync-excerpts.py"


def sync(root, *flags):
    return subprocess.run(
        ["python3", str(SYNC), "--root", str(root), *flags], capture_output=True, text=True
    )


def excerpt_site(tmp_path, src_lines):
    (tmp_path / "nora" / "app").mkdir(parents=True)
    (tmp_path / "nora" / "app" / "x.py").write_text("\n".join(src_lines) + "\n")
    (tmp_path / "index.html").write_text(
        "<body><!-- excerpt: nora/app/x.py:2-3 --><!-- /excerpt --></body>"
    )
    return tmp_path


def test_sync_fills_excerpt(tmp_path):
    root = excerpt_site(tmp_path, ["a", "if x < 1:", "    pass", "z"])
    assert sync(root).returncode == 0
    html = (root / "index.html").read_text()
    assert "if x &lt; 1:" in html and "    pass" in html and "z</code>" not in html
    assert 'href="nora/app/x.py"' in html


def test_sync_uses_link_base_marker(tmp_path):
    root = excerpt_site(tmp_path, ["a", "b", "c", "d"])
    idx = root / "index.html"
    idx.write_text("<!-- link-base: https://github.com/o/r/blob/main/ -->" + idx.read_text())
    assert sync(root).returncode == 0
    assert 'href="https://github.com/o/r/blob/main/nora/app/x.py"' in idx.read_text()
    assert sync(root, "--check").returncode == 0


def test_check_passes_after_sync(tmp_path):
    root = excerpt_site(tmp_path, ["a", "b", "c", "d"])
    sync(root)
    assert sync(root, "--check").returncode == 0


def test_check_fails_when_source_changes(tmp_path):
    root = excerpt_site(tmp_path, ["a", "b", "c", "d"])
    sync(root)
    (root / "nora" / "app" / "x.py").write_text("a\nCHANGED\nc\nd\n")
    assert sync(root, "--check").returncode == 1


def test_missing_file_is_error(tmp_path):
    (tmp_path / "index.html").write_text("<!-- excerpt: nora/nope.py:1-2 --><!-- /excerpt -->")
    assert sync(tmp_path).returncode == 2


# ---- build-mirror.sh ----
BUILD = ROOT / "scripts" / "build-mirror.sh"


def build(tmp_path, scrub=True, patch=True):
    src = make_repo(tmp_path, {"app/x.txt": "hello acmeco\nline2\n", "scripts/promote.sh": "#!/bin/sh\n", "scripts/release.sh": "#!/bin/sh\n"})
    priv = tmp_path / "priv"
    priv.mkdir()
    if scrub:
        (priv / "scrub.sed").write_text("s#acmeco#example#g\n")
    if patch:
        (priv / "mirror-forecast.patch").write_text(
            "--- a/app/x.txt\n+++ b/app/x.txt\n@@ -1,2 +1,2 @@\n hello example\n-line2\n+line two\n"  # patch is made against the scrubbed tree
        )
    out = tmp_path / "out"
    env = {**os.environ, "NORA_SRC": str(src), "NORA_OUT": str(out), "PRIVATE_DIR": str(priv)}
    r = subprocess.run(["bash", str(BUILD)], env=env, capture_output=True, text=True)
    return r, out


def test_build_fails_closed_without_scrub_file(tmp_path):
    r, out = build(tmp_path, scrub=False)
    assert r.returncode == 2 and not out.exists()


def test_build_fails_closed_without_patch_file(tmp_path):
    r, out = build(tmp_path, patch=False)
    assert r.returncode == 2 and not out.exists()


def test_build_applies_private_scrub_and_patch(tmp_path):
    r, out = build(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (out / "app" / "x.txt").read_text() == "hello example\nline two\n"
    assert "Sanitized snapshot" in (out / "MIRROR.md").read_text()


def test_history_scan_still_catches_leak_in_a_long_history(tmp_path):
    # rg -q closes the pipe on the first match; under pipefail that must not read as "no match"
    (tmp_path / "mirror").mkdir()
    repo = make_repo(tmp_path)
    env = {**os.environ, "GIT_AUTHOR_NAME": "Dev", "GIT_COMMITTER_NAME": "Dev",
           "GIT_AUTHOR_EMAIL": "dev@users.noreply.github.com", "GIT_COMMITTER_EMAIL": "dev@users.noreply.github.com"}
    big = "filler line\n" * 400
    for i in range(250):
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", f"c{i}\n\n{big}"],
                       env=env, check=True, capture_output=True)
    leak = {**env, "GIT_AUTHOR_EMAIL": "leak@example.com"}
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "newest"],
                   env=leak, check=True, capture_output=True)
    r = run(tmp_path / "mirror", names=names_file(tmp_path, "leak@example.com"), repo=repo)
    assert r.returncode == 1 and "history" in (r.stdout + r.stderr).lower()
