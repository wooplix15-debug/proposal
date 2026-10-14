"""Dedicated Implementation Plan workflow."""
import document_agent_common as common
TITLE = 'Implementation Plan'
FOCUS = 'Technical solution, components, data model, integrations, tasks, security, parallel workstreams and deployment steps.'
SECTIONS = ['solution_overview', 'proposed_architecture', 'components', 'data_model', 'integration_design', 'development_tasks', 'security_and_permissions', 'parallel_workstreams', 'deployment_plan', 'acceptance_criteria', 'assumptions_and_dependencies']

def analyze(text, project_name=""):
    return common.analyze(text, project_name, TITLE, FOCUS)

def build_document(analysis, answers=None):
    return common.build_document(analysis, answers, 'implementation', TITLE, FOCUS, SECTIONS)
