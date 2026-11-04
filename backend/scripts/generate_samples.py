"""Generate the sample documents in /samples (insurance-broking themed, matching Outmarket's domain).

Run:  python scripts/generate_samples.py  (needs reportlab + python-docx; see requirements-dev.txt)

The set is chosen to exercise every feature:
- Two versions of the same policy (versioning + comparison: changed premium, deductible, exclusion).
- A competing quote as DOCX with a table (multi-format parsing, cross-document chat).
- A broker agreement in Markdown (plain-text path).
- A plain-text procedure (.txt path).
- Edge cases: an image-only "scanned" PDF, a corrupt PDF and a renamed executable (clear errors).
"""

from pathlib import Path

from docx import Document as DocxDocument
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table

OUT = Path(__file__).resolve().parents[2] / "samples"
styles = getSampleStyleSheet()


def _policy(version: int) -> list:  # type: ignore[type-arg]
    renewal = version == 2
    period = "January 1, 2026 to January 1, 2027" if renewal else "January 1, 2025 to January 1, 2026"
    premium = "$48,750" if renewal else "$42,300"
    flood_deductible = "$50,000" if renewal else "$25,000"
    building_limit = "$12,500,000" if renewal else "$12,000,000"
    h1, h2, body = styles["Title"], styles["Heading2"], styles["BodyText"]

    story = [
        Paragraph("Commercial Property Insurance Policy", h1),
        Paragraph(f"{'Renewal ' if renewal else ''}Declarations Page", h2),
        Table(
            [
                ["Policy number", "CPP-4471-2026" if renewal else "CPP-4471-2025"],
                ["Named insured", "Acme Logistics, Inc."],
                ["Mailing address", "1200 Harbor Way, Oakland, CA 94607"],
                ["Insurer", "Granite Mutual Insurance Company"],
                ["Broker", "Bayline Risk Partners"],
                ["Policy period", period],
                ["Annual premium", premium],
            ],
            colWidths=[2 * inch, 4 * inch],
        ),
        Spacer(1, 12),
        Paragraph("Limits of Insurance", h2),
        Table(
            [
                ["Coverage", "Limit"],
                ["Building", building_limit],
                ["Business personal property", "$3,000,000"],
                ["Business income (12 months)", "$2,500,000"],
                ["Equipment breakdown", "$1,000,000"],
            ],
            colWidths=[3 * inch, 3 * inch],
        ),
        Spacer(1, 12),
        Paragraph("Deductibles", h2),
        Paragraph(
            f"All other perils deductible: $10,000 per occurrence. Flood deductible: {flood_deductible} per "
            "occurrence. Earthquake deductible: 5% of the total insured value at the affected location, "
            "subject to a minimum of $100,000. Windstorm deductible: 2% of the building limit.",
            body,
        ),
        PageBreak(),
        Paragraph("Section I - Covered Causes of Loss", h2),
        Paragraph(
            "This policy insures against direct physical loss or damage to covered property caused by fire, "
            "lightning, explosion, windstorm, hail, smoke, vandalism, sprinkler leakage, theft and flood, "
            "subject to the exclusions in Section II. Flood coverage is subject to an annual aggregate limit "
            "of $2,000,000. Water damage from a sudden and accidental discharge of plumbing systems is covered.",
            body,
        ),
        Spacer(1, 8),
        Paragraph("Section II - Exclusions", h2),
        Paragraph(
            "We will not pay for loss or damage caused by: (a) wear and tear, rust, corrosion or gradual "
            "deterioration; (b) mold or fungus, except when resulting from a covered water damage loss and "
            "reported within 30 days; (c) nuclear hazard; (d) war and military action; (e) government seizure; "
            "(f) faulty workmanship or design."
            + (
                " (g) Cyber incident: any loss arising from unauthorized access to, or malicious code affecting, "
                "computer systems, including resulting physical damage."
                if renewal
                else ""
            ),
            body,
        ),
        PageBreak(),
        Paragraph("Section III - Conditions", h2),
        Paragraph(
            "Notice of loss: the insured must notify the insurer within 30 days of discovering a loss. "
            "Proof of loss must be submitted within 60 days of the insurer's request. "
            "Valuation: buildings are valued at replacement cost; stock at selling price less discounts. "
            "Coinsurance: 90%. Cancellation: the insurer may cancel with 60 days' written notice, or 10 days "
            "for non-payment of premium. Protective safeguards: the automatic sprinkler system must be "
            "maintained in working order; failure to do so suspends fire coverage at that location.",
            body,
        ),
        Spacer(1, 8),
        Paragraph("Section IV - Endorsements", h2),
        Paragraph(
            "Endorsement CP-12: Ordinance or Law coverage, limit $500,000. "
            "Endorsement CP-19: Spoilage coverage for refrigerated stock, limit $250,000, deductible $5,000. "
            + ("Endorsement CP-27: Terrorism coverage (TRIA), premium $1,150." if renewal else ""),
            body,
        ),
    ]
    return story


