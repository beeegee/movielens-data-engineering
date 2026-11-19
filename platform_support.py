"""Standard-library-only native runtime names shared by setup entry points."""
from pathlib import Path
import platform


def runtime_target(system, machine):
    """Return the official MongoDB catalog (target, arch), or fail explicitly.

    Linux uses the Ubuntu 22.04 build (glibc/OpenSSL 3 required), not a
    claim that every Linux distribution or CPU can run MongoDB.
    """
    machine = machine.lower()
    if machine in {'amd64', 'x64', 'x86_64'}:
        arch = 'x86_64'
    elif machine in {'arm64', 'aarch64'}:
        arch = 'arm64'
    else:
        raise RuntimeError(f'Unsupported native runtime: {system} {machine}')
    if system == 'Linux':
        return 'ubuntu2204', 'aarch64' if arch == 'arm64' else arch
    if system == 'Darwin':
        return 'macos', arch
    if system == 'Windows' and arch == 'x86_64':
        return 'windows', arch
    raise RuntimeError(f'Unsupported native runtime: {system} {machine}')


def environment_python(base, system=None):
    """Path only; never create or modify a virtual environment."""
    system = platform.system() if system is None else system
    return Path(base) / '.venv' / ('Scripts/python.exe' if system == 'Windows' else 'bin/python')


def mongo_executable(base, system=None):
    system = platform.system() if system is None else system
    return Path(base) / '.runtime/mongodb/bin' / ('mongod.exe' if system == 'Windows' else 'mongod')
