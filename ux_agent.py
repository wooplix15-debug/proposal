"""Dedicated UI/UX Document workflow."""
import document_agent_common as common
TITLE = 'UI/UX Document'
FOCUS = 'User experience, journeys, navigation, page layouts, forms, states, accessibility and responsive behavior.'
FOCUS += ' Include only screens and actions required by the brief. Proposed visual defaults are fine; do not add carousels, customer editing, payments or unrelated scope.'
SECTIONS = ['design_overview', 'users_and_goals', 'navigation_structure', 'user_journeys', 'screen_specifications', 'forms_and_validation', 'interaction_states', 'responsive_design', 'accessibility', 'design_acceptance_criteria', 'assumptions']

def analyze(text, project_name=""):
    return common.analyze(text, project_name, TITLE, FOCUS)

def build_document(analysis, answers=None):
    return common.build_document(analysis, answers, 'ux', TITLE, FOCUS, SECTIONS)
