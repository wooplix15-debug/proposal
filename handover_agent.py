"""Dedicated Project Handover Document workflow."""
import document_agent_common as common
TITLE = 'Project Handover Document'
FOCUS = 'Deliverables, ownership, configuration inventory, access transfer, deployment checklist, support, outstanding items and sign-off fields.'
FOCUS += ' Prepare a handover checklist and blank factual inventory fields for an unbuilt project. Do not choose infrastructure or promise support durations. Never claim artifacts have been delivered.'
SECTIONS = ['project_overview', 'deliverables', 'system_and_configuration_inventory', 'ownership_and_responsibilities', 'access_transfer_checklist', 'deployment_checklist', 'training_and_knowledge_transfer', 'support_and_maintenance', 'outstanding_items', 'handover_acceptance', 'sign_off']

def analyze(text, project_name=""):
    return common.analyze(text, project_name, TITLE, FOCUS)

def build_document(analysis, answers=None):
    document = common.build_document(analysis, answers, 'handover', TITLE, FOCUS, SECTIONS)
    # An inventory records actual configuration; generic suggestions are not assets.
    source = analysis["source_text"].casefold()
    import re
    technologies = ["Zoho CRM", "Zoho Creator", "Zoho Analytics", "Zoho Projects", "Zoho One",
                    "Zoho Campaigns", "Zoho Marketing Automation", "Supabase", "PostgreSQL",
                    "React", "FastAPI", "AWS", "Azure", "Vercel", "Node.js", "SendGrid"]
    supplied = [name for name in technologies if re.search(r"\b" + re.escape(name.casefold()) + r"\b", source)]
    document["content"]["system_and_configuration_inventory"] = [
        {"item": "Systems named in the requirement", "details": ", ".join(supplied)},
        {"item": "Live application URL", "details": ""},
        {"item": "Source repository", "details": ""},
        {"item": "Hosting configuration", "details": ""},
        {"item": "Database configuration", "details": ""},
        {"item": "Email configuration", "details": ""},
        {"item": "Backup and monitoring configuration", "details": ""},
    ]
    return document
