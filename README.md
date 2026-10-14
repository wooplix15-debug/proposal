# Wooplix Proposal Agent

Wooplix Technologies Private Limited — Together, We Achieve More

A self-contained system that turns a customer requirement into a review-ready
Wooplix proposal in your house style (scope-of-work), as branded **DOCX + PDF**,
using your saved actual delivery-time data to inform the timeline.

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
   Full requirement checklist + saved delivery-time data
        |
   Questions for missing scope and delivery details
        |
   Groq proposal draft + scope coverage check
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

Only `GROQ_API_KEY` is required for drafting. The app uses the saved delivery data and does not automatically read CRM deals. Zoho credentials are only needed for an explicitly requested WorkDrive upload.

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
python3 wooplix_agent.py req.docx --workdrive          # also upload to WorkDrive
```

Each run prints the DOCX, PDF and JSON paths. Open the DOCX/PDF, review, then send.

## Deploy the upload UI to Vercel

The browser app accepts up to five requirement files (PDF, DOCX, TXT or MD) and
returns a ZIP containing a `Wooplix_Results/` folder with DOCX, PDF and JSON for
each requirement. It calls the existing proposal and PDF rendering functions;
the web and command-line versions use the same concise DOCX and PDF layout.

1. Import this GitHub repository in Vercel and use the repository root as the
   project directory. Vercel detects the static `index.html` and Python API.
2. In **Project Settings → Environment Variables**, add `GROQ_API_KEY`,
   `PDF_RENDER_TOKEN` (a long random secret), and optionally `GROQ_MODEL`.
   Never put secret values in Git or in the browser. The PHP function uses the
   Vercel community runtime configured in `vercel.json` and installs dompdf
   from `composer.lock`.
3. To enable accounts and saved output history, add
   `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` from
   the Supabase project. Run [`supabase/setup.sql`](supabase/setup.sql) once in
   the Supabase SQL Editor. Keep the `generated-documents` bucket private.
4. Add `APP_ACCESS_SECRET` in Vercel Project Settings → Environment Variables.
   Use a private random value at least 32 characters long. The app asks for one
   of its ten 15-character access codes before showing the workspace and also
   checks the signed session on every document-generation request. The code
   hashes are in the server code; the actual access codes and signing secret
   must stay private and must not be added to GitHub. Anyone with a code can
   use it, so share each code only with the intended person.
5. In Supabase **Authentication → URL Configuration**, add the deployed Vercel
   URL to the allowed redirect URLs so email confirmation can return to the app.
6. Deploy. The UI checks service configuration at `/api/health`; proposal
   generation is available after the key is set and the deployment is rebuilt.

The API limits each file to 4 MB, the total batch to 15 MB, and each batch to
five files. Uploaded requirements are kept in temporary function storage and
are not saved. Signed-in users can save generated PDF, DOCX or ZIP files to a
private Supabase bucket and see them in **My saved documents**. Each account can
read and delete only its own saved files. Supabase enables optional saved
document history; `APP_ACCESS_SECRET` is required to open the workspace and use
the generation endpoints.
Both flows use the approved bundled delivery file. Test CRM deals are excluded even when Zoho credentials are present.

Generated examples in `out/`, the local `.env`, Python caches and installed
Composer dependencies are excluded from the GitHub repository.

## Guardrails (why some numbers stay blank)

- The model never invents pricing, effort hours, dates or commitments.
- Every requested work area stays in scope, including products without saved delivery times. The draft is checked against the full requirement checklist before export.
- Requested cost categories appear with **To be quoted** where amounts are unavailable. Unknown durations stay **To be confirmed**. A partial estimate is never presented as the total project duration.
- Historical product timings guide phase estimates. A suite estimate is not added to individual application estimates. Mixed or unknown scheduling keeps the overall timeline open.
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

## Project delivery data used in proposals

The app uses the versioned file [`project_delivery_data.json`](project_delivery_data.json) in this repository. It does not contact Google Sheets while analyzing a requirement or generating a proposal. The file currently contains 72 product scope rows, including 31 actual product-time entries. The Past Delivered Projects tab has no completed-project records yet. Client/project reference names are not copied into this public file.

The source sheet is [Zoho Project Delivery Data](https://docs.google.com/spreadsheets/d/1GnpSpbL_B1s6wDOO5NQVml75Fiz56l4bPF1s9F7brEw/edit). To refresh the repository data after changing the sheet, run `python3 refresh_project_delivery_data.py` from the project folder. Review the updated JSON, then commit and push it to GitHub. Vercel will deploy the new bundled data. The refresh script is manual; the live app never runs it.

During analysis, the app selects relevant product scope and actual-time records from this file, sends those records to Groq, and keeps the selected evidence with the review. Proposal generation uses the same saved review data. Matched actual durations appear in the proposal timeline; ranges remain ranges. These are historical results to inform a new proposal, not promises about future work. If no schedule is supplied, the total explicitly assumes sequential delivery. A mixed schedule needs confirmed dependencies before a total is calculated. Missing times use the locally stored `market_delivery_benchmarks.json`, researched from published partner timelines. Its arithmetic averages range midpoints using five working days per week and rounds up; it is a planning estimate, not a measured market-wide average. Saved actual times and explicit user estimates take priority. WhatsApp/SMS integration is included in the cross-application integration phase, and support is excluded from the rollout total. No benchmark website is contacted during proposal generation. The original branded cover, section styling and scope tables are retained.

The web workflow is:

1. Upload a customer requirement or paste its text.
2. Select **Analyze requirements**. The app compares the request with its saved delivery data.
3. Review matches and timing. Answer each follow-up, type an **Other** answer, or choose **Leave open for discovery**.
4. Select **Generate proposal** to download the PDF. Review it before sharing.

This repository is public. Before refreshing the JSON, review it and keep confidential customer details and client names out of the data that gets committed. The refresh script omits the **Project / Client Reference** column.

The API also provides `/api/analyze` and `/api/generate`. The generation endpoint uses the signed review returned by analysis, so answers and source evidence remain attached to the same draft. The CLI uses the same checklist, questions, scope check and timing logic. Non-interactive CLI runs leave unanswered details open. The legacy Colab notebook is a separate optional workflow.

## Checks

Run `python3 -m unittest discover -s tests -v`. The regression checks cover full product detection, missing scope, automatic draft correction, source-only requirements, missing times, mixed scheduling and report content. `tests/fixtures/zoho_event_requirement.txt` contains a full multi-product sample for end-to-end review.

## Separate document tools

The frontend offers three separate choices. **Project Proposal** keeps the existing proposal workflow and layout. **Business Requirements Document** uses the dedicated `business_requirements_agent.py` workflow and its existing BRD layout. **Software Planning Pack** creates eight separate documents: requirements, proposal, BRD, UI/UX, implementation, QA/UAT, documentation, and handover. Its Proposal and BRD are generated by the same existing agents and document builders as the standalone Proposal and BRD options, so their cover pages, branding, and section structure stay consistent. The other six documents use the software planning workflow. Each tool has a separate analysis and generation endpoint.

The software planning tool asks up to three important follow-up questions and keeps unanswered items open. It preserves source requirements, uses stable requirement IDs, and checks that every requirement appears in both the development backlog and planned tests. Technology choices not named in the brief are left for technical discovery. The outputs are planning drafts; they do not claim that code was built, tests were run, software was deployed, or the client approved delivery.

For the software planning output, **Eight PDFs (ZIP)** downloads the eight separate PDFs, **Eight Word files (ZIP)** downloads eight editable DOCX documents, and **All files (ZIP)** includes both formats, JSON source files, and `run-summary.json`. The filenames follow the sequence `01-requirements` through `08-handover`. Saved document history labels this as a Software Planning Pack. If Supabase is already set up, rerun [`supabase/setup.sql`](supabase/setup.sql) to allow the `software_plan` document type; the script updates the existing constraint safely.

The new routes are `/api/software-plan/analyze` and `/api/software-plan/generate`. They require the same private workspace access code and Groq configuration as the other document tools.


### Individual document agents

The sidebar offers eight document workspaces: Proposal, BRD, Requirements,
UI/UX, Implementation, QA/UAT, Documentation and Training, and Handover.
The six added tools each have their own Python module (`requirements_agent.py`,
`ux_agent.py`, `implementation_agent.py`, `qa_agent.py`, `documentation_agent.py`,
`handover_agent.py`), focus, follow-up questions, and section structure.
They share LLM transport and Wooplix formatting through `document_agent_common.py`.
They do not run the software pack, Proposal, or BRD agents.

Each tool accepts uploaded requirements or pasted text, asks at most two essential
questions, and generates only its selected document. PDF and Word download directly;
ZIP contains just that document's PDF and Word files. Reviews are signed and bound
to the selected tool, so a review cannot be reused for another document type.
The existing Proposal and BRD workflows remain separately available.

For a new installation, run `supabase/document_agents.sql` once in the existing project's
SQL Editor. This extends the document type constraint and preserves existing rows.
The former software pack endpoints remain for compatibility; the pack is no longer
shown in the sidebar.

The new document tools use the existing authenticated PHP/Dompdf renderer on
`https://proposal-six-kappa.vercel.app` for Vercel PDF exports. This avoids preview
host access failures. `PDF_RENDER_BASE_URL` can override that URL; its renderer must
use the same `PDF_RENDER_TOKEN`. The token remains server-side.

