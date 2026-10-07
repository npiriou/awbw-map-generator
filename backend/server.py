from __future__ import annotations
import argparse
import base64
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import struct
import threading
import time
from urllib.parse import urlsplit
import uuid
from tools.map_constraints import check_map, validate_settings
from tools.map_codec import settings_schema
from backend.map_import import MapImportError, MapSettingsImporter
from backend.map_editor import validate_editor, validate_editable_map, resolve_editor_symmetry
ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / 'frontend'
DEFAULT_RUN = ROOT / 'output'
MODEL_PATH = ROOT / 'models' / 'ppo-48000.pt'
CHECKPOINT_NAMES = ('ppo-48000.pt',)
MAX_BODY = 2 * 1024 * 1024
MAX_PREVIEW_BODY = 8 * 1024 * 1024

def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return default

def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)

def validate_request(body):
    if not isinstance(body, dict):
        raise ValueError('The request must be a JSON object.')
    unknown = set(body) - {'settings', 'seed', 'temperature', 'refinement_steps', 'attempts', 'checkpoint', 'guidance_scale', 'editor', 'generator_revision', 'auto_correct'}
    if unknown:
        raise ValueError('Unknown fields: ' + ', '.join(sorted(unknown)))
    if 'generator_revision' in body and (not isinstance(body['generator_revision'], str)):
        raise ValueError('generator_revision must be a string.')
    settings = body.get('settings', {})
    errors = validate_settings(settings)
    if errors:
        raise ValueError(' ; '.join(errors))
    editor = None
    if 'editor' in body:
        editor, settings = validate_editor(body['editor'], settings)
        errors = validate_settings(settings)
        if errors:
            raise ValueError(' ; '.join(errors))
        resolve_editor_symmetry(editor, settings)
    from tools.map_model.deployment_symmetry import validate_deployment_request
    deployment_errors = validate_deployment_request(settings)
    if deployment_errors:
        raise ValueError(' ; '.join(deployment_errors))
    for key, lower, upper in (('width', 1, 50), ('height', 1, 50), ('players', 1, 20)):
        if key in settings and (not lower <= settings[key] <= upper):
            raise ValueError(f'{key} must be between {lower} and {upper} for this model.')
    options = {'settings': settings, 'seed': body.get('seed', 20261004), 'temperature': body.get('temperature', 1.0), 'refinement_steps': body.get('refinement_steps', 16), 'attempts': body.get('attempts', 4), 'checkpoint': body.get('checkpoint', 'ppo-48000.pt')}
    auto_correct = body.get('auto_correct', True)
    if type(auto_correct) is not bool:
        raise ValueError('auto_correct must be a boolean.')
    options['auto_correct'] = auto_correct
    if editor is not None:
        options['editor'] = editor
    for key, lower, upper in (('seed', 0, 2 ** 32 - 1), ('refinement_steps', 1, 64), ('attempts', 1, 16)):
        if type(options[key]) is not int or not lower <= options[key] <= upper:
            raise ValueError(f'{key} must be an integer between {lower} and {upper}.')
    temperature = options['temperature']
    if type(temperature) not in (int, float) or not math.isfinite(temperature) or (not 0.05 <= temperature <= 3):
        raise ValueError('Temperature must be between 0.05 and 3.')
    if options['checkpoint'] not in CHECKPOINT_NAMES:
        raise ValueError('Unknown checkpoint.')
    guidance = body.get('guidance_scale', 1.0)
    if type(guidance) not in (int, float) or not math.isfinite(guidance) or (not 1 <= guidance <= 5):
        raise ValueError('Guidance must be between 1 and 5.')
    if 'guidance_scale' in body:
        options['guidance_scale'] = guidance
    return options

