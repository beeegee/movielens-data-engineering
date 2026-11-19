"""Exercise the real local dashboard and capture dashboard screenshots in Chromium.

Requires requirements-dev.txt and a local Chromium executable. Credentials are
read privately from .env and submitted through normal login, never URL helpers.
"""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import math
import re
import shutil

from dotenv import dotenv_values
from playwright.sync_api import sync_playwright
import requests

BASE = Path(__file__).resolve().parents[1]
ROOT = BASE
ASSETS = ROOT / 'docs/screenshots'
EVIDENCE = ROOT / 'verification'
VALUES = dotenv_values(BASE / '.env')
URL = 'http://127.0.0.1:' + VALUES.get('PORT','5008')


def login(username, password):
    session = requests.Session()
    page = session.get(URL+'/login', timeout=30)
    page.raise_for_status()
    token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    response = session.post(URL+'/login', data={'username':username,'password':password,'csrf_token':token}, timeout=30)
    response.raise_for_status()
    assert response.url.rstrip('/') == URL, 'Normal login failed'
    return session


def cookies(session):
    return [{'name':cookie.name,'value':cookie.value,'url':URL,'httpOnly':True,'sameSite':'Lax'} for cookie in session.cookies]


def nearest_card(page, selector):
    return page.locator(selector).locator('xpath=ancestor::*[contains(concat(" ",normalize-space(@class)," ")," analytics-card ")][1]')


