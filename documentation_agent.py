"""Dedicated User Documentation and Training workflow."""
import document_agent_common as common
TITLE = 'User Documentation and Training'
FOCUS = 'Step-by-step guidance for users and administrators, operational tasks, training, troubleshooting and maintenance.'
FOCUS += ' Write end-user and administrator steps, not developer installation commands. If screens are not implemented, label navigation and controls proposed. Do not choose a technology stack.'
SECTIONS = ['system_overview', 'roles_and_access', 'getting_started', 'user_workflows', 'administrator_guide', 'reports_and_dashboards', 'training_plan', 'troubleshooting', 'maintenance_and_support', 'assumptions']

def analyze(text, project_name=""):
    return common.analyze(text, project_name, TITLE, FOCUS)

def build_document(analysis, answers=None):
    document = common.build_document(analysis, answers, 'documentation', TITLE, FOCUS, SECTIONS)
    document["content"].setdefault("assumptions", [])
    value = document["content"]["assumptions"]
    note = "Navigation and controls described in this draft are proposed unless supplied as existing features in the requirement."
    document["content"]["assumptions"] = [note] + (value if isinstance(value, list) else [value])
    return document
