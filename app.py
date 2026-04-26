from flask import Flask, request, jsonify, render_template, send_file
from PIL import Image, ImageSequence
import io, base64, colorsys, math
from pathlib import Path
from uuid import uuid4

app = Flask(__name__)

dirs = ['up', 'right', 'down', 'left']
parts = ['part1', 'part2', 'part3', 'part4']
# One uploaded sheet == one output row/frame
outfit_frames = []
recolor_uploads = {}


def to_b64(img):
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()


def bytes_to_data_uri(raw_bytes, mime_type):
    return f'data:{mime_type};base64,' + base64.b64encode(raw_bytes).decode()


def clamp01(value):
    return max(0.0, min(1.0, value))


def parse_hex_color(value, fallback=(255, 255, 255)):
    raw = (value or '').strip().lstrip('#')
    if len(raw) != 6:
        return fallback
    try:
        return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))
    except ValueError:
        return fallback


def rgb_to_hex(rgb):
    return '#%02x%02x%02x' % rgb


def circular_hue_distance(h1, h2):
    diff = abs(h1 - h2)
    return min(diff, 1.0 - diff)


def classify_group_key(r, g, b, a):
    if a < 12:
        return None
    h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)

    # Keep only near-black pixels excluded (typical hard outlines).
    if v < 0.07:
        return None

    # Keep neutrals together, with only a light/dark split.
    if s < 0.14:
        return f'n{min(2, int(v * 3))}'

    # Group by broad hue family so different shades of the same color stay together.
    h_bucket = int(h * 12) % 12
    return f'h{h_bucket}'


def extract_animation(file_storage):
    img = Image.open(file_storage)
    frames = []
    durations = []
    loop = int(img.info.get('loop', 0))
    src_format = (img.format or '').upper()

    if getattr(img, 'is_animated', False):
        for frame in ImageSequence.Iterator(img):
            frames.append(frame.convert('RGBA'))
            d = int(frame.info.get('duration', img.info.get('duration', 100)) or 100)
            durations.append(max(20, d))
    else:
        frames.append(img.convert('RGBA'))
        durations.append(100)

    return {
        'frames': frames,
        'durations': durations,
        'loop': loop,
        'source_format': src_format,
    }


AGGRESSIVENESS_THRESHOLDS = {
    1: 0.22,
    2: 0.15,
    3: 0.09,
    4: 0.05,
    5: 0.02,
}


