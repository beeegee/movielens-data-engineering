"""Real MongoDB loader regressions; fixture is SYNTHETIC, never MovieLens data.

Run: TEST_MONGO_URI='mongodb://127.0.0.1:27118/?replicaSet=movielens-rs' \
     .venv/bin/python -m pytest -q tests/test_loader.py
Each test gets a randomly named isolated DB, removed in fixture teardown.
No mongomock, downloads, or service startup. Without TEST_MONGO_URI DB tests skip.
"""

import csv
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import uuid
import zipfile

import pytest
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from werkzeug.security import check_password_hash

SPEC = importlib.util.spec_from_file_location("movielens_loader_under_test", Path(__file__).resolve().parents[1] / "load_data.py")
loader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(loader)

RATINGS = [(1, 1, 0.5, 100), (2, 2, 5.0, 101), (3, 1, 4.5, 102),
           (4, 1, 3.0, 103), (5, 2, 1.5, 104), (6, 1, 5.0, 105),
           (1, 1, 2.5, 106)]  # Preserve even repeated original user/movie rows.
SCORES = [(1, 1, 0.99), (2, 1, 0.2), (1, 2, 0.98), (1, 3, 0.97),
          (1, 4, 0.96), (1, 5, 0.95), (1, 6, 0.1), (1, 7, 0.95),
          (2, 2, 0.8), (1, 8, 0.99999), (1, 9, 0.02), (2, 3, 0.1)]


def synthetic_zip(path, scores=SCORES):
    """Explicitly tiny synthetic six-CSV fixture with interleaved boundaries."""
    data = {
        "movies.csv": (["movieId", "title", "genres"],
                       [(1, "First (1999)", "Action| Comedy |Action"),
                        (2, "No year", "(no genres listed)"),
                        (3, "Unknown", ""), (4, " Last (2000) ", "Drama")]),
        "links.csv": (["movieId", "imdbId", "tmdbId"], [(1, "0012345", "10"), (2, "", "")]),
        "ratings.csv": (["userId", "movieId", "rating", "timestamp"], RATINGS),
        "tags.csv": (["userId", "movieId", "tag", "timestamp"],
                     [(1, 1, " FUN ", 1), (2, 1, "fun", 2), (3, 1, "", 3),
                      (4, 4, " NA ", 4), (5, 1, "Other", 5), (6, 2, "no genre", 6)]),
        "genome-tags.csv": (["tagId", "tag"], [(i, f"Label {i}") for i in range(1, 10)]),
        "genome-scores.csv": (["movieId", "tagId", "relevance"], scores),
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SYNTHETIC-NOT-OFFICIAL.txt", "Tiny test data; NOT the official MovieLens corpus.")
        for filename, (header, rows) in data.items():
            stream = io.StringIO()
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)
            archive.writestr("synthetic/" + filename, stream.getvalue())
    return path


@pytest.fixture
def archive(tmp_path):
    return synthetic_zip(tmp_path / "synthetic-not-official.zip")


@pytest.fixture
def accounts(monkeypatch):
    for key, value in {"ADMIN_USERNAME": "fixture_admin", "ADMIN_PASSWORD": "synthetic-admin-password",
                       "USER_USERNAME": "fixture_user", "USER_PASSWORD": "synthetic-user-password"}.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def db(accounts):
    uri = os.getenv("TEST_MONGO_URI")
    if not uri:
        pytest.skip("Set TEST_MONGO_URI to run against a real isolated MongoDB database")
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")  # Configured-but-unreachable is a failure, not a skip.
    name = "movielens_loader_test_" + uuid.uuid4().hex
    try:
        yield client[name]
    finally:
        client.drop_database(name)
        assert name not in client.list_database_names()
        client.close()


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 250000])
def test_full_retention_conservation_and_top_five(db, archive, chunk_size):
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    meta = loader.load_dataset(db, archive, chunk_size=chunk_size)
    assert meta["status"] == "complete"
    assert meta["sourceRowCounts"] == {"movies": 4, "ratings": 7, "tags": 6, "links": 2,
                                         "genome_tags": 9, "genome_scores": 12}
    assert meta["dataset"] == "synthetic-not-official.zip"
    raw = list(db.ratings.find({}, {"_id": 0}).sort("timestamp", 1))
    assert raw == [dict(zip(("userId", "movieId", "rating", "timestamp"), row)) for row in RATINGS]
    assert all("username" not in rating for rating in raw)
    movies = list(db.movies.find())
    assert sum(m["ratingCount"] for m in movies) == len(RATINGS)
    assert sum(m["ratingSum"] for m in movies) == sum(r[2] for r in RATINGS)
    for movie in movies:
        assert sum(movie["ratingDistribution"].values()) == movie["ratingCount"]
        assert sum(float(k) * v for k, v in movie["ratingDistribution"].items()) == movie["ratingSum"]
        assert movie["avgRating"] == (movie["ratingSum"] / movie["ratingCount"] if movie["ratingCount"] else 0.0)
    first = db.movies.find_one({"movieId": 1})
    assert first["title"] == "First" and first["year"] == 1999
    assert first["genres"] == ["Action", "Comedy"]
    assert first["imdbId"] == "0012345" and first["tmdbId"] == "10"
    assert [tag["tagId"] for tag in first["topTags"]] == [8, 1, 2, 3, 4]
    assert first["topTags"][0]["relevance"] == 0.99999
    for mid in (2, 3):
        missing = db.movies.find_one({"movieId": mid})
        assert missing["year"] is None and missing["genres"] == []
        assert missing["imdbId"] == "" and missing["tmdbId"] == ""
    assert db.movies.find_one({"movieId": 3})["ratingSum"] == 0.0
    assert db.movies.find_one({"movieId": 4})["year"] == 2000
    assert db.tags.count_documents({}) == 6
    assert db.tags.find_one({"userId": 1})["tag"] == " FUN "
    assert db.tags.find_one({"userId": 3})["tag"] == ""
    assert db.genre_tags.find_one({"genre": "Action"})["topTags"][0] == {"tag": "fun", "count": 2}
    assert db.genre_tags.find_one({"genre": "Drama"})["topTag"] == "na"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest
    assert not list(archive.parent.glob(".movielens-etl-*"))
    assert check_password_hash(db.users.find_one({"username": "fixture_admin"})["password"], "synthetic-admin-password")
    assert db.users.find_one({"username": "fixture_user"})["role"] == "user"
    index = db.ratings.index_information()["unique_app_movie_username"]
    assert index["unique"] and index["partialFilterExpression"] == {"username": {"$exists": True}}
    db.ratings.insert_one({"movieId": 1, "username": "fixture_user", "rating": 4.0, "timestamp": 107})
    with pytest.raises(DuplicateKeyError):
        db.ratings.insert_one({"movieId": 1, "username": "fixture_user", "rating": 3.0})