class GeneratorService:

    def __init__(self, run_dir=DEFAULT_RUN, device='cpu', generate_function=None, map_importer=None):
        self.run_dir = Path(run_dir).resolve()
        self.device = device
        self.generate_function = generate_function
        self.map_importer = map_importer or MapSettingsImporter()
        self.lock = threading.Lock()
        self.jobs = OrderedDict()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='map-generation')
        self.checkpoint_metadata = {}
        self.checkpoint_lock = threading.Lock()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.loaded_generator = None

    def generator_info(self):
        if self.loaded_generator is None:
            if self.generate_function is not None:
                revision = getattr(self.generate_function, 'sampler_revision', 'custom')
                loaded_at = self.started_at
            else:
                from tools.map_model import sample
                revision = getattr(sample, 'SAMPLER_REVISION', 'legacy')
                loaded_at = getattr(sample, 'SAMPLER_LOADED_AT', None)
            self.loaded_generator = {'revision': revision, 'loaded_at': loaded_at, 'service_started_at': self.started_at}
        return dict(self.loaded_generator)

    def checkpoint_path(self, name):
        if name not in CHECKPOINT_NAMES:
            raise ValueError('Unknown checkpoint.')
        return MODEL_PATH

    def checkpoint_info(self, name):
        path = self.checkpoint_path(name)
        with self.checkpoint_lock, path.open('rb') as handle:
            stat = os.fstat(handle.fileno())
            signature = (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size)
            cached = self.checkpoint_metadata.get(name)
            if cached and cached[0] == signature:
                return cached[1]
            import torch
            checkpoint = torch.load(handle, map_location='cpu', weights_only=True)
            codec = checkpoint['codec']
            timestamp = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
            info = {'name': name, 'file_name': path.name, 'model_name': 'PPO 48000', 'label': 'PPO 48000', 'step': checkpoint['step'], 'ppo_updates': 48000, 'created_at': checkpoint.get('created_at_utc') or timestamp, 'modified_at': timestamp, 'size_bytes': stat.st_size, 'supports_editor': True, 'mask_policy': codec['mask_policy'], 'diffusion_steps': codec['diffusion_steps'], 'structured_sampling': bool(codec.get('structured_sampling')), 'gameplay_type_only': bool(checkpoint['vocab'].get('gameplay_type_only')), 'diffusion_factorization': 'joint', 'total_diffusion_steps': codec['diffusion_steps']}
            self.checkpoint_metadata[name] = (signature, info)
            return info

    def status(self):
        checkpoints = []
        for name in CHECKPOINT_NAMES:
            try:
                checkpoints.append(self.checkpoint_info(name))
            except OSError:
                pass
        return {'checkpoints': checkpoints, 'inference_device': self.device, 'generator': self.generator_info()}

    def meta(self):
        import torch
        checkpoint = torch.load(MODEL_PATH, map_location='cpu', weights_only=True)
        return {**self.status(), 'schema': settings_schema(), 'vocabulary': checkpoint['vocab'], 'limits': {'width': 50, 'height': 50, 'players': 20, 'attempts': 16}, 'model_stage': 'experimental'}

    def submit(self, body):
        options = validate_request(body)
        expected_revision = body.get('generator_revision')
        if expected_revision is not None and expected_revision != self.generator_info()['revision']:
            raise FileNotFoundError('The generator has changed. Refresh the interface before generating.')
        checkpoint_name = options.pop('checkpoint')
        path = self.checkpoint_path(checkpoint_name)
        if not path.is_file():
            raise FileNotFoundError('The checkpoint is not available yet.')
        if options.get('editor') is not None and (not self.checkpoint_info(checkpoint_name).get('supports_editor', True)):
            raise ValueError("This model generates new maps only. Turn off 'Preserve preplaced cells when generating' or choose another model.")
        with self.lock:
            pending = sum((job['status'] in {'queued', 'running'} for job in self.jobs.values()))
            if pending >= 3:
                raise RuntimeError('Three generations are already queued. Wait for them to finish.')
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = {'job_id': job_id, 'status': 'queued', 'progress': 0, 'created_at': datetime.now(timezone.utc).isoformat()}
            while len(self.jobs) > 100:
                oldest, job = next(iter(self.jobs.items()))
                if job['status'] in {'queued', 'running'}:
                    break
                self.jobs.pop(oldest)
        self.executor.submit(self._run, job_id, path, options, checkpoint_name)
        return {'job_id': job_id}

    def job(self, job_id):
        with self.lock:
            value = self.jobs.get(job_id)
            return json.loads(json.dumps(value)) if value else None

    def export(self, job_id, name):
        if not re.fullmatch('[0-9a-f]{32}', job_id) or name not in {'map.json', 'report.json', 'preview.png'}:
            raise FileNotFoundError('Unknown export.')
        stored = read_json(self.run_dir / 'generated' / f'{job_id}.json')
        if not stored:
            raise FileNotFoundError('Unknown generation.')
        if name == 'preview.png':
            return ((self.run_dir / 'generated' / f'{job_id}.png').read_bytes(), 'image/png')
        if name == 'map.json':
            value = stored['result']['map']
        else:
            value = {'request': stored['request'], 'result': {k: v for k, v in stored['result'].items() if k != 'map'}}
        return (json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8'), 'application/json; charset=utf-8')

    def save_preview(self, job_id, body):
        self.export(job_id, 'map.json')
        if not isinstance(body, dict) or set(body) != {'png'} or (not isinstance(body['png'], str)):
            raise ValueError('A PNG image is required.')
        prefix = 'data:image/png;base64,'
        if not body['png'].startswith(prefix):
            raise ValueError('Invalid PNG format.')
        try:
            data = base64.b64decode(body['png'][len(prefix):], validate=True)
        except (ValueError, base64.binascii.Error) as error:
            raise ValueError('Invalid PNG encoding.') from error
        if len(data) < 33 or data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
            raise ValueError('Invalid PNG image.')
        width, height = struct.unpack('>II', data[16:24])
        if not 1 <= width <= 4096 or not 1 <= height <= 4096:
            raise ValueError('Invalid PNG dimensions.')
        path = self.run_dir / 'generated' / f'{job_id}.png'
        with self.lock:
            if not path.exists() or path.read_bytes() != data:
                temporary = path.with_suffix('.png.tmp')
                temporary.write_bytes(data)
                for attempt in range(20):
                    try:
                        temporary.replace(path)
                        break
                    except PermissionError:
                        if attempt == 19:
                            raise
                        time.sleep(0.025)
        return {'url': f'/api/exports/{job_id}/preview.png'}

    def _run(self, job_id, checkpoint, options, checkpoint_name=None):

        def progress(value=None, **fields):
            update = value if isinstance(value, dict) else {'progress': value} if value is not None else {}
            with self.lock:
                self.jobs[job_id].update(update, **fields)
                job = self.jobs[job_id]
                if job.get('phase') == 'sampling' and job.get('steps'):
                    job['progress'] = min(1, (job.get('attempt', 1) - 1 + job.get('step', 0) / job['steps']) / options['attempts'])
                elif job.get('phase') == 'complete':
                    job['progress'] = 1
                self.jobs[job_id]['status'] = 'running'
        progress(0)
        try:
            function = self.generate_function
            if function is None:
                import torch
                torch.set_num_threads(2)
                from tools.map_model.sample import generate
                function = generate
            result = function(checkpoint, **options, device=self.device, progress_callback=progress)
            if self.generate_function is None and result.get('sampling', {}).get('generator_revision') != self.generator_info()['revision']:
                raise RuntimeError('The result came from a different generator revision. Restart this server.')
            result['export_id'] = job_id
            atomic_json(self.run_dir / 'generated' / f'{job_id}.json', {'request': {**options, 'checkpoint': checkpoint_name or checkpoint.name}, 'result': result})
            with self.lock:
                self.jobs[job_id].update(status='completed', progress=1, result=result)
        except Exception as error:
            with self.lock:
                self.jobs[job_id].update(status='failed', error=f'{type(error).__name__}: {error}')

class Handler(SimpleHTTPRequestHandler):

    def __init__(self, *args, service, **kwargs):
        self.service = service
        super().__init__(*args, directory=str(FRONTEND), **kwargs)

    def end_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def json(self, value, code=200):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        imported_map = re.fullmatch('/api/maps/([0-9]+)/(?:settings|map)', path)
        if imported_map:
            try:
                return self.json(self.service.map_importer.import_map(int(imported_map[1])))
            except ValueError as error:
                return self.json({'error': str(error)}, 400)
            except FileNotFoundError as error:
                return self.json({'error': str(error)}, 404)
            except MapImportError as error:
                return self.json({'error': str(error)}, 502)
        if path.startswith('/api/exports/'):
            parts = path.split('/')
            if len(parts) != 5:
                return self.json({'error': 'Unknown export.'}, 404)
            try:
                data, mime = self.service.export(parts[3], parts[4])
            except FileNotFoundError:
                return self.json({'error': 'Export unavailable.'}, 404)
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Disposition', f'attachment; filename="awbw-{parts[3][:8]}-{parts[4]}"')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == '/api/meta':
            return self.json(self.service.meta())
        if path == '/api/status':
            return self.json(self.service.status())
        if path.startswith('/api/jobs/'):
            job = self.service.job(path.removeprefix('/api/jobs/'))
            return self.json(job if job else {'error': 'Unknown generation.'}, 200 if job else 404)
        if path.startswith('/api/'):
            return self.json({'error': 'Unknown route.'}, 404)
        super().do_GET()

    def do_POST(self):
        path = urlsplit(self.path).path
        preview = re.fullmatch('/api/exports/([0-9a-f]{32})/preview', path)
        if path not in {'/api/generate', '/api/editor/validate'} and (not preview):
            return self.json({'error': 'Unknown route.'}, 404)
        origin = self.headers.get('Origin')
        if origin and urlsplit(origin).netloc != self.headers.get('Host'):
            return self.json({'error': 'Origin not allowed.'}, 403)
        if self.headers.get_content_type() != 'application/json':
            return self.json({'error': 'Content-Type application/json required.'}, 415)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= (MAX_PREVIEW_BODY if preview else MAX_BODY):
                return self.json({'error': 'Invalid request size.'}, 413)
            body = json.loads(self.rfile.read(length))
            if path == '/api/editor/validate':
                if not isinstance(body, dict) or set(body) - {'map', 'settings'}:
                    raise ValueError('Expected an object containing map and optional settings.')
                payload = validate_editable_map(body.get('map'))
                report = check_map(payload, body.get('settings', {}))
                from tools.map_model.strategy import map_strategic_diagnostics, apply_deployment_constraints
                strategy = map_strategic_diagnostics(payload, body.get('settings', {}))
                report = apply_deployment_constraints(report, strategy)
                result = {'report': report, 'strategy': strategy, 'success': report['status'] == 'matched'}
            else:
                result = self.service.save_preview(preview[1], body) if preview else self.service.submit(body)
        except (ValueError, TypeError, UnicodeDecodeError) as error:
            return self.json({'error': str(error)}, 400)
        except FileNotFoundError as error:
            return self.json({'error': str(error)}, 409)
        except RuntimeError as error:
            return self.json({'error': str(error)}, 429)
        self.json(result, 200 if preview or path == '/api/editor/validate' else 202)

def main(argv=None):
    parser = argparse.ArgumentParser(description='AWBW Map Generator')
    parser.add_argument('--port', type=int, default=8776)
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_RUN)
    parser.add_argument('--device', choices=('auto', 'cpu', 'cuda'), default='auto')
    parser.add_argument('--open-browser', action='store_true')
    args = parser.parse_args(argv)
    if not MODEL_PATH.is_file():
        parser.error('Model missing. Place ppo-48000.pt in the models folder.')
    import torch
    if args.device == 'auto':
        args.device = 'cuda' if torch.cuda.is_available() else 'cpu'
    service = GeneratorService(args.output_dir, args.device)
    try:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), partial(Handler, service=service))
    except OSError:
        service.executor.shutdown(wait=False, cancel_futures=True)
        parser.error(f'Cannot use port {args.port}. Close the other instance or choose another port with --port.')
    url = f'http://127.0.0.1:{args.port}'
    print(f'AWBW Map Generator: {url}', flush=True)
    print(f'Device: {args.device}', flush=True)
    print('Keep this window open. Press Ctrl+C to stop.', flush=True)
    if args.open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.executor.shutdown(wait=False, cancel_futures=True)
if __name__ == '__main__':
    main()
