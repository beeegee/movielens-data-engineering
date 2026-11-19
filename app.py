"""MovieLens Analytics: Flask API, account roles and transactional app ratings.

Data Engineering, IIT Palakkad. Run against a replica-set MongoDB database.
"""

import os
import time
import math
import re
import secrets
import stat
import fcntl
from pathlib import Path
from urllib.parse import urlsplit, unquote
from flask.sessions import SecureCookieSessionInterface
from werkzeug.exceptions import HTTPException
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern
from functools import wraps
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    jsonify,
)
from pymongo import MongoClient, ASCENDING, DESCENDING
from werkzeug.security import check_password_hash, generate_password_hash
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or None
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


class LocalSessionInterface(SecureCookieSessionInterface):
    """Create/load a private local key on first request, never at import."""
    def get_signing_serializer(self, application):
        if not application.secret_key:
            path = Path(os.getenv("SESSION_SECRET_FILE", str(Path(__file__).parent / ".runtime" / "session.key")))
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "r+") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                info = os.fstat(stream.fileno())
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    raise RuntimeError("Session secret must be owned by this user with mode 0600")
                key = stream.read().strip()
                if not key:
                    key = secrets.token_hex(32)
                    stream.write(key)
                    stream.flush()
                    os.fsync(stream.fileno())
                if len(key) < 32:
                    raise RuntimeError("Session secret file is invalid")
                application.secret_key = key
        return super().get_signing_serializer(application)


app.session_interface = LocalSessionInterface()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "movielens")

client = MongoClient(MONGO_URI, connect=False, serverSelectionTimeoutMS=5000)
db = client[DB_NAME]


# ---------------------------------------------------------------------------
# Authentication & Authorization Helpers
# ---------------------------------------------------------------------------

def auth_error(message, status):
    if request.path.startswith("/api/"):
        return jsonify({"error": message}), status
    if status == 401:
        return redirect(url_for("login", next=request.full_path))
    return message, status


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        username = session.get("user")
        user = db.users.find_one({"username": username}) if isinstance(username, str) else None
        if not user:
            session.clear()
            return auth_error("Authentication required", 401)
        # Role in the cookie is display-only; the database is authoritative.
        session["role"] = user.get("role", "user")
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if session.get("role") != "admin":
            return auth_error("Administrator privileges required", 403)
        return f(*args, **kwargs)
    return decorated_function


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def protect_csrf():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        supplied = request.headers.get("X-CSRF-Token") if request.is_json else request.form.get("csrf_token")
        expected = session.get("csrf_token")
        if not isinstance(supplied, str) or not expected or not secrets.compare_digest(supplied, expected):
            return jsonify({"error": "Invalid CSRF token"}), 400


@app.errorhandler(HTTPException)
def http_error(error):
    if request.path.startswith("/api/"):
        return jsonify({"error": error.description}), error.code
    return error


@app.errorhandler(DuplicateKeyError)
def duplicate_record(error):
    return jsonify({"error": "Record already exists; refresh and retry"}), 409


@app.errorhandler(PyMongoError)
def database_error(error):
    app.logger.exception("Database request failed")
    if request.path.startswith("/api/"):
        return jsonify({"error": "Database unavailable; request could not be completed"}), 503
    return "Database unavailable", 503


def safe_next(value):
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        return url_for("dashboard")
    decoded = unquote(value)
    if decoded.startswith("//") or "\\" in decoded or any(ord(c) < 32 for c in decoded):
        return url_for("dashboard")
    parsed = urlsplit(value)
    return value if not parsed.netloc and not parsed.scheme else url_for("dashboard")


# ---------------------------------------------------------------------------
# Auth Routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = db.users.find_one({"username": username})
        if user and check_password_hash(user["password"], password):
            session.clear()
            session["user"] = user["username"]
            session["role"] = user.get("role", "user")
            flash(f"Welcome back, {username}! Logged in as {session['role']}.", "success")
            next_url = request.args.get("next")
            return redirect(safe_next(next_url))

        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if len(username) < 3:
            flash("Username must be at least 3 characters.", "error")
            return render_template("register.html")
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("register.html")

        if db.users.find_one({"username": username}):
            flash("Username already exists. Please choose another.", "error")
            return render_template("register.html")

        db.users.insert_one({
            "username": username,
            "password": generate_password_hash(password),
            "role": "user",
            "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        flash("Registration successful! You may now log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Application Views
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/admin")
@admin_required
def admin_panel():
    return render_template("admin.html")


# ---------------------------------------------------------------------------
# Analytics API Endpoints (Questions a - g)
# ---------------------------------------------------------------------------

# Search movies autocomplete
@app.route("/api/movies/search")
@login_required
def api_movie_search():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])

    results = list(
        db.movies.find(
            {"title": {"$regex": re.escape(q[:200]), "$options": "i"}},
            {"_id": 0, "movieId": 1, "title": 1, "fullTitle": 1, "year": 1, "avgRating": 1, "ratingCount": 1},
        )
        .sort("ratingCount", -1)
        .limit(15)
    )
    return jsonify(results)


