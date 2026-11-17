import base64
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile

from files import services
from files.preview import load_preview
from storage.controller import StorageController, StorageError
from files.models import Transfer
from .test_app import BaseCase

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7xsAAAAASUVORK5CYII=")


class PreviewTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.alice)

    def urls(self, file):
        page = f"/files/{file.pk}/preview/"
        return page, page + "content/"

    def test_media_page_probes_one_chunk_and_content_streams_lazily(self):
        data = PNG + b"x" * 200_000
        file = self.upload(self.alice, data, "large.png")
        page, raw = self.urls(file)
        original = StorageController.fetch
        with patch.object(StorageController, "fetch", autospec=True, side_effect=original) as fetch:
            self.assertContains(self.client.get(page), '<img src=')
            self.assertEqual(fetch.call_count, 1)
        with patch.object(StorageController, "fetch", autospec=True, side_effect=original) as fetch:
            response = self.client.get(raw)
            self.assertTrue(response.streaming)
            self.assertEqual(fetch.call_count, 1)  # only the signature chunk, not the whole file
            iterator = iter(response.streaming_content)
            first = next(iterator)
            self.assertEqual(fetch.call_count, 1)  # reuse the signature chunk
            self.assertEqual(first + b"".join(iterator), data)
            self.assertEqual(fetch.call_count, file.current.chunk_count)
            response.close()

    def test_previews_do_not_log_downloads_but_actual_downloads_still_do(self):
        image = self.upload(self.alice, PNG + b"x" * 100_000, "image.png")
        text = self.upload(self.alice, b"hello", "notes.txt")
        self.client.get(self.urls(text)[0])
        page, raw = self.urls(image)
        self.client.get(page)
        response = self.client.get(raw)
        b"".join(response.streaming_content)
        response.close()
        aborted = self.client.get(raw)
        next(iter(aborted.streaming_content))
        aborted.close()
        self.assertEqual(Transfer.objects.filter(direction=Transfer.DOWNLOAD).count(), 0)
        self.assertEqual(Transfer.objects.count(), 2)  # the original uploads only
        response = self.client.get(f"/files/{image.pk}/download/")
        self.assertEqual(b"".join(response.streaming_content), PNG + b"x" * 100_000)
        response.close()
        self.assertEqual(Transfer.objects.filter(direction=Transfer.DOWNLOAD, status="ok").count(), 1)

    def test_unsupported_media_and_oversized_text_do_not_fetch_remaining_chunks(self):
        original = StorageController.fetch
        for name in ("unknown.bin", "large.txt"):
            file = self.upload(self.alice, b"x" * 200_000, name)
            with patch.object(StorageController, "fetch", autospec=True, side_effect=original) as fetch, \
                 patch("files.preview.MAX_TEXT_BYTES", 4):
                self.assertContains(self.client.get(self.urls(file)[0]), "Preview unavailable")
                self.assertEqual(fetch.call_count, 1)

    def test_closing_unconsumed_response_does_not_prefetch_remaining_chunks(self):
        file = self.upload(self.alice, PNG + b"x" * 200_000, "image.png")
        original = StorageController.fetch
        with patch.object(StorageController, "fetch", autospec=True, side_effect=original) as fetch:
            response = self.client.get(self.urls(file)[1])
            response.close()
            self.assertEqual(fetch.call_count, 1)
        self.assertFalse(Transfer.objects.filter(direction=Transfer.DOWNLOAD).exists())

    def test_later_storage_failure_interrupts_stream_without_logging_a_download(self):
        file = self.upload(self.alice, PNG + b"x" * 200_000, "image.png")
        original = StorageController.fetch

        def fetch(ctrl, sha, candidates, index=None):
            if index:
                raise StorageError("later chunk unavailable")
            return original(ctrl, sha, candidates, index)

        with patch.object(StorageController, "fetch", autospec=True, side_effect=fetch):
            response = self.client.get(self.urls(file)[1])
            iterator = iter(response.streaming_content)
            self.assertTrue(next(iterator).startswith(PNG))
            with self.assertRaises(StorageError):
                b"".join(iterator)
            response.close()
        self.assertFalse(Transfer.objects.filter(direction=Transfer.DOWNLOAD).exists())

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
        self.assertTrue(response.streaming)
        self.assertEqual(b"".join(response.streaming_content), PNG)
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
        self.assertTrue(response.streaming)
        self.assertEqual(b"".join(response.streaming_content), content)

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
            response = self.client.get(raw)
            self.assertEqual(response.status_code, 200)
            response.close()
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
