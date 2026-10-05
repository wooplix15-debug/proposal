"""Keep every requested work area in the proposal, independently of time records."""
import re


def words(value):
    return set(re.findall(r'[a-z0-9]+', str(value).lower()))


def requested_products(requirement, records):
    text = re.sub(r'\s+', ' ', requirement.lower())
    training = re.search(r'(?is)(?:^|\n)\s*(?:\d+[.)]\s*)?Training\s*\n(.*?)(?=\n\s*\d+[.)]\s*[^\n]+\n|\Z)', requirement)
    products = []
    for row in records:
        name = row.get('product', '')
        if not name or name in products:
            continue
        aliases = [name.lower()]
        if name in {'Zoho CRM', 'Zoho Marketing Automation', 'Zoho Creator',
                    'Zoho Analytics', 'Zoho Backstage'}:
            aliases.append(name[5:].lower())
        if name == 'Zoho Backstage':
            aliases += ['backstages']
        if name == 'Zoho Campaigns':
            aliases += ['marketing automation,campaign', 'marketing automation, campaign']
            # Generic campaign activity alone does not request another app.
            aliases = [x for x in aliases if x != 'campaigns']
        if name == 'Automation & Reports':
            aliases += ['automation and reports']
            if training:
                non_training = requirement.replace(training.group(0), '').lower()
                if not any(a in non_training for a in aliases):
                    continue
        if any(re.search(r'(?<![a-z0-9])' + re.escape(a) + r'(?![a-z0-9])', text) for a in aliases):
            products.append(name)
    # Ownership of a suite licence is not a separate delivery phase.
    if len([p for p in products if p.startswith('Zoho ') and p != 'Zoho One']) > 1:
        products = [p for p in products if p != 'Zoho One']
    return products


def scope_inventory(raw, products, requirement=None):
    def source_text(value):
        return re.sub(r'\s+', ' ', str(value).lower().translate(str.maketrans('‑–—', '---'))).strip()
    original = source_text(requirement) if requirement is not None else None
    sections = []
    for section in raw if isinstance(raw, list) else []:
        if not isinstance(section, dict) or not str(section.get('product', '')).strip():
            continue
        requirements = section.get('requirements', [])
        name = str(section['product']).strip()
        name = next((p for p in products if product_matches(p, name)), name)
        if name == 'Zoho One' and name not in products:
            continue
        extracted = [str(x).strip() for x in requirements
                                          if isinstance(x, str) and x.strip()
                                          and (original is None or source_text(x) in original)]
        existing = next((s for s in sections if s['product'] == name), None)
        if existing:
            existing['requirements'] = list(dict.fromkeys(existing['requirements'] + extracted))
        else:
            sections.append({'product': name, 'requirements': extracted})
    for product in products:
        if not any(product_matches(product, s['product']) for s in sections):
            sections.append({'product': product, 'requirements': []})
    return sections


def product_matches(expected, actual):
    def canonical(value):
        return re.sub(r'[^a-z0-9]', '', value.lower().replace('zoho ', '').replace('backstages', 'backstage').replace('campaigns', 'campaign'))
    expected, actual = canonical(expected), canonical(actual)
    return bool(expected) and (expected == actual or expected in actual)


def preserve_source_bullets(sections, requirement, products):
    """Retain every bullet under recognized product/training headings."""
    current = []
    for line in requirement.splitlines():
        value = line.strip()
        bullet = re.match(r'^[-•*]\s*(.+)', value)
        if bullet:
            for name in current:
                section = next((s for s in sections if product_matches(name, s['product'])), None)
                if section is None:
                    section = {'product': name, 'requirements': []}
                    sections.append(section)
                text = bullet.group(1).strip()
                if text not in section['requirements']:
                    section['requirements'].append(text)
            continue
        heading = re.sub(r'^\d+[.)]\s*', '', value)
        if re.match(r'^Training\s*$', heading, re.I):
            current = ['Training']
        elif re.match(r'^Zoho\b', heading, re.I) and len(heading) < 120:
            current = [name for name in products if product_matches(name, heading)]
        elif re.match(r'^(?:Cost Proposal|Main Objective|Our Requirement)', heading, re.I):
            current = []
    return sections


