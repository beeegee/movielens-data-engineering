"""Stream all six MovieLens CSVs from a retained local ZIP into MongoDB.

Ratings and user tags are retained in full; genome scores are reduced to exact
per-movie top five (ties: lower tagId first). No downloads or archive extraction.
Run with the application stopped: --reset replaces dataset collections (including
app ratings), but NEVER users. An interrupted load requires explicit --reset.
Memory scales with chunk size, movie count and five genome tags per movie, not
rating/tag/genome-score row count. Genre tag counts spill to local SQLite.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import zipfile

import pandas as pd
from dotenv import load_dotenv
from pymongo import MongoClient
from werkzeug.security import generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ZIP = BASE_DIR / "data" / "ml-25m.zip"
SCHEMA_VERSION = 2
METADATA_ID = "movielens"
DATA_COLLECTIONS = ("movies", "ratings", "tags", "links", "genome_tags", "genre_tags")
CSV_FILES = ("movies.csv", "ratings.csv", "tags.csv", "links.csv", "genome-tags.csv", "genome-scores.csv")
RATING_KEYS = tuple(str(i / 2) for i in range(1, 11))


def now():
    return datetime.now(timezone.utc)


def archive_members(archive):
    result = {}
    for name in CSV_FILES:
        matches = [n for n in archive.namelist() if not n.endswith("/") and Path(n).name == name]
        if len(matches) != 1:
            raise ValueError(f"ZIP must contain exactly one {name}; found {len(matches)}")
        result[name] = matches[0]
    return result


def fingerprint(archive, members):
    # Central-directory sizes and CRCs identify contents without a second full read.
    return {name: {"size": archive.getinfo(member).file_size,
                   "crc32": archive.getinfo(member).CRC}
            for name, member in members.items()}


def chunks(archive, members, name, chunk_size, dtype=None):
    with archive.open(members[name]) as stream:
        yield from pd.read_csv(stream, chunksize=chunk_size, dtype=dtype,
                               keep_default_na=False)


def insert_frame(collection, frame, batch_size=10_000):
    # Avoid retaining hundreds of thousands of Python dicts alongside the frame.
    for start in range(0, len(frame), batch_size):
        collection.insert_many(frame.iloc[start:start + batch_size].to_dict("records"), ordered=False)


def merge_top_tags(top, frame):
    """Merge exact chunk top-five into global top-five, independent of row order."""
    if not frame["relevance"].between(0, 1).all():
        raise ValueError("Genome relevance outside [0,1]")
    local = frame.sort_values(["movieId", "relevance", "tagId"], ascending=[True, False, True]).groupby("movieId", sort=False).head(5)
    for mid, group in local.groupby("movieId", sort=False):
        mid = int(mid)
        candidates = top.get(mid, []) + [(float(r.relevance), int(r.tagId)) for r in group.itertuples(index=False)]
        top[mid] = sorted(candidates, key=lambda pair: (-pair[0], pair[1]))[:5]


def account_specs():
    specs = []
    for prefix, role in (("ADMIN", "admin"), ("USER", "user")):
        username = os.getenv(f"{prefix}_USERNAME", "").strip()
        password = os.getenv(f"{prefix}_PASSWORD", "")
        if not username or not password:
            raise ValueError(f"Set {prefix}_USERNAME and {prefix}_PASSWORD before loading")
        specs.append((username, password, role))
    if specs[0][0] == specs[1][0]:
        raise ValueError("ADMIN_USERNAME and USER_USERNAME must differ")
    return specs


def seed_accounts(db, specs):
    db.users.create_index("username", unique=True)
    for username, password, role in specs:
        if db.users.find_one({"username": username}, {"_id": 1}) is None:
            db.users.update_one({"username": username}, {"$setOnInsert": {
                "username": username, "password": generate_password_hash(password),
                "role": role, "createdAt": now().isoformat()}}, upsert=True)


def create_indexes(db):
    db.movies.create_index("movieId", unique=True)
    db.movies.create_index([("title", "text")])
    db.movies.create_index([("year", 1), ("avgRating", -1), ("ratingCount", -1)])
    db.movies.create_index([("avgRating", -1), ("ratingCount", -1)])
    db.movies.create_index("genres")
    db.ratings.create_index([("movieId", 1), ("userId", 1)])
    db.ratings.create_index([("movieId", 1), ("username", 1)], unique=True,
                            partialFilterExpression={"username": {"$exists": True}},
                            name="unique_app_movie_username")
    db.ratings.create_index([("username", 1), ("timestamp", -1)],
                            partialFilterExpression={"username": {"$exists": True}})
    db.tags.create_index("movieId")
    db.links.create_index("movieId", unique=True)
    db.genome_tags.create_index("tagId", unique=True)
    db.genre_tags.create_index("genre", unique=True)


def load_dataset(db, zip_path, chunk_size=100_000, reset=False):
    """Load into an explicitly supplied database. Caller owns client lifecycle.

    metadata {_id:'movielens', status:'complete', dataset, sourceRowCounts,
    schemaVersion, archiveFingerprint, startedAt, completedAt} is the load marker.
    Refuse nonempty/incomplete data without --reset; completed identical loads
    skip without changing app ratings, aggregates, or existing accounts.
    """
    if not 1 <= chunk_size <= 250_000:
        raise ValueError("chunk-size must be between 1 and 250000")
    specs = account_specs()  # Validate before any destructive action.
    zip_path = Path(zip_path).expanduser().resolve(strict=True)
    with zipfile.ZipFile(zip_path) as archive:
        members = archive_members(archive)
        identity = fingerprint(archive, members)
        previous = db.metadata.find_one({"_id": METADATA_ID})
        if not reset and previous and previous.get("status") == "complete":
            if previous.get("schemaVersion") != SCHEMA_VERSION or previous.get("archiveFingerprint") != identity:
                raise RuntimeError("Different dataset/schema already loaded; explicit --reset required")
            seed_accounts(db, specs)
            print("Identical completed dataset already loaded; skipped (users and app ratings preserved).", flush=True)
            return previous
        if not reset and (previous or any(db[name].find_one({}, {"_id": 1}) for name in DATA_COLLECTIONS)):
            raise RuntimeError("Existing or incomplete dataset; explicit --reset required (stop app first)")
        if reset:
            for name in DATA_COLLECTIONS:
                db[name].drop()
        meta = {"_id": METADATA_ID, "schemaVersion": SCHEMA_VERSION,
                "dataset": zip_path.name, "datasetBasename": zip_path.name,
                "archiveFingerprint": identity, "status": "loading", "startedAt": now(),
                "sourceRowCounts": {}}
        db.metadata.replace_one({"_id": METADATA_ID}, meta, upsert=True)
        try:
            counts = {}
            movies = {}
            for frame in chunks(archive, members, "movies.csv", chunk_size):
                counts["movies"] = counts.get("movies", 0) + len(frame)
                for row in frame.itertuples(index=False):
                    mid = int(row.movieId)
                    full_title = str(row.title).strip()
                    match = re.search(r"\s*\((\d{4})\)$", full_title)
                    genres = list(dict.fromkeys(g.strip() for g in str(row.genres).split("|")
                                                if g.strip() and g.strip() != "(no genres listed)"))
                    if mid in movies:
                        raise ValueError(f"Duplicate movieId {mid}")
                    movies[mid] = {"movieId": mid, "title": full_title[:match.start()].strip() if match else full_title,
                        "fullTitle": full_title, "year": int(match[1]) if match else None,
                        "genres": genres, "imdbId": "", "tmdbId": "", "ratingCount": 0,
                        "ratingSum": 0.0, "avgRating": 0.0,
                        "ratingDistribution": dict.fromkeys(RATING_KEYS, 0), "topTags": []}
            counts.setdefault("movies", 0)
            tag_lookup = {}
            for frame in chunks(archive, members, "genome-tags.csv", chunk_size):
                counts["genome_tags"] = counts.get("genome_tags", 0) + len(frame)
                frame["tag"] = frame["tag"].astype(str).str.strip()
                tag_lookup.update((int(r.tagId), str(r.tag)) for r in frame.itertuples(index=False))
                insert_frame(db.genome_tags, frame)
            for frame in chunks(archive, members, "links.csv", chunk_size, dtype=str):
                counts["links"] = counts.get("links", 0) + len(frame)
                frame["movieId"] = frame["movieId"].astype("int64")
                frame["imdbId"] = frame["imdbId"].map(lambda s: s.strip().zfill(7) if s.strip() else "")
                frame["tmdbId"] = frame["tmdbId"].map(lambda s: str(int(float(s))) if s.strip() else "")
                for row in frame.itertuples(index=False):
                    if int(row.movieId) in movies:
                        movies[int(row.movieId)].update(imdbId=row.imdbId, tmdbId=row.tmdbId)
                insert_frame(db.links, frame)
            top = {}
            for frame in chunks(archive, members, "genome-scores.csv", chunk_size):
                counts["genome_scores"] = counts.get("genome_scores", 0) + len(frame)
                merge_top_tags(top, frame)
            for mid, values in top.items():
                if mid in movies:
                    movies[mid]["topTags"] = [{"tagId": tid, "tag": tag_lookup.get(tid, f"tag_{tid}"),
                                                 "relevance": score} for score, tid in values]
            del top
            for frame in chunks(archive, members, "ratings.csv", chunk_size,
                                dtype={"userId": "int64", "movieId": "int64", "rating": "float64", "timestamp": "int64"}):
                if not (frame["rating"].isin([i / 2 for i in range(1, 11)])).all():
                    raise ValueError("Ratings must use half-star steps from 0.5 through 5.0")
                counts["ratings"] = counts.get("ratings", 0) + len(frame)
                for (mid, rating), count in frame.groupby(["movieId", "rating"]).size().items():
                    if int(mid) not in movies:
                        raise ValueError(f"Rating references unknown movieId {mid}")
                    movie = movies[int(mid)]
                    movie["ratingCount"] += int(count)
                    movie["ratingSum"] += float(rating) * int(count)
                    movie["ratingDistribution"][str(float(rating))] += int(count)
                insert_frame(db.ratings, frame)
                print(f"Retained {counts['ratings']:,} ratings", flush=True)
            # SQLite keeps genre/tag cardinality off the Python heap. Its only
            # database file lives beside the archive, never in a /tmp fallback.
            with tempfile.TemporaryDirectory(prefix=".movielens-etl-", dir=zip_path.parent) as scratch:
                sql = sqlite3.connect(str(Path(scratch) / "genre_counts.sqlite"))
                try:
                    sql.execute("PRAGMA cache_size=-8192")
                    sql.execute("CREATE TABLE counts (genre TEXT, tag TEXT, n INTEGER, PRIMARY KEY(genre,tag)) WITHOUT ROWID")
                    for frame in chunks(archive, members, "tags.csv", chunk_size,
                                        dtype={"userId": "int64", "movieId": "int64", "tag": str, "timestamp": "int64"}):
                        counts["tags"] = counts.get("tags", 0) + len(frame)
                        insert_frame(db.tags, frame)  # Preserve even empty raw tag rows.
                        cleaned = frame["tag"].astype(str).str.lower().str.strip()
                        partial = Counter()
                        for mid, tag in zip(frame["movieId"], cleaned):
                            if tag:
                                for genre in movies.get(int(mid), {}).get("genres", []):
                                    partial[(genre, tag)] += 1
                        sql.executemany("INSERT INTO counts VALUES(?,?,?) ON CONFLICT(genre,tag) DO UPDATE SET n=n+excluded.n",
                                        ((genre, tag, n) for (genre, tag), n in partial.items()))
                        sql.commit()
                    genres = sorted({g for movie in movies.values() for g in movie["genres"]})
                    for genre in genres:
                        rows = list(sql.execute("SELECT tag,n FROM counts WHERE genre=? ORDER BY n DESC,tag ASC LIMIT 5", (genre,)))
                        if rows:
                            db.genre_tags.insert_one({"genre": genre, "topTag": rows[0][0], "count": rows[0][1],
                                "topTags": [{"tag": tag, "count": n} for tag, n in rows]})
                finally:
                    sql.close()
            batch = []
            for movie in movies.values():
                movie["avgRating"] = movie["ratingSum"] / movie["ratingCount"] if movie["ratingCount"] else 0.0
                batch.append(movie)
                if len(batch) == 5000:
                    db.movies.insert_many(batch, ordered=False)
                    batch = []
            if batch:
                db.movies.insert_many(batch, ordered=False)
            for name in ("movies", "ratings", "tags", "links", "genome_tags", "genome_scores"):
                counts.setdefault(name, 0)
            create_indexes(db)
            seed_accounts(db, specs)
            # Exact read-back checks before publishing completion; _id count uses
            # the existing index rather than scanning all full rating documents.
            for name in ("movies", "ratings", "tags", "links", "genome_tags"):
                if db[name].count_documents({}, hint="_id_") != counts[name]:
                    raise RuntimeError(f"Stored {name} count does not match source")
            meta.update(status="complete", sourceRowCounts=counts, completedAt=now(),
                        rawRatingsRetained=True, genomeScoresStorage="per-movie top five; original ZIP retained")
            db.metadata.replace_one({"_id": METADATA_ID}, meta)
            saved = db.metadata.find_one({"_id": METADATA_ID})
            if not saved or saved.get("status") != "complete" or saved.get("sourceRowCounts") != counts:
                raise RuntimeError("Completion marker read-back failed")
            print(f"Load complete: {counts}", flush=True)
            return saved
        except Exception as exc:
            db.metadata.update_one({"_id": METADATA_ID}, {"$set": {
                "status": "failed", "failedAt": now(), "errorType": type(exc).__name__}})
            raise


def run_etl(argv=None):
    load_dotenv(BASE_DIR / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path, default=DEFAULT_ZIP, help="Existing local MovieLens ZIP (never downloaded/deleted)")
    parser.add_argument("--chunk-size", type=int, default=100_000, help="CSV rows per chunk, 1..250000")
    parser.add_argument("--reset", action="store_true", help="Explicitly replace dataset/app ratings; preserve users. Stop application first.")
    args = parser.parse_args(argv)
    with MongoClient(os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017"), serverSelectionTimeoutMS=10_000) as client:
        client.admin.command("ping")
        return load_dataset(client[os.getenv("DB_NAME", "movielens")], args.zip, args.chunk_size, args.reset)


if __name__ == "__main__":
    run_etl()
