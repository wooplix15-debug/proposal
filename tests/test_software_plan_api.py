import io
import json
import zipfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from api import index as api


def sample_documents():
    return [{"kind": kind, "title": title, "project_name": "Sample Project", "client_name": "Client",
             "status": "Planning draft — for review", "prepared_by": "Wooplix", "date": "13 October 2026",
             "content": {"summary": f"Sample {title} content."}}
            for kind, title in (("requirements", "Requirements"), ("proposal", "Project Proposal"),
                                ("brd", "Business Requirements Document"), ("ux", "UI/UX"),
                                ("implementation", "Implementation"), ("qa", "QA and UAT"),
                                ("documentation", "Documentation"), ("handover", "Handover"))]


class SoftwarePlanningApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)

    def test_analysis_endpoint_returns_a_signed_review_and_uses_the_software_analyzer(self):
        analysis = {"summary": "A booking tool", "project_name": "", "client_name": "",
                    "requirements": [{"id": "REQ-A", "statement": "Create a booking tool."}],
                    "requirement_sections": [{"product": "Project requirements", "requirements": ["Create a booking tool."]}],
                    "questions": []}
        with patch.object(api, "_require_allowed_user"), \
             patch.object(api.agent, "GROQ_API_KEY", "configured"), \
             patch.object(api.software_plan_agent, "analyze", return_value=analysis), \
             patch.object(api.workflow, "seal", return_value="signed-token"):
            response = self.client.post("/api/software-plan/analyze", data={"text": "Create a booking tool."})
        self.assertEqual(response.status_code, 200)
        review = response.json()["reviews"][0]
        self.assertEqual(review["review_token"], "signed-token")
        self.assertEqual(review["requirements"][0]["id"], "REQ-A")

    def test_pdf_option_returns_eight_separate_pdfs_and_manifest_names(self):
        context = {"software_plan": True, "analysis": {"project_name": "Sample Project"}}
        def write_pdf(_document, target, _host=None):
            target.write_bytes(b"%PDF test")
        def write_docx(_data, target):
            Path(target).write_bytes(b"docx test")

        request = [{"review_token": "signed-token", "answers": {}}]
        with patch.object(api, "_require_allowed_user"), \
             patch.object(api.agent, "GROQ_API_KEY", "configured"), \
             patch.object(api.workflow, "unseal", return_value=context), \
             patch.object(api.software_plan_agent, "build_pack", return_value=sample_documents()), \
             patch.object(api, "_render_software_pack_pdf", side_effect=write_pdf), \
             patch.object(api.agent, "build_docx", side_effect=write_docx), \
             patch.object(api.brd_agent, "build_docx", side_effect=write_docx), \
             patch.object(api.software_plan_agent, "build_docx", side_effect=write_docx):
            response = self.client.post("/api/software-plan/generate?format=pdf", data={"reviews": json.dumps(request)})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            pdf_files = [name for name in archive.namelist() if name.endswith(".pdf")]
            self.assertEqual(pdf_files, [f"{n:02d}-{kind}.pdf" for n, kind in enumerate(
                ("requirements", "proposal", "brd", "ux", "implementation", "qa", "documentation", "handover"), 1)])
        self.assertIn("Software_Planning_Pack", response.headers["x-proposal-filename"])

    def test_word_option_returns_eight_docx_files_without_needing_pdf_renderer(self):
        context = {"software_plan": True, "analysis": {"project_name": "Sample Project"}}
        request = [{"review_token": "signed-token", "answers": {}}]
        with patch.object(api, "_require_allowed_user"), \
             patch.object(api.agent, "GROQ_API_KEY", "configured"), \
             patch.object(api.workflow, "unseal", return_value=context), \
             patch.object(api.software_plan_agent, "build_pack", return_value=sample_documents()), \
             patch.object(api, "_render_software_pack_pdf", side_effect=AssertionError("PDF should not be built")), \
             patch.object(api.agent, "build_docx", side_effect=lambda _data, path: Path(path).write_bytes(b"docx")), \
             patch.object(api.brd_agent, "build_docx", side_effect=lambda _data, path: Path(path).write_bytes(b"docx")), \
             patch.object(api.software_plan_agent, "build_docx", side_effect=lambda _data, path: Path(path).write_bytes(b"docx")):
            response = self.client.post("/api/software-plan/generate?format=docx", data={"reviews": json.dumps(request)})
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            docx_files = [name for name in archive.namelist() if name.endswith(".docx")]
            self.assertEqual(len(docx_files), 8)
            self.assertFalse(any(name.endswith(".pdf") for name in archive.namelist()))

    def test_all_files_option_includes_eight_pairs_and_run_summary(self):
        context = {"software_plan": True, "analysis": {"project_name": "Sample Project"}}
        request = [{"review_token": "signed-token", "answers": {}}]
        write_file = lambda _data, path, *_args: Path(path).write_bytes(b"file")
        with patch.object(api, "_require_allowed_user"), \
             patch.object(api.agent, "GROQ_API_KEY", "configured"), \
             patch.object(api.workflow, "unseal", return_value=context), \
             patch.object(api.software_plan_agent, "build_pack", return_value=sample_documents()), \
             patch.object(api, "_render_software_pack_pdf", side_effect=write_file), \
             patch.object(api.agent, "build_docx", side_effect=write_file), \
             patch.object(api.brd_agent, "build_docx", side_effect=write_file), \
             patch.object(api.software_plan_agent, "build_docx", side_effect=write_file):
            response = self.client.post("/api/software-plan/generate?format=zip", data={"reviews": json.dumps(request)})
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = archive.namelist()
            self.assertEqual(len([name for name in names if name.endswith(".pdf")]), 8)
            self.assertEqual(len([name for name in names if name.endswith(".docx")]), 8)
            summary = json.loads(archive.read("run-summary.json"))
            self.assertEqual(summary["status"], "pack_ready")
            self.assertEqual(summary["artifact_count"], 8)


if __name__ == "__main__":
    unittest.main()