# (a) For a particular movie, show the top-5 tags based on relevance scores
@app.route("/api/tags/top/<int:movie_id>")
@login_required
def api_top_tags(movie_id):
    movie = db.movies.find_one(
        {"movieId": movie_id},
        {"_id": 0, "movieId": 1, "title": 1, "topTags": 1},
    )
    if not movie or not movie.get("topTags"):
        return jsonify({"movieId": movie_id, "title": movie.get("title", "Unknown") if movie else "Unknown", "tags": []})

    return jsonify({
        "movieId": movie["movieId"],
        "title": movie.get("title", ""),
        "tags": movie["topTags"][:5],
    })


# (b) Show overall and year-wise top 10 movies based on the average rating
@app.route("/api/movies/top_rated")
@login_required
def api_top_rated():
    year_param = request.args.get("year", "").strip()

    if year_param:
        try:
            year = int(year_param)
            # Threshold to ensure statistical significance
            query = {"year": year, "ratingCount": {"$gte": 20}}
            results = list(
                db.movies.find(
                    query,
                    {"_id": 0, "movieId": 1, "title": 1, "year": 1, "avgRating": 1, "ratingCount": 1},
                )
                .sort([("avgRating", DESCENDING), ("ratingCount", DESCENDING)])
                .limit(10)
            )
            # If historical year has fewer movies with 20 ratings, fall back to >= 5
            if len(results) < 5:
                results = list(
                    db.movies.find(
                        {"year": year, "ratingCount": {"$gte": 5}},
                        {"_id": 0, "movieId": 1, "title": 1, "year": 1, "avgRating": 1, "ratingCount": 1},
                    )
                    .sort([("avgRating", DESCENDING), ("ratingCount", DESCENDING)])
                    .limit(10)
                )
        except ValueError:
            return jsonify({"error": "Invalid year format"}), 400
    else:
        # Overall top 10 with minimum 500 ratings for credibility
        results = list(
            db.movies.find(
                {"ratingCount": {"$gte": 500}},
                {"_id": 0, "movieId": 1, "title": 1, "year": 1, "avgRating": 1, "ratingCount": 1},
            )
            .sort([("avgRating", DESCENDING), ("ratingCount", DESCENDING)])
            .limit(10)
        )

    return jsonify(results)


# (c) What are the most popular tags given for each genre
@app.route("/api/tags/genre")
@login_required
def api_tags_by_genre():
    results = list(
        db.genre_tags.find(
            {},
            {"_id": 0, "genre": 1, "topTag": 1, "count": 1, "topTags": 1},
        ).sort("genre", ASCENDING)
    )
    return jsonify(results)


# (d) For a particular movie, show the distribution of users’ ratings
@app.route("/api/ratings/distribution/<int:movie_id>")
@login_required
def api_rating_distribution(movie_id):
    movie = db.movies.find_one(
        {"movieId": movie_id},
        {"_id": 0, "movieId": 1, "title": 1, "year": 1, "avgRating": 1, "ratingCount": 1, "ratingDistribution": 1},
    )
    if not movie:
        return jsonify({"error": "Movie not found"}), 404

    # Check if current user has rated this movie
    user_rating_doc = db.ratings.find_one(
        {"movieId": movie_id, "username": session.get("user")},
        {"_id": 0, "rating": 1},
    )
    user_rating = user_rating_doc["rating"] if user_rating_doc else None

    # Format distribution array
    ratings_order = ["0.5", "1.0", "1.5", "2.0", "2.5", "3.0", "3.5", "4.0", "4.5", "5.0"]
    dist = movie.get("ratingDistribution", {})
    distribution_list = [
        {"rating": float(r), "label": f"{r} ★", "count": int(dist.get(r, 0))}
        for r in ratings_order
    ]

    return jsonify({
        "movieId": movie["movieId"],
        "title": movie.get("title", ""),
        "year": movie.get("year"),
        "avgRating": movie.get("avgRating", 0.0),
        "ratingCount": movie.get("ratingCount", 0),
        "userRating": user_rating,
        "distribution": distribution_list,
    })


