"""Install project-local MongoDB and download the official MovieLens archive."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import json
import platform
import shutil
import tarfile
import stat
import sys
import tempfile
from urllib.parse import urlparse
import requests
import zipfile

BASE = Path(__file__).resolve().parents[1]
RUNTIME = BASE / '.runtime'
sys.path.insert(0, str(BASE))
from platform_support import mongo_executable, runtime_target
MONGODB_VERSION = '8.0.38'


def download(url, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + '.part')
    if target.exists():
        return
    with requests.get(url, stream=True, timeout=(30, 120)) as response:
        response.raise_for_status()
        with partial.open('wb') as output:
            for chunk in response.iter_content(1024 * 1024):
                output.write(chunk)
    partial.replace(target)


def select_artifact(catalog, system, machine):
    """Select the pinned Community artifact using publisher metadata, not URL guesses."""
    target, arch = runtime_target(system, machine)
    edition = 'targeted' if system == 'Linux' else 'base'
    candidates = [artifact for version in catalog['versions']
                  if version['version'] == MONGODB_VERSION
                  for artifact in version.get('downloads', [])
                  if (artifact.get('target'), artifact.get('arch'), artifact.get('edition'))
                  == (target, arch, edition)]
    if len(candidates) != 1:
        raise RuntimeError(f'Expected one MongoDB {MONGODB_VERSION} Community artifact for {target}/{arch}; found {len(candidates)}')
    artifact = candidates[0]
    archive = artifact['archive']
    url = urlparse(archive['url'])
    suffix = '.zip' if system == 'Windows' else '.tgz'
    if url.scheme != 'https' or url.netloc != 'fastdl.mongodb.org' or not url.path.endswith(suffix):
        raise ValueError('Unexpected MongoDB publisher archive URL')
    checksum = archive['sha256']
    if len(checksum) != 64 or any(c not in '0123456789abcdef' for c in checksum):
        raise ValueError('Invalid MongoDB publisher SHA-256')
    return artifact


LICENSES = {'LICENSE-Community.txt', 'SSPL.md', 'THIRD-PARTY-NOTICES', 'MPL-2'}


def archive_relative(name):
    """Validate even ignored entries against POSIX and Windows path attacks."""
    parts = name.rstrip('/').split('/')
    if any(p in {'', '.', '..'} or any(c in p for c in ':\\<>"|?*')
           or any(ord(c) < 32 for c in p) or p.endswith((' ', '.')) for p in parts):
        raise ValueError(f'Unsafe MongoDB archive path: {name!r}')
    # Reject Windows device names, including names with extensions.
    reserved = {'CON', 'PRN', 'AUX', 'NUL'} | {f'{p}{i}' for p in ('COM', 'LPT') for i in range(1, 10)}
    if any(p.split('.')[0].upper() in reserved for p in parts):
        raise ValueError(f'Unsafe MongoDB archive path: {name!r}')
    return parts[0], Path(*parts[1:]) if len(parts) > 1 else None


def extract_mongo_archive(archive, destination, system=None):
    """Extract only regular bin/license files into a fresh staging directory.

    No archive extraction API is trusted with paths, links, devices, or modes.
    Failed validation leaves an existing installation untouched.
    """
    system = platform.system() if system is None else system
    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError('MongoDB destination must not be a symlink')
    destination.parent.mkdir(parents=True, exist_ok=True)
    roots = set()
    written = set()
    with tempfile.TemporaryDirectory(prefix='mongodb-extract-', dir=destination.parent) as temporary:
        stage = Path(temporary) / 'mongodb'
        stage.mkdir()

        def copy_member(name, directory, mode, opener):
            root, relative = archive_relative(name)
            roots.add(root)
            if len(roots) != 1:
                raise ValueError('MongoDB archive must contain one top-level directory')
            if relative is None:
                if not directory:
                    raise ValueError('MongoDB archive root must be a directory')
                return
            if directory:
                return
            if relative.parts[0] != 'bin' and not (len(relative.parts) == 1 and relative.name in LICENSES):
                return
            key = relative.as_posix().casefold()
            if key in written:
                raise ValueError(f'Duplicate MongoDB archive path: {name}')
            written.add(key)
            output = stage / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            with opener() as source, output.open('xb') as target:
                shutil.copyfileobj(source, target)
            output.chmod(0o755 if mode & 0o111 else 0o644)

        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    mode = member.external_attr >> 16
                    kind = stat.S_IFMT(mode)
                    if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                        raise ValueError(f'Unsafe MongoDB ZIP entry: {member.filename}')
                    copy_member(member.filename, member.is_dir(), mode,
                                lambda m=member: bundle.open(m))
        else:
            with tarfile.open(archive, 'r:gz') as bundle:
                for member in bundle:
                    if not member.isfile() and not member.isdir():
                        raise ValueError(f'Unsafe MongoDB tar entry: {member.name}')
                    copy_member(member.name, member.isdir(), member.mode,
                                lambda m=member: bundle.extractfile(m))
        executable = stage / 'bin' / ('mongod.exe' if system == 'Windows' else 'mongod')
        if not executable.is_file():
            raise ValueError(f'MongoDB archive is missing bin/{executable.name}')
        if destination.exists():
            shutil.rmtree(destination)
        stage.replace(destination)


def mongo():
    system = platform.system()
    machine = platform.machine()
    runtime_target(system, machine)  # Fail before network/filesystem work.
    response = requests.get('https://downloads.mongodb.org/current.json', timeout=60)
    response.raise_for_status()
    artifact = select_artifact(response.json(), system, machine)
    suffix = '.zip' if system == 'Windows' else '.tgz'
    target, arch = runtime_target(system, machine)
    archive = RUNTIME / f'mongodb-{MONGODB_VERSION}-{target}-{arch}{suffix}'
    download(artifact['archive']['url'], archive)
    with archive.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    if digest != artifact['archive']['sha256']:
        raise ValueError('MongoDB checksum mismatch; remove the cached archive and retry')
    extract_mongo_archive(archive, RUNTIME / 'mongodb', system)
    (RUNTIME / 'mongodb-provenance.json').write_text(json.dumps({
        'version': MONGODB_VERSION, 'target': target, 'arch': arch,
        'url': artifact['archive']['url'], 'sha256': digest,
    }, indent=2) + '\n')
    print(f'MongoDB {MONGODB_VERSION}: verified and extracted', flush=True)
    return mongo_executable(RUNTIME.parent, system)


def dataset():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    target = BASE / 'data/ml-25m.zip'
    url = 'https://files.grouplens.org/datasets/movielens/ml-25m.zip'
    download(url, target)
    with zipfile.ZipFile(target) as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError(f'CSV archive CRC failure: {bad}')
        csv_names = [n for n in archive.namelist() if n.endswith('.csv')]
        if len(csv_names) != 6:
            raise ValueError(f'Expected six CSV files, found {len(csv_names)}')
    (RUNTIME / 'dataset-provenance.json').write_text(json.dumps({'url': url, 'sha256': hashlib.file_digest(target.open('rb'), 'sha256').hexdigest(), 'bytes': target.stat().st_size, 'csv_files': csv_names, 'zip_crc': 'PASS'}, indent=2) + '\n')
    print('MovieLens 25M: downloaded and ZIP CRC verified', flush=True)


if __name__ == '__main__':
    runtime_target(platform.system(), platform.machine())
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(mongo), pool.submit(dataset)]
        for job in jobs:
            job.result()
