from __future__ import annotations
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
import uuid
from tools.map_constraints import OFFICIAL_CATEGORIES
from tools.map_features.access_counts import analyze_access_counts
from tools.map_features.categories import structural_category_values
from tools.map_features.common import canonical_map, grid, terrain_reference
from tools.map_features.symmetry_team import SYMMETRY_TAGS, analyze_symmetry, analyze_team_play
from tools.map_features.terrain_units import analyze_terrain_units
ROOT = Path(__file__).resolve().parents[1]
MAP_URL = 'https://awbw.amarriner.com/prevmaps.php?maps_id={}'
API_URL = 'https://awbw.amarriner.com/api/map/map_info.php?maps_id={}'
MAX_RESPONSE = 8 * 1024 * 1024

class MapImportError(RuntimeError):
    pass

class CategoryParser(HTMLParser):

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.link = None
        self.text = []
        self.found = False
        self.categories = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'div':
            if self.depth:
                self.depth += 1
            elif attributes.get('id') == 'map-categories':
                self.depth = 1
                self.found = True
        if tag == 'a' and self.depth:
            href = urlsplit(attributes.get('href') or '')
            ids = parse_qs(href.query).get('categories_id', [])
            if href.path.rsplit('/', 1)[-1] == 'categories.php' and ids and ids[0].isdigit():
                self.link, self.text = (int(ids[0]), [])

    def handle_data(self, text):
        if self.link is not None:
            self.text.append(text)

    def handle_endtag(self, tag):
        if tag == 'a' and self.link is not None:
            name = ' '.join(''.join(self.text).split())
            if name in OFFICIAL_CATEGORIES and name not in self.categories:
                self.categories.append(name)
            self.link = None
        if tag == 'div' and self.depth:
            self.depth -= 1

def fetch_public(url):
    request = Request(url, headers={'User-Agent': 'awbw-map-generator/0.1', 'Accept': 'application/json,text/html'})
    with urlopen(request, timeout=20) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise MapImportError('AWBW returned too much data.')
    return raw

def usable_payload(value):
    if not isinstance(value, dict):
        return False
    try:
        width, height, _ = grid(value)
    except (ValueError, TypeError, KeyError):
        return False
    return width <= 100 and height <= 100

def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError, UnicodeDecodeError):
        return None

def parse_categories(raw):
    parser = CategoryParser()
    parser.feed(raw.decode('utf-8-sig', errors='replace'))
    if not parser.found or parser.depth:
        raise MapImportError('AWBW category list is unavailable. Import cancelled; try again later.')
    return parser.categories

def extract_settings(payload, categories):
    width, height, _ = grid(payload)
    settings = {'width': width, 'height': height}
    players = payload.get('Player Count')
    if type(players) is int and players > 0:
        settings['players'] = players
    notes, analysis, tags = ([], {}, {})

    def run(name, function):
        try:
            result = function()
        except (ValueError, TypeError, KeyError, IndexError) as error:
            notes.append(f'{name}: {error}')
            return None
        analysis[name] = result
        tags.update(result.get('tags', {}))
        return result
    run('terrain_units', lambda: analyze_terrain_units(payload))
    symmetry = run('symmetry', lambda: analyze_symmetry(payload))
    access = run('access_counts', lambda: analyze_access_counts(payload))
    run('team_play', lambda: {'tags': {'1vX team play': analyze_team_play(payload, symmetry or {}, access['evidence']['building_access'] if access else None)}})
    settings['tags'] = {name: result['status'] == 'matched' for name, result in tags.items() if name not in SYMMETRY_TAGS and result['status'] in {'matched', 'not_matched'}}
    selected = next((name for name in ('rotational symmetry', 'Flip symmetry', 'diagonal symmetry', 'Asymmetrical') if tags.get(name, {}).get('status') == 'matched'), '')
    if selected:
        settings['tags'][selected] = True
    unknown_tags = [name for name, result in tags.items() if result['status'] not in {'matched', 'not_matched'}]
    if unknown_tags:
        notes.append('Unverified tags left as Any: ' + ', '.join(unknown_tags) + '.')
    if access:
        settings['building_counts'] = {kind: {metric: counts[metric] for metric in ('total', 'pre_owned', 'neutral') if counts[metric] is not None} for kind, counts in access['building_counts'].items()}
        excluded = sum((row['neutral_unreachable'] for row in access['building_counts'].values()))
        uncertain = sum((row['neutral_unknown'] for row in access['building_counts'].values()))
        if excluded:
            notes.append(f'Excluded {excluded} unreachable neutral building(s).')
        if uncertain:
            notes.append(f'Accessibility unknown for {uncertain} neutral building(s); affected counts left as Any.')
    data = canonical_map(payload)
    if data['units_complete']:
        counts = Counter((unit['unit_type'] for unit in data['units']))
        settings['predeployed_counts'] = {kind: counts[kind] for kind in sorted(set(terrain_reference()['unit_types'].values()))}
    else:
        notes.append('Incomplete unit inventory; unit counts left as Any.')
    settings['categories'] = {name: name in categories for name in sorted(OFFICIAL_CATEGORIES)}
    inferred_categories = structural_category_values(payload)
    for name, value in inferred_categories.items():
        if settings['categories'][name] != value:
            notes.append(f"{name}: set to {('Yes' if value else 'No')} from map structure rather than the official category list.")
    settings['categories'].update(inferred_categories)
    analysis['structural_categories'] = inferred_categories
    if width > 50 or height > 50 or (type(players) is int and players > 20):
        notes.append("This map exceeds the model's size or player limits.")
    return (settings, selected, analysis, notes)

