"""Explicit project-local setup and foreground application runner.

Setup downloads MongoDB and MovieLens only into this project. Run supervises
MongoDB when needed and shuts down processes it owns on Ctrl+C. A MongoDB
instance that was already running is never stopped by this script.
"""
from contextlib import contextmanager
from pathlib import Path
import argparse
import os
import secrets
import subprocess
import sys
import time

from dotenv import dotenv_values
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, OperationFailure

BASE = Path(__file__).resolve().parents[1]
RUNTIME = BASE / '.runtime'
LOCAL_URI = 'mongodb://127.0.0.1:27118/?replicaSet=movielens-rs'


def configuration():
    path = BASE / '.env'
    if not path.exists():
        content = ('MONGO_URI=' + LOCAL_URI + '\nDB_NAME=movielens_course_fixed\nPORT=5008\n'
                   'SECRET_KEY=' + secrets.token_hex(32) + '\n'
                   'ADMIN_USERNAME=admin\nADMIN_PASSWORD=' + secrets.token_urlsafe(18) + '\n'
                   'USER_USERNAME=ben\nUSER_PASSWORD=' + secrets.token_urlsafe(18) + '\n')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
        print('Created private .env. Open it locally for the account credentials.', flush=True)
    values = {key: value for key, value in dotenv_values(path).items() if value is not None}
    for key in ('MONGO_URI', 'DB_NAME', 'SECRET_KEY'):
        if not values.get(key):
            raise RuntimeError(f'Set {key} in .env.')
    return values


def primary_ready(client):
    try:
        return bool(client.admin.command('hello').get('isWritablePrimary'))
    except ConnectionFailure:
        return False


@contextmanager
def mongodb(values):
    uri = values['MONGO_URI']
    client = MongoClient(uri, serverSelectionTimeoutMS=1500)
    owned = None
    log = None
    try:
        if not primary_ready(client):
            if uri != LOCAL_URI:
                raise RuntimeError('Start the replica-set MongoDB configured in .env first.')
            executable = RUNTIME / 'mongodb/bin/mongod'
            if not executable.exists():
                raise RuntimeError('MongoDB is missing. Run scripts/manage.py setup first.')
            (RUNTIME / 'db').mkdir(parents=True, exist_ok=True)
            log = (RUNTIME / 'mongodb-startup.log').open('a')
            owned = subprocess.Popen([
                str(executable), '--dbpath', str(RUNTIME / 'db'),
                '--bind_ip', '127.0.0.1', '--port', '27118', '--replSet', 'movielens-rs',
                '--oplogSize', '128', '--wiredTigerCacheSizeGB', '1',
                '--logpath', str(RUNTIME / 'mongod.log')], cwd=BASE,
                stdout=log, stderr=subprocess.STDOUT)
            direct = MongoClient('mongodb://127.0.0.1:27118/?directConnection=true', serverSelectionTimeoutMS=1500)
            try:
                for attempt in range(30):
                    if owned.poll() is not None:
                        raise RuntimeError('MongoDB stopped during startup. Read .runtime/mongod.log.')
                    try:
                        direct.admin.command('ping')
                        break
                    except ConnectionFailure:
                        time.sleep(.5)
                else:
                    raise RuntimeError('MongoDB startup timed out.')
                try:
                    direct.admin.command('replSetInitiate', {'_id':'movielens-rs', 'members':[{'_id':0,'host':'127.0.0.1:27118'}]})
                except OperationFailure as error:
                    if error.code != 23:  # AlreadyInitialized
                        raise
            finally:
                direct.close()
            for attempt in range(40):
                if primary_ready(client):
                    break
                if owned.poll() is not None:
                    raise RuntimeError('MongoDB stopped before becoming primary.')
                time.sleep(.5)
            else:
                raise RuntimeError('MongoDB replica set did not elect a primary.')
        print('MongoDB replica-set primary ready.', flush=True)
        yield client
    finally:
        client.close()
        if owned is not None and owned.poll() is None:
            owned.terminate()
            try:
                owned.wait(timeout=20)
            except subprocess.TimeoutExpired:
                owned.kill()
                owned.wait()
        if log is not None:
            log.close()


def loader_command(reset_data=False):
    command = [sys.executable, '-u', 'load_data.py', '--zip', 'data/ml-25m.zip']
    if reset_data:
        command.append('--reset')
    return command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['setup','run'])
    parser.add_argument('--reset-data', action='store_true',
                        help='setup only: replace dataset collections, app ratings and rating history; preserve accounts')
    args = parser.parse_args()
    if args.reset_data and args.action != 'setup':
        parser.error('--reset-data is only valid with setup')
    if args.reset_data:
        print('WARNING: replacing dataset collections, app ratings and rating history. Accounts are preserved.', flush=True)
    values = configuration()
    environment = dict(os.environ, **values)
    if args.action == 'setup':
        subprocess.run([sys.executable, 'scripts/bootstrap_runtime.py'], cwd=BASE, env=environment, check=True)
        with mongodb(values):
            subprocess.run(loader_command(args.reset_data), cwd=BASE, env=environment, check=True)
        print('Setup complete. Run scripts/manage.py run to start the app.', flush=True)
        return
    with mongodb(values) as client:
        metadata = client[values['DB_NAME']].metadata.find_one({'_id':'movielens'})
        if not metadata or metadata.get('status') != 'complete':
            raise RuntimeError('Dataset is not ready. Run scripts/manage.py setup first.')
        print('MovieLens Analytics: http://127.0.0.1:' + values.get('PORT', '5008'), flush=True)
        print('Press Ctrl+C to stop the app and any MongoDB process started here.', flush=True)
        process = subprocess.Popen([sys.executable, 'app.py'], cwd=BASE, env=environment)
        try:
            code = process.wait()
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            code = 0
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        if code:
            raise SystemExit(code)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
