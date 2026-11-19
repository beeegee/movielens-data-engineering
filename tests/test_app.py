"""Real replica-set regression tests; only fresh movielens_test_* DBs are written.
Run: MONGO_URI='mongodb://127.0.0.1:27118/?replicaSet=movielens-rs' .venv/bin/python -m pytest tests/test_app.py -q
No supplied MONGO_URI means explicit skips, never mock runtime evidence.
"""
import importlib.util
import os
from pathlib import Path
import secrets
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from werkzeug.security import generate_password_hash


def test_import_and_anonymous_guards_without_database(monkeypatch):
    # This exercises actual Flask routing, not a mocked Mongo backend.
    monkeypatch.setenv("MONGO_URI", "mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=20")
    monkeypatch.setenv("SECRET_KEY", secrets.token_hex(32))
    spec = importlib.util.spec_from_file_location("movielens_import_smoke", Path(__file__).resolve().parents[1] / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        client = module.app.test_client()
        for target in ["/api/years?demo_user=admin", "/api/admin/users?demo_user=admin"]:
            response = client.get(target)
            assert response.status_code == 401 and response.is_json
        assert client.post("/api/ratings/submit", json={"movieId": 1, "rating": 4}).status_code == 400
        with module.app.test_request_context():
            assert module.safe_next("/admin") == "/admin"
            for target in ["https://evil.invalid", "//evil.invalid", "/%2Fevil.invalid", "/%0aLocation:evil", "/\\evil.invalid"]:
                assert module.safe_next(target) == "/"
    finally:
        module.client.close()


@pytest.fixture
def backend(monkeypatch):
    uri = os.environ.get("MONGO_URI")
    if not uri:
        pytest.skip("MONGO_URI required for real replica-set tests")
    connection = MongoClient(uri, serverSelectionTimeoutMS=5000)
    hello = connection.admin.command("hello")
    assert hello.get("setName"), "Tests require a real MongoDB replica set"
    name = "movielens_test_" + uuid.uuid4().hex
    monkeypatch.setenv("DB_NAME", name)
    monkeypatch.setenv("SECRET_KEY", secrets.token_hex(32))
    spec = importlib.util.spec_from_file_location("movielens_backend_test", Path(__file__).resolve().parents[1] / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    database = module.db
    assert database.name == name and name.startswith("movielens_test_")
    try:
        database.users.create_index("username", unique=True)
        database.movies.create_index("movieId", unique=True)
        database.ratings.create_index([("movieId", 1), ("username", 1)], unique=True,
            partialFilterExpression={"username": {"$exists": True}})
        database.create_collection("rating_events")
        database.users.insert_many([
            {"username": "reader", "role": "user", "password": generate_password_hash("test-pass-only")},
            {"username": "operator", "role": "admin", "password": generate_password_hash("test-pass-only")},
        ])
        database.movies.insert_many([
            {"movieId": 1, "title": "Fixture", "year": 2001, "genres": ["Drama"],
             "ratingCount": 3, "ratingSum": 10.0, "avgRating": 3.33,
             "ratingDistribution": {"3.0": 2, "4.0": 1}},
            {"movieId": 2, "title": "Unrated", "year": 2002, "genres": ["Drama"],
             "ratingCount": 0, "ratingSum": 0.0, "avgRating": 0, "ratingDistribution": {}},
        ])
        database.ratings.insert_many([{"movieId": 1, "userId": i, "rating": value, "timestamp": 1}
                                     for i, value in enumerate([3.0, 3.0, 4.0], 1)])
        yield module
    finally:
        assert name.startswith("movielens_test_")
        connection.drop_database(name)
        module.client.close()
        connection.close()


def logged_client(backend, username="reader"):
    client = backend.app.test_client()
    with client.session_transaction() as state:
        state["csrf_token"] = "test-csrf"
    response = client.post("/login", data={"username": username, "password": "test-pass-only", "csrf_token": "test-csrf"})
    assert response.status_code == 302
    with client.session_transaction() as state:
        state["csrf_token"] = "test-csrf"
    return client


def submit(client, rating, movie=1, **extra):
    return client.post("/api/ratings/submit", json={"movieId": movie, "rating": rating, **extra},
                       headers={"X-CSRF-Token": "test-csrf"})


def assert_summary(database, movie_id):
    records = list(database.ratings.find({"movieId": movie_id}))
    movie = database.movies.find_one({"movieId": movie_id})
    total = sum(r["rating"] for r in records)
    assert movie["ratingCount"] == len(records)
    assert movie["ratingSum"] == total
    assert movie["avgRating"] == pytest.approx(total / len(records))
    expected = Counter(f"{r['rating']:.1f}" for r in records)
    assert {k: v for k, v in movie["ratingDistribution"].items() if v} == dict(expected)


def test_demo_bypass_denied(backend):
    client = backend.app.test_client()
    for username in ["operator", "reader", "nonexistent"]:
        for path in ["/api/admin/users", "/api/years"]:
            response = client.get(path, query_string={"demo_user": username})
            assert response.status_code == 401 and response.is_json
    assert client.get("/?demo_user=operator").status_code == 302


def test_permissions_follow_database_not_cookie(backend):
    client = logged_client(backend)
    with client.session_transaction() as state:
        state["role"] = "admin"
    assert client.get("/api/admin/users").status_code == 403
    backend.db.users.update_one({"username": "reader"}, {"$set": {"role": "admin"}})
    assert client.get("/api/admin/users").status_code == 200
    backend.db.users.update_one({"username": "reader"}, {"$set": {"role": "user"}})
    assert client.get("/api/admin/users").status_code == 403
    backend.db.users.delete_one({"username": "reader"})
    assert client.get("/api/years").status_code == 401


def test_admin_toggle_role(backend):
    regular = logged_client(backend)
    assert regular.post("/api/admin/toggle_role", json={"username": "operator"}, headers={"X-CSRF-Token": "test-csrf"}).status_code == 403
    admin = logged_client(backend, "operator")
    result = admin.post("/api/admin/toggle_role", json={"username": "reader"}, headers={"X-CSRF-Token": "test-csrf"})
    assert result.status_code == 200
    assert backend.db.users.find_one({"username": "reader"})["role"] == "admin"
    assert regular.get("/api/admin/users").status_code == 200


@pytest.mark.parametrize("rating", [None, True, False, "NaN", "Infinity", "-Infinity", 0, 5.5, 3.1, [], {}])
def test_invalid_ratings(backend, rating):
    response = submit(logged_client(backend), rating)
    assert response.status_code == 400 and response.is_json
    assert backend.db.ratings.count_documents({"username": {"$exists": True}}) == 0
    assert backend.db.movies.find_one({"movieId": 1})["ratingSum"] == 10


@pytest.mark.parametrize("movie", [True, 1.2, "1.2", {}, [], -1, 0, 2**63])
def test_invalid_movie_ids(backend, movie):
    assert submit(logged_client(backend), 4, movie).status_code == 400


def test_csrf_and_json_validation(backend):
    client = logged_client(backend)
    assert client.post("/api/ratings/submit", json={"movieId": 1, "rating": 4}).status_code == 400
    assert client.post("/api/ratings/submit", json=[], headers={"X-CSRF-Token": "test-csrf"}).status_code == 400
    assert submit(client, 4, 999).status_code == 404


def test_first_edit_exact_sum_and_ownership(backend):
    client = logged_client(backend)
    first = submit(client, 4.5, username="operator")
    assert first.status_code == 200
    assert first.json["newAvg"] == 14.5 / 4  # Not rounded baseline avg * count.
    assert first.json["newCount"] == 4
    assert_summary(backend.db, 1)
    edited = submit(client, 1.5)
    assert edited.status_code == 200 and edited.json["newCount"] == 4
    assert backend.db.ratings.count_documents({"username": "operator"}) == 0
    assert backend.db.ratings.count_documents({"username": "reader", "movieId": 1}) == 1
    assert_summary(backend.db, 1)
    assert backend.db.rating_events.count_documents({}) == 2
    assert submit(client, 1.5).status_code == 200
    assert backend.db.rating_events.count_documents({}) == 2
    assert_summary(backend.db, 1)


def test_missing_sum_does_not_write(backend):
    backend.db.movies.update_one({"movieId": 1}, {"$unset": {"ratingSum": ""}})
    assert submit(logged_client(backend), 4).status_code == 409
    assert backend.db.ratings.count_documents({"username": "reader"}) == 0
    assert backend.db.rating_events.count_documents({}) == 0


@pytest.mark.parametrize("same_user", [True, False])
def test_concurrent_submissions(backend, same_user):
    names = ["reader"] * 8 if same_user else [f"voter{i}" for i in range(8)]
    if not same_user:
        backend.db.users.insert_many([{"username": n, "role": "user", "password": generate_password_hash("test-pass-only")} for n in names])
    clients = [logged_client(backend, name) for name in names]
    barrier = Barrier(len(clients))
    def vote(pair):
        index, client = pair
        barrier.wait(timeout=15)
        response = submit(client, (index % 10 + 1) / 2, 2)
        return response.status_code, response.json
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(vote, enumerate(clients)))
    assert all(status == 200 for status, body in results), results
    assert backend.db.ratings.count_documents({"movieId": 2}) == (1 if same_user else 8)
    assert_summary(backend.db, 2)
    with pytest.raises(DuplicateKeyError):
        backend.db.ratings.insert_one({"movieId": 2, "username": names[0], "rating": 5})


def test_zero_bucket_and_stats(backend):
    admin = logged_client(backend, "operator")
    buckets = admin.get("/api/ratings/total_distribution").json
    assert next(b for b in buckets if b["range"] == "0")["count"] == 1
    assert sum(b["count"] for b in buckets) == 2
    assert submit(admin, 5, 2).status_code == 200
    buckets = admin.get("/api/ratings/total_distribution").json
    assert next(b for b in buckets if b["range"] == "0")["count"] == 0
    stats = admin.get("/api/admin/stats")
    assert stats.status_code == 200
    assert stats.json["userRatingsCount"] == 1
    assert stats.json["latestRatings"][0]["username"] == "operator"
    assert stats.json["totalMovies"] == 2
    assert stats.json["baselineRatings"] == 3
    assert stats.json["totalRatings"] == 4
    assert stats.json["baselineRatingsEstimated"] is True
    backend.db.metadata.insert_one({"_id": "movielens", "status": "complete", "sourceRowCounts": {"ratings": 3, "tags": 20, "genome_scores": 50}})
    stats = admin.get("/api/admin/stats")
    assert stats.json["baselineRatings"] == 3
    assert stats.json["totalRatings"] == 4
    assert stats.json["totalTags"] == 20
    assert stats.json["genomeScores"] == 50
    assert stats.json["baselineRatingsEstimated"] is False
    assert len(stats.json["recentEvents"]) == 1


@pytest.mark.parametrize("target", ["https://evil.invalid", "//evil.invalid", "/\\evil.invalid", "///evil.invalid", "/%0aLocation:evil"])
def test_safe_next(backend, target):
    client = backend.app.test_client()
    with client.session_transaction() as state:
        state["csrf_token"] = "test-csrf"
    response = client.post("/login", query_string={"next": target}, data={"username": "reader", "password": "test-pass-only", "csrf_token": "test-csrf"})
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/")
    assert not response.headers["Location"].startswith("//")
    assert response.headers["Location"] == "/"


def test_years_include_early_cinema_and_exclude_unknown(backend):
    backend.db.movies.insert_many([
        {"movieId": 3, "title": "Early cinema fixture", "year": 1888},
        {"movieId": 4, "title": "Turn of century fixture", "year": 1900},
        {"movieId": 5, "title": "Unknown year fixture", "year": 0},
        {"movieId": 6, "title": "Missing year fixture", "year": None},
    ])
    response = logged_client(backend).get("/api/years")
    assert response.status_code == 200
    assert response.json == [2002, 2001, 1900, 1888]


def test_dashboard_api_shapes(backend):
    client = logged_client(backend)
    for path in ["/api/tags/genre", "/api/movies/top_rated", "/api/genres/coverage", "/api/years", "/api/ratings/total_distribution"]:
        response = client.get(path)
        assert response.status_code == 200 and isinstance(response.json, list)
    for path, key in [("/api/tags/top/1", "tags"), ("/api/ratings/distribution/1", "distribution"), ("/api/genres/releases?year=2001", "releases")]:
        response = client.get(path)
        assert response.status_code == 200 and isinstance(response.json[key], list)
