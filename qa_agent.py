"""Dedicated QA and UAT Plan workflow."""
import document_agent_common as common
TITLE = 'QA and UAT Plan'
FOCUS = 'Test scope, functional and negative scenarios, realistic steps, expected outcomes, integration testing and user acceptance.'
FOCUS += ' Derive test steps from actual business rules and role permissions. Do not choose email, identity or hosting vendors. No tests have been executed.'
SECTIONS = ['test_overview', 'test_scope', 'test_environment_and_data', 'functional_test_cases', 'negative_and_edge_cases', 'integration_tests', 'security_and_access_tests', 'uat_scenarios', 'defect_management', 'release_criteria', 'assumptions']

def analyze(text, project_name=""):
    return common.analyze(text, project_name, TITLE, FOCUS)

def build_document(analysis, answers=None):
    return common.build_document(analysis, answers, 'qa', TITLE, FOCUS, SECTIONS)