def coverage_gaps(proposal, sections):
    """Check product coverage and concrete requirement words before exporting."""
    gaps = []
    scope = proposal.get('scope') or []
    stop = {'zoho', 'and', 'or', 'the', 'a', 'an', 'for', 'of', 'with', 'to', 'in',
            'we', 'need', 'required', 'full', 'complete', 'implementation', 'setup',
            'configuration', 'configure', 'management', 'create', 'set', 'up'}
    for section in sections:
        if any(x in section['product'].lower() for x in ('licen', 'commercial', 'cost', 'pricing')):
            import json
            content = words(json.dumps(proposal.get('commercials') or {}, ensure_ascii=False))
            for item in section.get('requirements', []):
                meaningful = words(item) - stop
                if meaningful and len(meaningful & content) / len(meaningful) < 0.5:
                    gaps.append('Cost breakdown: ' + item)
            continue
        matching = [s for s in scope if isinstance(s, dict)
                    and product_matches(section['product'], str(s.get('product', '')))]
        if not matching:
            gaps.append('Missing work area: ' + section['product'])
            continue
        import json
        content = words(json.dumps(matching, ensure_ascii=False))
        for item in section.get('requirements', []):
            meaningful = words(item) - stop
            if meaningful and len(meaningful & content) / len(meaningful) < 0.5:
                gaps.append(section['product'] + ': ' + item)
    return gaps


def unsupported_scope(proposal, requirement):
    """Reject common scope expansions unless the customer explicitly supplied them."""
    import json
    source, _, rest = requirement.partition('\n\nUSER CLARIFICATIONS\n')
    if rest:
        raw = rest.split('\n\nREQUIRED SCOPE CHECKLIST', 1)[0]
        try:
            source += ' ' + ' '.join(x['answer'] for x in json.loads(raw)
                                     if x.get('answer') != 'Leave open for discovery')
        except (ValueError, KeyError, TypeError):
            pass
    content = json.dumps(proposal.get('scope') or [], ensure_ascii=False).lower()
    original = source.lower()
    topics = [('ticket', 'ticketing'), ('speaker', 'speaker profiles'),
              ('registration', 'event registration'), ('agenda', 'event agenda'),
              ('attendee', 'attendee management'), ('web form', 'web-form lead capture'),
              ('custom module', 'custom CRM modules'),
              ('custom backstage application', 'custom Backstage application development'),
              ('survey', 'surveys'), ('revenue', 'revenue reports'),
              ('scoring', 'lead scoring'), ('script', 'custom scripts'),
              ('api', 'custom API development'), ('billing rate', 'billing rates'),
              ('onsite', 'onsite delivery'), ('native whatsapp', 'native WhatsApp provider'),
              ('native sms', 'native SMS provider')]
    return ['Remove unconfirmed scope: ' + label for key, label in topics
            if re.search(r'\b' + re.escape(key), content) and key not in original]


def confirmed_scope(sections, requirement):
    """Export source activities rather than model-inferred implementation commitments."""
    import json
    groups = []
    if re.search(r'(?i)Zoho Marketing Automation\s*[,/&]+\s*(?:Zoho\s+)?campaign', requirement):
        groups.append((['Zoho Marketing Automation', 'Zoho Campaigns'], 'Zoho Marketing Automation & Zoho Campaigns'))
    if re.search(r'(?i)Zoho Creator\s*[,/&]+\s*(?:Zoho\s+)?backstage', requirement):
        groups.append((['Zoho Creator', 'Zoho Backstage'], 'Zoho Creator & Zoho Backstage'))
    modules = []
    defaults = {
        'Integration / Customization': 'Define and implement the agreed cross-application integrations and customizations.',
        'WhatsApp & SMS Integration': 'Configure WhatsApp and SMS integrations using the approved providers, templates and message volumes.',
        'Data Migration / Cleansing': 'Clean and migrate existing CRM data using the agreed data volume and cleansing rules.',
        'Post-Implementation Support': 'Provide post-implementation support under the agreed scope, duration and response terms.',
    }
    for section in sections:
        name = section['product']
        if any(x in name.lower() for x in ('licen', 'commercial', 'cost', 'pricing')):
            continue
        group = next((title for names, title in groups if name in names), name)
        module = next((x for x in modules if x['product'] == group), None)
        if module is None:
            module = {'product': group, 'areas': [{'area': 'Practical training topics' if name == 'Training' else 'Included work', 'tasks': []}]}
            modules.append(module)
        tasks = section.get('requirements') or []
        if not tasks or tasks == [name]:
            tasks = [defaults.get(name, 'Confirm the detailed scope and configure the requested product.')]
        module['areas'][0]['tasks'] = list(dict.fromkeys(module['areas'][0]['tasks'] + tasks))
    _, _, rest = requirement.partition('\n\nUSER CLARIFICATIONS\n')
    if rest:
        try:
            clarifications = json.loads(rest.split('\n\nREQUIRED SCOPE CHECKLIST', 1)[0])
        except (ValueError, TypeError):
            clarifications = []
        for row in clarifications:
            answer, question = row.get('answer', ''), row.get('question', '').lower()
            if answer == 'Leave open for discovery' or 'working-day estimate' in question or 'scheduled' in question:
                continue
            target = None
            if 'provider' in question and any(x in question for x in ('whatsapp', 'sms')):
                target = ('WhatsApp & SMS Integration', 'Messaging provider: ')
            elif 'migrat' in question or 'cleans' in question:
                target = ('Data Migration / Cleansing', 'Data scope: ')
            elif 'training' in question:
                target = ('Training', 'Training delivery: ')
            elif 'support' in question:
                target = ('Post-Implementation Support', 'Support terms: ')
            else:
                for module in modules:
                    names = module['product'].lower().replace('zoho ', '').split(' & ')
                    if any(n in question for n in names):
                        if any(x in question for x in ('user', 'attendee', 'participant', 'license', 'count')):
                            label = 'User / attendee volume: '
                        elif any(x in question for x in ('form', 'workflow', 'application', 'module')):
                            label = 'Custom forms & workflows: '
                        else:
                            label = 'Confirmed scope: '
                        target = (module['product'], label)
                        break
            if target:
                module = next((x for x in modules if product_matches(target[0], x['product'])), None)
                if module:
                    existing_area = next((a for a in module.get('areas', []) if a.get('area') == 'Confirmed details'), None)
                    if existing_area:
                        existing_area['tasks'].append(target[1] + answer)
                    else:
                        module.setdefault('areas', []).append({'area': 'Confirmed details', 'tasks': [target[1] + answer]})
    return modules


