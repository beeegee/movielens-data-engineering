"""Native runtime tests. All generated archives/catalogs are synthetic fixtures."""
import importlib.util
import io
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import zipfile

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from platform_support import environment_python, mongo_executable, runtime_target


@pytest.fixture
def bootstrap():
    spec = importlib.util.spec_from_file_location('runtime_bootstrap', BASE / 'scripts/bootstrap_runtime.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('system,machine,expected', [
    ('Linux', 'x86_64', ('ubuntu2204', 'x86_64')),
    ('Linux', 'AMD64', ('ubuntu2204', 'x86_64')),
    ('Linux', 'x64', ('ubuntu2204', 'x86_64')),
    ('Linux', 'ARM64', ('ubuntu2204', 'aarch64')),
    ('Linux', 'aarch64', ('ubuntu2204', 'aarch64')),
    ('Darwin', 'arm64', ('macos', 'arm64')),
    ('Darwin', 'aarch64', ('macos', 'arm64')),
    ('Darwin', 'x86_64', ('macos', 'x86_64')),
    ('Windows', 'AMD64', ('windows', 'x86_64')),
    ('Windows', 'x86_64', ('windows', 'x86_64')),
    ('Windows', 'x64', ('windows', 'x86_64')),
])
def test_native_target(system, machine, expected):
    assert runtime_target(system, machine) == expected


@pytest.mark.parametrize('system,machine', [('Windows', 'ARM64'), ('Linux', 'i686'), ('FreeBSD', 'amd64')])
def test_unsupported_target(system, machine):
    with pytest.raises(RuntimeError, match='Unsupported native runtime'):
        runtime_target(system, machine)


@pytest.mark.parametrize('system', ['Linux', 'Darwin', 'Windows'])
def test_paths_are_side_effect_free(tmp_path, system):
    assert environment_python(tmp_path, system) == tmp_path / '.venv' / ('Scripts/python.exe' if system == 'Windows' else 'bin/python')
    assert mongo_executable(tmp_path, system) == tmp_path / '.runtime/mongodb/bin' / ('mongod.exe' if system == 'Windows' else 'mongod')
    assert not list(tmp_path.iterdir())


def fixture_archive(path, kind, entries):
    """Build a synthetic fixture: entries contain (name, bytes, file type)."""
    if kind == 'zip':
        with zipfile.ZipFile(path, 'w') as bundle:
            for name, data, entry_kind in entries:
                member = zipfile.ZipInfo(name)
                # ZipInfo normalizes backslashes on Windows. Preserve the raw
                # fixture name so malicious archives are tested consistently.
                member.filename = name
                member.create_system = 3
                member.external_attr = ((stat.S_IFLNK if entry_kind == 'symlink' else stat.S_IFREG) | 0o755) << 16
                bundle.writestr(member, data)
    else:
        with tarfile.open(path, 'w:gz') as bundle:
            for name, data, entry_kind in entries:
                member = tarfile.TarInfo(name)
                member.mode = 0o755
                if entry_kind in {'symlink', 'hardlink'}:
                    member.type = tarfile.SYMTYPE if entry_kind == 'symlink' else tarfile.LNKTYPE
                    member.linkname = '../../outside'
                    bundle.addfile(member)
                else:
                    member.size = len(data)
                    bundle.addfile(member, io.BytesIO(data))


@pytest.mark.parametrize('kind,system', [('tar', 'Linux'), ('tar', 'Darwin'), ('zip', 'Windows')])
def test_production_extractor_bin_and_licenses_only(bootstrap, tmp_path, kind, system):
    archive = tmp_path / ('fixture.zip' if kind == 'zip' else 'fixture.tgz')
    name = 'mongod.exe' if system == 'Windows' else 'mongod'
    fixture_archive(archive, kind, [
        (f'package/bin/{name}', b'synthetic executable fixture', 'file'),
        ('package/bin/mongos', b'synthetic fixture', 'file'),
        ('package/bin/runtime.dll', b'synthetic bundled DLL fixture', 'file'),
        ('package/LICENSE-Community.txt', b'synthetic license fixture', 'file'),
        ('package/MPL-2', b'synthetic license fixture', 'file'),
        ('package/README', b'ignored fixture', 'file'),
        ('package/lib/ignored', b'ignored fixture', 'file'),
    ])
    destination = tmp_path / 'mongodb'
    bootstrap.extract_mongo_archive(archive, destination, system)
    assert (destination / 'bin' / name).read_bytes() == b'synthetic executable fixture'
    assert (destination / 'LICENSE-Community.txt').is_file()
    assert (destination / 'MPL-2').is_file()
    assert (destination / 'bin/runtime.dll').read_bytes() == b'synthetic bundled DLL fixture'
    assert not (destination / 'README').exists()
    assert not (destination / 'lib').exists()
    if system != 'Windows' and sys.platform != 'win32':
        # Windows chmod/stat expose read-only state, not POSIX execute bits.
        assert (destination / 'bin/mongod').stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize('kind', ['tar', 'zip'])