# (e) Show the distribution of total ratings for movies
@app.route("/api/ratings/total_distribution")
@login_required
def api_total_rating_distribution():
    # Distribution of movie popularity (how many movies fall in each rating count bracket)
    pipeline = [
        {"$set": {"ratingCount": {"$ifNull": ["$ratingCount", 0]}}},
        {
            "$bucket": {
                "groupBy": "$ratingCount",
                "boundaries": [0, 1, 10, 50, 100, 500, 1000, 5000, 10000, 50000],
                "default": "50000+",
                "output": {"count": {"$sum": 1}},
            }
        },
    ]
    raw_buckets = list(db.movies.aggregate(pipeline))

    range_labels = {
        0: "0",
        1: "1 – 9",
        10: "10 – 49",
        50: "50 – 99",
        100: "100 – 499",
        500: "500 – 999",
        1000: "1,000 – 4,999",
        5000: "5,000 – 9,999",
        10000: "10,000 – 49,999",
        "50000+": "50,000+",
    }

    if not any(b["_id"] == 0 for b in raw_buckets):
        raw_buckets.insert(0, {"_id": 0, "count": 0})
    formatted = []
    for b in raw_buckets:
        _id = b["_id"]
        label = range_labels.get(_id, str(_id))
        formatted.append({"range": label, "count": b["count"]})

    return jsonify(formatted)


