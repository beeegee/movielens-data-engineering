"""Installer dispatch and safety checks, without downloads or database writes."""
import importlib.util
from pathlib import Path
import subprocess

import pytest


@pytest.fixture
def installer():
    path = Path(__file__).resolve().parents[1] / "install.py"
    spec = importlib.util.spec_from_file_location("movielens_installer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rejects_old_python_before_installing(installer, monkeypatch):
    monkeypatch.setattr(installer.sys, "version_info", (3, 11, 9))
    with pytest.raises(RuntimeError, match="Python 3.12"):
        installer.check_runtime()


@pytest.mark.parametrize("system,machine", [("Windows", "ARM64"), ("Darwin", "sparc"), ("Linux", "riscv64")])
def test_rejects_unsupported_runtime(installer, monkeypatch, system, machine):
    monkeypatch.setattr(installer.sys, "version_info", (3, 12, 12))
    monkeypatch.setattr(installer.platform, "system", lambda: system)
    monkeypatch.setattr(installer.platform, "machine", lambda: machine)
    with pytest.raises(RuntimeError):
        installer.check_runtime()


def setup_dispatch(installer, monkeypatch, tmp_path):
    monkeypatch.setattr(installer, "BASE", tmp_path)
    monkeypatch.setattr(installer, "check_runtime", lambda: None)
    python = tmp_path / ".venv/bin/python"
    monkeypatch.setattr(installer, "environment_python", lambda: python)
    calls = []
    monkeypatch.setattr(installer, "run", lambda command: calls.append([str(part) for part in command]))
    return python, calls


def test_default_installs_dependencies_and_sets_up_database(installer, monkeypatch, tmp_path):
    python, calls = setup_dispatch(installer, monkeypatch, tmp_path)
    installer.main([])
    assert calls == [
        [str(python), "-m", "pip", "install", "-r", "requirements.txt"],
        [str(python), "scripts/manage.py", "setup"],
    ]
    assert not any("--reset" in call for call in calls)


def test_dependencies_only_never_runs_database_setup(installer, monkeypatch, tmp_path):
    python, calls = setup_dispatch(installer, monkeypatch, tmp_path)
    installer.main(["--dependencies-only"])
    assert calls == [[str(python), "-m", "pip", "install", "-r", "requirements.txt"]]
    assert not (tmp_path / ".env").exists()


def test_dev_installs_dev_requirements(installer, monkeypatch, tmp_path):
    python, calls = setup_dispatch(installer, monkeypatch, tmp_path)
    installer.main(["--dev", "--dependencies-only"])
    assert calls == [[str(python), "-m", "pip", "install", "-r", "requirements-dev.txt"]]


def test_refuses_to_overwrite_non_environment_directory(installer, monkeypatch, tmp_path):
    monkeypatch.setattr(installer, "BASE", tmp_path)
    folder = tmp_path / ".venv"
    folder.mkdir()
    marker = folder / "keep.txt"
    marker.write_text("user file")
    with pytest.raises(RuntimeError, match="not a usable virtual environment"):
        installer.environment_python()
    assert marker.read_text() == "user file"


def test_existing_environment_is_reused_without_recreating(installer, monkeypatch, tmp_path):
    monkeypatch.setattr(installer, "BASE", tmp_path)
    environment = tmp_path / ".venv"
    python = installer.native_environment_python(tmp_path)
    python.parent.mkdir(parents=True)
    (environment / "pyvenv.cfg").write_text("version = 3.12.12\n")
    python.touch()
    calls = []
    monkeypatch.setattr(installer, "run", lambda command: calls.append([str(part) for part in command]))
    assert installer.environment_python() == python
    assert not any(command[1:3] == ["-m", "venv"] for command in calls)
    assert any(command[1:3] == ["-m", "ensurepip"] for command in calls)


def test_venv_creation_failure_has_actionable_message(installer, monkeypatch, tmp_path):
    monkeypatch.setattr(installer, "BASE", tmp_path)
    def fail(command):
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(installer, "run", fail)
    with pytest.raises(RuntimeError, match="python3-venv"):
        installer.environment_python()


def test_explicit_recovery_is_forwarded_only_when_requested(installer, monkeypatch, tmp_path):
    python, calls = setup_dispatch(installer, monkeypatch, tmp_path)
    installer.main(["--reset-data"])
    assert calls[-1] == [str(python), "scripts/manage.py", "setup", "--reset-data"]


def test_reset_and_dependencies_only_are_rejected_before_work(installer, monkeypatch, tmp_path):
    _, calls = setup_dispatch(installer, monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as error:
        installer.main(["--reset-data", "--dependencies-only"])
    assert error.value.code == 2
    assert calls == []


@pytest.fixture
def manager():
    path = Path(__file__).resolve().parents[1] / "scripts/manage.py"
    spec = importlib.util.spec_from_file_location("movielens_setup_manager", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_loader_never_resets_by_default(manager):
    assert "--reset" not in manager.loader_command()
    assert manager.loader_command(True)[-1] == "--reset"


def test_run_rejects_reset_before_creating_credentials(manager, monkeypatch, tmp_path):
    monkeypatch.setattr(manager, "BASE", tmp_path)
    monkeypatch.setattr(manager.sys, "argv", ["manage.py", "run", "--reset-data"])
    with pytest.raises(SystemExit) as error:
        manager.main()
    assert error.value.code == 2
    assert not (tmp_path / ".env").exists()


@pytest.fixture
def launcher():
    path = Path(__file__).resolve().parents[1] / "run.py"
    spec = importlib.util.spec_from_file_location("movielens_native_launcher", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launcher_requires_environment_without_starting_process(launcher, monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "environment_python", lambda base: tmp_path / "missing-python")
    assert launcher.main() == 1


def test_launcher_uses_native_python_and_preserves_child_exit(launcher, monkeypatch, tmp_path):
    python = tmp_path / "python.exe"
    python.touch()
    monkeypatch.setattr(launcher, "environment_python", lambda base: python)
    calls = []
    class Process:
        def wait(self):
            return 7
    def spawn(command, **kwargs):
        calls.append((command, kwargs))
        return Process()
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    assert launcher.main() == 7
    assert calls[0][0] == [str(python), "scripts/manage.py", "run"]
    assert calls[0][1]['cwd'] == launcher.BASE


def test_launcher_allows_child_to_clean_up_after_ctrl_c(launcher, monkeypatch, tmp_path):
    python = tmp_path / "python"
    python.touch()
    monkeypatch.setattr(launcher, "environment_python", lambda base: python)
    waits = []
    class Process:
        def wait(self, timeout=None):
            waits.append(timeout)
            if timeout is None:
                raise KeyboardInterrupt
            return 0
        def terminate(self):
            pytest.fail("should allow the child to shut down gracefully")
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *args, **kwargs: Process())
    assert launcher.main() == 0
    assert waits == [None, 35]