def main():
    ASSETS.mkdir(parents=True, exist_ok=True)
    EVIDENCE.mkdir(exist_ok=True)
    manifest = []
    checks = {}
    errors = []
    requests_seen = set()
    regular = login(VALUES['USER_USERNAME'], VALUES['USER_PASSWORD'])
    administrator = login(VALUES['ADMIN_USERNAME'], VALUES['ADMIN_PASSWORD'])
    assert requests.get(URL+'/?demo_user=admin', allow_redirects=False, timeout=30).status_code == 302
    assert requests.get(URL+'/api/admin/stats?demo_user=admin', timeout=30).status_code == 401
    assert regular.get(URL+'/api/admin/stats', timeout=30).status_code == 403
    checks['normal_login_and_admin_separation'] = 'PASS'
    endpoints = ['/api/tags/top/1','/api/ratings/distribution/1','/api/movies/top_rated',
                 '/api/movies/top_rated?year=1995','/api/tags/genre','/api/ratings/total_distribution',
                 '/api/genres/releases?year=1995','/api/genres/coverage','/api/years']
    for endpoint in endpoints:
        response = regular.get(URL+endpoint, timeout=60)
        response.raise_for_status()
        assert response.json(), endpoint
    initial = regular.get(URL+'/api/ratings/distribution/1', timeout=30).json()
    executable = shutil.which('chromium') or shutil.which('chromium-browser') or shutil.which('google-chrome')
    assert executable, 'Install Chromium or provide a supported browser executable'
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=executable,headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        context = browser.new_context(viewport={'width':1680,'height':1300},device_scale_factor=2)
        context.add_cookies(cookies(regular))
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests_seen.add(request.url))
        page.goto(URL, wait_until='networkidle', timeout=60000)
        page.wait_for_function('Object.values(charts).every(Boolean)', timeout=60000)
        assert page.locator('.user-pill .role-chip').inner_text() == 'USER'
        assert page.get_by_role('link',name='Admin Console',exact=True).count() == 0
        assert page.locator('#genreTagsTable tr').count() > 0
        assert page.evaluate('charts.e.data.labels[0]') == '0'
        assert len(page.evaluate('charts.b.data.labels')) == 10
        years = [int(value) for value in page.locator('#yearFilterB option').evaluate_all('(options) => options.map(option => option.value)') if value]
        assert min(years) < 1900 and 1900 in years
        checks['release_years_include_early_cinema'] = {'status':'PASS','oldestYear':min(years),'turnOfCenturyIncluded':True}
        assert page.evaluate("escapeHTML('<img src=x onerror=alert(1)>')") == '&lt;img src=x onerror=alert(1)&gt;'
        checks['escaped_database_text'] = 'PASS'
        checks['all_seven_dashboard_views_rendered'] = 'PASS'
        checks['zero_rating_bucket_rendered'] = 'PASS'
        for score in ['4.0','4.5']:
            page.locator('#userRatingSelect').select_option(score)
            with page.expect_response(lambda response: response.url.endswith('/api/ratings/submit') and response.request.method=='POST') as pending:
                page.locator('#submitRatingBtn').click()
            result = pending.value.json()
            assert pending.value.status == 200 and result['ok'], result
            page.wait_for_function('(score) => document.querySelector("#ratingStatusMsg").textContent.includes("Your saved rating: " + Number(score))', arg=score, timeout=30000)
        final = regular.get(URL+'/api/ratings/distribution/1',timeout=30).json()
        expected_count = initial['ratingCount'] + (0 if initial.get('userRating') is not None else 1)
        assert final['ratingCount'] == expected_count, (initial,final)
        assert final['userRating'] == 4.5
        assert sum(row['count'] for row in final['distribution']) == expected_count
        page.wait_for_function('(count) => charts.d.data.datasets[0].data.reduce((a,b)=>a+b,0)===count',arg=expected_count)
        checks['browser_rating_insert_and_replace'] = {'status':'PASS','movieId':1,'initialCount':initial['ratingCount'],'finalCount':final['ratingCount'],'savedRating':4.5,'firstAction':'replace' if initial.get('userRating') is not None else 'insert'}
        page.wait_for_load_state('networkidle')
        def asset(name, role='user', locator=None, clip=None, target=None, full_page=False):
            target = target or page
            path = ASSETS/(name+'.png')
            if locator is not None:
                locator.screenshot(path=str(path),animations='disabled')
            else:
                target.screenshot(path=str(path),clip=clip,animations='disabled',full_page=full_page)
            manifest.append({'asset':'assets/'+name+'.png','asset_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                             'source':'live_application','role':role,'url':target.url,
                             'captured_at':datetime.now(timezone.utc).isoformat(),'device_scale_factor':2})
        page.evaluate('window.scrollTo(0,0)')
        bottom = max(nearest_card(page,'#chartA').bounding_box()['y']+nearest_card(page,'#chartA').bounding_box()['height'],
                     nearest_card(page,'#chartD').bounding_box()['y']+nearest_card(page,'#chartD').bounding_box()['height'])
        assert bottom < 1300, bottom
        asset('user-dashboard',clip={'x':0,'y':0,'width':1680,'height':math.ceil(bottom+16)})
        for name,selector in [('movie-tags','#chartA'),('movie-ratings','#chartD'),('top-movies','#chartB'),('genre-tags','#genreTagsTable'),('rating-counts','#chartE'),('year-genres','#chartF'),('genre-coverage','#chartG')]:
            card = nearest_card(page,selector)
            if name == 'genre-tags':
                card.scroll_into_view_if_needed()
                box = card.bounding_box()
                row = page.locator('#genreTagsBody tr').nth(2).bounding_box()
                scroll_y = page.evaluate('window.scrollY')
                asset(name,clip={'x':box['x'],'y':box['y']+scroll_y,'width':box['width'],'height':row['y']+row['height']-box['y']+12},full_page=True)
            else:
                asset(name,locator=card)
        overall_labels = page.evaluate('charts.b.data.labels')
        with page.expect_response(lambda response: '/api/movies/top_rated?year=1995' in response.url):
            page.locator('#yearFilterB').select_option('1995')
        page.wait_for_function('(old) => JSON.stringify(charts.b.data.labels)!==JSON.stringify(old)', arg=overall_labels)
        yearly = page.evaluate('charts.b.data.labels')
        assert len(yearly)==10 and all('(1995)' in title for title in yearly)
        nearest_card(page,'#chartB').screenshot(path=str(EVIDENCE/'top-rated-1995.png'),animations='disabled')
        checks['release_year_filter_in_browser'] = {'status':'PASS','year':1995,'movies':10}
        page.locator('#yearFilterB').select_option('')
        page.wait_for_function('(labels) => JSON.stringify(charts.b.data.labels)===JSON.stringify(labels)',arg=overall_labels)
        page.evaluate('window.scrollTo(0,0)')
        asset('rating-form',locator=page.locator('.movie-rate-box'))
        page.locator('#movieSearch').fill('Toy Story')
        page.locator('#searchResults .search-item[data-id="1"]').wait_for(state='visible')
        search = page.locator('.search-box-wrapper').bounding_box()
        dropdown = page.locator('#searchResults').bounding_box()
        asset('movie-search',clip={'x':search['x'],'y':search['y'],'width':search['width'],'height':dropdown['y']+dropdown['height']-search['y']})
        page.locator('#searchResults .search-item[data-id="1"]').click()
        page.wait_for_function('currentMovieId===1')
        checks['search_and_movie_selection'] = 'PASS'
        admin_context = browser.new_context(viewport={'width':1680,'height':1300},device_scale_factor=2)
        admin_context.add_cookies(cookies(administrator))
        admin_page = admin_context.new_page()
        admin_page.on('pageerror',lambda error: errors.append(str(error)))
        admin_page.on('request',lambda request: requests_seen.add(request.url))
        admin_page.on('dialog',lambda dialog: dialog.accept())
        admin_page.goto(URL+'/admin',wait_until='networkidle')
        admin_page.wait_for_function('document.querySelector("#statBaseline").textContent==="25,000,095"')
        user_row = admin_page.locator('#usersTableBody tr').filter(has_text=VALUES['USER_USERNAME'])
        user_row.get_by_role('button',name='Make admin',exact=True).click()
        user_row.get_by_role('button',name='Make user',exact=True).wait_for(state='visible')
        assert context.request.get(URL+'/api/admin/stats').status == 200
        user_row.get_by_role('button',name='Make user',exact=True).click()
        user_row.get_by_role('button',name='Make admin',exact=True).wait_for(state='visible')
        assert context.request.get(URL+'/api/admin/stats').status == 403
        checks['admin_role_controls_and_live_permission_refresh'] = 'PASS'
        assert admin_page.locator('#adminError').inner_text() == ''
        assert admin_page.locator('#statUserRatings').inner_text() == '1'
        assert admin_page.locator('#auditTableBody tr').count() >= 2
        assert admin_page.locator('.role-chip.admin').first.inner_text() == 'ADMIN'
        admin_page.evaluate('window.scrollTo(0,0)')
        box = admin_page.locator('main').bounding_box()
        asset('admin-dashboard',role='admin',clip={'x':0,'y':0,'width':1680,'height':math.ceil(box['y']+box['height']+12)},target=admin_page)
        anonymous = browser.new_context(viewport={'width':1680,'height':1300},device_scale_factor=2)
        login_page = anonymous.new_page()
        login_page.goto(URL+'/login',wait_until='networkidle')
        assert login_page.locator('.demo-logins').count() == 0
        asset('login',role='anonymous',locator=login_page.locator('.auth-card'),target=login_page)
        browser.close()
    assert not errors, errors
    external = [url for url in requests_seen if not url.startswith(URL)]
    assert not external, external
    checks['browser_javascript_errors'] = []
    checks['external_frontend_requests'] = []
    result = {'status':'PASS','base_url':URL,'browser':'Chromium','checks':checks,'live_assets':len(manifest),
              'captured_at':datetime.now(timezone.utc).isoformat(),
              'side_effects':'One current regular-user rating for movie 1, with submission history. Role changes restored to user/admin.'}
    (EVIDENCE/'screenshot-crops.json').write_text(json.dumps(manifest,indent=2)+'\n')
    (EVIDENCE/'browser.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))


if __name__ == '__main__':
    main()
