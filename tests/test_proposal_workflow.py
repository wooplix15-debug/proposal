import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import project_records
import proposal_workflow as workflow
import wooplix_agent as agent
from proposal_scope import (requested_products, coverage_gaps, scope_inventory,
                            unsupported_scope, preserve_source_bullets,
                            finalize_client_content)

REQ = (Path(__file__).parent / 'fixtures/zoho_event_requirement.txt').read_text()


class ProposalWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.records, _ = project_records.load_project_records()

    def test_all_requested_apps_and_aliases_are_detected(self):
        products = requested_products(REQ, self.records)
        for name in ['Zoho CRM', 'Zoho Marketing Automation', 'Zoho Campaigns',
                     'Zoho Projects', 'Zoho Creator', 'Zoho Backstage', 'Zoho Analytics']:
            self.assertIn(name, products)
        self.assertNotIn('Zoho One', products)
        self.assertNotIn('Automation & Reports', products)

    def test_generic_words_do_not_request_unrelated_products(self):
        products = requested_products('Zoho CRM requires approval sign-off and forms for new leads.', self.records)
        self.assertEqual(products, ['Zoho CRM'])

    def test_missing_scope_and_activity_are_detected(self):
        sections = [{'product': 'Zoho Backstage', 'requirements': ['Event registration and attendee workflows']},
                    {'product': 'Zoho CRM', 'requirements': ['Duplicate company management']}]
        gaps = coverage_gaps({'scope': [{'product': 'Zoho CRM', 'areas': [{'area': 'Leads', 'tasks': ['Assign leads']}]}]}, sections)
        self.assertTrue(any('Backstage' in x for x in gaps))
        self.assertTrue(any('Duplicate' in x for x in gaps))

    def test_source_checklist_excludes_reference_only_features(self):
        sections = scope_inventory([{'product': 'Zoho CRM', 'requirements':
                                     ['Lead automatic creation and BDM assignment', 'Lead scoring']}], ['Zoho CRM'], REQ)
        self.assertEqual(sections[0]['requirements'], ['Lead automatic creation and BDM assignment'])

    def test_campaign_alias_does_not_duplicate_phase(self):
        sections = scope_inventory([{'product': 'Zoho Campaign', 'requirements': ['Email Campaign']},
                                    {'product': 'Zoho Campaigns', 'requirements': ['Campaign Tracking']}], ['Zoho Campaigns'], REQ)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]['product'], 'Zoho Campaigns')

    def test_original_bullets_survive_incomplete_model_extraction(self):
        sections = preserve_source_bullets([], REQ, requested_products(REQ, self.records))
        crm = next(s for s in sections if s['product'] == 'Zoho CRM')
        self.assertIn('Event-wise and batch-wise data management', crm['requirements'])
        analytics = next(s for s in sections if s['product'] == 'Zoho Analytics')
        self.assertEqual(len(analytics['requirements']), 10)
        training = next(s for s in sections if s['product'] == 'Training')
        self.assertIn('Automation and Reports', training['requirements'])

    def test_unanswered_question_does_not_approve_ticketing(self):
        requirement = REQ + '\n\nUSER CLARIFICATIONS\n' + json.dumps([
            {'question': 'Do you want ticketing?', 'answer': 'Leave open for discovery'}])
        proposal = {'scope': [{'product': 'Zoho Backstage', 'areas': [{'tasks': ['Configure ticketing.']}]}]}
        self.assertTrue(unsupported_scope(proposal, requirement))
        requirement = REQ + '\n\nUSER CLARIFICATIONS\n' + json.dumps([
            {'question': 'Do you want ticketing?', 'answer': 'Configure ticketing'}])
        self.assertFalse(unsupported_scope(proposal, requirement))

    def test_user_supplied_missing_estimate_is_applied(self):
        analysis = {'requirement_sections': [{'product': 'Zoho Analytics'}], 'duration_estimates': [],
                    'questions': [{'id': 'q1', 'question': 'What working-day estimate should we use for Zoho Analytics?'}]}
        proposal = workflow.apply_actual_delivery_timeline({}, analysis, {'q1': '5 working days'})
        self.assertIn('5 working days', proposal['timeline']['overall'])
        self.assertIn('1 week', proposal['timeline']['overall'])

    def test_analysis_keeps_all_products_if_model_returns_only_crm(self):
        result = {'summary': 'CRM setup', 'questions': [], 'duration_uses': [{'record_id': 'product:2'}],
                  'requirement_sections': [{'product': 'Zoho CRM', 'requirements': ['Lead assignment']}], 'comparisons': []}
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)))])
        with patch('groq.Groq') as client:
            client.return_value.chat.completions.create.return_value = response
            analysis = workflow.analyze(REQ, self.records, '', self.records)
            payload = json.loads(client.return_value.chat.completions.create.call_args.kwargs['messages'][1]['content'])
        self.assertIn('Zoho Analytics', [x['product'] for x in analysis['requirement_sections']])
        self.assertIn('Zoho Backstage', [x['product'] for x in analysis['requirement_sections']])
        self.assertIn('Zoho Backstage', [x['product'] for x in analysis['duration_estimates']])
        self.assertNotIn('Zoho One', [x['product'] for x in analysis['duration_estimates']])
        self.assertIn('Zoho Analytics', [x['product'] for x in payload['matching_product_scope_records']])

    def test_partial_timeline_is_not_presented_as_a_total(self):
        crm = next(x for x in self.records if x.get('product') == 'Zoho CRM')
        analysis = {'requirement_sections': [{'product': 'Zoho CRM'}, {'product': 'Unspecified custom application'}],
                    'duration_estimates': [crm], 'questions': []}
        proposal = {'timeline': {'overall': '6-8 days', 'phases': [{'phase': 'Zoho One', 'duration': '14-18 days'}]}}
        result = workflow.apply_actual_delivery_timeline(proposal, analysis, {})['timeline']
        self.assertEqual(result['overall'], '')
        self.assertEqual([x['phase'] for x in result['phases']], ['Zoho CRM', 'Unspecified custom application'])
        self.assertEqual(result['phases'][1]['duration'], '')

    def test_all_requested_cost_categories_are_preserved_without_invented_prices(self):
        proposal = {'commercials': {'items': [{'item': 'Zoho CRM', 'amount': 'Approved price'}]}}
        analysis = {'commercial_categories': ['Zoho CRM', 'Training', 'Post-Implementation Support']}
        result = workflow.apply_requested_cost_breakdown(proposal, analysis)['commercials']['items']
        self.assertEqual([x['item'] for x in result], analysis['commercial_categories'])
        self.assertEqual(result[0]['amount'], 'Approved price')
        self.assertEqual(result[1]['amount'], '')

    def test_work_areas_are_scheduled_in_parallel(self):
        records = [x for x in self.records if x.get('product') in ('Zoho CRM', 'Zoho Creator')]
        analysis = {'requirement_sections': [{'product': x['product']} for x in records], 'duration_estimates': records,
                    'questions': []}
        result = workflow.apply_actual_delivery_timeline({}, analysis, {})
        self.assertIn('parallel', result['timeline']['overall'])
        self.assertNotIn('assuming sequential', result['timeline']['overall'])
        self.assertIn('weeks', result['timeline']['overall'])

    def test_no_question_asks_whether_work_areas_run_in_parallel(self):
        self.assertTrue(workflow._asks_about_work_area_sequence(
            'How would you like the work to be scheduled?'))
        self.assertTrue(workflow._asks_about_work_area_sequence(
            'Should these implementation phases happen one after another or at the same time?'))
        self.assertFalse(workflow._asks_about_work_area_sequence(
            'When should the customer site visit be scheduled?'))

    def test_proposal_clears_unknown_placeholders(self):
        proposal = {'timeline': {'overall': 'To be confirmed',
                                'phases': [{'phase': 'Zoho CRM', 'duration': 'To be confirmed'}]},
                    'commercials': {'items': [{'item': 'Training', 'amount': 'To be quoted'}]}}
        result = finalize_client_content(proposal, {'requirement_sections': []}, {}, 'CRM requirement')
        self.assertEqual(result['timeline']['overall'], '')
        self.assertEqual(result['timeline']['phases'][0]['duration'], '')
        self.assertEqual(result['commercials']['items'][0]['amount'], '')

    def test_renderer_does_not_inject_filler_or_false_terms(self):
        proposal = {'scope': [{'product': 'Zoho CRM', 'areas': [{'area': 'Leads', 'tasks': ['Assign leads to BDMs.']}]}],
                    'project_introduction': 'Implement Zoho CRM.', 'prerequisites': [], 'deliverables': []}
        html = agent.build_html(copy.deepcopy(proposal))
        for unwanted in ['Business Rules &amp; Logic', 'Target Application', 'Scheduled Sync',
                         'Implementation Notes', 'Valid for 30 days', 'Actual Delivery Reference']:
            self.assertNotIn(unwanted, html)
        self.assertIn('Assign leads to BDMs.', html)
        self.assertIn('cover-spec', html)
        self.assertIn('Configuration Matrix', html)

    def test_export_preserves_all_requested_scope_even_if_model_omits_products(self):
        sections = [{'product': 'Zoho CRM', 'requirements': []}, {'product': 'Zoho Backstage', 'requirements': []}]
        bad = {'scope': [{'product': 'Zoho CRM', 'areas': []}]}
        good = {'scope': [{'product': 'Zoho CRM', 'areas': []}, {'product': 'Zoho Backstage', 'areas': []}]}
        def response(value):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value)))])
        with patch('groq.Groq') as client:
            client.return_value.chat.completions.create.side_effect = [response(bad), response(good)]
            draft = agent.draft_proposal(REQ, '', sections)
            self.assertEqual(client.return_value.chat.completions.create.call_count, 1)
        self.assertEqual(coverage_gaps(draft, sections), [])

    def test_benchmarks_fill_missing_times_without_overwriting_actuals(self):
        crm = next(x for x in self.records if x.get('product') == 'Zoho CRM')
        analysis = {'requirement_sections': [{'product': 'Zoho CRM'}, {'product': 'Zoho Analytics'},
                    {'product': 'Integration / Customization'}, {'product': 'WhatsApp & SMS Integration'},
                    {'product': 'Post-Implementation Support'}], 'duration_estimates': [crm], 'questions': []}
        timeline = workflow.apply_actual_delivery_timeline({}, analysis, {})['timeline']
        phases = {x['phase']: x['duration'] for x in timeline['phases']}
        self.assertEqual(phases['Zoho CRM'].replace('–', '-'), '6-8 working days')
        self.assertEqual(phases['Zoho Analytics'], '22 working days')
        self.assertEqual(phases['WhatsApp & SMS Integration'], 'Included in integration work area')
        self.assertIn('22 working days', timeline['overall'])
        self.assertIn('weeks', timeline['overall'])
        self.assertIn('parallel', timeline['overall'])
        self.assertTrue(timeline['benchmark_sources'])


if __name__ == '__main__':
    unittest.main()