def build_pdf(path: Path, story: list) -> None:  # type: ignore[type-arg]
    SimpleDocTemplate(str(path), pagesize=LETTER, title=path.stem).build(story)


def build_quote_docx(path: Path) -> None:
    doc = DocxDocument()
    doc.add_heading("General Liability Insurance Quote", 0)
    doc.add_paragraph("Prepared for: Acme Logistics, Inc.   Prepared by: Northwind Specialty Insurance")
    doc.add_paragraph("Quote number: NW-GL-88213. Quote valid until: December 15, 2025.")
    doc.add_heading("Proposed limits", level=1)
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Coverage", "Limit"
    for coverage, limit in [
        ("Each occurrence", "$1,000,000"),
        ("General aggregate", "$2,000,000"),
        ("Products-completed operations aggregate", "$2,000,000"),
        ("Personal and advertising injury", "$1,000,000"),
        ("Damage to rented premises", "$300,000"),
        ("Medical expense (any one person)", "$10,000"),
    ]:
        row = table.add_row().cells
        row[0].text, row[1].text = coverage, limit
    doc.add_heading("Premium and terms", level=1)
    doc.add_paragraph(
        "Annual premium: $18,400 plus a $250 policy fee. Deductible: $2,500 per claim (bodily injury and "
        "property damage). Payment: 25% deposit, remainder in 9 monthly installments."
    )
    doc.add_heading("Subjectivities", level=1)
    for item in [
        "Signed application dated within 30 days of binding.",
        "Five-year currently valued loss runs.",
        "Confirmation that no warehouse operations involve hazardous materials.",
    ]:
        doc.add_paragraph(item, style="List Bullet")
    doc.add_heading("Notable exclusions", level=1)
    doc.add_paragraph(
        "Professional services, pollution (except hostile fire), auto liability, employment practices, "
        "and communicable disease are excluded."
    )
    doc.save(str(path))


AGREEMENT_MD = """# Broker Service Agreement

**Between:** Bayline Risk Partners ("Broker") and Acme Logistics, Inc. ("Client")
**Effective date:** November 1, 2025
**Term:** 24 months, renewing automatically for 12-month periods unless either party gives 90 days' notice.

## 1. Services
The Broker will market the Client's property, general liability, auto and cyber insurance to at least
five (5) insurers each renewal, present a written comparison of quotes, and support claims advocacy.

## 2. Compensation
The Broker is compensated by insurer commission of 10% of written premium on property and liability lines,
and a flat annual fee of $15,000 for cyber and risk-consulting services, invoiced quarterly.

## 3. Service levels
- Certificates of insurance issued within 1 business day of request.
- Claims acknowledged within 4 business hours.
- Renewal strategy meeting at least 120 days before each policy expiration.

## 4. Confidentiality
Both parties keep non-public information confidential for 3 years after termination.

## 5. Termination
Either party may terminate for convenience with 60 days' written notice. Commission already earned on
bound policies is not refundable.

## 6. Governing law
This agreement is governed by the laws of the State of California.
"""


CLAIMS_TXT = """ACME LOGISTICS - CLAIMS REPORTING PROCEDURE (internal, plain text)

1. Report any property loss to the Facilities Manager, Priya Raman, within 24 hours.
2. Facilities notifies the broker (Bayline Risk Partners, claims desk: claims@bayline.example) the same day.
3. Photograph the damage before any clean-up, except where needed to prevent further loss.
4. Keep damaged stock for inspection by the insurer's adjuster for at least 14 days.
5. Losses expected to exceed $250,000 must also be reported to the CFO, Daniel Okafor.
"""


def build_scanned_pdf(path: Path) -> None:
    """A PDF with only vector drawing (no text layer) - simulates a scanned page."""
    c = canvas.Canvas(str(path), pagesize=LETTER)
    for i in range(12):
        c.rect(72, 700 - i * 40, 400 - i * 15, 18, fill=1)
    c.showPage()
    c.save()


def main() -> None:
    OUT.mkdir(exist_ok=True)
    build_pdf(OUT / "acme_property_policy_2025.pdf", _policy(1))
    build_pdf(OUT / "acme_property_policy_2026_renewal.pdf", _policy(2))
    build_quote_docx(OUT / "northwind_general_liability_quote.docx")
    (OUT / "bayline_broker_service_agreement.md").write_text(AGREEMENT_MD)
    (OUT / "acme_claims_procedure.txt").write_text(CLAIMS_TXT)
    edge = OUT / "edge-cases"
    edge.mkdir(exist_ok=True)
    build_scanned_pdf(edge / "scanned_no_text.pdf")
    (edge / "corrupt.pdf").write_bytes(b"%PDF-1.7\n" + b"\x00garbage" * 200)
    (edge / "not_really_a.pdf").write_bytes(b"MZ\x90\x00 this is an executable renamed to .pdf")
    print(f"Samples written to {OUT}")


if __name__ == "__main__":
    main()
