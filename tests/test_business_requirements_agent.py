from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from docx import Document

import business_requirements_agent as brd
import wooplix_agent as brand
from brd_structure import source_tables, validated_model_tables, proposed_design_tables
from project_records import load_project_records
from proposal_scope import preserve_source_bullets, requested_products


SAMPLE = (Path(__file__).parent / "fixtures/zoho_event_requirement.txt").read_text()


class BusinessRequirementsAgentTests(unittest.TestCase):
    def setUp(self):
        records, _ = load_project_records()
        products = requested_products(SAMPLE, records)
        self.sections = preserve_source_bullets([], SAMPLE, products)
        self.analysis = {
            "summary": "The business wants a connected system for its event work.",
            "requirement_sections": self.sections,
            "questions": [{"id": "q1", "question": "How should work be scheduled?"}],
            "commercial_categories": ["Zoho CRM", "Training"],
        }
        self.answers = {"q1": "Leave open for discovery"}

    def build(self, requirement=SAMPLE, analysis=None):
        with patch.object(brd, "_draft_brief",
                          side_effect=lambda sections, answers, fallback: {
                              "overview": fallback, "process_views": []}):
            return brd.build_document(requirement, analysis or self.analysis,
                                      self.answers, "Sample requirement.txt")

    def test_shared_product_heading_does_not_duplicate_the_same_checklist(self):
        doc = self.build()
        areas = [row["name"] for row in doc["requirements_by_area"]]
        self.assertIn("Zoho Marketing Automation & Zoho Campaigns", areas)
        self.assertIn("Zoho Creator & Zoho Backstage", areas)
        self.assertEqual(len(doc["requirements"]), 47)
        self.assertEqual(len({row["id"] for row in doc["requirements"]}), 47)

    def test_different_product_checklists_remain_separate(self):
        requirement = "Zoho Creator and Zoho Backstage are both requested."
        analysis = dict(self.analysis, requirement_sections=[
            {"product": "Zoho Creator", "requirements": ["Build custom forms"]},
            {"product": "Zoho Backstage", "requirements": ["Manage event pages"]},
        ])
        doc = self.build(requirement, analysis)
        names = [row["name"] for row in doc["requirements_by_area"]]
        self.assertIn("Zoho Creator", names)
        self.assertIn("Zoho Backstage", names)
        self.assertNotIn("Zoho Creator & Zoho Backstage", names)

    def test_word_export_contains_requirements_and_skips_empty_sections(self):
        doc = self.build()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "brd.docx"
            brd.build_docx(doc, str(path))
            exported = Document(path)
        text = "\n".join(p.text for p in exported.paragraphs)
        text += "\n" + "\n".join(cell.text for table in exported.tables
                                   for row in table.rows for cell in row.cells)
        self.assertIn("Zoho Creator & Zoho Backstage", text)
        self.assertIn("Event-wise project setup", text)
        self.assertEqual(text.count("CRM Integration"), 1)
        self.assertNotIn("No specific requirements were stated", text)
        self.assertNotIn("Training requirements were not specified", text)

    def test_html_keeps_open_question_without_mixing_in_proposal_prices(self):
        doc = self.build()
        html = brd.build_html(doc)
        self.assertNotIn("Questions and Decisions", html)
        self.assertNotIn("To be confirmed", html)
        self.assertNotIn("Commercial Items Requested", html)

    def test_schedule_question_is_ignored_and_does_not_add_placeholder(self):
        analysis = dict(self.analysis, questions=[{
            "id": "schedule", "question": "How would you like the work to be scheduled?"}])
        with patch.object(brd, "_draft_brief", return_value={"overview": "Summary", "process_views": []}):
            doc = brd.build_document(SAMPLE, analysis, {})
        html = brd.build_html(doc)
        self.assertEqual(doc["open_decisions"], [])
        self.assertNotIn("To be confirmed", html)
        self.assertNotIn("Questions and Decisions", html)

    def test_proposed_tables_are_labelled_and_unrelated_scope_is_filtered(self):
        requirements = [{"id": "BR-001", "area": "Zoho CRM",
                         "requirement": "Lead automatic creation and BDM assignment"}]
        raw = [{"section": "Fields and Master Data", "title": "Lead data",
                "columns": ["Field", "Purpose"],
                "rows": [["Lead source", "Record origin"],
                         ["Event budget", "Track event spending"]],
                "requirement_ids": ["BR-001"]}]
        tables = proposed_design_tables(raw, requirements, "Cost proposal requested")
        self.assertEqual(tables[0]["rows"], [["Lead source", "Record origin"]])
        self.assertEqual(tables[0]["origin"], "proposed")

    def test_source_configuration_fallback_keeps_an_omitted_area_in_the_brd(self):
        rows = [{"id": "BR-021", "area": "Zoho Forms",
                 "requirement": "All website form submissions should create or update CRM leads."}]
        table = brd._source_configuration_table("Zoho Forms", rows)
        self.assertEqual(table["origin"], "source")
        self.assertEqual(table["requirement_ids"], ["BR-021"])
        self.assertIn("create or update CRM leads", table["rows"][0][0])

    def test_acceptance_criteria_are_read_from_flattened_document_table(self):
        text = "Project heading\n9.2 Document Sign-Off\n# | Acceptance Criterion | Verified By | Status\n"
        text += "1 | Lead assignment works as agreed | Sales Manager | Pending\n"
        text += "Name & Designation | Organization | Signature | Date\n"
        self.assertEqual(brd._acceptance_table_rows(text), ["Lead assignment works as agreed"])
        doc = self.build(SAMPLE + "\n" + text)
        self.assertEqual(doc["acceptance_criteria"], ["Lead assignment works as agreed"])

    def test_word_tables_stay_with_their_heading(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.docx"
            source = Document()
            source.add_heading("Lead Fields", 1)
            table = source.add_table(rows=2, cols=3)
            for cell, value in zip(table.rows[0].cells, ["Field Name", "Data Type", "Mandatory"]):
                cell.text = value
            for cell, value in zip(table.rows[1].cells, ["Phone", "Text", "Yes"]):
                cell.text = value
            source.add_heading("Workflow", 1)
            source.add_paragraph("Create a follow-up task after assignment.")
            source.save(path)
            text = brand.extract_requirement(str(path))
        self.assertLess(text.index("Lead Fields"), text.index("Phone | Text | Yes"))
        self.assertLess(text.index("Phone | Text | Yes"), text.index("Workflow"))
        tables = source_tables(text)
        self.assertEqual(tables[0]["section"], "Fields and Master Data")
        self.assertEqual(tables[0]["title"], "Lead Fields")

    def test_model_cannot_invent_field_types_or_thresholds(self):
        table = {"section": "Fields and Master Data", "title": "Lead",
                 "columns": ["Field", "Type"],
                 "rows": [["Phone", "Text"], ["Phone", "Integer"],
                          ["Phone", "To be confirmed"]],
                 "evidence": ["Phone | Text"] * 3}
        validated = validated_model_tables([table], "Phone | Text", [])
        self.assertEqual(validated[0]["rows"], [["Phone", "Text"], ["Phone", ""]])

    def test_model_cannot_mix_values_from_unrelated_fields(self):
        table = {"section": "Fields and Master Data", "columns": ["Field", "Type"],
                 "rows": [["Phone", "Currency"]], "evidence": ["Phone | Text"]}
        self.assertEqual(validated_model_tables([table], "Phone | Text\nBudget | Currency", []), [])

    def test_model_gets_question_text_and_full_source(self):
        with patch.object(brd, "_draft_brief", return_value={"overview": "Summary", "process_views": []}) as draft:
            brd.build_document(SAMPLE, self.analysis, self.answers)
        payload = draft.call_args.args[1]
        self.assertEqual(payload["source_requirement"], SAMPLE)
        self.assertEqual(payload["decisions"][0]["question"], "How should work be scheduled?")

    def test_source_tables_render_in_both_formats(self):
        source = SAMPLE + "\nLead fields\nField Name | Data Type | Mandatory\nPhone | Text | Yes\n\n"
        doc = self.build(source)
        html = brd.build_html(doc)
        self.assertIn("<td>Phone</td><td>Text</td><td>Yes</td>", html)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "brd.docx"
            brd.build_docx(doc, str(path))
            exported = Document(path)
        matching = [table for table in exported.tables if table.cell(0, 0).text == "Field Name"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].cell(1, 0).text, "Phone")

    def test_future_scope_is_not_committed(self):
        analysis = dict(self.analysis, requirement_sections=[
            {"product": "Zoho CRM", "requirements": ["Lead assignment", "Vendor management is not required in this phase"]}])
        doc = self.build("Lead assignment. Vendor management is not required in this phase", analysis)
        self.assertEqual([r["requirement"] for r in doc["requirements"]], ["Lead assignment"])
        self.assertTrue(any(t["section"] == "Scope Boundaries" for t in doc["specification_tables"]))


if __name__ == "__main__":
    unittest.main()
