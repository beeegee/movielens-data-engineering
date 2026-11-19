"""Read-only full-dataset conservation check against the retained source ZIP.

Runs on the configured local application database. Does not modify MongoDB.
Writes verification/dataset.json with the full-dataset verification results.
"""
from collections import Counter
from pathlib import Path
import csv
import hashlib
import io
import json
import os
import zipfile

from dotenv import load_dotenv
from pymongo import MongoClient

BASE = Path(__file__).resolve().parents[1]
ROOT = BASE
load_dotenv(BASE / '.env')


def main():
    client = MongoClient(os.environ['MONGO_URI'], serverSelectionTimeoutMS=5000)
    database = client[os.environ['DB_NAME']]
    meta = database.metadata.find_one({'_id':'movielens'})
    assert meta and meta['status'] == 'complete', 'Import is not complete'
    archive_path = BASE / 'data/ml-25m.zip'
    digest = hashlib.file_digest(archive_path.open('rb'), 'sha256').hexdigest()
    with zipfile.ZipFile(archive_path) as source_archive:
        fingerprint = {Path(info.filename).name:{'size':info.file_size,'crc32':info.CRC}
                       for info in source_archive.infolist() if info.filename.endswith('.csv')}
        assert meta['archiveFingerprint'] == fingerprint, 'Archive differs from imported source'
    source_ratings = 0
    source_sum_twice = 0
    source_sample = {}
    sample_ids = {1, 50, 260, 296, 318, 527, 858, 1193, 1210, 2571}
    with zipfile.ZipFile(archive_path) as archive:
        member = next(name for name in archive.namelist() if name.endswith('/ratings.csv'))
        with archive.open(member) as stream:
            rows = csv.reader(io.TextIOWrapper(stream, encoding='utf-8-sig', newline=''))
            assert next(rows) == ['userId','movieId','rating','timestamp']
            for row in rows:
                source_ratings += 1
                score_twice = int(float(row[2]) * 2)
                source_sum_twice += score_twice
                movie_id = int(row[1])
                if movie_id in sample_ids:
                    item = source_sample.setdefault(movie_id, {'count':0,'sum_twice':0,'histogram':Counter()})
                    item['count'] += 1
                    item['sum_twice'] += score_twice
                    item['histogram'][str(score_twice / 2)] += 1
    assert source_ratings == meta['sourceRowCounts']['ratings']
    app_votes = list(database.ratings.find({'username':{'$type':'string'}}, {'movieId':1,'rating':1,'_id':0}))
    app_by_movie = {}
    for vote in app_votes:
        app_by_movie.setdefault(vote['movieId'], []).append(float(vote['rating']))
    original_stored = database.ratings.count_documents({}) - len(app_votes)
    assert original_stored == source_ratings, (original_stored, source_ratings)
    movie_count = total_count = 0
    total_sum_twice = 0
    genre_counts = Counter()
    sample_checks = []
    for movie in database.movies.find({}, {'_id':0,'movieId':1,'ratingCount':1,'ratingSum':1,'avgRating':1,'ratingDistribution':1,'genres':1,'topTags':1}):
        movie_count += 1
        count = movie['ratingCount']
        histogram = movie['ratingDistribution']
        assert sum(histogram.values()) == count, movie['movieId']
        assert sum(int(float(k)*2)*v for k,v in histogram.items()) == int(movie['ratingSum']*2), movie['movieId']
        expected_average = movie['ratingSum']/count if count else 0
        assert abs(movie['avgRating']-expected_average) < 1e-12, movie['movieId']
        total_count += count
        total_sum_twice += int(movie['ratingSum']*2)
        genre_counts.update(movie['genres'])
        scores = [tag['relevance'] for tag in movie.get('topTags',[])]
        assert len(scores)<=5 and scores==sorted(scores,reverse=True), movie['movieId']
        if movie['movieId'] in source_sample:
            expected = source_sample[movie['movieId']]
            current_votes = app_by_movie.get(movie['movieId'],[])
            expected_histogram = expected['histogram'].copy()
            expected_histogram.update(str(float(v)) for v in current_votes)
            assert count == expected['count']+len(current_votes), movie['movieId']
            assert int(movie['ratingSum']*2) == expected['sum_twice']+sum(int(v*2) for v in current_votes), movie['movieId']
            assert {k:v for k,v in histogram.items() if v} == dict(expected_histogram), movie['movieId']
            sample_checks.append(movie['movieId'])
    assert movie_count == meta['sourceRowCounts']['movies']
    assert total_count == source_ratings+len(app_votes)
    assert total_sum_twice == source_sum_twice+sum(int(v['rating']*2) for v in app_votes)
    assert set(sample_checks) == sample_ids
    assert '(no genres listed)' not in genre_counts
    result = {'status':'PASS','database':database.name,'source_archive_sha256':digest,
              'source_row_counts':meta['sourceRowCounts'],
              'stored_original_ratings':original_stored,'current_app_ratings':len(app_votes),
              'movie_histograms_checked':movie_count,'total_rating_count':total_count,
              'exact_source_rating_sum':source_sum_twice/2,
              'source_csv_vs_database_movies':sorted(sample_checks),
              'checks':['complete import metadata','source archive member fingerprints','full raw rating retention',
                        'all movie histogram counts and sums','all movie averages',
                        'global source CSV rating count and exact sum','ten independent per-movie CSV histograms',
                        'top-tag cardinality and score ordering','genre placeholder excluded']}
    directory = ROOT / 'verification'
    directory.mkdir(exist_ok=True)
    (directory/'dataset.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
    client.close()


if __name__ == '__main__':
    main()