def analyze_color_groups(
    frames,
    max_groups=64,
    min_pixel_share=0.00005,
    merge_threshold=0.09,
):
    stats = {}
    total_pixels = 0
    frame_count = max(1, len(frames))
    for frame in frames:
        frame_seen = set()
        for r, g, b, a in frame.getdata():
            key = classify_group_key(r, g, b, a)
            if not key:
                continue
            total_pixels += 1
            entry = stats.get(key)
            if entry is None:
                entry = {
                    'count': 0,
                    'sum_r': 0,
                    'sum_g': 0,
                    'sum_b': 0,
                    'sum_s': 0.0,
                    'sum_v': 0.0,
                    'hue_x': 0.0,
                    'hue_y': 0.0,
                    'frame_hits': 0,
                    'color_counts': {},
                }
                stats[key] = entry
            h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
            entry['count'] += 1
            entry['sum_r'] += r
            entry['sum_g'] += g
            entry['sum_b'] += b
            entry['sum_s'] += s
            entry['sum_v'] += v
            if s >= 0.14:
                angle = h * math.tau
                entry['hue_x'] += math.cos(angle)
                entry['hue_y'] += math.sin(angle)
            hex_color = rgb_to_hex((r, g, b))
            entry['color_counts'][hex_color] = entry['color_counts'].get(hex_color, 0) + 1
            frame_seen.add(key)

        for key in frame_seen:
            stats[key]['frame_hits'] += 1

    ranked = sorted(
        stats.items(),
        key=lambda it: (it[1]['frame_hits'], it[1]['count']),
        reverse=True,
    )

    selected = []
    for key, info in ranked:
        pixel_ratio = info['count'] / max(1, total_pixels)
        if pixel_ratio >= min_pixel_share:
            selected.append((key, info))

    # Ensure we always keep top groups even on tiny/clean animations.
    if len(selected) < min(16, len(ranked)):
        selected_keys = {key for key, _ in selected}
        for key, info in ranked:
            if key not in selected_keys:
                selected.append((key, info))
            if len(selected) >= min(16, len(ranked)):
                break

    selected = selected[:max_groups]

    normalized = []
    for key, info in selected:
        count = max(1, info['count'])
        avg_h = None
        if info['hue_x'] or info['hue_y']:
            avg_h = (math.atan2(info['hue_y'], info['hue_x']) / math.tau) % 1.0
        normalized.append({
            'keys': [key],
            'count': info['count'],
            'sum_r': info['sum_r'],
            'sum_g': info['sum_g'],
            'sum_b': info['sum_b'],
            'sum_s': info['sum_s'],
            'sum_v': info['sum_v'],
            'hue_x': info['hue_x'],
            'hue_y': info['hue_y'],
            'frame_hits': info['frame_hits'],
            'color_counts': dict(info['color_counts']),
            'avg_h': avg_h,
            'neutral': key.startswith('n'),
        })

    neutrals = [entry for entry in normalized if entry['neutral']]
    chromatic = [entry for entry in normalized if not entry['neutral']]
    chromatic.sort(key=lambda entry: entry['avg_h'] if entry['avg_h'] is not None else -1.0)

    merged_chromatic = []
    for entry in chromatic:
        if not merged_chromatic:
            merged_chromatic.append(entry)
            continue

        prev = merged_chromatic[-1]
        if prev['avg_h'] is None or entry['avg_h'] is None:
            merged_chromatic.append(entry)
            continue

        prev_s = prev['sum_s'] / max(1, prev['count'])
        prev_v = prev['sum_v'] / max(1, prev['count'])
        entry_s = entry['sum_s'] / max(1, entry['count'])
        entry_v = entry['sum_v'] / max(1, entry['count'])
        should_merge = (
            circular_hue_distance(prev['avg_h'], entry['avg_h']) <= merge_threshold
            and abs(prev_s - entry_s) <= 0.35
            and abs(prev_v - entry_v) <= 0.42
        )
        if not should_merge:
            merged_chromatic.append(entry)
            continue

        prev['keys'].extend(entry['keys'])
        prev['count'] += entry['count']
        prev['sum_r'] += entry['sum_r']
        prev['sum_g'] += entry['sum_g']
        prev['sum_b'] += entry['sum_b']
        prev['sum_s'] += entry['sum_s']
        prev['sum_v'] += entry['sum_v']
        prev['hue_x'] += entry['hue_x']
        prev['hue_y'] += entry['hue_y']
        prev['frame_hits'] = max(prev['frame_hits'], entry['frame_hits'])
        for hex_color, px_count in entry['color_counts'].items():
            prev['color_counts'][hex_color] = prev['color_counts'].get(hex_color, 0) + px_count
        prev['avg_h'] = (math.atan2(prev['hue_y'], prev['hue_x']) / math.tau) % 1.0

    merged_entries = merged_chromatic + neutrals
    merged_entries.sort(key=lambda entry: (entry['frame_hits'], entry['count']), reverse=True)

    groups = []
    by_key = {}
    for idx, info in enumerate(merged_entries, start=1):
        count = max(1, info['count'])
        rgb = (
            int(round(info['sum_r'] / count)),
            int(round(info['sum_g'] / count)),
            int(round(info['sum_b'] / count)),
        )
        group = {
            'id': f'g{idx}',
            'key': info['keys'][0],
            'hex': rgb_to_hex(rgb),
            'pixel_count': info['count'],
            'frame_hits': info['frame_hits'],
            'avg_s': info['sum_s'] / count,
            'avg_v': info['sum_v'] / count,
            'colors': [
                {
                    'hex': hex_color,
                    'pixel_count': px_count,
                }
                for hex_color, px_count in sorted(
                    info['color_counts'].items(),
                    key=lambda item: item[1],
                    reverse=True,
                )[:160]
            ],
        }
        groups.append(group)
        for key in info['keys']:
            by_key[key] = group

    return groups, by_key


