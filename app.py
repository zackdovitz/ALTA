import os
import uuid
import base64
import json
import io
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from werkzeug.utils import secure_filename
import anthropic

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 150 * 1024 * 1024  # 150MB

UPLOAD_DIR = Path('uploads')
UPLOAD_DIR.mkdir(exist_ok=True)
METADATA_FILE = UPLOAD_DIR / 'metadata.json'
ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'tiff', 'tif'}


# ---------------------------------------------------------------------------
# File metadata helpers
# ---------------------------------------------------------------------------

def load_metadata():
    if METADATA_FILE.exists():
        try:
            return json.loads(METADATA_FILE.read_text())
        except Exception:
            return []
    return []


def save_metadata(data):
    METADATA_FILE.write_text(json.dumps(data, indent=2))


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# ---------------------------------------------------------------------------
# Image / PDF helpers
# ---------------------------------------------------------------------------

def compress_image(img_bytes, max_width=1800, quality=82):
    """Resize and JPEG-compress image bytes; return (b64_string, media_type)."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(img_bytes))
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=quality, optimize=True)
        buf.seek(0)
        return base64.standard_b64encode(buf.read()).decode(), 'image/jpeg'
    except Exception:
        return base64.standard_b64encode(img_bytes).decode(), 'image/png'


def pdf_to_images(filepath, max_pages=15):
    """Return list of (b64, media_type, page_num) tuples + total page count."""
    try:
        import fitz
        doc = fitz.open(str(filepath))
        total = len(doc)
        results = []
        for i in range(min(total, max_pages)):
            page = doc.load_page(i)
            mat = fitz.Matrix(200 / 72, 200 / 72)
            pix = page.get_pixmap(matrix=mat)
            b64, mt = compress_image(pix.tobytes('png'))
            results.append((b64, mt, i + 1))
        doc.close()
        return results, total
    except Exception:
        return [], 0


def extract_pdf_text(filepath, max_chars=60000):
    """Extract text from PDF, trying PyMuPDF then pypdf."""
    try:
        import fitz
        doc = fitz.open(str(filepath))
        text = '\n\n'.join(page.get_text() for page in doc)
        doc.close()
        return text[:max_chars]
    except Exception:
        pass
    try:
        import pypdf
        reader = pypdf.PdfReader(str(filepath))
        text = '\n\n'.join(p.extract_text() or '' for p in reader.pages)
        return text[:max_chars]
    except Exception:
        return ''


def image_to_b64(filepath):
    """Read an image file and return (b64, media_type)."""
    with open(filepath, 'rb') as f:
        raw = f.read()
    return compress_image(raw, max_width=2000)


# ---------------------------------------------------------------------------
# Routes – file management
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/files', methods=['GET'])
def list_files():
    return jsonify(load_metadata())


@app.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    doc_type = request.form.get('type', 'title_document')
    if not f or f.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    if not allowed_file(f.filename):
        return jsonify({'error': f'Unsupported file type. Allowed: {", ".join(sorted(ALLOWED_EXTENSIONS))}'}), 400

    ext = Path(f.filename).suffix.lower()
    file_id = str(uuid.uuid4())
    saved_name = f'{file_id}{ext}'
    dest = UPLOAD_DIR / saved_name
    f.save(str(dest))

    entry = {
        'id': file_id,
        'original_name': f.filename,
        'display_name': f.filename,
        'saved_name': saved_name,
        'type': doc_type,
        'size': dest.stat().st_size,
        'ext': ext.lstrip('.'),
    }
    meta = load_metadata()
    meta.append(entry)
    save_metadata(meta)
    return jsonify(entry)


@app.route('/api/files/delete', methods=['POST'])
def delete_files():
    ids = set((request.get_json() or {}).get('ids', []))
    meta = load_metadata()
    kept, deleted = [], []
    for e in meta:
        if e['id'] in ids:
            try:
                (UPLOAD_DIR / e['saved_name']).unlink(missing_ok=True)
            except Exception:
                pass
            deleted.append(e['id'])
        else:
            kept.append(e)
    save_metadata(kept)
    return jsonify({'deleted': deleted})


@app.route('/api/files/rename', methods=['POST'])
def rename_file():
    data = request.get_json() or {}
    file_id = data.get('id', '')
    new_name = (data.get('name') or '').strip()
    if not new_name:
        return jsonify({'error': 'Name cannot be empty'}), 400
    meta = load_metadata()
    for e in meta:
        if e['id'] == file_id:
            e['display_name'] = new_name
            save_metadata(meta)
            return jsonify(e)
    return jsonify({'error': 'File not found'}), 404


@app.route('/api/files/clear-all', methods=['POST'])
def clear_all():
    for e in load_metadata():
        try:
            (UPLOAD_DIR / e['saved_name']).unlink(missing_ok=True)
        except Exception:
            pass
    save_metadata([])
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Analysis endpoint (streaming)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a senior real estate attorney with 25+ years of practice focused exclusively on \
commercial real estate transactions. You have personally reviewed thousands of ALTA/NSPS \
Land Title Surveys and are nationally recognized for the depth and precision of your survey \
analysis. Your task is to produce a comprehensive, partner-level survey review memorandum. \
Write as if you are handing this memo directly to a sophisticated client and their deal team \
on the eve of a significant transaction closing.

Guidelines:
• Be specific. Cite exact bearings, distances, dimensions, and schedule references visible \
  in the documents. Do not generalize.
• Risk-rate every finding using exactly one of these labels on its own line before the \
  finding text:  🔴 HIGH RISK  |  🟡 MEDIUM RISK  |  🟢 LOW RISK  |  🔵 INFORMATIONAL
• Where the survey or documents are ambiguous or information appears to be missing, say so \
  explicitly and explain the legal consequence of the gap.
• Conclude with a plain-English Executive Summary a non-lawyer client can read in 90 seconds.
• Format using markdown headers, bold labels, and bullet points for readability.
"""

