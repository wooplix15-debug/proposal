# Wooplix Proposal Agent

Wooplix Technologies Private Limited — Together, We Achieve More

A self-contained system that turns a customer requirement into a review-ready
Wooplix proposal in your house style (scope-of-work), as branded **DOCX + PDF**,
learning from your past deals in Zoho CRM when available.

## What's in this folder

| File | Purpose |
|---|---|
| `wooplix_agent.py` | The system. Run it from a terminal — no Colab needed. |
| `html_to_pdf.php` | Converts the house-style HTML to PDF using dompdf. Called automatically. |
| `composer.json` / `vendor/` | dompdf (PHP) — created by `composer install`. |
| `requirements.txt` | Python packages to install. |
| `.env.example` | Template for your secrets. Copy to `.env` and fill in. |
| `wooplix_logo.png` | Official logo, embedded automatically into every output. |
| `Wooplix_Proposal.ipynb` | Same agent as a Google Colab notebook (optional path). |
| `Wooplix_Proposal_Template.docx` | Branded Zoho Writer master with `{MergeField}` placeholders (optional). |
| `sample_requirement_XY.txt` | A sample requirement you can run immediately. |
| `out/` | Generated proposals land here. Ships with one example. |

## How it works

```
Customer requirement (TXT / MD / DOCX / PDF, or pasted text)
        |
   Text extraction
        |
   Past deals from Zoho CRM  (precedent — optional)
        |
   Groq LLM  (Proposal Solution Architect, house-style prompt)
        |
   Structured proposal JSON
        |
   Branded DOCX (python-docx)  +  house-style HTML -> dompdf (PHP) -> PDF   ->  out/
        |  (optional --workdrive)
   Zoho WorkDrive upload
        |
   Human review / send
```

## Setup (once)

The PDF is rendered by **dompdf**, a PHP library, so you need PHP + Composer once:

```bash
brew install php composer          # macOS (already done on this machine)
cd Wooplix_Proposal_Agent
composer install                   # installs dompdf into vendor/
python3 -m pip install -r requirements.txt
cp .env.example .env               # then edit .env and add your real keys
chmod 600 .env                     # keep secrets readable only by you
```

Only `GROQ_API_KEY` is required. The Zoho keys are optional: without them the
agent simply skips CRM precedent and WorkDrive upload and still produces the
branded DOCX + PDF from the requirement alone.

> The DOCX needs only Python. The PDF needs PHP + dompdf (`composer install`).
> If PHP or `vendor/` is missing, the agent stops with a clear message.

> Confirm which models your Groq key allows:
> `curl -s https://api.groq.com/openai/v1/models -H "Authorization: Bearer $GROQ_API_KEY"`

## Run it

Just run it with no arguments — it asks you for the input:

```bash
python3 wooplix_agent.py
```

It prompts:
- **Enter a file path** (drag a `.txt/.md/.docx/.pdf` into the window), **or**
- **press Enter and paste text**, then type `END` on its own line (or press Ctrl-D).

You can also skip the prompts and pass things directly:

```bash
python3 wooplix_agent.py "/path/to/requirement.pdf"   # a file
python3 wooplix_agent.py -                            # paste text, then Ctrl-D
```

Useful options:

```bash
python3 wooplix_agent.py req.docx --out ./out          # where to save
python3 wooplix_agent.py req.docx --model openai/gpt-oss-120b
python3 wooplix_agent.py req.docx --skip-crm           # ignore CRM precedent
python3 wooplix_agent.py req.docx --workdrive          # also upload to WorkDrive
```

Each run prints the DOCX, PDF and JSON paths. Open the DOCX/PDF, review, then send.

## Deploy the upload UI to Vercel

The browser app accepts up to five requirement files (PDF, DOCX, TXT or MD) and
returns a ZIP containing a `Wooplix_Results/` folder with DOCX, PDF and JSON for
each requirement. It calls the existing proposal and PDF rendering functions;
the DOCX and Dompdf HTML/PDF template remain the same as the command-line app.

1. Import this GitHub repository in Vercel and use the repository root as the
   project directory. Vercel detects the static `index.html` and Python API.