# (f) For a particular year, show which genre released the highest number of movies
@app.route("/api/genres/releases")
@login_required
def api_genre_releases():
    year = request.args.get("year", type=int)
    if not year:
        return jsonify({"error": "year parameter required"}), 400

    pipeline = [
        {"$match": {"year": year}},
        {"$unwind": "$genres"},
        {"$group": {"_id": "$genres", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$project": {"_id": 0, "genre": "$_id", "count": 1}},
    ]
    results = list(db.movies.aggregate(pipeline))
    return jsonify({"year": year, "releases": results})


# (g) Show genre-wise coverage of movies
@app.route("/api/genres/coverage")
@login_required
def api_genre_coverage():
    pipeline = [
        {"$unwind": "$genres"},
        {"$group": {"_id": "$genres", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$project": {"_id": 0, "genre": "$_id", "count": 1}},
    ]
    results = list(db.movies.aggregate(pipeline))
    return jsonify(results)


# ---------------------------------------------------------------------------
# Interactive Features: User Rating Modification (Regulation 4)
# ---------------------------------------------------------------------------

@app.route("/api/ratings/submit", methods=["POST"])
@login_required
def api_submit_rating():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object required"}), 400
    raw_id, raw_rating = data.get("movieId"), data.get("rating")
    try:
        # Do not silently truncate fractional movie IDs or accept booleans/objects.
        if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
            raise ValueError
        if isinstance(raw_id, str) and not re.fullmatch(r"[0-9]+", raw_id):
            raise ValueError
        movie_id = int(raw_id)
        if not 0 < movie_id <= 2147483647:
            raise ValueError
        if isinstance(raw_rating, bool) or not isinstance(raw_rating, (str, int, float)):
            raise ValueError
        rating_val = float(raw_rating)
        if not math.isfinite(rating_val) or not 0.5 <= rating_val <= 5 or rating_val * 2 % 1:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        return jsonify({"error": "Positive integer movieId and finite half-star rating (0.5–5.0) required"}), 400

    username = session["user"]  # Never accept ownership from request JSON.

    def save_vote(txn):
        movie = db.movies.find_one({"movieId": movie_id}, session=txn)
        if movie is None:
            return {"error": "Movie not found"}, 404
        if "ratingSum" not in movie:
            return {"error": "Movie exact ratingSum missing; reload dataset before voting"}, 409
        existing = db.ratings.find_one({"movieId": movie_id, "username": username}, session=txn)
        old = float(existing["rating"]) if existing else None
        count = int(movie.get("ratingCount", 0)) + (0 if existing else 1)
        total = float(movie["ratingSum"]) + rating_val - (old if old is not None else 0)
        distribution = dict(movie.get("ratingDistribution", {}))
        if old is not None:
            old_key = f"{old:.1f}"
            if count <= 0 or distribution.get(old_key, 0) <= 0:
                return {"error": "Movie rating summary is inconsistent; reload dataset"}, 409
            distribution[old_key] -= 1
        key = f"{rating_val:.1f}"
        distribution[key] = distribution.get(key, 0) + 1
        average = total / count
        # Updating the shared movie serializes concurrent voters through MongoDB's
        # document write conflicts. with_transaction retries the complete snapshot.
        db.movies.update_one({"_id": movie["_id"]}, {"$set": {
            "ratingSum": total, "ratingCount": count, "avgRating": average,
            "ratingDistribution": distribution,
        }}, session=txn)
        timestamp = int(time.time())
        db.ratings.update_one({"movieId": movie_id, "username": username}, {"$set": {
            "movieId": movie_id, "username": username, "rating": rating_val,
            "timestamp": timestamp,
        }}, upsert=True, session=txn)
        if old != rating_val:
            db.rating_events.insert_one({"username": username, "movieId": movie_id,
                "oldRating": old, "rating": rating_val, "timestamp": timestamp}, session=txn)
        return {"ok": True,
            "message": f"Rating of {rating_val} ★ successfully saved for '{movie.get('title', '')}'!",
            "newAvg": average, "newCount": count}, 200

    # The loader creates the partial unique (movieId, username) index. Never
    # fall back to nontransactional writes on standalone/unsupported MongoDB.
    with client.start_session() as mongo_session:
        result, status = mongo_session.with_transaction(save_vote,
            read_concern=ReadConcern("snapshot"), write_concern=WriteConcern("majority"))
    return jsonify(result), status


# ---------------------------------------------------------------------------
# Administrative & Utility Endpoints
# ---------------------------------------------------------------------------

@app.route("/api/years")
@login_required
def api_years():
    years = db.movies.distinct("year")
    filtered = sorted([int(y) for y in years if y and y > 0], reverse=True)
    return jsonify(filtered)


@app.route("/api/admin/stats")
@admin_required
def api_admin_stats():
    total_movies = db.movies.estimated_document_count()
    total_users = db.users.count_documents({})
    total_genre_tags = db.genre_tags.estimated_document_count()
    app_votes = {"username": {"$exists": True}}
    # Force the loader's partial index: baseline userId-only rows are excluded.
    vote_index = [("movieId", ASCENDING), ("username", ASCENDING)]
    user_ratings_count = db.ratings.count_documents(app_votes, hint=vote_index)
    latest_ratings = list(
        db.ratings.find(app_votes, {"_id": 0}).hint(vote_index)
        .sort("timestamp", DESCENDING)
        .limit(10)
    )

    metadata = db.metadata.find_one({"_id": "movielens", "status": "complete"}) or {}
    counts = metadata.get("sourceRowCounts", {})
    baseline = counts.get("ratings")
    baseline_estimated = not isinstance(baseline, int)
    if baseline_estimated:
        baseline = max(0, db.ratings.estimated_document_count() - user_ratings_count)
    recent_events = list(db.rating_events.find({}, {"_id": 0})
                         .sort([("timestamp", DESCENDING), ("_id", DESCENDING)]).limit(10))
    return jsonify({
        "baselineRatings": baseline,
        "baselineRatingsEstimated": baseline_estimated,
        "totalRatings": baseline + user_ratings_count,
        "totalTags": counts.get("tags", db.tags.estimated_document_count()),
        "genomeScores": counts.get("genome_scores", 0),
        "recentEvents": recent_events,
        "totalMovies": total_movies,
        "totalUsers": total_users,
        "totalGenreTags": total_genre_tags,
        "userRatingsCount": user_ratings_count,
        "latestRatings": latest_ratings,
    })


@app.route("/api/admin/users")
@admin_required
def api_admin_users():
    users = list(db.users.find({}, {"_id": 0, "password": 0}).sort("createdAt", DESCENDING))
    return jsonify(users)


@app.route("/api/admin/toggle_role", methods=["POST"])
@admin_required
def api_admin_toggle_role():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object required"}), 400
    target_username = data.get("username")
    if not isinstance(target_username, str) or not target_username:
        return jsonify({"error": "Target username required"}), 400

    if target_username == session.get("user"):
        return jsonify({"error": "Cannot change your own role"}), 400

    user = db.users.find_one({"username": target_username})
    if not user:
        return jsonify({"error": "User not found"}), 404

    new_role = "admin" if user.get("role") == "user" else "user"
    db.users.update_one({"username": target_username}, {"$set": {"role": new_role}})

    return jsonify({"ok": True, "username": target_username, "newRole": new_role})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=port, debug=os.getenv("FLASK_DEBUG", "0") == "1")