ANALYSIS_PROMPT = """\
Please produce a comprehensive ALTA Survey Analysis Memorandum structured exactly as follows:

---

# ALTA SURVEY ANALYSIS MEMORANDUM

**Prepared by:** AI Legal Analysis Engine (for review and verification by licensed counsel)
**Document(s) Reviewed:** [list the survey(s) and supporting documents you received]
**Standards Reference:** ALTA/NSPS Minimum Standard Detail Requirements (most current version)

---

## PART I — SURVEY CERTIFICATION & STANDARDS COMPLIANCE
- Surveyor name, license number, firm, and state
- Date of survey and date of last revision (if any)
- Parties to whom the survey is certified; confirm all required parties are named
- Certification language — confirm it follows the approved ALTA language or flag deviations
- Table A optional items: list each item number that appears to have been requested; note \
  any items that appear missing or incompletely addressed
- Overall standards compliance assessment

## PART II — LEGAL DESCRIPTION ANALYSIS
- Reproduce or summarize the legal description shown on the survey
- Cross-reference to the legal description in the title commitment (if provided)
- Identify any discrepancy, even minor (e.g., acreage rounding, missing call, alternate \
  metes-and-bounds language)
- Assess gap/overlap risk from any discrepancy found
- Note whether description is by metes and bounds, lot/block, government survey, or other

## PART III — BOUNDARY, CLOSURE & ACREAGE
- Describe the shape and boundary of the parcel
- Confirm or question closure of the traverse
- State total area (acres and/or sq ft) as shown on survey
- Note monuments found vs. set — assess adequacy
- Calls to adjoiners — confirm consistency with record documents
- Flag any unresolved gaps, overlaps, strips, or gores along the boundary

## PART IV — ENCROACHMENTS & ENCUMBRANCES
For each encroachment or encumbrance plotted or noted:
  - Describe it specifically (what, where, dimensions if visible)
  - State direction: does subject property encroach outward, or does something encroach inward?
  - Rate severity: boundary-line encroachment vs. setback encroachment vs. easement-area \
    encroachment
  - Cross-reference to Schedule B of title commitment — is it already excepted?
  - If NOT already excepted: flag as new exception/objection required
  - Recommend insurance coverage approach or physical cure

## PART V — EASEMENT ANALYSIS
For each easement plotted or referenced:
  - Type (utility, access, drainage, pipeline, conservation, etc.)
  - Grantor / grantee / beneficiary if ascertainable
  - Location and dimensions relative to the parcel
  - Proximity to existing improvements and planned development
  - Beneficial, burdensome, or deal-breaking assessment
  - Access easement analysis: legal and practical ingress/egress adequacy for intended use
  - Cross-access / shared driveway arrangements — documented and insurable?
  - Recommended endorsements (ALTA 28, ALTA 28.1, etc.)

## PART VI — SETBACK LINE ANALYSIS
- Identify all setback lines shown (regulatory/zoning vs. plat/deed-restriction)
- Confirm whether existing improvements comply with each setback
- Non-conforming structures: flag, assess insurability, analyze rebuilding exposure under \
  local zoning if structure is damaged >50%
- Development program feasibility: map available building envelope against all setbacks

## PART VII — ROADS, RIGHTS-OF-WAY & ACCESS
- Nature of road access (public dedicated/accepted, private, easement-only)
- Confirm whether the property abuts a publicly dedicated and accepted right-of-way
- Width of right-of-way and road surface improvements
- If access is by easement only: confirm it is recorded, runs with the land, and is insurable
- Adequacy for lender, permitting, and client's operational/development requirements
- Curb cuts, shared access, traffic issues visible on survey

## PART VIII — FLOOD ZONE, UTILITIES & OTHER OBSERVATIONS
- FEMA flood zone designation and FIRM panel number/date (if shown)
- Utility lines, poles, meters plotted — any in conflict with improvements or development?
- Parking, striping, or other site features shown — any issues?
- Any survey notes or legends that require legal attention
- Anything else on the face of the survey requiring attorney review

## PART IX — TITLE COMMITMENT CROSS-REFERENCE
(Complete this section only if a title commitment was provided)
- Schedule A: confirm insured amount, effective date, and vesting
- Schedule B-I (requirements): flag any outstanding requirements affecting survey matters
- Schedule B-II (exceptions): cross-reference every survey-plotted item to a Schedule B \
  exception; flag any survey matters without a corresponding exception
- Recommend specific ALTA endorsements: (e.g., ALTA 9 – restrictions; ALTA 17/17.1 – access; \
  ALTA 19 – contiguity; ALTA 25 – same-as-survey; ALTA 28/28.1 – easements; \
  ALTA 29 – interest rate swap [if applicable]; flood endorsements, etc.)

---

## PART X — ACTION ITEMS (PRIORITIZED)

### 🔴 IMMEDIATE — Potential Deal Issues or Closing Blockers
(Number each item)

### 🟡 PRE-CLOSING REQUIREMENTS
(Number each item)

### 🟢 STANDARD FOLLOW-UP / SELLER CURE ITEMS
(Number each item)

### 🔵 INFORMATIONAL / NO ACTION REQUIRED
(Number each item)

---

## EXECUTIVE SUMMARY

Write 3–5 paragraphs in plain English suitable for a sophisticated but non-lawyer client. \
Cover: overall survey quality, major risks identified, whether the deal appears insurable as \
surveyed, key items requiring attention before closing, and your overall risk assessment \
(e.g., Low / Moderate / High / Elevated). Do NOT use legal jargon without explanation.

---

Now produce the full memorandum based on all documents provided above.\
"""