### Google Stitch UI design (new branch)

The **Generate UI Design** sidebar option accepts an approved BRD or requirements
as PDF, DOC/DOCX, TXT, Markdown, or pasted text. It generates one selected screen
through Google's hosted Stitch MCP server, previews its actual HTML and original
screenshot, and downloads a ZIP with local HTML, screenshots and an asset manifest.
Existing Proposal, BRD and individual document agents use their existing workflows.

Local setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn api.index:app --host 127.0.0.1 --port 8777
```

Add `STITCH_API_KEY` to the ignored `.env` file in the checkout. Obtain it through
Stitch's Settings/API Keys page. Do not put it in frontend code or commit it.
Alternatively, set both `STITCH_ACCESS_TOKEN` and `GOOGLE_CLOUD_PROJECT` for OAuth.
OAuth access tokens expire and must be refreshed manually with this configuration.
The app does not currently implement an OAuth refresh flow.

Open `http://127.0.0.1:8777`, enter an existing workspace access code, select
**Generate UI Design**, provide the BRD, select a screen and device, then generate.
A BRD can be uploaded after it has been reviewed and approved; this feature does
not itself grant approval. Generation sends its supplied requirements to Google Stitch.

`stitch_client.py` uses the official MCP Python SDK and Stitch's documented tools.
`stitch_export.py` downloads artifacts from approved Google/asset hosts, embeds
styles, fonts and images, and removes arbitrary executable content. `stitch_ui.js`
provides the isolated workflow. Preview frames use `sandbox="allow-scripts"`
without same-origin permission, plus a restrictive content security policy.
The export is a visual prototype, not a functional application or production code.