def finalize_client_content(proposal, analysis, answers, requirement):
    """Keep the summary complete and unresolved items concise, without a questionnaire."""
    names = [s['product'] for s in analysis.get('requirement_sections', [])
             if s['product'].startswith('Zoho ') and s['product'] != 'Zoho One']
    if names:
        purpose = "for the client's event management business" if 'event management' in requirement.lower() else 'for the requirements defined below'
        proposal['project_introduction'] = 'Wooplix Technologies Private Limited will implement and customize ' + ', '.join(dict.fromkeys(names)) + ' ' + purpose + '.'
        services = []
        for key, label in [('integration', 'integrations'), ('cleansing', 'data cleansing'), ('training', 'training'), ('support', 'support')]:
            if any(key in s['product'].lower() for s in analysis.get('requirement_sections', [])):
                services.append(label)
        if services:
            proposal['project_introduction'] += ' The scope also includes ' + ', '.join(services) + '.'
    proposal['deliverables'] = [x for x in proposal.get('deliverables', [])
                                if not unsupported_scope({'scope': [{'text': x}]}, requirement)]
    pending = []
    for question in analysis.get('questions', []):
        answer = str(answers.get(question['id'], ''))
        if answer not in ('Leave open for discovery', 'Confirm during discovery', 'Enter an estimate using Other', ''):
            continue
        text = question['question'].lower()
        if any(x in text for x in ('working-day estimate', 'schedule', 'overlap')) and not (proposal.get('timeline') or {}).get('overall', '').startswith('To be confirmed'):
            continue
        if 'working-day estimate' in text:
            product = next((n for n in names if n.lower() in text), 'this phase')
            item = product + ' implementation duration.'
        elif 'schedule' in text or 'overlap' in text:
            item = 'Project sequencing and dependencies.'
        elif 'provider' in text:
            item = 'WhatsApp and SMS providers and integration requirements.'
        elif 'migrat' in text or ('crm' in text and 'volume' in text):
            item = 'CRM data volume, quality and migration rules.'
        elif 'message' in text and 'volume' in text:
            item = 'Expected messaging volume and service limits.'
        elif 'creator' in text:
            item = 'Creator application, form and workflow quantities.'
        elif 'backstage' in text:
            item = 'Detailed Backstage event workflows.'
        elif 'training' in text:
            item = 'Training format and participants.'
        elif 'support' in text:
            item = 'Support response terms and coverage.'
        elif 'user' in text or 'licen' in text:
            item = 'User counts and licence requirements.'
        else:
            continue  # Do not repeat the raw questionnaire in the client proposal.
        if item not in pending:
            pending.append(item)
    proposal['open_points'] = pending
    return proposal
