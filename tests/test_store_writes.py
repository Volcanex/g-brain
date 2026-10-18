"""Competing writers use invented records in isolated stores."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread

import frontmatter
import pytest


@pytest.fixture(params=["create", "upsert", "ticket"])
def store(request, tmp_path, monkeypatch):
    import brain_todos
    from g_todos import tickets

    if request.param == "ticket":
        monkeypatch.setattr(tickets, "TICKETS_DIR", tmp_path / "tickets")
        return tickets, tickets.TICKETS_DIR, tickets.create_ticket, "_iter"
    monkeypatch.setattr(brain_todos, "TODOS_DIR", tmp_path / "todos")
    if request.param == "upsert":
        def create(title, source_key):
            return brain_todos.upsert(title.lower(), {"title": title}, "", {}, source_key)
    else:
        create = brain_todos.create
    return brain_todos, brain_todos.TODOS_DIR, create, "_iter_todos"


def test_concurrent_source_key_creates_one_record(store, monkeypatch):
    module, directory, create, scan_name = store
    first_saving, second_scanned = Event(), Event()
    save, scan = module._save, getattr(module, scan_name)
    second_scans = 0
    scans_before_write = 2 if create is getattr(module, "create", None) else 1

    def delayed_save(slug, post):
        if current_thread().name == "first":
            first_saving.set()
            second_scanned.wait(0.2)
        save(slug, post)

    def observed_scan():
        nonlocal second_scans
        yield from scan()
        if current_thread().name == "second":
            second_scans += 1
            if second_scans >= scans_before_write:
                second_scanned.set()

    def run(name, title):
        current_thread().name = name
        return create(title, source_key="fake:shared-event")

    monkeypatch.setattr(module, "_save", delayed_save)
    monkeypatch.setattr(module, scan_name, observed_scan)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, "first", "Dentist")
        assert first_saving.wait(2)
        second = pool.submit(run, "second", "Optician")
        results = [first.result(timeout=5), second.result(timeout=5)]
    assert len(list(directory.glob("*.md"))) == 1
    assert results[0]["slug"] == results[1]["slug"]
    assert results[1]["deduped"]


def test_atomic_write_preserves_prior_record_on_replace_failure(store, monkeypatch):
    import os
    module, directory, create, _ = store
    item = create("Dentist", source_key="fake:atomic-event")
    path = directory / (item["slug"] + ".md")
    before = path.read_bytes()
    post = frontmatter.load(path)
    post["title"] = "Changed title"
    replacements = []

    def fail_replace(source, target):
        from pathlib import Path
        source, target = Path(source), Path(target)
        assert source.parent == target.parent == directory
        assert frontmatter.load(source)["title"] == "Changed title"
        replacements.append(target)
        raise OSError("invented replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="invented replace failure"):
        module._save(item["slug"], post)
    assert replacements == [path]
    assert path.read_bytes() == before
    assert sorted(p.name for p in directory.iterdir() if p.name != ".lock") == [path.name]


def test_atomic_write_keeps_mode_and_new_files_follow_umask(tmp_path):
    import os
    from g_todos.storage import atomic_write

    existing = tmp_path / "existing.md"
    existing.write_text("old")
    existing.chmod(0o664)
    atomic_write(existing, "new")
    assert existing.stat().st_mode & 0o777 == 0o664
    umask = os.umask(0o022)
    try:
        atomic_write(tmp_path / "fresh.md", "new")
    finally:
        os.umask(umask)
    assert (tmp_path / "fresh.md").stat().st_mode & 0o777 == 0o644


def test_root_writer_leaves_store_owned_by_its_user(tmp_path):
    import os
    from g_todos.storage import atomic_write, store_lock

    if os.geteuid() != 0:
        pytest.skip("ownership hand-off only applies to root writers")
    store = tmp_path / "todos"
    store.mkdir()
    os.chown(store, 65534, 65534)
    with store_lock(store):
        atomic_write(store / "record.md", "text")
    assert (store / "record.md").stat().st_uid == 65534
    assert (store / ".lock").stat().st_uid == 65534