@pytest.mark.parametrize('bad_name', [
    'package/bin/../../escape', '../escape', '/absolute',
    'package/bin/C:escape', 'package/bin/evil\\escape',
    'package/bin/NUL.txt', 'package/bin/trailing.',
])
def test_extractor_rejects_unsafe_paths_without_replacing_install(bootstrap, tmp_path, kind, bad_name):
    archive = tmp_path / 'fixture'
    fixture_archive(archive, kind, [('package/bin/mongod', b'fixture', 'file'), (bad_name, b'fixture', 'file')])
    destination = tmp_path / 'mongodb'
    destination.mkdir()
    marker = destination / 'keep'
    marker.write_text('existing installation')
    with pytest.raises(ValueError, match='Unsafe'):
        bootstrap.extract_mongo_archive(archive, destination, 'Linux')
    assert marker.read_text() == 'existing installation'
    assert not (tmp_path / 'escape').exists()
    assert not list(tmp_path.glob('mongodb-extract-*'))


@pytest.mark.parametrize('kind,entry_kind', [('tar', 'symlink'), ('tar', 'hardlink'), ('zip', 'symlink')])
def test_extractor_rejects_links_even_outside_allowlist(bootstrap, tmp_path, kind, entry_kind):
    archive = tmp_path / 'fixture'
    fixture_archive(archive, kind, [('package/ignored', b'link fixture', entry_kind)])
    with pytest.raises(ValueError, match='Unsafe'):
        bootstrap.extract_mongo_archive(archive, tmp_path / 'mongodb', 'Linux')
    assert not (tmp_path / 'mongodb').exists()


@pytest.mark.parametrize('entries', [
    [('package/bin/mongod', b'a', 'file'), ('package/bin/MONGOD', b'b', 'file')],
    [('package/bin/mongod', b'a', 'file'), ('other/bin/mongod', b'b', 'file')],
    [('package/README', b'a', 'file')],
])
def test_extractor_rejects_ambiguous_or_incomplete_archive(bootstrap, tmp_path, entries):
    archive = tmp_path / 'fixture.tgz'
    fixture_archive(archive, 'tar', entries)
    with pytest.raises(ValueError):
        bootstrap.extract_mongo_archive(archive, tmp_path / 'mongodb', 'Linux')


def fixture_catalog(system, machine):
    target, arch = runtime_target(system, machine)
    suffix = 'zip' if system == 'Windows' else 'tgz'
    return {'versions': [{'version': '8.0.38', 'downloads': [{
        'target': target, 'arch': arch, 'edition': 'targeted' if system == 'Linux' else 'base',
        'archive': {'url': f'https://fastdl.mongodb.org/fixture.{suffix}', 'sha256': 'a' * 64},
    }]}]}


@pytest.mark.parametrize('system,machine', [('Linux', 'aarch64'), ('Linux', 'x86_64'), ('Darwin', 'arm64'), ('Darwin', 'x86_64'), ('Windows', 'AMD64')])
def test_selects_exact_pinned_community_artifact(bootstrap, system, machine):
    catalog = fixture_catalog(system, machine)
    artifact = catalog['versions'][0]['downloads'][0]
    enterprise = dict(artifact, edition='enterprise')
    catalog['versions'][0]['downloads'].insert(0, enterprise)
    catalog['versions'].insert(0, {'version': '99.0.0', 'downloads': [enterprise]})
    assert bootstrap.select_artifact(catalog, system, machine) is artifact


def test_missing_artifact_is_actionable(bootstrap):
    with pytest.raises(RuntimeError, match='8.0.38.*macos/arm64'):
        bootstrap.select_artifact({'versions': []}, 'Darwin', 'arm64')


def test_checksum_mismatch_prevents_extraction(bootstrap, monkeypatch, tmp_path):
    catalog = fixture_catalog('Linux', 'x86_64')
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return catalog
    monkeypatch.setattr(bootstrap.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(bootstrap.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(bootstrap.requests, 'get', lambda *a, **kw: Response())
    monkeypatch.setattr(bootstrap, 'RUNTIME', tmp_path)
    monkeypatch.setattr(bootstrap, 'download', lambda url, path: path.write_bytes(b'synthetic bad checksum fixture'))
    with pytest.raises(ValueError, match='checksum mismatch'):
        bootstrap.mongo()
    assert not (tmp_path / 'mongodb').exists()
    assert not (tmp_path / 'mongodb-provenance.json').exists()


def test_import_by_script_path_has_no_runtime_side_effects(tmp_path):
    # Copy only source, deliberately omit project cwd/PYTHONPATH and .runtime.
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts/bootstrap_runtime.py').write_bytes((BASE / 'scripts/bootstrap_runtime.py').read_bytes())
    (tmp_path / 'platform_support.py').write_bytes((BASE / 'platform_support.py').read_bytes())
    source = "import runpy,sys; runpy.run_path(sys.argv[1], run_name='import_only')"
    subprocess.run([sys.executable, '-I', '-c', source, str(tmp_path / 'scripts/bootstrap_runtime.py')], cwd=tmp_path.parent, check=True)
    assert not (tmp_path / '.runtime').exists()