def recolor_animation(upload, replacements):
    frames_out = []
    by_key = upload['groups_by_key']

    repl = replacements or {}
    if isinstance(repl, dict) and ('group' in repl or 'individual' in repl or 'individual_hex' in repl):
        group_replacements = repl.get('group', {}) or {}
        individual_replacements = repl.get('individual', {}) or {}
        individual_hex = repl.get('individual_hex', {}) or {}
    else:
        group_replacements = repl if isinstance(repl, dict) else {}
        individual_replacements = {}
        individual_hex = {}

    normalized_individual = {}
    if isinstance(individual_replacements, dict):
        for group_id, mapping in individual_replacements.items():
            if not isinstance(mapping, dict):
                continue
            group_map = {}
            for src_hex, dst_hex in mapping.items():
                src = (src_hex or '').strip().lower()
                if not src.startswith('#'):
                    src = '#' + src
                if len(src) != 7:
                    continue
                group_map[src] = parse_hex_color(dst_hex, parse_hex_color(src, (255, 255, 255)))
            if group_map:
                normalized_individual[group_id] = group_map

    normalized_individual_hex = {}
    if isinstance(individual_hex, dict):
        for src_hex, dst_hex in individual_hex.items():
            src = (src_hex or '').strip().lower()
            if not src.startswith('#'):
                src = '#' + src
            if len(src) != 7:
                continue
            normalized_individual_hex[src] = parse_hex_color(dst_hex, parse_hex_color(src, (255, 255, 255)))

    target_rgb = {}
    target_hsv = {}
    for group in upload['groups']:
        picked = parse_hex_color(group_replacements.get(group['id'], group['hex']), parse_hex_color(group['hex']))
        target_rgb[group['id']] = picked
        target_hsv[group['id']] = colorsys.rgb_to_hsv(picked[0] / 255.0, picked[1] / 255.0, picked[2] / 255.0)

    auto_shade_maps = {}
    for group in upload['groups']:
        group_id = group['id']
        trg_h, trg_s, trg_v = target_hsv[group_id]
        ref_v = max(0.06, group.get('avg_v', 0.5))
        ref_s = max(0.04, group.get('avg_s', 0.4))

        shade_map = {}
        for color_info in group.get('colors', []):
            src_hex = (color_info.get('hex') or '').strip().lower()
            if not src_hex:
                continue
            sr, sg, sb = parse_hex_color(src_hex)
            _, src_s, src_v = colorsys.rgb_to_hsv(sr / 255.0, sg / 255.0, sb / 255.0)

            v_scale = src_v / ref_v
            s_scale = src_s / ref_s

            new_h = trg_h
            new_s = clamp01((trg_s * s_scale * 0.65) + (trg_s * 0.35))
            new_v = clamp01(trg_v * v_scale)

            nr, ng, nb = colorsys.hsv_to_rgb(new_h, new_s, new_v)
            shade_map[src_hex] = (
                int(round(nr * 255)),
                int(round(ng * 255)),
                int(round(nb * 255)),
            )
        auto_shade_maps[group_id] = shade_map

    shade_cache = {}

    for frame in upload['frames']:
        recolored_pixels = []
        for r, g, b, a in frame.getdata():
            key = classify_group_key(r, g, b, a)
            group = by_key.get(key) if key else None
            if group is None:
                recolored_pixels.append((r, g, b, a))
                continue

            group_id = group['id']
            src_hex = rgb_to_hex((r, g, b)).lower()

            if src_hex in normalized_individual_hex:
                rr, gg, bb = normalized_individual_hex[src_hex]
                recolored_pixels.append((rr, gg, bb, a))
                continue

            exact_map = normalized_individual.get(group_id, {})
            if src_hex in exact_map:
                rr, gg, bb = exact_map[src_hex]
                recolored_pixels.append((rr, gg, bb, a))
                continue

            auto_map = auto_shade_maps.get(group_id, {})
            if src_hex in auto_map:
                rr, gg, bb = auto_map[src_hex]
                recolored_pixels.append((rr, gg, bb, a))
                continue

            cache_key = (group_id, r, g, b)
            cached = shade_cache.get(cache_key)
            if cached is not None:
                recolored_pixels.append((cached[0], cached[1], cached[2], a))
                continue

            src_h, src_s, src_v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
            trg_h, trg_s, trg_v = target_hsv[group_id]
            ref_v = max(0.08, group['avg_v'])

            shade_scale = src_v / ref_v
            new_h = trg_h
            new_s = clamp01((trg_s * 0.75) + (src_s * 0.25))
            new_v = clamp01(trg_v * shade_scale)

            nr, ng, nb = colorsys.hsv_to_rgb(new_h, new_s, new_v)
            rr, gg, bb = int(round(nr * 255)), int(round(ng * 255)), int(round(nb * 255))
            shade_cache[cache_key] = (rr, gg, bb)
            recolored_pixels.append((rr, gg, bb, a))

        new_frame = Image.new('RGBA', frame.size)
        new_frame.putdata(recolored_pixels)
        frames_out.append(new_frame)

    return frames_out


