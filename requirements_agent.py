"""Dedicated Requirements Document workflow."""
import document_agent_common as common
TITLE = 'Requirements Document'
FOCUS = 'Business goals, complete scope, user roles, processes, functional needs, data, integrations and acceptance criteria.'
FOCUS += ' Capture the full stated scope faithfully. Do not add authentication providers, scale targets or new features; separate optional recommendations from source requirements.'
SECTIONS = ['project_overview', 'business_objectives', 'scope', 'users_and_roles', 'business_processes', 'functional_requirements', 'data_requirements', 'integrations', 'non_functional_requirements', 'acceptance_criteria', 'assumptions_and_dependencies']

def analyze(text, project_name=""):
    return common.analyze(text, project_name, TITLE, FOCUS)

def build_document(analysis, answers=None):
    return common.build_document(analysis, answers, 'requirements', TITLE, FOCUS, SECTIONS)