2. In **Project Settings → Environment Variables**, add `GROQ_API_KEY`,
   `PDF_RENDER_TOKEN` (a long random secret), and optionally `GROQ_MODEL`.
   Never put secret values in Git or in the browser. The PHP function uses the
   Vercel community runtime configured in `vercel.json` and installs dompdf
   from `composer.lock`.
3. Deploy. The UI checks service configuration at `/api/health`; proposal
   generation is available after the key is set and the deployment is rebuilt.

The API limits each file to 4 MB, the total batch to 15 MB, and each batch to
five files. Uploaded documents are kept in temporary function storage and the
generated ZIP is returned directly; this app does not persist uploads or results.
If the Zoho credentials are configured as Vercel environment variables, the
hosted flow also uses CRM precedent, as the existing command-line generator does.

Generated examples in `out/`, the local `.env`, Python caches and installed
Composer dependencies are excluded from the GitHub repository.

## Guardrails (why some numbers stay blank)

- The model never invents pricing, effort hours, dates or commitments.
- Commercials/Timeline appear only when the requirement or genuine CRM precedent
  supplies the figures; otherwise those sections are omitted — matching how you
  actually send proposals.
- Every draft is marked `DRAFT`. A human approves before sending.
- Style is enforced to read human: plain consultant English, imperative scope
  bullets, the client's concrete values carried through verbatim,
  "(If Required)" for optional items, and a banned list of AI-tell words.

## Optional: Zoho Writer template merge

1. Open `Wooplix_Proposal_Template.docx` in Zoho Writer and save it as a Template.
2. In the Writer editor, replace each blue `{FieldName}` with Insert → Merge Field
   of the same name (Zoho does not auto-convert placeholders from an imported DOCX).
   Fields: ClientName, ProjectName, Contact, ProjectIntroduction, ScopeText,
   Prerequisites, Deliverables, OpenPoints, TimelineText, CommercialsText, ProposalStatus.
3. Copy the template id from the URL (`writer.zoho.in/writer/template/<ID>`) into a
   `ZOHO_WRITER_TEMPLATE_ID` entry in `.env`. The Colab notebook merges into it.

## Security

- Rotate any credential that was ever pasted into chat, code, or a shared file.
- Keep secrets only in `.env` (`chmod 600`), environment variables, or Colab Secrets.
- Never commit `.env`. Only `.env.example` (no real values) belongs in version control.

## Use project times in proposals

The live app checks the Zoho Project Delivery Data Google Sheet each time you analyze a request:

[Open the Zoho Project Delivery Data sheet](https://docs.google.com/spreadsheets/d/1GnpSpbL_B1s6wDOO5NQVml75Fiz56l4bPF1s9F7brEw/edit)

Fill in **Zoho Product Master → Standard Days** with your usual working days for a product. Keep **Typical Proposal Scope**, **Complexity Level**, **Typical User Range**, and **Data Migration** current so the agent can compare what the customer asked for with the work your time covers. Add more detail to **Past Delivered Projects**: products, delivered scope, complexity, migration/integration, and **Actual Working Days**.

When a new requirement arrives, the app reads both tabs, compares it with saved times and delivered projects, shows the matched time estimates, then asks follow-up questions about missing scope. The user can pick a suggested answer, write their own answer, or leave the point open. The app uses those answers and the matched sheet evidence to create the PDF in the existing proposal design. Times in the sheet are estimates, not delivery promises. The app does not add up module days into a total schedule when it cannot tell which work overlaps.

Sheet access needs to remain available to the app. The current sheet can be read as a CSV export without signing in. If its sharing settings change, the app will show a read error and will have no fresh sheet times. Sheet edits are read on the next analysis; they do not require a GitHub commit or Vercel redeployment.

The web workflow is:

1. Upload a customer requirement or paste its text.
2. Select **Analyze requirements**. The app checks the Google Sheet and any optional records you add.
3. Review matches and timing. Answer each follow-up, type an **Other** answer, or choose **Leave open for discovery**.
4. Select **Generate proposal** to download the PDF. Review it before sharing.

The sheet contains project and customer data. Only include information you are comfortable sharing with anyone who can access the sheet. Do not put credentials in it.

The API also provides `/api/analyze` and `/api/generate`. The generation endpoint uses the signed review returned by analysis, so answers and source evidence remain attached to the same draft. The CLI and Colab notebook retain their one-step workflow.
