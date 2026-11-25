from pathlib import Path

from agent import file_safety as safety


def test_resolved_children_and_read_denials_match_full_resolution(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    home = root / "profiles" / "one"
    alias = home.with_name("alias")
    outside = tmp_path / "outside"
    home.mkdir(parents=True)
    outside.mkdir()
    alias.symlink_to(home, target_is_directory=True)
    (root / "auth.json").touch()
    (outside / "token").touch()
    (home / ".env").symlink_to(outside / "token")
    (home / "auth").symlink_to(outside, target_is_directory=True)
    (outside / "google_oauth.json").touch()
    (root / "auth.lock").symlink_to(outside / "dangling")
    (root / "vault").symlink_to(outside / "vault", target_is_directory=True)
    (outside / "vault").mkdir()
    (home / "auth.lock").symlink_to(home / "auth.lock")
    monkeypatch.setattr(safety, "_hermes_root_path", lambda: root)
    monkeypatch.setattr(safety, "_hermes_home_path", lambda: alias)
    homes = [root, home, alias]
    names = (*safety._CREDENTIAL_FILE_NAMES, *(name for name, *_ in safety._READ_DENIED_DIRS))
    assert safety._resolved_children(homes, names) == safety._resolve_each(
        hd / name for hd in homes for name in names)

    def check(path, reason):
        expected = f"Access denied: {path} {reason}{safety._DID_SUFFIX}" if reason else None
        assert safety.get_read_block_error(str(path)) == expected

    for directory in homes:
        for name in safety._CREDENTIAL_FILE_NAMES:
            path = directory / name
            for target in safety._resolve_each([path]):
                reason = ("is a Hermes credential store and cannot be read directly. Provider tools "
                          "consume these credentials through internal channels.")
                check(path, reason)
                check(target, reason)
        for name, dir_msg, file_msg in safety._READ_DENIED_DIRS:
            path = directory / name
            for target in (path, path.resolve()):
                check(target, dir_msg)
                check(target / "child", file_msg)
    for path in (outside / "auth.json", home / "auth.json.example", outside / "public"):
        check(path, None)


def test_full_realpaths_scale_with_homes_not_credential_names(tmp_path, monkeypatch):
    count = 8
    for index in range(count - 1):
        (tmp_path / "profiles" / str(index)).mkdir(parents=True)
    monkeypatch.setattr(safety, "_hermes_root_path", lambda: tmp_path)
    monkeypatch.setattr(safety, "_hermes_home_path", lambda: tmp_path)
    resolve = Path.resolve
    calls = []

    def counted(path, *args, **kwargs):
        calls.append(path)
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", counted)
    assert safety.get_read_block_error(str(tmp_path / "allowed.png")) is None
    assert len(calls) <= count + 3
