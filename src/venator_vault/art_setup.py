"""Reproduce reviewed artwork from pinned public sources, without shipping image bytes."""
from __future__ import annotations

import fcntl
import hashlib
import io
import json
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

SOURCE_HOSTS = frozenset({
    'lumiere-a.akamaihd.net', 'forums.ea.com', 'media.contentapi.ea.com', 'www.swtor.com',
    'cdn.svc.asmodee.net', 'cdn-www.swtor.com', 'shared.akamai.steamstatic.com', 'web.archive.org',
})
MAX_DOWNLOAD = 32 * 1024 * 1024
MAX_PIXELS = 32_000_000
REQUEST_TIMEOUT = 20
TRANSFER_TIMEOUT = 90
DIGEST = re.compile(r'[0-9a-f]{64}')
ART_PATH = re.compile(r'assets/art/[a-z0-9][a-z0-9_-]*\.png')


def validate_url(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (parsed.scheme == 'https' and parsed.hostname in SOURCE_HOSTS
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password
                 and not parsed.fragment)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError('Source URL must use HTTPS on an approved artwork host.')
    return url


class SafeRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        validate_url(new_url)
        return super().redirect_request(request, file, code, message, headers, new_url)


def download(url):
    validate_url(url)
    request = urllib.request.Request(url, headers={'User-Agent': 'KyberDex artwork setup/1.0'})
    deadline = time.monotonic() + TRANSFER_TIMEOUT
    # Redirects are checked before following them; no cookies or credentials are sent.
    with urllib.request.build_opener(SafeRedirects()).open(request, timeout=REQUEST_TIMEOUT) as response:
        validate_url(response.geturl())
        length = response.headers.get('Content-Length')
        if length is not None and (not length.isdecimal() or int(length) > MAX_DOWNLOAD):
            raise ValueError('Source response exceeds the download size limit.')
        chunks, total = [], 0
        while True:
            if time.monotonic() > deadline:
                raise ValueError('Source download exceeded the transfer time limit.')
            chunk = response.read(min(64 * 1024, MAX_DOWNLOAD + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                raise ValueError('Source response exceeds the download size limit.')
    return b''.join(chunks)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def local_path(root, relative):
    path = root / relative
    if path.is_absolute() and not path.is_relative_to(root):
        raise ValueError('Artwork path is outside the checkout.')
    if any(part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(root)):
        raise ValueError('Artwork and cache paths must not contain symlinks.')
    return path


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.setup-art-', delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def dimensions(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(type(item) is not int or item <= 0 for item in value)
            or value[0] * value[1] > MAX_PIXELS):
        raise ValueError('Invalid or oversized artwork dimensions.')
    return tuple(value)


def crop_source(source, box):
    if (not isinstance(box, (list, tuple)) or len(box) != 4
            or any(type(item) not in (int, float) or not math.isfinite(item) for item in box)
            or not (0 <= box[0] < box[2] <= source.width and 0 <= box[1] < box[3] <= source.height)):
        raise ValueError('Source crop must stay within the decoded image.')
    return source.crop(tuple(box))


def decode_source(raw, entry):
    if entry.get('source_format', 'image') == 'pdf-image':
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError
        page = entry.get('pdf_page')
        name = entry.get('pdf_image_name')
        if type(page) is not int or not 0 <= page < 200 or not isinstance(name, str) or not name:
            raise ValueError('Invalid PDF image selection.')
        try:
            reader = PdfReader(io.BytesIO(raw), strict=True)
            if reader.is_encrypted or len(reader.pages) > 200 or page >= len(reader.pages):
                raise ValueError('PDF image source is encrypted, oversized or missing its selected page.')
            opened = reader.pages[page].images[name].image
        except (PyPdfError, KeyError, IndexError) as error:
            raise ValueError('The selected PDF source image could not be decoded.') from error
        dimensions(opened.size)
        return ImageOps.exif_transpose(opened).convert('RGB')
    if entry.get('source_format', 'image') != 'image':
        raise ValueError('Unsupported source image format.')
    with Image.open(io.BytesIO(raw)) as opened:
        dimensions(opened.size)
        return ImageOps.exif_transpose(opened).convert('RGB')


def transform(raw, entry):
    source = decode_source(raw, entry)
    norm = entry['normalization']
    size = (entry['width'], entry['height'])
    if entry['recipe'] == 'official-source-v1':
        if dimensions(norm['output_size']) != size:
            raise ValueError('Recipe output dimensions do not match the reviewed image.')
        cropped = crop_source(source, norm['requested_crop_box'])
        if norm['fit_mode'] == 'cover':
            center = norm['crop_center']
            if (not isinstance(center, list) or len(center) != 2
                    or any(type(x) not in (int, float) or not 0 <= x <= 1 for x in center)):
                raise ValueError('Invalid artwork crop center.')
            result = ImageOps.fit(cropped, size, Image.Resampling.LANCZOS, centering=tuple(center))
        elif norm['fit_mode'] == 'contain':
            placement = norm['placement']
            tile = ImageOps.contain(cropped, (1024, 1180), Image.Resampling.LANCZOS)
            if dimensions(placement['size']) != tile.size:
                raise ValueError('Contain placement differs from the reviewed recipe.')
            xy = placement['xy']
            if (len(xy) != 2 or any(type(x) is not int or x < 0 for x in xy)
                    or xy[0] + tile.width > size[0] or xy[1] + tile.height > size[1]):
                raise ValueError('Contain placement is outside the output.')
            if placement['matte_rgb'] != [4, 12, 28]:
                raise ValueError('Unrecognized artwork matte.')
            result = Image.new('RGB', size, tuple(placement['matte_rgb']))
            result.paste(tile, tuple(xy))
        else:
            raise ValueError('Unsupported artwork fit mode.')
    elif entry['recipe'] == 'published-window-v1':
        if dimensions(norm['canvas']) != size:
            raise ValueError('Recipe canvas does not match the reviewed image.')
        cropped = crop_source(source, norm['crop_box'])
        brightness = norm['brightness']
        if type(brightness) not in (int, float) or not 0 < brightness <= 5:
            raise ValueError('Invalid artwork brightness.')
        if brightness != 1:
            cropped = ImageEnhance.Brightness(cropped).enhance(brightness)
        result = ImageOps.fit(cropped, size, Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(38))
        result = Image.blend(result, Image.new('RGB', size, (4, 12, 28)), .75)
        box = norm['foreground_window']
        if (not isinstance(box, list) or len(box) != 4 or any(type(x) is not int for x in box)
                or not (0 <= box[0] < box[2] <= size[0] and 0 <= box[1] < box[3] <= size[1])):
            raise ValueError('Invalid foreground window.')
        tile = ImageOps.contain(cropped, (box[2] - box[0], box[3] - box[1]), Image.Resampling.LANCZOS)
        xy = (box[0] + (box[2] - box[0] - tile.width) // 2, box[1] + (box[3] - box[1] - tile.height) // 2)
        if list(tile.size) != norm['placement_size'] or list(xy) != norm['placement_xy'] or norm['feather_px'] != 10:
            raise ValueError('Foreground placement differs from the reviewed recipe.')
        mask = Image.new('L', tile.size, 255)
        for inset in range(10):
            ImageDraw.Draw(mask).rectangle((inset, inset, tile.width-1-inset, tile.height-1-inset),
                                           outline=round(255*inset/10), width=1)
        result.paste(tile, xy, mask)
    else:
        raise ValueError('Unsupported artwork recipe.')
    result.info.clear()
    return result


def verified_pixels(data, entry):
    with Image.open(io.BytesIO(data)) as opened:
        if opened.format != 'PNG' or opened.mode != 'RGB' or opened.size != (entry['width'], entry['height']):
            raise ValueError('Local artwork format or dimensions differ from the reviewed image.')
        dimensions(opened.size)
        if digest(opened.tobytes()) != entry['pixel_sha256']:
            raise ValueError('Artwork RGB pixels differ from the reviewed image.')
    return digest(data)


def load_recipes(root, sources):
    if sources.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('Artwork source registry exceeds its size limit.')
    document = json.loads(sources.read_text())
    if not isinstance(document, dict):
        raise ValueError('Artwork source registry must be an object.')  # noqa: TRY004 - invalid configuration
    if (document.get('format') != 'kyberdex-reviewed-art-recipes' or document.get('schema_version') != 1
            or not isinstance(document.get('assets'), dict) or not document['assets']):
        raise ValueError('Unsupported or empty artwork source registry.')
    catalog = {card['id']: card for card in json.loads((root / 'data/cards.json').read_text())['cards']}
    entries = document['assets']
    if set(entries) != set(catalog):
        raise ValueError('Artwork registry must match every catalog definition exactly.')
    groups = {}
    for identity, entry in entries.items():
        card = catalog[identity]
        if (not isinstance(entry, dict) or not isinstance(entry.get('path'), str)
                or not ART_PATH.fullmatch(entry['path'])
                or entry['path'] != card.get('art') or entry.get('subject_id') != card.get('subject_id')
                or entry.get('mode') != 'RGB' or not isinstance(entry.get('pixel_sha256'), str)
                or not DIGEST.fullmatch(entry['pixel_sha256'])
                or entry.get('status') not in ('available', 'manual-source-required')):
            raise ValueError(f'Invalid artwork registry entry: {identity}.')
        dimensions([entry.get('width'), entry.get('height')])
        if entry['width'] * 3 != entry['height'] * 2:
            raise ValueError('Reviewed artwork must have a 2:3 portrait aspect ratio.')
        local_path(root, entry['path'])
        if entry['status'] == 'available':
            validate_url(entry.get('source_image_url'))
            if (not isinstance(entry.get('source_raw_sha256'), str)
                    or not DIGEST.fullmatch(entry['source_raw_sha256'])
                    or entry.get('recipe') not in ('official-source-v1', 'published-window-v1')
                    or not isinstance(entry.get('normalization'), dict)):
                raise ValueError(f'Invalid source recipe: {identity}.')
        shared = groups.setdefault(entry['path'], [])
        if shared:
            fields = ('pixel_sha256', 'width', 'height', 'source_raw_sha256', 'source_image_url', 'recipe', 'normalization',
                      'source_format', 'pdf_page', 'pdf_image_name')
            previous = entries[shared[0]]
            if any(previous.get(key) != entry.get(key) for key in fields):
                raise ValueError('Definitions sharing an artwork path must have identical source recipes.')
        shared.append(identity)
    return entries, groups


def import_asset(root, cache, entry):
    output = local_path(root, entry['path'])
    if output.is_file() and output.stat().st_size <= MAX_DOWNLOAD:
        try:
            return verified_pixels(output.read_bytes(), entry), 'reused'
        except (ValueError, OSError):
            pass  # A stale or interrupted output is rebuilt from a verified source.
    if entry['status'] != 'available':
        raise ValueError('A reviewed downloadable source is not yet available for this asset.')
    raw_path = local_path(root, str((cache / (entry['source_raw_sha256'] + '.blob')).relative_to(root)))
    raw = raw_path.read_bytes() if raw_path.is_file() and raw_path.stat().st_size <= MAX_DOWNLOAD else None
    if raw is None or digest(raw) != entry['source_raw_sha256']:
        raw = download(entry['source_image_url'])
        if digest(raw) != entry['source_raw_sha256']:
            raise ValueError('Downloaded source checksum differs from the pinned source.')
        atomic_write(raw_path, raw)
    result = transform(raw, entry)
    if result.size != (entry['width'], entry['height']) or digest(result.tobytes()) != entry['pixel_sha256']:
        raise ValueError('Transformed RGB pixels differ from the reviewed image; no artwork was installed.')
    buffer = io.BytesIO()
    result.save(buffer, format='PNG', optimize=True)
    data = buffer.getvalue()
    final_hash = verified_pixels(data, entry)
    atomic_write(output, data)
    return final_hash, 'installed'


def setup_art(root, *, sources=None, workers=4, progress=print):
    root = Path(root).resolve()
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError('Artwork workers must be between 1 and 4.')
    entries, groups = load_recipes(root, Path(sources) if sources else root / 'data/art-sources.json')
    cache = local_path(root, '.cache/kyberdex-art/raw')
    cache.mkdir(parents=True, exist_ok=True)
    lock_path = local_path(root, '.cache/kyberdex-art/setup.lock')
    manifest_path = local_path(root, 'assets/art/manifest.json')
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('Another artwork setup is already running.') from error
        assets, errors = {}, []
        installed = reused = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(import_asset, root, cache, entries[identities[0]]): identities
                       for identities in groups.values()}
            for completed, future in enumerate(as_completed(futures), 1):
                identities = futures[future]
                try:
                    final_hash, action = future.result()
                    installed += action == 'installed'
                    reused += action == 'reused'
                    for identity in identities:
                        entry = entries[identity]
                        assets[identity] = {key: entry[key] for key in ('path', 'subject_id', 'width', 'height')}
                        assets[identity].update(status='ready', visual_qa='verified', sha256=final_hash,
                                                pixel_sha256=entry['pixel_sha256'])
                except (ValueError, OSError, KeyError, TypeError, Image.DecompressionBombError) as error:
                    if isinstance(error, urllib.error.HTTPError):
                        message = f'Source download returned HTTP {error.code}.'
                    elif isinstance(error, urllib.error.URLError):
                        message = 'Source download could not connect; check connectivity and retry.'
                    else:
                        message = str(error) if isinstance(error, ValueError) else type(error).__name__
                    errors.append({'id': identities[0], 'error': message})
                    progress(f"Artwork failed: {identities[0]} ({message})")
                if completed % 25 == 0 or completed == len(futures):
                    progress(f'Artwork: {completed}/{len(futures)} checked; {installed} installed, {reused} reused, {len(errors)} failed.')
        report = {'complete': not errors, 'definitions': len(assets), 'files': len(groups),
                  'installed': installed, 'reused': reused, 'failed': len(errors), 'errors': errors}
        manifest = {'format': 'kyberdex-reviewed-art', 'version': 1, 'assets': dict(sorted(assets.items())),
                    'setup': {'complete': not errors, 'failed': len(errors)}}
        atomic_write(manifest_path, (json.dumps(manifest, indent=2) + '\n').encode())
    return report