The lightweight `GEMINI_3_5_FLASH_LITE` model is the default. `STITCH_MODEL` can
select another model supported by your account. Actual quota and billing depend
on your Stitch account; the integration cannot guarantee that generation is free.
Generation calls are never automatically retried. **Check status** makes only
read calls and can retry artifact downloads without generating another screen.
If Stitch needs clarification, its message is shown when available; review the
project in Stitch before requesting another generation.

Unzip the download and open `index.html` to view the included screens. The exported
screenshots are offline files. HTML uses bundled assets and a local Tailwind runtime
when present in the original design. `manifest.json` records whether assets were
fully bundled and lists any missing fonts, images, styles or removed custom scripts.
A warning means some visual or interactive behavior may differ. No API keys or
provider download links are included in the export.

Ready artifacts use a bounded 15-minute in-memory cache to avoid downloading
fonts and images again for each export. A server restart or cold instance fetches
them again through read-only calls. Sessions are signed and expire after one hour. A browser refresh can retain the
session token in the same tab. Screens are stored by Stitch, not by Supabase in this
first integration. When the provider does not index a pending generation yet,
**Check status** may remain pending; no duplicate generation is started automatically.

For Vercel, configure the same server-only Stitch environment variable on the
chosen preview deployment. A generation and asset export must finish within the
existing 300-second function limit; slow provider jobs may need status recovery.
This branch was tested locally; its automatic Git preview needs its own environment
configuration and is not assumed to be production-ready without a deployment test.

Official references:
- https://stitch.withgoogle.com/docs/mcp/setup
- https://github.com/google-labs-code/stitch-sdk
- https://github.com/modelcontextprotocol/python-sdk

Regression tests: `python -m unittest discover -s tests -v`.