def apply_background_to_frames(frames, background_mode, background_hex):
    if (background_mode or 'transparent').lower() != 'solid':
        return frames

    bg_rgb = parse_hex_color(background_hex, (255, 255, 255))
    flattened = []
    for frame in frames:
        canvas = Image.new('RGBA', frame.size, (bg_rgb[0], bg_rgb[1], bg_rgb[2], 255))
        canvas.alpha_composite(frame.convert('RGBA'))
        flattened.append(canvas)
    return flattened


def save_animation_bytes(frames, durations, loop, out_format):
    if not frames:
        raise ValueError('No frames to export.')

    buf = io.BytesIO()
    fmt = (out_format or '').lower()
    if fmt == 'gif':
        # Let Pillow quantize from RGBA while preserving transparent pixels.
        rgba_frames = [f.convert('RGBA') for f in frames]
        rgba_frames[0].save(
            buf,
            format='GIF',
            save_all=True,
            append_images=rgba_frames[1:],
            loop=int(loop),
            duration=durations,
            transparency=0,
            disposal=2,
            optimize=False,
        )
        return buf.getvalue(), 'image/gif', 'gif'

    if fmt == 'apng':
        frames[0].save(
            buf,
            format='PNG',
            save_all=True,
            append_images=frames[1:],
            loop=int(loop),
            duration=durations,
            optimize=False,
        )
        return buf.getvalue(), 'image/apng', 'png'

    raise ValueError('Unsupported export format. Use gif or apng.')


def paste(canvas, img, x, y):
    cw, ch = canvas.size
    pw = min(img.width, cw - x)
    ph = min(img.height, ch - y)
    if pw > 0 and ph > 0:
        region = img.crop((0, 0, pw, ph))
        canvas.paste(region, (x, y), region)


def fit_tile_to_cell(img, cell_w, cell_h, bbox=None):
    img = img.convert('RGBA')
    if bbox is None:
        bbox = img.split()[3].getbbox()
    if bbox is None:
        bbox = (0, 0, img.width, img.height)
    cropped = img.crop(bbox)
    if cropped.width <= cell_w and cropped.height <= cell_h:
        return cropped

    scale = min(cell_w / max(1, cropped.width), cell_h / max(1, cropped.height))
    new_size = (
        max(1, int(round(cropped.width * scale))),
        max(1, int(round(cropped.height * scale))),
    )
    return cropped.resize(new_size, Image.LANCZOS)


def colorize_flat(img, hex_color):
    hx = (hex_color or '').lstrip('#')
    if len(hx) != 6:
        return img
    rt, gt, bt = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
    img = img.convert('RGBA')
    px = list(img.getdata())
    new_px = []
    for _, _, _, a in px:
        if a > 0:
            new_px.append((rt, gt, bt, a))
        else:
            new_px.append((0, 0, 0, 0))
    img.putdata(new_px)
    return img


