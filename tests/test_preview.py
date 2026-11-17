import base64
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile

from files import services
from files.preview import load_preview
from storage.controller import StorageError
from .test_app import BaseCase

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7xsAAAAASUVORK5CYII=")


class PreviewTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.alice)

    def urls(self, file):
        page = f"/files/{file.pk}/preview/"
        return page, page + "content/"

    def test_text_is_escaped_and_selected_version_is_preserved(self):
        file = self.upload(self.alice, b"<script>alert('unsafe')</script>", "notes.txt")
        services.upload_version(self.alice, file, SimpleUploadedFile("notes.txt", b"updated"))
        page, raw = self.urls(file)
        response = self.client.get(page, {"v": 1})
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert")
        self.assertContains(response, "Download v1")
        self.assertContains(response, "Back to file")
        self.assertContains(self.client.get(page), "updated")
        self.assertEqual(self.client.get(raw).status_code, 415)

    def test_image_content_is_detected_from_bytes_and_not_trusted_mime(self):
        file = services.upload_file(self.alice, SimpleUploadedFile("picture.bin", PNG, content_type="text/html"))
        page, raw = self.urls(file)
        self.assertContains(self.client.get(page), '<img src=')
        response = self.client.get(raw)
        self.assertEqual(response.content, PNG)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertTrue(response["Content-Disposition"].startswith("inline;"))
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("default-src 'none'", response["Content-Security-Policy"])

    def test_pdf_uses_inline_viewer(self):
        content = b"%PDF-1.4\npreview test\n%%EOF"
        file = self.upload(self.alice, content, "report.pdf")
        page, raw = self.urls(file)
        self.assertContains(self.client.get(page), '<iframe src=')
        response = self.client.get(raw)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response.content, content)

    def test_html_svg_and_mislabelled_images_cannot_be_served_inline(self):
        for name in ("bad.html", "bad.svg", "fake.png", "archive.zip"):
            with self.subTest(name=name):
                file = self.upload(self.alice, b"<html><script>alert(1)</script></html>", name)
                page, raw = self.urls(file)
                response = self.client.get(page)
                self.assertContains(response, "Preview unavailable")
                self.assertNotContains(response, "<script>alert(1)</script>")
                self.assertEqual(self.client.get(raw).status_code, 415)

    def test_readers_and_writers_can_preview_but_revoked_users_cannot(self):
        file = self.upload(self.alice, PNG, "shared.png")
        page, raw = self.urls(file)
        for permission in ("read", "write"):
            services.share(self.alice, file, "bob", permission)
            self.client.force_login(self.bob)
            self.assertEqual(self.client.get(page).status_code, 200)
            self.assertEqual(self.client.get(raw).status_code, 200)
        services.revoke(self.alice, file, "bob")
        self.assertEqual(self.client.get(page).status_code, 404)
        self.assertEqual(self.client.get(raw).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(raw).status_code, 302)

    def test_size_and_encoding_fallbacks(self):
        file = self.upload(self.alice, b"longer text", "notes.txt")
        page, _ = self.urls(file)
        with patch("files.preview.MAX_TEXT_BYTES", 4):
            self.assertContains(self.client.get(page), "Text preview is limited")
        with patch("files.preview.MAX_PREVIEW_BYTES", 4):
            self.assertContains(self.client.get(page), "Preview is limited")
        invalid = self.upload(self.alice, b"\xff\xfe\x80", "notes.txt")
        self.assertContains(self.client.get(self.urls(invalid)[0]), "not UTF-8")
        binary = self.upload(self.alice, b"abc\x00def", "notes.txt")
        self.assertContains(self.client.get(self.urls(binary)[0]), "Preview unavailable")

    def test_integrity_and_unavailable_storage_fail_closed(self):
        file = self.upload(self.alice, PNG, "test.png")
        page, raw = self.urls(file)
        with patch("storage.controller.StorageController.fetch", side_effect=StorageError("unavailable")):
            self.assertEqual(self.client.get(raw).status_code, 400)
        file.current.chunks.update(sha256="0" * 64)
        response = self.client.get(page)
        self.assertEqual(response.status_code, 400)
        self.assertIn("no-store", response["Cache-Control"])
        with self.assertRaises(services.ServiceError):
            load_preview(self.alice, file, 1)

    def test_invalid_versions_and_non_get_requests(self):
        file = self.upload(self.alice, b"hello", "notes.txt")
        page, raw = self.urls(file)
        for number in ("bad", "0", "-1", "999"):
            self.assertEqual(self.client.get(page, {"v": number}).status_code, 400)
        self.assertEqual(self.client.post(raw).status_code, 405)
        self.assertContains(self.client.get(f"/files/{file.pk}/"), "Preview v1")
