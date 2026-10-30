"""German mutual NDA ("Gegenseitige Geheimhaltungs- und Nichtumgehungsvereinbarung") as PDF.

Template NDA-DE-1.0. Structure follows common German B2B practice: Vertraulichkeitsdefinition,
Ausnahmen (angelehnt an § 2 Nr. 1 GeschGehG), Need-to-know, Nichtumgehung (Schutz der Flächeneigentümer),
Vertragsstrafe nach "Hamburger Brauch" (§ 315 BGB, mit Höchstbetrag), Rückgabe/Löschung, DSGVO,
Laufzeit/Nachwirkung, Textform (§ 126b BGB).

The text is a TEMPLATE: have a German lawyer approve it before production use.
Only WinAnsi characters are used so the standard PDF fonts render ä ö ü ß § „ “ – € correctly.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

TEMPLATE_VERSION = "NDA-DE-1.0"


@dataclass
class Party:
    company_name: str
    street: str
    postal_code: str
    city: str
    representative: str  # vertretungsberechtigte Person(en)
    register: str = ""  # e.g. "Amtsgericht Potsdam, HRB 12345"
    email: str = ""

    def block(self) -> str:
        lines = [f"<b>{escape(self.company_name)}</b>", f"{escape(self.street)}, {escape(self.postal_code)} {escape(self.city)}"]
        if self.register:
            lines.append(escape(self.register))
        lines.append(f"vertreten durch: {escape(self.representative)}")
        if self.email:
            lines.append(f"E-Mail für Mitteilungen: {escape(self.email)}")
        return "<br/>".join(lines)


@dataclass
class NdaParams:
    nda_id: str
    match_id: str
    created_at: datetime
    platform_name: str = "Flächenpool-Plattform"
    term_years: int = 2
    aftereffect_years: int = 5
    non_circumvention_months: int = 24
    max_penalty_eur: int = 50_000
    jurisdiction_city: str = "Berlin"


@dataclass
class Signature:
    party_label: str  # "A" | "B"
    signer_name: str
    signer_role: str
    signed_at: datetime
    ip: str = ""


# ------------------------------------------------------------------ text

def _eur(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _sections(a: Party, b: Party, p: NdaParams) -> list[tuple[str, list[str]]]:
    """(heading, paragraphs). A paragraph starting with '  ' is rendered as an indented sub-item."""
    return [
        ("§ 1 Vertrauliche Informationen", [
            "(1) „Vertrauliche Informationen“ sind sämtliche Informationen, die eine Partei („Offenlegende Partei“) der anderen "
            "Partei („Empfangende Partei“) im Zusammenhang mit dem Zweck – schriftlich, mündlich, elektronisch, durch Einsichtnahme "
            "oder auf sonstige Weise, über die Plattform oder unmittelbar, vor, bei oder nach Abschluss dieser Vereinbarung – "
            "zugänglich macht, und zwar unabhängig davon, ob sie als vertraulich gekennzeichnet sind.",
            "(2) Vertrauliche Informationen sind insbesondere:",
            "  a) Lage, Abgrenzung, Größe und Flurstücksbezeichnungen der Flächen der Offenlegenden Partei sowie Karten- und Geodaten;",
            "  b) Identität und Kontaktdaten von Grundstückseigentümern, Verpächtern, Landwirten und sonstigen Nutzungsberechtigten "
            "sowie bestehende oder geplante Vereinbarungen mit diesen (Pacht-, Nutzungs-, Options- und Gestattungsverträge, "
            "Vorverträge, Konditionen);",
            "  c) Gutachten, Standort-, Wind- und Ertragsanalysen, Genehmigungs-, Netzanschluss- und Wirtschaftlichkeitsunterlagen "
            "sowie Planungsunterlagen;",
            "  d) Geschäftsmodelle, Preise, Kalkulationen, Finanzierungs- und Beteiligungsstrukturen;",
            "  e) die Tatsache, der Inhalt und der Stand der Gespräche zwischen den Parteien sowie der Inhalt dieser Vereinbarung.",
            "(3) Soweit Vertrauliche Informationen Geschäftsgeheimnisse im Sinne des § 2 Nr. 1 GeschGehG sind, genießen sie "
            "zusätzlich den Schutz des Gesetzes zum Schutz von Geschäftsgeheimnissen.",
        ]),
        ("§ 2 Ausnahmen", [
            "(1) Keine Vertraulichen Informationen sind Informationen, bei denen die Empfangende Partei nachweisen kann, dass sie",
            "  a) allgemein bekannt oder öffentlich zugänglich sind oder werden, ohne dass dies auf einem Verstoß gegen diese "
            "Vereinbarung beruht;",
            "  b) ihr bereits vor der Offenlegung rechtmäßig und ohne Geheimhaltungspflicht bekannt waren;",
            "  c) ihr von einem Dritten rechtmäßig und ohne Geheimhaltungspflicht mitgeteilt wurden; oder",
            "  d) von ihr selbständig und ohne Nutzung der Vertraulichen Informationen entwickelt wurden.",
            "(2) Einzelne Informationen sind nicht allein deshalb allgemein bekannt, weil sie nur in Teilen oder in anderer "
            "Zusammenstellung öffentlich zugänglich sind.",
            "(3) Eine Offenlegung ist zulässig, soweit die Empfangende Partei hierzu aufgrund zwingender gesetzlicher Vorschriften "
            "oder einer vollstreckbaren gerichtlichen oder behördlichen Anordnung verpflichtet ist. Sie hat die Offenlegende "
            "Partei – soweit rechtlich zulässig – vorher unverzüglich in Textform zu informieren, die Offenlegung auf das "
            "zwingend Erforderliche zu beschränken und auf die weitere Vertraulichkeit hinzuwirken.",
        ]),
        ("§ 3 Pflichten der Empfangenden Partei", [
            "(1) Die Empfangende Partei hat die Vertraulichen Informationen geheim zu halten, ausschließlich für den Zweck zu "
            "verwenden und nicht an Dritte weiterzugeben oder anderweitig zu verwerten.",
            "(2) Sie hat angemessene Geheimhaltungsmaßnahmen im Sinne des § 2 Nr. 1 lit. b GeschGehG zu treffen, mindestens "
            "jedoch mit der Sorgfalt, die sie bei eigenen Informationen vergleichbarer Bedeutung anwendet, und stets mit der "
            "Sorgfalt eines ordentlichen Kaufmanns.",
            "(3) Zugang dürfen nur Organmitglieder, Mitarbeiter und verbundene Unternehmen sowie Berater (Rechtsanwälte, "
            "Steuerberater, Wirtschaftsprüfer, Gutachter, Finanzierungspartner) erhalten, die die Informationen zur Verfolgung des "
            "Zwecks zwingend benötigen („Need-to-know“) und zur Verschwiegenheit verpflichtet sind, sei es berufsrechtlich oder "
            "durch eine dieser Vereinbarung gleichwertige Verpflichtung. Die Empfangende Partei haftet für Verstöße dieser "
            "Personen wie für eigene.",
            "(4) Kopien und Aufzeichnungen sind nur im für den Zweck erforderlichen Umfang zulässig und in gleicher Weise "
            "geheim zu halten.",
            "(5) Verstöße oder der Verdacht eines Verstoßes sind der Offenlegenden Partei unverzüglich in Textform anzuzeigen; "
            "die Empfangende Partei wirkt an der Schadensbegrenzung mit.",
            "(6) Durch die Offenlegung werden keine Rechte, insbesondere keine Eigentums-, Lizenz-, Nutzungs- oder Optionsrechte "
            "an den Vertraulichen Informationen eingeräumt.",
        ]),
        ("§ 4 Nichtumgehung; Schutz der Flächenbeziehungen", [
            f"(1) Jede Partei verpflichtet sich, für die Dauer von {p.non_circumvention_months} Monaten ab Wirksamwerden dieser "
            "Vereinbarung (Sperrfrist) ohne vorherige Zustimmung der anderen Partei in Textform",
            "  a) mit den ihr durch die Offenlegung bekannt gewordenen Eigentümern, Verpächtern, Landwirten oder sonstigen "
            "Nutzungsberechtigten der Flächen der anderen Partei keinen Kontakt zum Zweck der Anbahnung, Änderung, Ablösung oder "
            "des Abschlusses von Pacht-, Nutzungs-, Options-, Gestattungs- oder vergleichbaren Verträgen über diese Flächen "
            "aufzunehmen;",
            "  b) keine solchen Verträge über diese Flächen abzuschließen und bestehende Verträge der anderen Partei weder zu "
            "beeinträchtigen noch zu beenden oder abzulösen zu versuchen; und",
            "  c) Dritte weder zu solchen Handlungen zu veranlassen noch dabei zu unterstützen.",
            "(2) Absatz 1 gilt nicht, soweit die Partei nachweist, dass sie bereits vor der Offenlegung mit dem Betreffenden "
            "hinsichtlich derselben Fläche in einer Vertragsbeziehung oder in konkreten, dokumentierten Vertragsverhandlungen stand.",
            "(3) Die Beziehungen jeder Partei zu den Eigentümern, Verpächtern und Nutzungsberechtigten ihrer eigenen Flächen "
            "bleiben unberührt.",
        ]),
        ("§ 5 Vertragsstrafe", [
            "(1) Verstößt eine Partei schuldhaft gegen § 3 oder § 4, hat sie an die andere Partei eine Vertragsstrafe zu zahlen. "
            "Deren Höhe setzt die berechtigte Partei nach billigem Ermessen (§ 315 BGB) fest; sie kann im Streitfall vom "
            f"zuständigen Gericht auf Angemessenheit überprüft werden. Die Vertragsstrafe beträgt höchstens EUR {_eur(p.max_penalty_eur)} "
            "je Verstoß.",
            "(2) Ein Dauerverstoß gilt bis zu seiner Beendigung als ein Verstoß.",
            "(3) Weitergehende Ansprüche, insbesondere auf Schadensersatz, Unterlassung und Auskunft (auch nach §§ 6 ff. GeschGehG), "
            "bleiben unberührt. Eine gezahlte Vertragsstrafe wird auf einen Schadensersatzanspruch angerechnet.",
        ]),
        ("§ 6 Rückgabe und Löschung", [
            "(1) Auf Verlangen der Offenlegenden Partei in Textform, spätestens bei Beendigung der Gespräche, hat die Empfangende "
            "Partei sämtliche Vertraulichen Informationen einschließlich aller Kopien unverzüglich zurückzugeben oder zu löschen "
            "bzw. zu vernichten und dies in Textform zu bestätigen.",
            "(2) Ausgenommen sind Unterlagen, die aufgrund gesetzlicher Aufbewahrungspflichten aufzubewahren sind, sowie "
            "automatisch erstellte Sicherungskopien, die nur mit unverhältnismäßigem Aufwand gelöscht werden können. Für diese "
            "gelten die Pflichten dieser Vereinbarung fort.",
        ]),
        ("§ 7 Datenschutz", [
            "(1) Enthalten Vertrauliche Informationen personenbezogene Daten (etwa von Eigentümern, Landwirten oder "
            "Ansprechpartnern), legt die Offenlegende Partei diese nur offen, soweit dies für den Zweck erforderlich und "
            "datenschutzrechtlich zulässig ist (insbesondere Art. 6 Abs. 1 lit. b oder f DSGVO). Die Parteien sind jeweils "
            "eigenständig Verantwortliche im Sinne des Art. 4 Nr. 7 DSGVO.",
            "(2) Die Empfangende Partei verarbeitet diese Daten ausschließlich für den Zweck, trifft angemessene technische und "
            "organisatorische Maßnahmen (Art. 32 DSGVO) und erfüllt ihre Informationspflichten (insbesondere Art. 14 DSGVO) "
            "eigenverantwortlich.",
        ]),
        ("§ 8 Keine Verpflichtung zum Vertragsschluss; keine Gewähr", [
            "(1) Diese Vereinbarung begründet keine Pflicht, Verhandlungen aufzunehmen oder fortzuführen oder einen Flächenpool-, "
            "Kooperations- oder sonstigen Vertrag zu schließen. Ein solcher Vertrag bedarf einer gesonderten Vereinbarung.",
            "(2) Vertrauliche Informationen werden ohne Gewähr für Richtigkeit und Vollständigkeit offengelegt; hiervon unberührt "
            "bleibt die Haftung für Vorsatz und arglistiges Verschweigen.",
            "(3) Ergebnisse eines über die Plattform durchgeführten Eignungsaudits sind unverbindliche Orientierungswerte und "
            "ersetzen keine eigene Prüfung der Parteien.",
        ]),
        ("§ 9 Laufzeit und Nachwirkung", [
            f"(1) Diese Vereinbarung wird mit der Bestätigung durch beide Parteien wirksam und hat eine Laufzeit von "
            f"{p.term_years} Jahren. Das Recht zur Kündigung aus wichtigem Grund (§ 314 BGB) bleibt unberührt.",
            f"(2) Die Pflichten aus den §§ 3, 6 und 7 bestehen nach Ablauf oder Beendigung für weitere {p.aftereffect_years} Jahre "
            "fort, hinsichtlich personenbezogener Daten und Geschäftsgeheimnissen solange, wie diese Eigenschaft besteht. "
            "Die Sperrfrist nach § 4 bestimmt sich nach dortiger Regelung.",
        ]),
        ("§ 10 Schlussbestimmungen", [
            "(1) Die Parteien schließen diese Vereinbarung elektronisch über die Plattform in Textform (§ 126b BGB) durch "
            "Bestätigung der jeweils vertretungsberechtigten Person. Das Signaturprotokoll (Anlage 1) ist Bestandteil dieser "
            "Vereinbarung. Änderungen und Ergänzungen, auch dieser Klausel, bedürfen der Textform, soweit nicht zwingend eine "
            "strengere Form vorgeschrieben ist.",
            "(2) Mitteilungen nach dieser Vereinbarung sind an die eingangs genannten Anschriften bzw. E-Mail-Adressen zu richten.",
            "(3) Rechte und Pflichten aus dieser Vereinbarung dürfen nur mit Zustimmung der anderen Partei in Textform übertragen "
            "werden. Die Vereinbarung bindet die Rechtsnachfolger.",
            "(4) Sollte eine Bestimmung dieser Vereinbarung unwirksam oder undurchführbar sein oder werden, bleibt die Wirksamkeit "
            "der übrigen Bestimmungen unberührt. Anstelle der unwirksamen Bestimmung gilt diejenige wirksame Regelung als "
            "vereinbart, die dem wirtschaftlichen Zweck am nächsten kommt. Entsprechendes gilt für Regelungslücken.",
            "(5) Es gilt das Recht der Bundesrepublik Deutschland unter Ausschluss des UN-Kaufrechts und der Verweisungsnormen "
            "des internationalen Privatrechts.",
            f"(6) Ausschließlicher Gerichtsstand ist, soweit gesetzlich zulässig (§ 38 ZPO), {p.jurisdiction_city}. "
            "Das Recht jeder Partei, einstweiligen Rechtsschutz bei jedem zuständigen Gericht zu beantragen, bleibt unberührt.",
            "(7) Die Parteien erhalten diese Vereinbarung als PDF-Datei; maßgeblich ist die über die Plattform gespeicherte "
            "Fassung mit dem im Signaturprotokoll genannten Prüfwert (SHA-256).",
        ]),
    ]


# ------------------------------------------------------------------ rendering

_BASE = dict(fontName="Helvetica", fontSize=9.6, leading=13)
ST_BODY = ParagraphStyle("body", alignment=TA_JUSTIFY, spaceAfter=3.5, **_BASE)
ST_ITEM = ParagraphStyle("item", parent=ST_BODY, leftIndent=16, spaceAfter=2.5)
ST_H = ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=11, leading=14, spaceBefore=9, spaceAfter=4, keepWithNext=1)
ST_TITLE = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=15, leading=19, alignment=TA_CENTER, spaceAfter=4)
ST_SUB = ParagraphStyle("sub", fontName="Helvetica", fontSize=9.5, leading=13, alignment=TA_CENTER, textColor=colors.HexColor("#444444"), spaceAfter=10)
ST_PARTY = ParagraphStyle("party", parent=ST_BODY, alignment=0, leftIndent=14, spaceAfter=6)
ST_SMALL = ParagraphStyle("small", fontName="Helvetica", fontSize=8.4, leading=11)


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%d.%m.%Y, %H:%M:%S UTC")


class _NumberedCanvas(rl_canvas.Canvas):
    """Adds 'Seite x von y' and the document id to every page footer."""

    footer_text = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved: list[dict] = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self.setFont("Helvetica", 8)
            self.setFillColor(colors.HexColor("#555555"))
            self.drawString(2.2 * cm, 1.3 * cm, self.footer_text)
            self.drawRightString(A4[0] - 2.2 * cm, 1.3 * cm, f"Seite {self._pageNumber} von {total}")
            super().showPage()
        super().save()


def _paragraphs(a: Party, b: Party, p: NdaParams) -> list:
    out: list = [
        Paragraph("Gegenseitige Geheimhaltungs- und Nichtumgehungsvereinbarung", ST_TITLE),
        Paragraph(f"Mutual Non-Disclosure Agreement · Vorlage {TEMPLATE_VERSION}", ST_SUB),
        Paragraph("zwischen", ST_BODY),
        Paragraph(a.block() + "<br/>– nachfolgend „<b>Partei A</b>“ –", ST_PARTY),
        Paragraph("und", ST_BODY),
        Paragraph(b.block() + "<br/>– nachfolgend „<b>Partei B</b>“ –", ST_PARTY),
        Paragraph("– Partei A und Partei B nachfolgend gemeinsam die „Parteien“ und einzeln eine „Partei“ –", ST_BODY),
        Paragraph("Präambel", ST_H),
        Paragraph(
            f"(A) Die Parteien planen jeweils die Entwicklung von Windenergie- und/oder Photovoltaikanlagen auf Flächen, die in "
            f"räumlicher Nähe zueinander liegen. Sie wurden über die Plattform „{escape(p.platform_name)}“ unter der "
            f"Vermittlungskennung {escape(p.match_id)} zusammengeführt. Der Plattformbetreiber ist nicht Partei dieser Vereinbarung.",
            ST_BODY),
        Paragraph(
            "(B) Die Parteien erwägen eine Zusammenarbeit, insbesondere die gemeinsame Projektentwicklung, die Bildung eines "
            "Flächenpools und eine gemeinsame Netzanbindung (der „Zweck“).", ST_BODY),
        Paragraph(
            "(C) Zur Prüfung dieser Zusammenarbeit müssen die Parteien einander vertrauliche Informationen offenlegen. "
            "Dies vorausgeschickt, vereinbaren sie Folgendes:", ST_BODY),
    ]
    for heading, paras in _sections(a, b, p):
        out.append(Paragraph(escape(heading), ST_H))
        for t in paras:
            out.append(Paragraph(escape(t.strip()) if "<" not in t else t.strip(), ST_ITEM if t.startswith("  ") else ST_BODY))
    return out


def _signature_block(a: Party, b: Party, sigs: dict[str, Signature]) -> KeepTogether:
    def cell(label: str, party: Party) -> list:
        s = sigs.get(label)
        head = Paragraph(f"<b>Partei {label}</b><br/>{escape(party.company_name)}", ST_SMALL)
        if s:
            body = Paragraph(
                f"<font color='#0a6b2a'><b>Elektronisch bestätigt</b></font><br/>{escape(s.signer_name)}"
                f"<br/>{escape(s.signer_role)}<br/>{_fmt(s.signed_at)}", ST_SMALL)
        else:
            body = Paragraph("<br/><br/>Ort, Datum<br/><br/>______________________________<br/>"
                             f"{escape(party.representative)}", ST_SMALL)
        return [head, Spacer(1, 6), body]

    t = Table([[cell("A", a), cell("B", b)]], colWidths=[8.3 * cm, 8.3 * cm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEABOVE", (0, 0), (-1, 0), 0.6, colors.HexColor("#999999")),
                           ("TOPPADDING", (0, 0), (-1, -1), 6)]))
    return KeepTogether([Spacer(1, 14), Paragraph("Unterschriften / Bestätigungen", ST_H), t])


def _protocol(a: Party, b: Party, p: NdaParams, sigs: dict[str, Signature], unsigned_sha256: str) -> list:
    rows = [["Partei", "Bestätigt durch", "Funktion", "Zeitpunkt", "IP-Adresse"]]
    for label, party in (("A", a), ("B", b)):
        s = sigs[label]
        rows.append([f"{label}: {party.company_name}", s.signer_name, s.signer_role, _fmt(s.signed_at), s.ip or "–"])
    tbl = Table([[Paragraph(escape(c), ST_SMALL) for c in r] for r in rows], colWidths=[4.0 * cm, 3.4 * cm, 3.0 * cm, 3.7 * cm, 2.5 * cm])
    tbl.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#aaaaaa")),
                             ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return [
        PageBreak(),
        Paragraph("Anlage 1 – Signaturprotokoll", ST_TITLE),
        Spacer(1, 8),
        Paragraph(f"<b>Vereinbarung:</b> Gegenseitige Geheimhaltungs- und Nichtumgehungsvereinbarung ({TEMPLATE_VERSION})", ST_BODY),
        Paragraph(f"<b>Dokumentkennung:</b> {escape(p.nda_id)}", ST_BODY),
        Paragraph(f"<b>Vermittlungskennung:</b> {escape(p.match_id)}", ST_BODY),
        Paragraph(f"<b>Erstellt am:</b> {_fmt(p.created_at)}", ST_BODY),
        Paragraph(f"<b>Prüfwert (SHA-256) der unterschriftsreifen Fassung:</b><br/><font face='Courier' size='8.5'>{unsigned_sha256}</font>", ST_BODY),
        Spacer(1, 8),
        tbl,
        Spacer(1, 10),
        Paragraph(
            "Beide Parteien haben den Wortlaut der Vereinbarung mit dem oben genannten Prüfwert über die Plattform bestätigt. "
            "Es handelt sich um eine einfache elektronische Bestätigung in Textform (§ 126b BGB), nicht um eine qualifizierte "
            "elektronische Signatur im Sinne der eIDAS-Verordnung. Die Bestätigung wurde von der jeweils angegebenen Person "
            "für die jeweilige Partei abgegeben.", ST_BODY),
    ]


def build_nda_pdf(a: Party, b: Party, p: NdaParams, signatures: Optional[dict[str, Signature]] = None,
                  unsigned_sha256: str = "") -> bytes:
    """Render the NDA. With `signatures` (keys "A","B") and the hash of the unsigned PDF, the signed
    version with the Signaturprotokoll is produced."""
    sigs = signatures or {}
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=2.2 * cm, rightMargin=2.2 * cm, topMargin=2.0 * cm, bottomMargin=2.2 * cm,
        title="Gegenseitige Geheimhaltungs- und Nichtumgehungsvereinbarung", author=p.platform_name,
        subject=f"NDA {p.nda_id} (Vermittlung {p.match_id})", creator=f"{p.platform_name} · {TEMPLATE_VERSION}",
        keywords=f"NDA, Geheimhaltung, {TEMPLATE_VERSION}", lang="de-DE", invariant=1,
    )
    story = _paragraphs(a, b, p) + [_signature_block(a, b, sigs)]
    if sigs:
        story += _protocol(a, b, p, sigs, unsigned_sha256)

    class Canvas(_NumberedCanvas):
        footer_text = f"{TEMPLATE_VERSION} · Kennung {p.nda_id}" + (" · unterschrieben" if sigs else " · Entwurf zur Bestätigung")

    doc.build(story, canvasmaker=lambda *a, **k: Canvas(*a, **k))
    return buf.getvalue()