@app.route('/api/analyze', methods=['POST'])
def analyze():
    data = request.get_json() or {}
    context_notes = (data.get('context_notes') or '').strip()

    meta = load_metadata()
    surveys = [f for f in meta if f['type'] == 'alta_survey']
    commitments = [f for f in meta if f['type'] == 'title_commitment']
    docs = [f for f in meta if f['type'] == 'title_document']

    if not surveys:
        return jsonify({'error': 'Please upload at least one ALTA Survey before running analysis.'}), 400

    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'error': 'ANTHROPIC_API_KEY environment variable is not set.'}), 500

    # Build content array
    content = []

    # ---- ALTA Surveys (visual + text) ----
    for idx, s in enumerate(surveys):
        fp = UPLOAD_DIR / s['saved_name']
        ext = s['ext'].lower()
        content.append({'type': 'text', 'text':
            f"\n{'='*70}\nALTA/NSPS LAND TITLE SURVEY #{idx + 1}: {s['display_name']}\n{'='*70}\n"
            "Examine this survey document carefully — every line, note, legend entry, "
            "bearing, dimension, easement, encroachment, setback, and monument.\n"})

        if ext == 'pdf':
            images, total = pdf_to_images(str(fp), max_pages=15)
            content.append({'type': 'text',
                'text': f"PDF survey — {total} total page(s); {len(images)} page(s) rendered below:\n"})
            for b64, mt, pn in images:
                content.append({'type': 'text', 'text': f'[Survey Page {pn}]'})
                content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': mt, 'data': b64}})
            text = extract_pdf_text(str(fp))
            if text:
                content.append({'type': 'text', 'text': f'\n[Extracted survey text]\n{text}\n'})
        elif ext in ('png', 'jpg', 'jpeg', 'tiff', 'tif'):
            b64, mt = image_to_b64(str(fp))
            content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': mt, 'data': b64}})

    # ---- Title Commitment (text + first pages visual) ----
    for idx, tc in enumerate(commitments):
        fp = UPLOAD_DIR / tc['saved_name']
        ext = tc['ext'].lower()
        content.append({'type': 'text', 'text':
            f"\n{'='*70}\nTITLE COMMITMENT #{idx + 1}: {tc['display_name']}\n{'='*70}\n"})
        if ext == 'pdf':
            text = extract_pdf_text(str(fp), max_chars=80000)
            if text:
                content.append({'type': 'text', 'text': text})
            images, _ = pdf_to_images(str(fp), max_pages=8)
            for b64, mt, pn in images:
                content.append({'type': 'text', 'text': f'[Commitment Page {pn}]'})
                content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': mt, 'data': b64}})
        elif ext in ('png', 'jpg', 'jpeg'):
            b64, mt = image_to_b64(str(fp))
            content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': mt, 'data': b64}})

    # ---- Title Documents (text) ----
    for idx, td in enumerate(docs):
        fp = UPLOAD_DIR / td['saved_name']
        ext = td['ext'].lower()
        content.append({'type': 'text', 'text':
            f"\n{'='*70}\nTITLE DOCUMENT #{idx + 1}: {td['display_name']}\n{'='*70}\n"})
        if ext == 'pdf':
            text = extract_pdf_text(str(fp), max_chars=30000)
            if text:
                content.append({'type': 'text', 'text': text})
        elif ext in ('png', 'jpg', 'jpeg'):
            b64, mt = image_to_b64(str(fp))
            content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': mt, 'data': b64}})

    # ---- Context notes ----
    if context_notes:
        content.append({'type': 'text', 'text':
            f"\n{'='*70}\nCLIENT / DEAL TEAM CONTEXT NOTES:\n{'='*70}\n{context_notes}\n"})

    # ---- Analysis instruction ----
    content.append({'type': 'text', 'text': ANALYSIS_PROMPT})

    def generate():
        try:
            client = anthropic.Anthropic(api_key=api_key)
            with client.messages.stream(
                model='claude-opus-4-6',
                max_tokens=8192,
                system=SYSTEM_PROMPT,
                messages=[{'role': 'user', 'content': content}],
            ) as stream:
                for chunk in stream.text_stream:
                    yield f'data: {json.dumps({"text": chunk})}\n\n'
            yield f'data: {json.dumps({"done": True})}\n\n'
        except anthropic.APIError as e:
            yield f'data: {json.dumps({"error": f"API error: {e}"})}\n\n'
        except Exception as e:
            yield f'data: {json.dumps({"error": f"Analysis failed: {e}"})}\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'\n  ALTA Survey Analyzer running at http://localhost:{port}\n')
    app.run(debug=False, port=port, host='0.0.0.0')