class MapSettingsImporter:

    def __init__(self, cache=None, fetcher=None):
        self.cache = Path(cache) if cache is not None else ROOT / 'output/imported-maps'
        self.fetcher = fetcher or fetch_public

    def _local(self, map_id):
        cached = read_json(self.cache / f'{map_id}.json')
        if isinstance(cached, dict) and cached.get('map_id') == map_id and usable_payload(cached.get('map')):
            return cached
        return None

    def _fetch_categories(self, map_id):
        try:
            return parse_categories(self.fetcher(MAP_URL.format(map_id)))
        except (HTTPError, URLError, TimeoutError, ConnectionError, OSError, ValueError) as error:
            raise MapImportError('Cannot read AWBW categories. Import cancelled; try again later.') from error

    def _remote(self, map_id):
        with ThreadPoolExecutor(max_workers=2) as pool:
            map_future = pool.submit(self.fetcher, API_URL.format(map_id))
            category_future = pool.submit(self.fetcher, MAP_URL.format(map_id))
            try:
                raw = map_future.result()
                payload = json.loads(raw)
            except HTTPError as error:
                if error.code in {404, 410}:
                    raise FileNotFoundError('Map not found on AWBW.') from error
                raise MapImportError(f'AWBW returned HTTP {error.code}. Try again later.') from error
            except (URLError, TimeoutError, ConnectionError, OSError) as error:
                raise MapImportError('Cannot reach AWBW. Try again later.') from error
            except (ValueError, UnicodeDecodeError) as error:
                raise MapImportError('AWBW returned an unreadable map response.') from error
            if not usable_payload(payload):
                raise FileNotFoundError('AWBW did not return a usable map for this ID.')
            try:
                categories = parse_categories(category_future.result())
            except (HTTPError, URLError, TimeoutError, ConnectionError, OSError, ValueError) as error:
                raise MapImportError('Cannot read AWBW categories. Import cancelled; try again later.') from error
        return {'map_id': map_id, 'map': payload, 'categories': categories, 'categories_known': True, 'fetched_at': datetime.now(timezone.utc).isoformat(), 'notes': []}

    def import_map(self, map_id):
        if type(map_id) is not int or not 1 <= map_id <= 2 ** 31 - 1:
            raise ValueError('Enter a positive map ID.')
        record = self._local(map_id)
        source = 'local'
        save_record = False
        if record is None:
            record, source = (self._remote(map_id), 'awbw')
            save_record = True
        if not record.get('categories_known'):
            record['categories'] = self._fetch_categories(map_id)
            record['categories_known'] = True
            record['notes'] = [note for note in record.get('notes', []) if not note.startswith('AWBW categories unavailable')]
            record['categories_fetched_at'] = datetime.now(timezone.utc).isoformat()
            save_record = True
        if save_record:
            temporary = self.cache / f'.{map_id}.{uuid.uuid4().hex}.tmp'
            try:
                self.cache.mkdir(parents=True, exist_ok=True)
                temporary.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
                temporary.replace(self.cache / f'{map_id}.json')
            except OSError:
                record['notes'].append('Could not cache this map locally.')
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        settings, symmetry, analysis, notes = extract_settings(record['map'], record.get('categories', []))
        return {'map_id': map_id, 'source': source, 'url': MAP_URL.format(map_id), 'name': record['map'].get('Name', f'Map {map_id}'), 'author': record['map'].get('Author'), 'published_at': record['map'].get('Published Date'), 'settings': settings, 'symmetry': symmetry, 'categories_known': record.get('categories_known', False), 'map': record['map'], 'analysis': analysis, 'notes': record.get('notes', []) + notes}
