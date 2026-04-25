from flask import Flask, request, jsonify, render_template, send_file
from PIL import Image
import io, base64
from pathlib import Path

app = Flask(__name__)

dirs = ['up', 'right', 'down', 'left']
parts = ['part1', 'part2', 'part3', 'part4']
# One uploaded sheet == one output row/frame
outfit_frames = []


def to_b64(img):
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()


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


if __name__ == '__main__':
    app.run(debug=True, port=5000)