def parse_sheet(img, ss):
    # Expected: 4 columns (directions), 5 rows (base + 4 parts)
    need_w, need_h = ss * 4, ss * 5
    if img.width < need_w or img.height < need_h:
        raise ValueError(f'Sheet must be at least {need_w}x{need_h}px for sprite size {ss}.')

    # Use top-left 4x5 grid region.
    frame = {
        'base': {},
        'parts': {p: {} for p in parts}
    }
    for di, dr in enumerate(dirs):
        x0 = di * ss
        frame['base'][dr] = img.crop((x0, 0, x0 + ss, ss)).convert('RGBA')
        frame['parts']['part1'][dr] = img.crop((x0, ss, x0 + ss, ss * 2)).convert('RGBA')
        frame['parts']['part2'][dr] = img.crop((x0, ss * 2, x0 + ss, ss * 3)).convert('RGBA')
        frame['parts']['part3'][dr] = img.crop((x0, ss * 3, x0 + ss, ss * 4)).convert('RGBA')
        frame['parts']['part4'][dr] = img.crop((x0, ss * 4, x0 + ss, ss * 5)).convert('RGBA')
    return frame


def build_outfit_image(mode, colors):
    nf = len(outfit_frames)
    if nf == 0:
        raise ValueError('No sheets uploaded yet.')

    cell_w, cell_h = 64, 64
    cols = 4 if mode == 'outfit-plain' else 8
    if mode not in ('outfit-plain', 'outfit-colored'):
        raise ValueError('Unknown mode.')
    target_w = cell_w * cols
    target_h = cell_h * nf
    canvas = Image.new('RGBA', (target_w, target_h), (0, 0, 0, 0))

    for fi, frame in enumerate(outfit_frames):
        y = fi * cell_h
        if y >= target_h:
            break

        for di, dr in enumerate(dirs):
            base_img = frame['base'][dr]
            all_tiles = [base_img] + [frame['parts'][p][dr] for p in parts]

            # Use union bbox so base/color stay perfectly aligned and fill the cell.
            bbox = None
            for tile in all_tiles:
                b = tile.split()[3].getbbox()
                if b is None:
                    continue
                if bbox is None:
                    bbox = b
                else:
                    bbox = (
                        min(bbox[0], b[0]), min(bbox[1], b[1]),
                        max(bbox[2], b[2]), max(bbox[3], b[3])
                    )

            if mode == 'outfit-plain':
                x = di * cell_w
                fitted = fit_tile_to_cell(base_img, cell_w, cell_h, bbox)
                paste(canvas, fitted, x + cell_w - fitted.width, y + cell_h - fitted.height)
            else:
                x_base = di * 2 * cell_w
                x_color = x_base + cell_w

                fitted_base = fit_tile_to_cell(base_img, cell_w, cell_h, bbox)
                paste(
                    canvas,
                    fitted_base,
                    x_base + cell_w - fitted_base.width,
                    y + cell_h - fitted_base.height,
                )

                merged = Image.new('RGBA', (cell_w, cell_h), (0, 0, 0, 0))
                for p in parts:
                    layer = colorize_flat(frame['parts'][p][dr].copy(), colors.get(p, ''))
                    fitted_layer = fit_tile_to_cell(layer, cell_w, cell_h, bbox)
                    layer_canvas = Image.new('RGBA', (cell_w, cell_h), (0, 0, 0, 0))
                    paste(
                        layer_canvas,
                        fitted_layer,
                        cell_w - fitted_layer.width,
                        cell_h - fitted_layer.height,
                    )
                    merged = Image.alpha_composite(merged, layer_canvas)
                paste(canvas, merged, x_color, y)

    return canvas


@app.route('/')
def landing():
    return render_template('landing.html')


@app.route('/outfits')
def outfits_lab():
    return render_template('index.html')


@app.route('/items')
def items_lab():
    return render_template('items.html')


@app.route('/recolor')
def recolor_lab():
    return render_template('recolor.html')


@app.route('/padlabs-icon.png')
def padlabs_icon():
    icon_path = Path(app.root_path) / 'padlabs-icon.png'
    return send_file(icon_path, mimetype='image/png')


@app.route('/api/upload_sheets', methods=['POST'])
def upload_sheets():
    ss_raw = request.form.get('sprite_size', '64')
    try:
        ss = max(1, int(ss_raw))
    except (TypeError, ValueError):
        ss = 64

    files = request.files.getlist('files')
    if not files:
        return jsonify({'error': 'No files uploaded.'}), 400

    added = []
    for f in files:
        try:
            img = Image.open(f).convert('RGBA')
            frame = parse_sheet(img, ss)
        except Exception as e:
            return jsonify({'error': f'{f.filename}: {e}'}), 400

        outfit_frames.append(frame)

        thumb = img.copy()
        thumb.thumbnail((96, 96), Image.LANCZOS)
        added.append({'name': f.filename, 'thumb': to_b64(thumb), 'size': list(img.size)})

    return jsonify({'count': len(outfit_frames), 'added': added})


