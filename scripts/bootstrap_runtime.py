"""Install project-local MongoDB and download the official MovieLens archive."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import json
import os
import platform
import shutil
import tarfile
import requests
import zipfile

BASE = Path(__file__).resolve().parents[1]
RUNTIME = BASE / '.runtime'
RUNTIME.mkdir(exist_ok=True)
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


def mongo():
    data = requests.get('https://downloads.mongodb.org/current.json', timeout=60).json()
    candidates = [(v['version'], a) for v in data['versions'] for a in v.get('downloads', [])
                  if v['version'] == MONGODB_VERSION and a.get('target') == 'ubuntu2204'
                  and a.get('arch') == 'x86_64' and a.get('edition') == 'targeted']
    version, artifact = max(candidates, key=lambda item: tuple(map(int, item[0].split('.'))))
    archive = RUNTIME / f'mongodb-{version}.tgz'
    download(artifact['archive']['url'], archive)
    digest = hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest()
    if digest != artifact['archive']['sha256']:
        raise ValueError('MongoDB checksum mismatch')
    destination = RUNTIME / 'mongodb'
    destination.mkdir(exist_ok=True)
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            relative = Path(*Path(member.name).parts[1:])
            if relative.parts and (relative.parts[0] == 'bin' or relative.name in ['LICENSE-Community.txt', 'SSPL.md', 'THIRD-PARTY-NOTICES']):
                member.name = str(relative)
                bundle.extract(member, destination, filter='data')
    (RUNTIME / 'mongodb-provenance.json').write_text(json.dumps({'version': version, 'url': artifact['archive']['url'], 'sha256': digest}, indent=2) + '\n')
    print(f'MongoDB {version}: verified and extracted', flush=True)


def dataset():
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
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise SystemExit('The bundled runtime bootstrap supports Linux x86_64 only. Use an external MongoDB replica set on other systems.')
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(mongo), pool.submit(dataset)]
        for job in jobs:
            job.result()
