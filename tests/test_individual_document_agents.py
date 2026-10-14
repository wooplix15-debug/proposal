import io
import json
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from docx import Document
from fastapi.testclient import TestClient
from api import index as api
import document_agent_common as common

BRIEF = 'Project: Booking Portal\nCustomers book appointments. Staff approve bookings. Managers view monthly reports.'

class IndividualDocumentTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)

    def test_every_agent_has_its_own_prompt_and_sections(self):
        for kind, builder in api.DOCUMENT_AGENTS.items():
            with self.subTest(kind=kind), patch.object(common, '_call') as call:
                call.return_value = {'summary': 'Booking project', 'questions': []}
                analysis = builder.analyze(BRIEF)
                self.assertIn(builder.TITLE, call.call_args.args[0])
                call.return_value = {'content': {key: 'Specific booking guidance' for key in builder.SECTIONS}}
                document = builder.build_document(analysis, {})
                self.assertEqual(document['kind'], kind)
                self.assertEqual(list(document['content']), builder.SECTIONS)
                self.assertEqual(document['project_name'], 'Booking Portal')
                self.assertEqual(document['client_name'], '')
                self.assertEqual(call.call_count, 2)

    def test_direct_section_response_is_accepted_without_losing_sections(self):
        with patch.object(common,'_call',return_value={key:['Project-specific guidance'] for key in api.ux_agent.SECTIONS}):
            doc=api.ux_agent.build_document({'source_text':BRIEF,'questions':[]},{})
            self.assertEqual(list(doc['content']),api.ux_agent.SECTIONS)

    def test_signed_review_is_bound_to_selected_agent(self):
        analysis = {'summary':'Booking','questions':[],'source_text':BRIEF}
        with patch.object(api, '_require_allowed_user'), patch.object(api.agent, 'GROQ_API_KEY','test-key'), \
             patch.object(api.ux_agent,'analyze',return_value=analysis), patch.object(api.workflow,'seal',return_value='token') as seal:
            res = self.client.post('/api/index.py/documents/ux/analyze', data={'text':BRIEF})
            self.assertEqual(res.status_code,200)
            self.assertEqual(seal.call_args.args[0]['document_kind'],'ux')
        with patch.object(api, '_require_allowed_user'), patch.object(api.agent,'GROQ_API_KEY','test-key'), \
             patch.object(api.workflow,'unseal',return_value={'document_kind':'ux','analysis':analysis}), \
             patch.object(api.qa_agent,'build_document') as build:
            res=self.client.post('/api/documents/qa/generate',data={'reviews':json.dumps([{'review_token':'token'}])})
            self.assertEqual(res.status_code,400)
            build.assert_not_called()

    def test_each_word_download_is_one_real_document_and_calls_only_selected_agent(self):
        for kind,builder in api.DOCUMENT_AGENTS.items():
            with self.subTest(kind=kind), patch.object(api,'_require_allowed_user'), \
                 patch.object(api.agent,'GROQ_API_KEY','test-key'), \
                 patch.object(api.workflow,'unseal',return_value={'document_kind':kind,'analysis':{}}), \
                 patch.object(builder,'build_document',return_value={'kind':kind,'title':builder.TITLE,'content':{'overview':'Booking workflow'}}) as build, \
                 patch.object(api.software_plan_agent,'build_pack',side_effect=AssertionError('Must not build a pack')):
                res=self.client.post(f'/api/documents/{kind}/generate?format=docx',data={'reviews':json.dumps([{'review_token':'token','answers':{}}])})
                self.assertEqual(res.status_code,200,res.text)
                self.assertIn('wordprocessingml',res.headers['content-type'])
                doc=Document(io.BytesIO(res.content))
                self.assertIn(builder.TITLE,' '.join(p.text for p in doc.paragraphs))
                build.assert_called_once()

    def test_pdf_and_zip_formats_are_single_document_exports(self):
        for fmt in ['pdf','zip']:
            with self.subTest(fmt=fmt), patch.object(api,'_require_allowed_user'), \
                 patch.object(api.agent,'GROQ_API_KEY','test-key'), \
                 patch.object(api.workflow,'unseal',return_value={'document_kind':'ux','analysis':{}}), \
                 patch.object(api.ux_agent,'build_document',return_value={'kind':'ux','title':'UI/UX Document','content':{'overview':'Booking'}}), \
                 patch.object(api,'_render_html_pdf',side_effect=lambda html,path,host:path.write_bytes(b'%PDF-1.4 test')):
                res=self.client.post(f'/api/documents/ux/generate?format={fmt}',data={'reviews':json.dumps([{'review_token':'token','answers':{}}])})
                self.assertEqual(res.status_code,200,res.text)
                if fmt=='pdf': self.assertTrue(res.content.startswith(b'%PDF'))
                else:
                    with zipfile.ZipFile(io.BytesIO(res.content)) as archive:
                        self.assertEqual(len(archive.namelist()),2)
                        self.assertEqual(sorted(Path(x).suffix for x in archive.namelist()),['.docx','.pdf'])

    def test_missing_sections_and_bad_answers_fail_with_feedback(self):
        with patch.object(common,'_call',return_value={'content':{'project_overview':'Incomplete'}}):
            with self.assertRaisesRegex(ValueError,'missing important sections'):
                api.requirements_agent.build_document({'source_text':BRIEF,'questions':[]},{})
        with self.assertRaisesRegex(ValueError,'Answer each question'):
            api.ux_agent.build_document({'source_text':BRIEF,'questions':[{'id':'q1','question':'Who approves?'}]}, {})

    def test_generation_requires_access(self):
        res=self.client.post('/api/documents/ux/generate',data={'reviews':'[]'})
        self.assertEqual(res.status_code,401)

if __name__=='__main__': unittest.main()