def test_completed_rerun_and_explicit_reset_preserve_users(db, archive):
    original = loader.load_dataset(db, archive, chunk_size=2)
    db.users.update_one({"username": "fixture_user"}, {"$set": {"role": "admin", "password": "retained-value"}})
    db.users.insert_one({"username": "registered", "password": "keep", "role": "user"})
    db.ratings.insert_one({"movieId": 1, "username": "fixture_user", "rating": 4.0})
    db.movies.update_one({"movieId": 1}, {"$inc": {"ratingCount": 1, "ratingSum": 4.0}})
    again = loader.load_dataset(db, archive, chunk_size=1)
    assert again["completedAt"] == original["completedAt"]
    assert db.ratings.count_documents({}) == 8
    assert db.movies.find_one({"movieId": 1})["ratingCount"] == 6
    assert db.users.find_one({"username": "fixture_user"})["password"] == "retained-value"
    assert db.users.find_one({"username": "fixture_user"})["role"] == "admin"
    loader.load_dataset(db, archive, chunk_size=3, reset=True)
    assert db.ratings.count_documents({}) == 7
    assert db.users.count_documents({}) == 3
    assert db.users.find_one({"username": "registered"})["password"] == "keep"
    assert db.users.find_one({"username": "fixture_user"})["password"] == "retained-value"


def test_existing_and_failed_load_require_explicit_reset(db, archive):
    db.movies.insert_one({"movieId": 987})
    with pytest.raises(RuntimeError, match="reset"):
        loader.load_dataset(db, archive)
    assert db.movies.find_one({"movieId": 987})
    bad = synthetic_zip(archive.parent / "bad-synthetic.zip", scores=[(1, 1, 2.0)])
    with pytest.raises(ValueError, match="relevance"):
        loader.load_dataset(db, bad, reset=True)
    assert db.metadata.find_one({"_id": loader.METADATA_ID})["status"] == "failed"
    with pytest.raises(RuntimeError, match="reset"):
        loader.load_dataset(db, archive)
    assert loader.load_dataset(db, archive, reset=True)["status"] == "complete"


def test_changed_archive_requires_explicit_reset(db, archive):
    loader.load_dataset(db, archive)
    synthetic_zip(archive, scores=[(1, 1, 0.75)])
    with pytest.raises(RuntimeError, match="reset"):
        loader.load_dataset(db, archive)
    assert db.ratings.count_documents({}) == 7


def test_archive_preflight_preserves_existing_data(db, archive, monkeypatch):
    db.movies.insert_one({"movieId": 987})
    incomplete = archive.parent / "missing-synthetic.zip"
    with zipfile.ZipFile(incomplete, "w") as z:
        z.writestr("synthetic/movies.csv", "movieId,title,genres\n")
    with pytest.raises(ValueError, match="exactly one"):
        loader.load_dataset(db, incomplete, reset=True)
    monkeypatch.delenv("ADMIN_PASSWORD")
    with pytest.raises(ValueError, match="ADMIN_PASSWORD"):
        loader.load_dataset(db, archive, reset=True)
    assert db.movies.find_one({"movieId": 987})


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 250000])
def test_streaming_top_five_without_database(archive, chunk_size):
    with zipfile.ZipFile(archive) as z:
        members = loader.archive_members(z)
        top = {}
        for frame in loader.chunks(z, members, "genome-scores.csv", chunk_size):
            loader.merge_top_tags(top, frame)
        assert top[1] == [(0.99999, 8), (0.99, 1), (0.98, 2), (0.97, 3), (0.96, 4)]
        assert top[2] == [(0.8, 2), (0.2, 1), (0.1, 3)]
        # Both equal-score candidates occur across chunks; lower tagId wins.
        tie_top = {}
        tie_frame = loader.pd.DataFrame([(1, i, 0.8) for i in range(9, 0, -1)],
                                        columns=["movieId", "tagId", "relevance"])
        for start in range(0, len(tie_frame), chunk_size):
            loader.merge_top_tags(tie_top, tie_frame.iloc[start:start + chunk_size])
        assert [tag_id for _, tag_id in tie_top[1]] == [1, 2, 3, 4, 5]
        raw = []
        for frame in loader.chunks(z, members, "ratings.csv", chunk_size):
            raw.extend(frame.itertuples(index=False, name=None))
        assert raw == RATINGS


def test_archive_members_and_configuration(archive):
    with zipfile.ZipFile(archive) as z:
        assert len(loader.archive_members(z)) == 6
        assert set(loader.fingerprint(z, loader.archive_members(z))) == set(loader.CSV_FILES)
    with pytest.raises(ValueError, match="chunk-size"):
        loader.load_dataset(None, archive, chunk_size=250001)
