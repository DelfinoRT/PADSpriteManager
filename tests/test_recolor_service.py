import io
import unittest

from PIL import Image

from app import app, recolor_uploads


class RecolorServiceTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        recolor_uploads.clear()

    @staticmethod
    def _build_test_gif_bytes():
        frame_a = Image.new('RGBA', (16, 16), (0, 0, 0, 0))
        frame_b = Image.new('RGBA', (16, 16), (0, 0, 0, 0))

        for x in range(16):
            for y in range(16):
                frame_a.putpixel((x, y), (230, 50, 50, 255) if x < 8 else (40, 180, 80, 255))
                frame_b.putpixel((x, y), (70, 110, 240, 255) if y < 8 else (230, 190, 40, 255))

        data = io.BytesIO()
        frame_a.save(
            data,
            format='GIF',
            save_all=True,
            append_images=[frame_b],
            duration=[100, 100],
            loop=0,
            transparency=0,
            disposal=2,
        )
        return data.getvalue()

    def _upload_sprite(self):
        gif_bytes = self._build_test_gif_bytes()
        response = self.client.post(
            '/api/recolor/upload',
            data={
                'file': (io.BytesIO(gif_bytes), 'sprite.gif'),
                'aggressiveness': '3',
            },
            content_type='multipart/form-data',
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertIn('upload_id', body)
        self.assertTrue(body['upload_id'])
        self.assertGreaterEqual(len(body.get('groups') or []), 1)
        return body['upload_id']

    def test_upload_preview_reanalyze_export_and_clear(self):
        upload_id = self._upload_sprite()

        preview_response = self.client.post(
            '/api/recolor/preview',
            json={
                'upload_id': upload_id,
                'replacements': {'group': {}, 'individual_hex': {}},
            },
        )
        self.assertEqual(preview_response.status_code, 200)
        preview_body = preview_response.get_json()
        self.assertIn('preview', preview_body)
        self.assertTrue(preview_body['preview'].startswith('data:image/gif;base64,'))

        reanalyze_response = self.client.post(
            '/api/recolor/reanalyze',
            json={'upload_id': upload_id, 'aggressiveness': 4},
        )
        self.assertEqual(reanalyze_response.status_code, 200)
        reanalyze_body = reanalyze_response.get_json()
        self.assertIn('groups', reanalyze_body)
        self.assertGreaterEqual(len(reanalyze_body['groups']), 1)

        export_response = self.client.post(
            '/api/recolor/export',
            json={
                'upload_id': upload_id,
                'replacements': {'group': {}, 'individual_hex': {}},
                'format': 'gif',
                'background_mode': 'transparent',
                'background_color': '#ffffff',
            },
        )
        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(export_response.headers.get('Content-Type'), 'image/gif')
        self.assertGreater(len(export_response.data), 0)

        clear_response = self.client.post('/api/recolor/clear', json={'upload_id': upload_id})
        self.assertEqual(clear_response.status_code, 200)

        missing_response = self.client.post(
            '/api/recolor/reanalyze',
            json={'upload_id': upload_id, 'aggressiveness': 3},
        )
        self.assertEqual(missing_response.status_code, 404)

    def test_upload_missing_file_returns_400(self):
        response = self.client.post('/api/recolor/upload', data={}, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 400)
        body = response.get_json()
        self.assertEqual(body.get('error'), 'No file uploaded.')

    def test_preview_ignores_malformed_replacements_shape(self):
        upload_id = self._upload_sprite()
        response = self.client.post(
            '/api/recolor/preview',
            json={
                'upload_id': upload_id,
                'replacements': {
                    'group': 'not-a-map',
                    'individual': [],
                    'individual_hex': ['bad'],
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue((body.get('preview') or '').startswith('data:image/gif;base64,'))

    def test_export_invalid_format_returns_400(self):
        upload_id = self._upload_sprite()
        response = self.client.post(
            '/api/recolor/export',
            json={
                'upload_id': upload_id,
                'replacements': {'group': {}, 'individual_hex': {}},
                'format': 'webp',
            },
        )
        self.assertEqual(response.status_code, 400)
        body = response.get_json()
        self.assertIn('Unsupported export format', body.get('error', ''))


if __name__ == '__main__':
    unittest.main()