@app.route('/api/clear_sheets', methods=['POST'])
def clear_sheets():
    outfit_frames.clear()
    return jsonify({'ok': True})


@app.route('/api/build_outfit', methods=['POST'])
def build_outfit():
    d = request.json or {}
    mode = d.get('mode', 'outfit-colored')
    preview = bool(d.get('preview', False))
    default_colors = {
        'part1': '#ff0000',
        'part2': '#00ff00',
        'part3': '#0000ff',
        'part4': '#ffff00',
    }
    colors = {**default_colors, **(d.get('colors', {}) or {})}

    try:
        out = build_outfit_image(mode, colors)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    if preview:
        return jsonify({'preview': to_b64(out)})

    buf = io.BytesIO()
    out.save(buf, 'PNG')
    buf.seek(0)
    filename = 'outfit_colored.png' if mode == 'outfit-colored' else 'outfit.png'
    return send_file(buf, mimetype='image/png', as_attachment=True, download_name=filename)


@app.route('/api/recolor/upload', methods=['POST'])
def recolor_upload():
    file_obj = request.files.get('file')
    if not file_obj:
        return jsonify({'error': 'No file uploaded.'}), 400

    try:
        aggressiveness = int(request.form.get('aggressiveness', 3))
        aggressiveness = max(1, min(5, aggressiveness))
        threshold = AGGRESSIVENESS_THRESHOLDS[aggressiveness]
        parsed = extract_animation(file_obj)
        groups, by_key = analyze_color_groups(parsed['frames'], merge_threshold=threshold)
    except Exception as exc:
        return jsonify({'error': f'Unable to read sprite: {exc}'}), 400

    if not groups:
        return jsonify({'error': 'No editable color groups found. Try a sprite with brighter body colors.'}), 400

    upload_id = uuid4().hex
    recolor_uploads[upload_id] = {
        'frames': parsed['frames'],
        'durations': parsed['durations'],
        'loop': parsed['loop'],
        'source_format': parsed['source_format'],
        'groups': groups,
        'groups_by_key': by_key,
    }

    # Keep memory bounded for iterative use.
    while len(recolor_uploads) > 24:
        first_key = next(iter(recolor_uploads.keys()))
        recolor_uploads.pop(first_key, None)

    preview_bytes, preview_mime, _ = save_animation_bytes(
        parsed['frames'],
        parsed['durations'],
        parsed['loop'],
        'gif',
    )

    individual_counts = {}
    individual_group_refs = {}
    for group in groups:
        gid = group['id']
        for color in group.get('colors', []):
            src_hex = (color.get('hex') or '').lower()
            if not src_hex:
                continue
            individual_counts[src_hex] = individual_counts.get(src_hex, 0) + int(color.get('pixel_count', 0) or 0)
            refs = individual_group_refs.get(src_hex)
            if refs is None:
                refs = set()
                individual_group_refs[src_hex] = refs
            refs.add(gid)

    individual_colors = [
        {
            'hex': hex_color,
            'pixel_count': px_count,
            'groups': sorted(list(individual_group_refs.get(hex_color, set()))),
        }
        for hex_color, px_count in sorted(individual_counts.items(), key=lambda item: item[1], reverse=True)
    ]

    return jsonify({
        'upload_id': upload_id,
        'frame_count': len(parsed['frames']),
        'size': list(parsed['frames'][0].size),
        'source_format': parsed['source_format'] or 'UNKNOWN',
        'individual_colors': individual_colors,
        'groups': [
            {
                'id': g['id'],
                'hex': g['hex'],
                'pixel_count': g['pixel_count'],
                'frame_hits': g['frame_hits'],
                'colors': g['colors'],
            }
            for g in groups
        ],
        'original_preview': bytes_to_data_uri(preview_bytes, preview_mime),
    })


@app.route('/api/recolor/reanalyze', methods=['POST'])
def recolor_reanalyze():
    body = request.get_json(force=True, silent=True) or {}
    upload_id = body.get('upload_id', '')
    upload = recolor_uploads.get(upload_id)
    if not upload:
        return jsonify({'error': 'Upload not found. Please re-upload the sprite.'}), 404

    aggressiveness = int(body.get('aggressiveness', 3))
    aggressiveness = max(1, min(5, aggressiveness))
    threshold = AGGRESSIVENESS_THRESHOLDS[aggressiveness]

    try:
        groups, by_key = analyze_color_groups(upload['frames'], merge_threshold=threshold)
    except Exception as exc:
        return jsonify({'error': f'Re-analysis failed: {exc}'}), 500

    if not groups:
        return jsonify({'error': 'No editable color groups found.'}), 400

    upload['groups'] = groups
    upload['groups_by_key'] = by_key

    individual_counts = {}
    individual_group_refs = {}
    for group in groups:
        gid = group['id']
        for color in group.get('colors', []):
            src_hex = (color.get('hex') or '').lower()
            if not src_hex:
                continue
            individual_counts[src_hex] = individual_counts.get(src_hex, 0) + int(color.get('pixel_count', 0) or 0)
            refs = individual_group_refs.get(src_hex)
            if refs is None:
                refs = set()
                individual_group_refs[src_hex] = refs
            refs.add(gid)

    individual_colors = [
        {
            'hex': hex_color,
            'pixel_count': px_count,
            'groups': sorted(list(individual_group_refs.get(hex_color, set()))),
        }
        for hex_color, px_count in sorted(individual_counts.items(), key=lambda item: item[1], reverse=True)
    ]

    return jsonify({
        'individual_colors': individual_colors,
        'groups': [
            {
                'id': g['id'],
                'hex': g['hex'],
                'pixel_count': g['pixel_count'],
                'frame_hits': g['frame_hits'],
                'colors': g['colors'],
            }
            for g in groups
        ],
    })


@app.route('/api/recolor/preview', methods=['POST'])
def recolor_preview():
    payload = request.json or {}
    upload_id = payload.get('upload_id')
    upload = recolor_uploads.get(upload_id)
    if not upload:
        return jsonify({'error': 'Sprite upload not found. Please upload again.'}), 404

    replacements = payload.get('replacements', {}) or {}
    try:
        recolored = recolor_animation(upload, replacements)
        preview_bytes, preview_mime, _ = save_animation_bytes(
            recolored,
            upload['durations'],
            upload['loop'],
            'gif',
        )
    except Exception as exc:
        return jsonify({'error': f'Preview failed: {exc}'}), 400

    return jsonify({'preview': bytes_to_data_uri(preview_bytes, preview_mime)})


@app.route('/api/recolor/export', methods=['POST'])
def recolor_export():
    payload = request.json or {}
    upload_id = payload.get('upload_id')
    upload = recolor_uploads.get(upload_id)
    if not upload:
        return jsonify({'error': 'Sprite upload not found. Please upload again.'}), 404

    replacements = payload.get('replacements', {}) or {}
    out_format = (payload.get('format') or 'gif').lower()
    background_mode = (payload.get('background_mode') or 'transparent').lower()
    background_color = payload.get('background_color') or '#ffffff'

    try:
        recolored = recolor_animation(upload, replacements)
        recolored = apply_background_to_frames(recolored, background_mode, background_color)
        file_bytes, mime_type, ext = save_animation_bytes(
            recolored,
            upload['durations'],
            upload['loop'],
            out_format,
        )
    except Exception as exc:
        return jsonify({'error': f'Export failed: {exc}'}), 400

    out = io.BytesIO(file_bytes)
    out.seek(0)
    return send_file(
        out,
        mimetype=mime_type,
        as_attachment=True,
        download_name=f'sprite_recolor.{ext}',
    )


@app.route('/api/recolor/clear', methods=['POST'])
def recolor_clear():
    payload = request.json or {}
    upload_id = payload.get('upload_id')
    if upload_id:
        recolor_uploads.pop(upload_id, None)
    return jsonify({'ok': True})


if __name__ == '__main__':
    app.run(debug=True, port=5000)
